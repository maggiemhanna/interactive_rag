"""Compliance & Governance package (Pillar 4) for InteractiveRAG.

Provides modular regulatory, residency, policy, and audit capabilities:
- `residency`: Single-region GCP location validation (`validate_gcp_region`).
- `audit`: PII-free Cloud Logging publisher (`emit_audit_event`) and SHA-256 query hashing (`hash_query`).
- `policy`: Per-user sliding-window rate limiters (`HTTP 429`) and Chroma collection count quotas (`HTTP 403`).
- `dsar`: GDPR Art. 15 personal data inventory and Art. 17 complete versioned GCS/local erasure.
- `provenance`: EU AI Act Art. 50 AI transparency, citation grounding, and Model Armor provenance metadata.
- `state`: Per-request compliance guardrail and retrieval state helpers stored on `RequestContext`.
- `routes`: Self-service `GET` and `DELETE /compliance/my-data` FastAPI router.
"""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.core.compliance.audit import (
        DEFAULT_AUDIT_LOG_NAME,
        emit_audit_event,
        get_audit_log_name,
        hash_query,
        register_audit_listener,
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
    from src.core.compliance.state import (
        GUARDRAIL_FILTER_LABELS,
        get_guardrail_actions,
        get_retrieved_chunks_count,
        mark_model_armor_active,
        mark_pii_filtered,
        record_guardrail_action,
        record_retrieved_chunks,
        was_model_armor_active,
        was_pii_filtered,
    )

_EXPORT_MAP: dict[str, str] = {
    "DEFAULT_AUDIT_LOG_NAME": "src.core.compliance.audit",
    "emit_audit_event": "src.core.compliance.audit",
    "get_audit_log_name": "src.core.compliance.audit",
    "hash_query": "src.core.compliance.audit",
    "register_audit_listener": "src.core.compliance.audit",
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
    "GUARDRAIL_FILTER_LABELS": "src.core.compliance.state",
    "get_guardrail_actions": "src.core.compliance.state",
    "get_retrieved_chunks_count": "src.core.compliance.state",
    "mark_model_armor_active": "src.core.compliance.state",
    "mark_pii_filtered": "src.core.compliance.state",
    "record_guardrail_action": "src.core.compliance.state",
    "record_retrieved_chunks": "src.core.compliance.state",
    "was_model_armor_active": "src.core.compliance.state",
    "was_pii_filtered": "src.core.compliance.state",
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
