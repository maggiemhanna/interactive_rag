"""AI-specific performance & quality telemetry publisher (Pillar 5: Observability & AI Monitoring).

Captures per-turn stage latency breakdowns, streaming Time-To-First-Token (`ttft_ms`),
Vertex AI Gemini token consumption (`input_tokens`, `output_tokens`, `total_tokens`, `llm_calls`),
and retrieval/grounding quality signals (`top_similarity_score`, `mean_similarity_score`,
`retrieved_passages`, `tool_calls_count`, `citation_count`, `grounded`), publishing one
structured JSON record per completed request to `interactiverag-telemetry`.
"""

from datetime import datetime, timezone
import json
import math
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
import uuid

from google.cloud import logging as cloud_logging

from src.core.compliance.state import get_retrieved_chunks_count
from src.core.config import (
    DEFAULT_TELEMETRY_LOG_NAME,
    get_google_cloud_location,
    get_google_cloud_project,
    get_telemetry_log_name,
    is_telemetry_cloud_logging_enabled,
)
from src.core.context import (
    get_active_span_id,
    get_request_context,
    get_service_name,
    scrub_query_fields,
)
from src.core.logger import setup_logging
from src.core.observability.state import (
    get_elapsed_latency_ms,
    get_recorded_spans,
    get_retrieval_scores_summary,
    get_stage_latency_ms,
    get_token_usage_summary,
    get_tool_calls_count,
    get_ttft_ms,
    record_retrieval_scores,
    record_token_usage,
    record_tool_call,
)

logger = setup_logging(name="observability.telemetry")

_cloud_loggers: Dict[Tuple[str, str], Any] = {}
_cloud_loggers_lock = threading.Lock()
_telemetry_listeners: List[Callable[[Dict[str, Any]], None]] = []


def _get_cloud_logger(project_id: str, log_name: str) -> Any:
    """Returns a cached `google.cloud.logging.Logger` writing to `projects/<project>/logs/<log_name>`."""
    key = (project_id, log_name)
    cached = _cloud_loggers.get(key)
    if cached is not None:
        return cached

    with _cloud_loggers_lock:
        cached = _cloud_loggers.get(key)
        if cached is None:
            client = cloud_logging.Client(project=project_id)
            cached = client.logger(log_name)
            _cloud_loggers[key] = cached
        return cached


def register_telemetry_listener(
    listener: Callable[[Dict[str, Any]], None],
) -> Callable[[], None]:
    """Registers an in-process telemetry event observer and returns an unsubscribe function.

    Args:
        listener: Callable invoked with a shallow copy of every emitted telemetry record dictionary.

    Returns:
        Zero-argument callable that removes `listener` when invoked.
    """
    _telemetry_listeners.append(listener)

    def _remove() -> None:
        if listener in _telemetry_listeners:
            _telemetry_listeners.remove(listener)

    return _remove


def record_llm_usage(response: Any) -> Dict[str, int]:
    """Extracts token counts from a LangChain `AIMessage` and accumulates them on the active request.

    Inspects `response.usage_metadata` as well as `response.response_metadata["usage_metadata"]`
    and `response.response_metadata["token_usage"]` so both Vertex AI Gemini and mocked test
    models report accurate per-call and cumulative multi-turn token consumption.

    Args:
        response: LangChain `AIMessage` or model response object.

    Returns:
        Dictionary with `input_tokens`, `output_tokens`, and `total_tokens` for this LLM call.
    """
    usage: Dict[str, Any] = {}
    raw_usage = getattr(response, "usage_metadata", None)
    if isinstance(raw_usage, dict):
        usage.update(raw_usage)

    resp_meta = getattr(response, "response_metadata", None)
    if isinstance(resp_meta, dict):
        nested = resp_meta.get("usage_metadata") or resp_meta.get("token_usage")
        if isinstance(nested, dict):
            for k, v in nested.items():
                usage.setdefault(k, v)

    input_tokens = int(
        usage.get("input_tokens")
        or usage.get("prompt_token_count")
        or usage.get("prompt_tokens")
        or 0
    )
    output_tokens = int(
        usage.get("output_tokens")
        or usage.get("candidates_token_count")
        or usage.get("completion_tokens")
        or 0
    )
    total_tokens = int(
        usage.get("total_tokens")
        or usage.get("total_token_count")
        or (input_tokens + output_tokens)
    )

    record_token_usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )
    return {
        "input_tokens": max(0, input_tokens),
        "output_tokens": max(0, output_tokens),
        "total_tokens": max(0, total_tokens),
    }


