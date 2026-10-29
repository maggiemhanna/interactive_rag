"""Distributed tracing and hierarchical per-stage span instrumentation (Pillar 5: Observability).

Provides `start_span(name, stage=None, **attributes)` to time internal operations across
Cloud Storage synchronization, PDF chunking, Vertex AI embedding/chat calls, ChromaDB
retrieval, and Model Armor guardrails, recording completed spans and stage latencies into
`src.core.observability.state` and optionally exporting spans to Google Cloud Trace.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import os
import time
from typing import Any, Callable, Dict, Iterator, List, Optional

from src.core.config import is_cloud_trace_enabled
from src.core.context import (
    generate_span_id,
    get_active_span_id,
    get_request_context,
    get_request_id,
    get_service_name,
    get_trace_id,
    hash_query,
    set_active_span_id,
)
from src.core.observability.state import record_span

_RAW_QUERY_ATTRS = {"query", "prompt", "raw_query", "user_prompt"}
_span_listeners: List[Callable[[Dict[str, Any]], None]] = []


def register_span_listener(listener: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
    """Registers an in-process callback invoked with every completed span record (for unit tests).

    Args:
        listener: Callable accepting a completed span dictionary.

    Returns:
        Unregister function that removes `listener` when called.
    """
    _span_listeners.append(listener)

    def _unregister() -> None:
        if listener in _span_listeners:
            _span_listeners.remove(listener)

    return _unregister


def _sanitize_span_attributes(raw_attrs: Dict[str, Any]) -> Dict[str, Any]:
    """Scrubs any raw query/prompt strings and drops `None` values from span attributes."""
    clean: Dict[str, Any] = {}
    for key, val in raw_attrs.items():
        if val is None:
            continue
        if key in _RAW_QUERY_ATTRS and isinstance(val, str):
            clean["query_sha256"] = hash_query(val)
            clean["query_chars"] = len(val)
            continue
        clean[key] = val
    return clean


def _export_span_to_cloud_trace(span_record: Dict[str, Any]) -> None:
    """Best-effort export of a completed span to Google Cloud Trace v2 API if installed and enabled."""
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    trace_id = str(span_record.get("trace_id") or "").strip()
    if not project_id or not trace_id or not is_cloud_trace_enabled():
        return

    try:
        from google.cloud import trace_v2  # type: ignore[import-untyped]
        from google.protobuf import timestamp_pb2  # type: ignore[import-untyped]

        client = trace_v2.TraceServiceClient()
        span_id = str(span_record["span_id"])
        span_name = f"projects/{project_id}/traces/{trace_id}/spans/{span_id}"

        start_dt = datetime.fromisoformat(span_record["start_time"])
        end_dt = datetime.fromisoformat(span_record["end_time"])
        start_pb = timestamp_pb2.Timestamp()
        start_pb.FromDatetime(start_dt)
        end_pb = timestamp_pb2.Timestamp()
        end_pb.FromDatetime(end_dt)

        span_pb = trace_v2.Span(
            name=span_name,
            span_id=span_id,
            parent_span_id=str(span_record.get("parent_span_id") or ""),
            display_name=trace_v2.TruncatableString(value=str(span_record["name"])[:128]),
            start_time=start_pb,
            end_time=end_pb,
        )
        client.create_span(request=span_pb)
    except Exception:
        # Never allow optional Cloud Trace export failures to disrupt request handling
        pass


@contextmanager
def start_span(
    name: str,
    stage: Optional[str] = None,
    **attributes: Any,
) -> Iterator[Dict[str, Any]]:
    """Context manager that times a pipeline operation and records a hierarchical span.

    Automatically links to the active parent `span_id` on `RequestContext`, updates
    `active_span_id` for the duration of the block so nested spans form a tree, and
    records the completed span and per-stage latency in `src.core.observability.state`.

    Args:
        name: Dot-namespaced operation name (e.g., `'agent.model_call'`, `'ingestion.split_pdfs'`).
        stage: Optional pipeline stage key (`'gcs_sync'`, `'chunking'`, `'embedding'`,
            `'retrieval'`, `'input_guard'`, `'llm'`, `'output_guard'`).
        **attributes: Initial structured metadata attributes to attach to the span.

    Yields:
        Mutable span context dictionary (`span["attributes"]` can be updated inside the block).
    """
    span_id = generate_span_id()
    parent_span_id = get_active_span_id()
    has_ctx = get_request_context() is not None
    if has_ctx:
        set_active_span_id(span_id)

    start_dt = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    span_context: Dict[str, Any] = {
        "name": name,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "stage": stage or "",
        "status": "ok",
        "attributes": dict(attributes),
    }

    try:
        yield span_context
    except Exception as exc:
        span_context["status"] = "error"
        span_context["attributes"]["error_type"] = type(exc).__name__
        raise
    finally:
        duration_ms = round(max(0.0, (time.perf_counter() - t0) * 1000.0), 2)
        end_dt = datetime.now(timezone.utc)
        if has_ctx:
            set_active_span_id(parent_span_id)

        clean_attrs = _sanitize_span_attributes(span_context.get("attributes") or {})
        completed_record: Dict[str, Any] = {
            "name": name,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "trace_id": get_trace_id(),
            "request_id": get_request_id(),
            "service": get_service_name(),
            "stage": span_context.get("stage") or "",
            "status": span_context.get("status") or "ok",
            "duration_ms": duration_ms,
            "start_time": start_dt.isoformat(),
            "end_time": end_dt.isoformat(),
            "attributes": clean_attrs,
        }

        record_span(completed_record)

        for listener in list(_span_listeners):
            try:
                listener(dict(completed_record))
            except Exception:
                pass

        _export_span_to_cloud_trace(completed_record)


__all__ = [
    "register_span_listener",
    "start_span",
]
