"""Observability & AI Monitoring package (Pillar 5) for InteractiveRAG.

Provides modular observability capabilities separated from `src/core/compliance/`:
- `state`: Per-request latency, TTFT, token, retrieval score, and span state accumulators stored on `RequestContext`.
- `logging`: Structured GCP JSON formatting (`GCPJsonFormatter`) and request context log filtering.
- `tracing`: Hierarchical span instrumentation (`start_span`) and optional Cloud Trace export.
- `telemetry`: AI performance & quality telemetry publisher (`interactiverag-telemetry`).
"""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.core.observability.logging import (
        GCPJsonFormatter,
        RequestContextLogFilter,
    )
    from src.core.observability.state import (
        get_elapsed_latency_ms,
        get_recorded_spans,
        get_retrieval_scores_summary,
        get_stage_latency_ms,
        get_token_usage_summary,
        get_tool_calls_count,
        get_ttft_ms,
        record_retrieval_scores,
        record_span,
        record_token_usage,
        record_tool_call,
        record_ttft,
    )

_EXPORT_MAP: dict[str, str] = {
    "GCPJsonFormatter": "src.core.observability.logging",
    "RequestContextLogFilter": "src.core.observability.logging",
    "get_elapsed_latency_ms": "src.core.observability.state",
    "get_recorded_spans": "src.core.observability.state",
    "get_retrieval_scores_summary": "src.core.observability.state",
    "get_stage_latency_ms": "src.core.observability.state",
    "get_token_usage_summary": "src.core.observability.state",
    "get_tool_calls_count": "src.core.observability.state",
    "get_ttft_ms": "src.core.observability.state",
    "record_retrieval_scores": "src.core.observability.state",
    "record_span": "src.core.observability.state",
    "record_token_usage": "src.core.observability.state",
    "record_tool_call": "src.core.observability.state",
    "record_ttft": "src.core.observability.state",
}

__all__ = sorted(_EXPORT_MAP.keys())


def __getattr__(name: str) -> Any:
    """Lazily resolves public package exports without eager cross-package import cycles."""
    module_path = _EXPORT_MAP.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_path)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Lists available attributes on `src.core.observability` for interactive introspection."""
    return sorted(set(globals().keys()) | set(__all__))
