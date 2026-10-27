"""Per-request compliance correlation and guardrail telemetry context (Pillar 4: Compliance & Governance).

Maintains thread-safe `contextvars` state (`AuditRequestState`) across FastAPI handlers,
background `ThreadPoolExecutor` workers, and live Server-Sent Events (SSE) streams so
audit logs and EU AI Act Art. 50 provenance metadata share a single source of truth per turn.
"""

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
import os
import re
import threading
from typing import Any, Dict, List, Optional
import uuid

# Allow safe, single-line correlation IDs and prevent CRLF / header log injection
REQUEST_ID_PATTERN = re.compile(r"^[a-zA-Z0-9._:-]{1,128}$")
TRACE_ID_PATTERN = re.compile(r"^[a-fA-F0-9]{16,64}$")

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


@dataclass
class AuditRequestState:
    """Mutable per-request correlation state shared across async tasks, worker threads, and SSE streams.

    Attributes:
        request_id: Validated client `X-Request-Id` or auto-generated `req-<uuid>` correlation ID.
        trace_id: Extracted hex trace identifier from `X-Cloud-Trace-Context`, if present.
        service_name: Logical service identifier (`ingestion-api` or `agent-api`).
        uid: Verified tenant user identifier bound during JWT authentication.
        method: HTTP request method (e.g., `'POST'`).
        path: HTTP request path (e.g., `'/agent/stream'`).
        pii_filtered: True when Cloud DLP / Model Armor masked sensitive content during the turn.
        model_armor_active: True when Cloud Model Armor screened prompt, chunks, or output.
        retrieved_chunks_count: Cumulative count of Chroma passages retrieved during the turn.
        guardrail_actions: Deduplicated list of guardrail block/redaction events recorded on the turn.
    """

    request_id: str
    trace_id: str = ""
    service_name: str = "interactiverag"
    uid: Optional[str] = None
    method: str = ""
    path: str = ""
    pii_filtered: bool = False
    model_armor_active: bool = False
    retrieved_chunks_count: int = 0
    guardrail_actions: List[Dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)


_audit_state_var: ContextVar[Optional[AuditRequestState]] = ContextVar(
    "interactiverag_audit_state", default=None
)


def sanitize_request_id(raw_value: Optional[str]) -> str:
    """Validates an incoming `X-Request-Id` header or generates a fresh correlation identifier.

    Args:
        raw_value: Raw `X-Request-Id` HTTP header value supplied by the caller.

    Returns:
        Header-safe correlation ID matching `REQUEST_ID_PATTERN` or a new `req-<hex>` UUID.
    """
    if isinstance(raw_value, str):
        candidate = raw_value.strip()
        if candidate and REQUEST_ID_PATTERN.match(candidate):
            return candidate
    return f"req-{uuid.uuid4().hex}"


def extract_trace_id(cloud_trace_header: Optional[str]) -> str:
    """Extracts the trace hex identifier from GCP's `X-Cloud-Trace-Context` HTTP header.

    Args:
        cloud_trace_header: Raw `X-Cloud-Trace-Context` value (`TRACE_ID/SPAN_ID;o=1`).

    Returns:
        Lowercase hex trace ID when valid, or an empty string otherwise.
    """
    if not isinstance(cloud_trace_header, str) or not cloud_trace_header.strip():
        return ""
    raw_trace = cloud_trace_header.strip().split("/", 1)[0].split(";", 1)[0].strip()
    if TRACE_ID_PATTERN.match(raw_trace):
        return raw_trace.lower()
    return ""


def bind_audit_state(state: AuditRequestState) -> Token[Optional[AuditRequestState]]:
    """Binds a fresh `AuditRequestState` to the current execution context.

    Args:
        state: Initialized `AuditRequestState` for the incoming HTTP request.

    Returns:
        ContextVar reset token to restore once the HTTP response finishes.
    """
    return _audit_state_var.set(state)


def reset_audit_state(token: Token[Optional[AuditRequestState]]) -> None:
    """Restores the `AuditRequestState` ContextVar using the token from `bind_audit_state`.

    Args:
        token: Token previously returned by `bind_audit_state`.
    """
    _audit_state_var.reset(token)


def get_audit_state() -> Optional[AuditRequestState]:
    """Returns the active per-request `AuditRequestState` if executing inside an HTTP request."""
    return _audit_state_var.get()


def get_request_id() -> str:
    """Returns the active request correlation ID, generating an ephemeral ID outside HTTP context."""
    state = _audit_state_var.get()
    if state is not None and state.request_id:
        return state.request_id
    return f"req-{uuid.uuid4().hex}"