def _normalize_similarity_score(raw_score: Any) -> Optional[float]:
    """Normalizes a vector search score or distance into a bounded `[0.0, 1.0]` similarity float."""
    if raw_score is None or isinstance(raw_score, bool):
        return None
    try:
        val = float(raw_score)
    except (TypeError, ValueError):
        return None
    if math.isnan(val) or math.isinf(val):
        return None
    if 0.0 <= val <= 1.0:
        return round(val, 4)
    if val < 0.0:
        return round(max(0.0, min(1.0, 1.0 + val)), 4)
    return round(1.0 / (1.0 + val), 4)


def record_retrieval_metrics(
    docs_or_scored: Iterable[Any],
    *,
    is_tool_call: bool = False,
) -> Dict[str, Any]:
    """Captures vector similarity scores and optional tool-call count on the active request.

    Accepts either an iterable of `(Document, score)` tuples (from
    `similarity_search_with_relevance_scores` / `similarity_search_with_score`) or `Document`
    objects carrying optional `similarity_score` / `relevance_score` metadata.

    Args:
        docs_or_scored: Retrieved documents or `(Document, score)` pairs.
        is_tool_call: When True, increments the per-request retriever tool call counter.

    Returns:
        Dictionary summarizing `top_similarity_score`, `mean_similarity_score`, and `tool_calls_count`.
    """
    if is_tool_call:
        record_tool_call()

    scores: List[float] = []
    for item in docs_or_scored or []:
        raw_score: Any = None
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            raw_score = item[1]
        else:
            meta = getattr(item, "metadata", None)
            if isinstance(meta, dict):
                raw_score = (
                    meta.get("similarity_score")
                    if meta.get("similarity_score") is not None
                    else meta.get("relevance_score")
                )
        norm_score = _normalize_similarity_score(raw_score)
        if norm_score is not None:
            scores.append(norm_score)

    if scores:
        record_retrieval_scores(scores)

    summary = get_retrieval_scores_summary()
    summary["tool_calls_count"] = get_tool_calls_count()
    return summary


