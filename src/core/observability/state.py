"""Per-request observability state accumulators (Pillar 5: Observability & AI Monitoring).

Stores and retrieves per-request observability state (`get_elapsed_latency_ms`, `record_ttft`,
`get_ttft_ms`, `record_tool_call`, `get_tool_calls_count`, `record_retrieval_scores`,
`get_retrieval_scores_summary`, `record_token_usage`, `get_token_usage_summary`, `record_span`,
`get_recorded_spans`, `get_stage_latency_ms`) on the active `RequestContext` (`src/core/context.py`)
so both `tracing.py` and `telemetry.py` can read and update request state without circular imports.
"""

import time
from typing import Any, Dict, List, Optional, Sequence

from src.core.context import get_request_context


def get_elapsed_latency_ms() -> float:
    """Returns elapsed milliseconds since `RequestContext.request_start_perf` (or `0.0` if unbound)."""
    ctx = get_request_context()
    if ctx is None:
        return 0.0
    return round(max(0.0, (time.perf_counter() - ctx.request_start_perf) * 1000.0), 2)


def record_ttft() -> Optional[float]:
    """Records Time-to-First-Token (`ttft_ms`) on the first streaming event if not already set.

    Returns:
        Recorded `ttft_ms` in milliseconds, or `None` if no `RequestContext` is bound.
    """
    ctx = get_request_context()
    if ctx is None:
        return None
    with ctx.lock:
        if ctx.data.get("ttft_ms") is None:
            ctx.data["ttft_ms"] = round(
                max(0.0, (time.perf_counter() - ctx.request_start_perf) * 1000.0), 2
            )
        return float(ctx.data["ttft_ms"])


def get_ttft_ms() -> Optional[float]:
    """Returns the recorded Time-to-First-Token in milliseconds, if any."""
    ctx = get_request_context()
    if ctx is None:
        return None
    with ctx.lock:
        val = ctx.data.get("ttft_ms")
        return float(val) if val is not None else None


def record_tool_call(count: int = 1) -> None:
    """Accumulates the number of retriever tool invocations executed during the active request."""
    ctx = get_request_context()
    if ctx is not None and count > 0:
        with ctx.lock:
            ctx.data["tool_calls_count"] = int(ctx.data.get("tool_calls_count", 0)) + int(count)


def get_tool_calls_count() -> int:
    """Returns the total number of retriever tool invocations executed during the active request."""
    ctx = get_request_context()
    if ctx is None:
        return 0
    with ctx.lock:
        return int(ctx.data.get("tool_calls_count", 0))


def record_retrieval_scores(scores: Sequence[float]) -> None:
    """Appends normalized vector similarity scores (`0.0` to `1.0`) to the active request context.

    Args:
        scores: Sequence of similarity scores returned for retrieved document chunks.
    """
    ctx = get_request_context()
    if ctx is None or not scores:
        return
    valid_scores = [round(float(s), 4) for s in scores if isinstance(s, (int, float))]
    if not valid_scores:
        return
    with ctx.lock:
        stored: List[float] = ctx.data.setdefault("retrieval_scores", [])
        stored.extend(valid_scores)


def get_retrieval_scores_summary() -> Dict[str, Optional[float]]:
    """Returns `top_similarity_score` and `mean_similarity_score` for the active request."""
    ctx = get_request_context()
    if ctx is None:
        return {"top_similarity_score": None, "mean_similarity_score": None}
    with ctx.lock:
        scores: List[float] = ctx.data.get("retrieval_scores") or []
        if not scores:
            return {"top_similarity_score": None, "mean_similarity_score": None}
        top_score = round(max(scores), 4)
        mean_score = round(sum(scores) / len(scores), 4)
        return {
            "top_similarity_score": top_score,
            "mean_similarity_score": mean_score,
        }


def record_token_usage(
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
) -> None:
    """Accumulates LLM prompt, completion, and total token counts on the active request context.

    Args:
        input_tokens: Prompt/input token count from a model invocation.
        output_tokens: Completion/output token count from a model invocation.
        total_tokens: Total token count (defaults to `input_tokens + output_tokens` if 0).
    """
    ctx = get_request_context()
    if ctx is None:
        return
    in_tok = max(0, int(input_tokens or 0))
    out_tok = max(0, int(output_tokens or 0))
    tot_tok = max(0, int(total_tokens or 0)) or (in_tok + out_tok)
    with ctx.lock:
        ctx.data["input_tokens"] = int(ctx.data.get("input_tokens", 0)) + in_tok
        ctx.data["output_tokens"] = int(ctx.data.get("output_tokens", 0)) + out_tok
        ctx.data["total_tokens"] = int(ctx.data.get("total_tokens", 0)) + tot_tok
        ctx.data["llm_calls"] = int(ctx.data.get("llm_calls", 0)) + 1


def get_token_usage_summary() -> Dict[str, int]:
    """Returns accumulated `input_tokens`, `output_tokens`, `total_tokens`, and `llm_calls`."""
    ctx = get_request_context()
    if ctx is None:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "llm_calls": 0,
        }
    with ctx.lock:
        return {
            "input_tokens": int(ctx.data.get("input_tokens", 0)),
            "output_tokens": int(ctx.data.get("output_tokens", 0)),
            "total_tokens": int(ctx.data.get("total_tokens", 0)),
            "llm_calls": int(ctx.data.get("llm_calls", 0)),
        }


def record_span(span_record: Dict[str, Any]) -> None:
    """Appends a completed span record and accumulates its duration by stage on the active request.

    Args:
        span_record: Dictionary containing `name`, `span_id`, `parent_span_id`, `stage`,
            `duration_ms`, `status`, and optional `attributes`.
    """
    ctx = get_request_context()
    if ctx is None:
        return
    stage = str(span_record.get("stage") or "").strip()
    duration_ms = float(span_record.get("duration_ms") or 0.0)
    with ctx.lock:
        spans: List[Dict[str, Any]] = ctx.data.setdefault("spans", [])
        spans.append(dict(span_record))
        if stage:
            stage_map: Dict[str, float] = ctx.data.setdefault("stage_latency_ms", {})
            stage_key = stage if stage.endswith("_ms") else f"{stage}_ms"
            stage_map[stage_key] = round(stage_map.get(stage_key, 0.0) + duration_ms, 2)


def get_recorded_spans() -> List[Dict[str, Any]]:
    """Returns a shallow copy of all completed span records on the active request."""
    ctx = get_request_context()
    if ctx is None:
        return []
    with ctx.lock:
        spans: List[Dict[str, Any]] = ctx.data.get("spans") or []
        return [dict(item) for item in spans]


def get_stage_latency_ms() -> Dict[str, float]:
    """Returns a copy of accumulated per-stage latencies (in milliseconds) on the active request."""
    ctx = get_request_context()
    if ctx is None:
        return {}
    with ctx.lock:
        stage_map: Dict[str, float] = ctx.data.get("stage_latency_ms") or {}
        return dict(stage_map)


__all__ = [
    "get_elapsed_latency_ms",
    "get_recorded_spans",
    "get_retrieval_scores_summary",
    "get_stage_latency_ms",
    "get_token_usage_summary",
    "get_tool_calls_count",
    "get_ttft_ms",
    "record_retrieval_scores",
    "record_span",
    "record_token_usage",
    "record_tool_call",
    "record_ttft",
]
