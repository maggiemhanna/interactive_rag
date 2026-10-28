"""Compliance & Governance package (Pillar 4) for InteractiveRAG.

Provides modular regulatory, residency, policy, and audit capabilities:
- `residency`: Single-region GCP location validation (`validate_gcp_region`).
- `context`: Per-request `contextvars` correlation and guardrail telemetry (`AuditRequestState`).
- `audit`: PII-free Cloud Logging publisher (`emit_audit_event`), SHA-256 query hashing, and ASGI middleware.
- `policy`: Per-user sliding-window rate limiters (`HTTP 429`) and Chroma collection count quotas (`HTTP 403`).
- `dsar`: GDPR Art. 15 personal data inventory and Art. 17 complete versioned GCS/local erasure.
- `provenance`: EU AI Act Art. 50 AI transparency, citation grounding, and Model Armor metadata.
- `routes`: Self-service `GET` and `DELETE /compliance/my-data` FastAPI router.
"""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.core.compliance.audit import (
        AuditCorrelationMiddleware,
        DEFAULT_AUDIT_LOG_NAME,
        emit_audit_event,
        get_audit_log_name,
        hash_query,
        register_audit_listener,
    )
    from src.core.compliance.context import (
        AuditRequestState,
        GUARDRAIL_FILTER_LABELS,
        extract_trace_id,
        get_audit_state,
        get_audit_uid,
        get_guardrail_actions,
        get_request_id,
        get_retrieved_chunks_count,
        get_service_name,
        get_trace_id,
        mark_model_armor_active,
        mark_pii_filtered,
        record_guardrail_action,
        record_retrieved_chunks,
        sanitize_request_id,
        set_audit_uid,
        was_model_armor_active,
        was_pii_filtered,
    )
    from src.core.compliance.dsar import get_user_data_inventory, purge_all_user_data
    from src.core.compliance.policy import (
        DEFAULT_WINDOW_SECONDS,
        SlidingWindowRateLimiter,
        enforce_collection_quota,
        enforce_ingest_rate_limit,
        enforce_query_rate_limit,
        reset_rate_limiters,
    )
    from src.core.compliance.provenance import (
        ComplianceMetadata,
        GuardrailAction,
        MODEL_ARMOR_DEFAULT_FILTERS,
        build_compliance_metadata,
    )
    from src.core.compliance.residency import validate_gcp_region
    from src.core.compliance.routes import build_compliance_router

_EXPORT_MAP: dict[str, str] = {
    "AuditCorrelationMiddleware": "src.core.compliance.audit",
    "DEFAULT_AUDIT_LOG_NAME": "src.core.compliance.audit",
    "emit_audit_event": "src.core.compliance.audit",
    "get_audit_log_name": "src.core.compliance.audit",
    "hash_query": "src.core.compliance.audit",
    "register_audit_listener": "src.core.compliance.audit",
    "AuditRequestState": "src.core.compliance.context",
    "GUARDRAIL_FILTER_LABELS": "src.core.compliance.context",
    "extract_trace_id": "src.core.compliance.context",
    "get_audit_state": "src.core.compliance.context",
    "get_audit_uid": "src.core.compliance.context",
    "get_guardrail_actions": "src.core.compliance.context",
    "get_request_id": "src.core.compliance.context",
    "get_retrieved_chunks_count": "src.core.compliance.context",
    "get_service_name": "src.core.compliance.context",
    "get_trace_id": "src.core.compliance.context",
    "mark_model_armor_active": "src.core.compliance.context",
    "mark_pii_filtered": "src.core.compliance.context",
    "record_guardrail_action": "src.core.compliance.context",
    "record_retrieved_chunks": "src.core.compliance.context",
    "sanitize_request_id": "src.core.compliance.context",
    "set_audit_uid": "src.core.compliance.context",
    "was_model_armor_active": "src.core.compliance.context",
    "was_pii_filtered": "src.core.compliance.context",
    "get_user_data_inventory": "src.core.compliance.dsar",
    "purge_all_user_data": "src.core.compliance.dsar",
    "DEFAULT_WINDOW_SECONDS": "src.core.compliance.policy",
    "SlidingWindowRateLimiter": "src.core.compliance.policy",
    "enforce_collection_quota": "src.core.compliance.policy",
    "enforce_ingest_rate_limit": "src.core.compliance.policy",
    "enforce_query_rate_limit": "src.core.compliance.policy",
    "reset_rate_limiters": "src.core.compliance.policy",
    "ComplianceMetadata": "src.core.compliance.provenance",
    "GuardrailAction": "src.core.compliance.provenance",
    "MODEL_ARMOR_DEFAULT_FILTERS": "src.core.compliance.provenance",
    "build_compliance_metadata": "src.core.compliance.provenance",
    "validate_gcp_region": "src.core.compliance.residency",
    "build_compliance_router": "src.core.compliance.routes",
}

__all__ = sorted(_EXPORT_MAP.keys())


def __getattr__(name: str) -> Any:
    """Lazily resolves public package exports without eager cross-package import cycles.

    Args:
        name: Exported attribute name requested on `src.core.compliance`.

    Returns:
        The resolved attribute from its defining submodule.

    Raises:
        AttributeError: If `name` is not part of the package public API.
    """
    module_path = _EXPORT_MAP.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_path)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Lists available attributes on `src.core.compliance` for interactive introspection."""
    return sorted(set(globals().keys()) | set(__all__))
