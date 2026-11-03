"""Per-request compliance guardrail and provenance state helpers (Pillar 4: Compliance & Governance).

Stores and retrieves compliance-specific state (`GUARDRAIL_FILTER_LABELS`, `mark_pii_filtered`,
`was_pii_filtered`, `mark_model_armor_active`, `was_model_armor_active`, `record_guardrail_action`,
`get_guardrail_actions`, `record_retrieved_chunks`, `get_retrieved_chunks_count`) on the active
`RequestContext` so both `audit.py` and `provenance.py` can read guardrail and retrieval state
without coupling to each other.
"""

from typing import Any, Dict, List, Optional

from src.core.context import get_request_context

GUARDRAIL_FILTER_LABELS: Dict[str, str] = {
    "pi_and_jailbreak": "Prompt Injection / Jailbreak",
    "malicious_uri": "Malicious URI",
    "rai": "RAI Safety",
    "sdp": "PII / Sensitive Data",
    "csam": "CSAM Safety",
    "gemini_safety": "Gemini Safety",
    "model_armor_policy": "Model Armor Policy",
    "missing_model_armor_config": "Model Armor Config",
    "model_armor_error": "Model Armor Screening",
    "invalid_query_text": "Query Validation",
}


def mark_pii_filtered(filtered: bool = True) -> None:
    """Marks that Sensitive Data Protection / Model Armor redacted PII during the active request.

    Args:
        filtered: Boolean flag indicating PII masking occurred (defaults to `True`).
    """
    ctx = get_request_context()
    if ctx is not None and filtered:
        with ctx.lock:
            ctx.data["pii_filtered"] = True


def was_pii_filtered() -> bool:
    """Returns True if any chunk, prompt, or model output had PII masked during the active request."""
    ctx = get_request_context()
    if ctx is None:
        return False
    with ctx.lock:
        return bool(ctx.data.get("pii_filtered", False))


def mark_model_armor_active(active: bool = True) -> None:
    """Marks that Google Cloud Model Armor actively screened the current request.

    Args:
        active: Boolean flag indicating Model Armor executed (defaults to `True`).
    """
    ctx = get_request_context()
    if ctx is not None and active:
        with ctx.lock:
            ctx.data["model_armor_active"] = True


def was_model_armor_active() -> bool:
    """Returns True when Cloud Model Armor screened prompt, retrieval, or output on this request."""
    ctx = get_request_context()
    if ctx is None:
        return False
    with ctx.lock:
        return bool(ctx.data.get("model_armor_active", False))


def record_guardrail_action(
    filter_name: str,
    action: str,
    stage: str,
    label: Optional[str] = None,
    details: Optional[List[str]] = None,
) -> None:
    """Records a Cloud Model Armor or Gemini safety guardrail action on the active request.

    Thread-safe across concurrent `ThreadPoolExecutor` chunk sanitizers and deduplicates
    repeated `(filter, action, stage)` entries while merging subcategory `details`.

    Args:
        filter_name: Machine-readable filter key (e.g., `'pi_and_jailbreak'`, `'rai'`, `'sdp'`).
        action: Enforcement action taken (`'blocked'` or `'redacted'`).
        stage: Pipeline stage (`'input'`, `'retrieval'`, `'model'`, or `'output'`).
        label: Optional human-readable display label override.
        details: Optional list of matched subcategories (e.g., `['DANGEROUS', 'HARASSMENT']`).
    """
    ctx = get_request_context()
    if ctx is None:
        return

    clean_filter = (filter_name or "model_armor_policy").strip()
    clean_action = (action or "blocked").strip()
    clean_stage = (stage or "output").strip()
    resolved_label = label or GUARDRAIL_FILTER_LABELS.get(clean_filter, clean_filter)
    normalized_details = [str(d).strip() for d in (details or []) if str(d).strip()]

    with ctx.lock:
        if clean_filter == "sdp" and clean_action == "redacted":
            ctx.data["pii_filtered"] = True

        actions: List[Dict[str, Any]] = ctx.data.setdefault("guardrail_actions", [])
        for existing in actions:
            if (
                existing["filter"] == clean_filter
                and existing["action"] == clean_action
                and existing["stage"] == clean_stage
            ):
                for item in normalized_details:
                    if item not in existing["details"]:
                        existing["details"].append(item)
                return

        actions.append(
            {
                "filter": clean_filter,
                "label": resolved_label,
                "action": clean_action,
                "stage": clean_stage,
                "details": normalized_details,
            }
        )


def get_guardrail_actions() -> List[Dict[str, Any]]:
    """Returns a deep-copied snapshot of all guardrail actions triggered on the active request."""
    ctx = get_request_context()
    if ctx is None:
        return []
    with ctx.lock:
        actions: List[Dict[str, Any]] = ctx.data.get("guardrail_actions") or []
        return [
            {
                "filter": item["filter"],
                "label": item["label"],
                "action": item["action"],
                "stage": item["stage"],
                "details": list(item["details"]),
            }
            for item in actions
        ]


def record_retrieved_chunks(count: int) -> None:
    """Accumulates the number of vectorstore chunks retrieved during the active request.

    Args:
        count: Number of chunks returned by a retriever tool invocation.
    """
    ctx = get_request_context()
    if ctx is not None and count > 0:
        with ctx.lock:
            ctx.data["retrieved_chunks_count"] = int(ctx.data.get("retrieved_chunks_count", 0)) + int(count)


def get_retrieved_chunks_count() -> int:
    """Returns the total number of chunks retrieved during the active request."""
    ctx = get_request_context()
    if ctx is None:
        return 0
    with ctx.lock:
        return int(ctx.data.get("retrieved_chunks_count", 0))