def get_trace_id() -> str:
    """Returns the GCP Cloud Trace ID bound to the active request, or an empty string."""
    state = _audit_state_var.get()
    return state.trace_id if state is not None else ""


def get_service_name() -> str:
    """Returns the logical service name associated with the active request or container."""
    state = _audit_state_var.get()
    if state is not None and state.service_name:
        return state.service_name
    return os.getenv("K_SERVICE", "").strip() or "interactiverag"


def set_audit_uid(uid: Optional[str]) -> None:
    """Binds the verified tenant `uid` to the active request's correlation state.

    Args:
        uid: Verified path-safe user identifier.
    """
    state = _audit_state_var.get()
    if state is not None and uid:
        state.uid = uid


def get_audit_uid() -> Optional[str]:
    """Returns the verified tenant `uid` bound to the active request, if any."""
    state = _audit_state_var.get()
    return state.uid if state is not None else None


def mark_pii_filtered(filtered: bool = True) -> None:
    """Marks that Sensitive Data Protection / Model Armor redacted PII during the active request.

    Args:
        filtered: Boolean flag indicating PII masking occurred (defaults to `True`).
    """
    state = _audit_state_var.get()
    if state is not None and filtered:
        with state._lock:
            state.pii_filtered = True


def was_pii_filtered() -> bool:
    """Returns True if any chunk, prompt, or model output had PII masked during the active request."""
    state = _audit_state_var.get()
    return bool(state.pii_filtered) if state is not None else False


def mark_model_armor_active(active: bool = True) -> None:
    """Marks that Google Cloud Model Armor actively screened the current request.

    Args:
        active: Boolean flag indicating Model Armor executed (defaults to `True`).
    """
    state = _audit_state_var.get()
    if state is not None and active:
        with state._lock:
            state.model_armor_active = True


def was_model_armor_active() -> bool:
    """Returns True when Cloud Model Armor screened prompt, retrieval, or output on this request."""
    state = _audit_state_var.get()
    return bool(state.model_armor_active) if state is not None else False


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
    state = _audit_state_var.get()
    if state is None:
        return

    clean_filter = (filter_name or "model_armor_policy").strip()
    clean_action = (action or "blocked").strip()
    clean_stage = (stage or "output").strip()
    resolved_label = label or GUARDRAIL_FILTER_LABELS.get(clean_filter, clean_filter)
    normalized_details = [str(d).strip() for d in (details or []) if str(d).strip()]

    with state._lock:
        if clean_filter == "sdp" and clean_action == "redacted":
            state.pii_filtered = True

        for existing in state.guardrail_actions:
            if (
                existing["filter"] == clean_filter
                and existing["action"] == clean_action
                and existing["stage"] == clean_stage
            ):
                for item in normalized_details:
                    if item not in existing["details"]:
                        existing["details"].append(item)
                return

        state.guardrail_actions.append(
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
    state = _audit_state_var.get()
    if state is None:
        return []
    with state._lock:
        return [
            {
                "filter": item["filter"],
                "label": item["label"],
                "action": item["action"],
                "stage": item["stage"],
                "details": list(item["details"]),
            }
            for item in state.guardrail_actions
        ]


def record_retrieved_chunks(count: int) -> None:
    """Accumulates the number of vectorstore passages retrieved during the active request.

    Args:
        count: Number of passages returned by a retriever tool invocation.
    """
    state = _audit_state_var.get()
    if state is not None and count > 0:
        with state._lock:
            state.retrieved_chunks_count += int(count)


def get_retrieved_chunks_count() -> int:
    """Returns the total number of passages retrieved during the active request."""
    state = _audit_state_var.get()
    return int(state.retrieved_chunks_count) if state is not None else 0


__all__ = [
    "AuditRequestState",
    "GUARDRAIL_FILTER_LABELS",
    "REQUEST_ID_PATTERN",
    "TRACE_ID_PATTERN",
    "bind_audit_state",
    "extract_trace_id",
    "get_audit_state",
    "get_audit_uid",
    "get_guardrail_actions",
    "get_request_id",
    "get_retrieved_chunks_count",
    "get_service_name",
    "get_trace_id",
    "mark_model_armor_active",
    "mark_pii_filtered",
    "record_guardrail_action",
    "record_retrieved_chunks",
    "reset_audit_state",
    "sanitize_request_id",
    "set_audit_uid",
    "was_model_armor_active",
    "was_pii_filtered",
]