def emit_telemetry_event(
    event_type: str,
    *,
    severity: str = "INFO",
    uid: Optional[str] = None,
    status: str = "success",
    **fields: Any,
) -> Dict[str, Any]:
    """Publishes a structured AI/performance telemetry record to `interactiverag-telemetry`.

    Automatically enriches the record with `request_id`, `trace_id`, `span_id`, `service`,
    `region`, `total_latency_ms`, `ttft_ms`, `stage_latency_ms`, `span_count`, `spans`,
    token usage (`input_tokens`, `output_tokens`, `total_tokens`, `llm_calls`), and
    retrieval quality metrics (`tool_calls_count`, `retrieved_passages`,
    `top_similarity_score`, `mean_similarity_score`) from `src.core.observability.state`.

    Args:
        event_type: Canonical domain event identifier matching `emit_audit_event`
            (`'document.ingest'`, `'vectorstore.query'`, or `'agent.query'`).
        severity: Cloud Logging severity (`'INFO'`, `'WARNING'`, `'ERROR'`).
        uid: Optional explicit tenant `uid` (falls back to `RequestContext.uid`).
        status: Request outcome status (`'success'`, `'blocked'`, `'error'`, etc.).
        **fields: Additional structured telemetry attributes (e.g., `model_name`, `collection_name`,
            `grounded`, `citation_count`).

    Returns:
        Complete structured telemetry record dictionary that was published.
    """
    state = get_request_context()
    project_id = get_google_cloud_project()
    resolved_uid = uid or (state.uid if state is not None else None)
    request_id = state.request_id if state is not None else f"req-{uuid.uuid4().hex}"
    trace_id = state.trace_id if state is not None else ""
    span_id = get_active_span_id()
    service_name = state.service_name if state is not None else get_service_name()
    normalized_severity = (severity or "INFO").strip().upper()
    safe_fields = scrub_query_fields(fields)

    spans = get_recorded_spans()
    stage_latency_ms = get_stage_latency_ms()
    ttft_ms = get_ttft_ms()
    tokens = get_token_usage_summary()
    scores = get_retrieval_scores_summary()

    record: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "severity": normalized_severity,
        "status": status,
        "service": service_name,
        "region": get_google_cloud_location(),
        "request_id": request_id,
        "total_latency_ms": get_elapsed_latency_ms(),
        "stage_latency_ms": stage_latency_ms,
        "span_count": len(spans),
    }
    if trace_id:
        record["trace_id"] = trace_id
    if span_id:
        record["span_id"] = span_id
    if resolved_uid:
        record["uid"] = resolved_uid
    if state is not None and state.method:
        record["http_method"] = state.method
    if state is not None and state.path:
        record["http_path"] = state.path
    if ttft_ms is not None:
        record["ttft_ms"] = ttft_ms

    # Attach AI & retrieval telemetry metrics for agent/query workloads (or whenever populated)
    is_agent_or_query = event_type in {"agent.query", "vectorstore.query"}
    if event_type == "agent.query" or tokens["llm_calls"] > 0:
        record["input_tokens"] = tokens["input_tokens"]
        record["output_tokens"] = tokens["output_tokens"]
        record["total_tokens"] = tokens["total_tokens"]
        record["llm_calls"] = tokens["llm_calls"]
        record["tool_calls_count"] = get_tool_calls_count()

    if is_agent_or_query or get_retrieved_chunks_count() > 0 or scores["top_similarity_score"] is not None:
        record["retrieved_passages"] = get_retrieved_chunks_count()
        record["top_similarity_score"] = scores["top_similarity_score"]
        record["mean_similarity_score"] = scores["mean_similarity_score"]

    record.update(safe_fields)

    for listener in list(_telemetry_listeners):
        try:
            listener(dict(record))
        except Exception:
            pass

    shipped_to_gcp = False
    if project_id and is_telemetry_cloud_logging_enabled():
        try:
            cloud_logger = _get_cloud_logger(project_id, get_telemetry_log_name())
            log_kwargs: Dict[str, Any] = {"severity": normalized_severity}
            if trace_id:
                log_kwargs["trace"] = f"projects/{project_id}/traces/{trace_id}"
            if span_id:
                log_kwargs["span_id"] = span_id
            cloud_logger.log_struct(record, **log_kwargs)
            shipped_to_gcp = True
        except Exception as exc:
            logger.warning(
                f"Cloud Logging publish failed for telemetry event '{event_type}' (falling back to stdout JSON): {exc}"
            )

    if not shipped_to_gcp:
        serialized = json.dumps(record, sort_keys=True, default=str)
        if normalized_severity in {"ERROR", "CRITICAL"}:
            logger.error(f"TELEMETRY_EVENT {serialized}")
        elif normalized_severity == "WARNING":
            logger.warning(f"TELEMETRY_EVENT {serialized}")
        else:
            logger.info(f"TELEMETRY_EVENT {serialized}")

    return record


__all__ = [
    "DEFAULT_TELEMETRY_LOG_NAME",
    "emit_telemetry_event",
    "get_telemetry_log_name",
    "record_llm_usage",
    "record_retrieval_metrics",
    "register_telemetry_listener",
]
