import hashlib
import json
import os
import re
import sys
import threading
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from google.cloud import logging as cloud_logging

from src.core.config import get_google_cloud_location
from src.core.logger import setup_logging

logger = setup_logging(name="audit")

DEFAULT_AUDIT_LOG_NAME = "interactiverag-audit"

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
    """Mutable per-request correlation state shared across async tasks, worker threads, and SSE streams."""

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

_cloud_loggers: Dict[Tuple[str, str], Any] = {}
_cloud_loggers_lock = threading.Lock()
_audit_listeners: List[Callable[[Dict[str, Any]], None]] = []


def sanitize_request_id(raw_value: Optional[str]) -> str:
    """Returns a validated client X-Request-Id or generates a fresh correlation ID."""
    if isinstance(raw_value, str):
        candidate = raw_value.strip()
        if candidate and REQUEST_ID_PATTERN.match(candidate):
            return candidate
    return f"req-{uuid.uuid4().hex}"


def extract_trace_id(cloud_trace_header: Optional[str]) -> str:
    """Extracts the trace ID from GCP's `X-Cloud-Trace-Context: TRACE_ID/SPAN_ID;o=1` header."""
    if not isinstance(cloud_trace_header, str) or not cloud_trace_header.strip():
        return ""
    raw_trace = cloud_trace_header.strip().split("/", 1)[0].split(";", 1)[0].strip()
    if TRACE_ID_PATTERN.match(raw_trace):
        return raw_trace.lower()
    return ""


def hash_query(text: Optional[str]) -> str:
    """Computes a deterministic SHA-256 hex digest so queries can be correlated without storing PII."""
    normalized = (text or "").strip().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def get_audit_state() -> Optional[AuditRequestState]:
    """Returns the active per-request AuditRequestState if running inside an HTTP request."""
    return _audit_state_var.get()


def get_request_id() -> str:
    """Returns the current request correlation ID (generating an ephemeral one outside HTTP context)."""
    state = _audit_state_var.get()
    if state is not None and state.request_id:
        return state.request_id
    return f"req-{uuid.uuid4().hex}"


def get_trace_id() -> str:
    """Returns the current GCP Cloud Trace ID if present on the active request."""
    state = _audit_state_var.get()
    return state.trace_id if state is not None else ""


def get_service_name() -> str:
    """Returns the logical service name associated with the current request."""
    state = _audit_state_var.get()
    if state is not None and state.service_name:
        return state.service_name
    return os.getenv("K_SERVICE", "").strip() or "interactiverag"


def set_audit_uid(uid: Optional[str]) -> None:
    """Binds the verified user UID to the active request's correlation state."""
    state = _audit_state_var.get()
    if state is not None and uid:
        state.uid = uid


def get_audit_uid() -> Optional[str]:
    """Returns the verified user UID bound to the active request, if any."""
    state = _audit_state_var.get()
    return state.uid if state is not None else None


def mark_pii_filtered(filtered: bool = True) -> None:
    """Records that Sensitive Data Protection / Model Armor redacted sensitive content during this request."""
    state = _audit_state_var.get()
    if state is not None and filtered:
        with state._lock:
            state.pii_filtered = True


def was_pii_filtered() -> bool:
    """Returns True if any chunk or model output had PII masked during the active request."""
    state = _audit_state_var.get()
    return bool(state.pii_filtered) if state is not None else False


def mark_model_armor_active(active: bool = True) -> None:
    """Records that Google Cloud Model Armor actively screened this request."""
    state = _audit_state_var.get()
    if state is not None and active:
        with state._lock:
            state.model_armor_active = True


def was_model_armor_active() -> bool:
    """Returns True when Cloud Model Armor screened prompt, retrieval, or output during this request."""
    state = _audit_state_var.get()
    return bool(state.model_armor_active) if state is not None else False


def record_guardrail_action(
    filter_name: str,
    action: str,
    stage: str,
    label: Optional[str] = None,
    details: Optional[List[str]] = None,
) -> None:
    """Records a Model Armor or Gemini safety guardrail action (`blocked` or `redacted`) on the active request."""
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
    """Returns a copy of all guardrail actions triggered during the active request."""
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
    """Accumulates the number of vectorstore passages retrieved during the active request."""
    state = _audit_state_var.get()
    if state is not None and count > 0:
        with state._lock:
            state.retrieved_chunks_count += int(count)


def get_retrieved_chunks_count() -> int:
    """Returns the total number of passages retrieved during the active request."""
    state = _audit_state_var.get()
    return int(state.retrieved_chunks_count) if state is not None else 0


def get_audit_log_name() -> str:
    """Returns the target GCP Cloud Logging log name (defaults to `interactiverag-audit`)."""
    return os.getenv("AUDIT_LOG_NAME", DEFAULT_AUDIT_LOG_NAME).strip() or DEFAULT_AUDIT_LOG_NAME


def _is_cloud_logging_enabled() -> bool:
    """Determines whether audit events should be shipped over the network to GCP Cloud Logging."""
    explicit = os.getenv("AUDIT_CLOUD_LOGGING_ENABLED", "").strip().lower()
    if explicit in {"1", "true", "yes"}:
        return bool(os.getenv("GOOGLE_CLOUD_PROJECT", "").strip())
    if explicit in {"0", "false", "no"}:
        return False

    # Keep offline unit test suites fast and side-effect-free unless explicitly enabled
    if "unittest" in sys.modules or "pytest" in sys.modules:
        return False

    return bool(os.getenv("GOOGLE_CLOUD_PROJECT", "").strip())


def _get_cloud_logger(project_id: str, log_name: str):
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


def register_audit_listener(listener: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
    """Registers an in-process listener (used by compliance unit tests) and returns an unsubscribe callable."""
    _audit_listeners.append(listener)

    def _remove() -> None:
        if listener in _audit_listeners:
            _audit_listeners.remove(listener)

    return _remove


def emit_audit_event(
    event_type: str,
    *,
    severity: str = "INFO",
    uid: Optional[str] = None,
    status: str = "success",
    **fields: Any,
) -> Dict[str, Any]:
    """Emits a structured, PII-free compliance audit record to GCP Cloud Logging (`interactiverag-audit`).

    Automatically enriches the record with `request_id`, `trace_id`, `service`, `region`, and `uid`
    from the active request context, and falls back to structured JSON logging when offline.
    """
    state = _audit_state_var.get()
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    resolved_uid = uid or (state.uid if state is not None else None)
    request_id = state.request_id if state is not None else f"req-{uuid.uuid4().hex}"
    trace_id = state.trace_id if state is not None else ""
    service_name = state.service_name if state is not None else get_service_name()
    normalized_severity = (severity or "INFO").strip().upper()

    safe_fields: Dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        # Defense-in-depth: never allow raw query or prompt strings inside audit payloads
        if key in {"query", "prompt", "raw_query", "user_prompt"} and isinstance(value, str):
            safe_fields["query_sha256"] = hash_query(value)
            safe_fields["query_chars"] = len(value)
            continue
        safe_fields[key] = value

    record: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "severity": normalized_severity,
        "status": status,
        "service": service_name,
        "region": get_google_cloud_location(),
        "request_id": request_id,
    }
    if trace_id:
        record["trace_id"] = trace_id
    if resolved_uid:
        record["uid"] = resolved_uid
    if state is not None and state.method:
        record["http_method"] = state.method
    if state is not None and state.path:
        record["http_path"] = state.path

    record.update(safe_fields)

    for listener in list(_audit_listeners):
        try:
            listener(dict(record))
        except Exception:
            pass

    shipped_to_gcp = False
    if project_id and _is_cloud_logging_enabled():
        try:
            cloud_logger = _get_cloud_logger(project_id, get_audit_log_name())
            log_kwargs: Dict[str, Any] = {"severity": normalized_severity}
            if trace_id:
                log_kwargs["trace"] = f"projects/{project_id}/traces/{trace_id}"
            cloud_logger.log_struct(record, **log_kwargs)
            shipped_to_gcp = True
        except Exception as exc:
            logger.warning(
                f"Cloud Logging publish failed for event '{event_type}' (falling back to stdout JSON): {exc}"
            )

    if not shipped_to_gcp:
        serialized = json.dumps(record, sort_keys=True, default=str)
        if normalized_severity in {"ERROR", "CRITICAL"}:
            logger.error(f"AUDIT_EVENT {serialized}")
        elif normalized_severity == "WARNING":
            logger.warning(f"AUDIT_EVENT {serialized}")
        else:
            logger.info(f"AUDIT_EVENT {serialized}")

    return record


class AuditCorrelationMiddleware:
    """Pure ASGI middleware propagating `X-Request-Id` and `X-Cloud-Trace-Context` across sync & SSE routes."""

    def __init__(self, app: Any, service_name: str) -> None:
        self.app = app
        self.service_name = service_name

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        raw_headers: Dict[bytes, bytes] = {}
        for k, v in scope.get("headers") or []:
            if k not in raw_headers:
                raw_headers[k.lower()] = v

        raw_req_id = (
            raw_headers.get(b"x-request-id", b"").decode("latin-1", errors="ignore")
            if b"x-request-id" in raw_headers
            else None
        )
        raw_trace = (
            raw_headers.get(b"x-cloud-trace-context", b"").decode("latin-1", errors="ignore")
            if b"x-cloud-trace-context" in raw_headers
            else None
        )

        request_id = sanitize_request_id(raw_req_id)
        trace_id = extract_trace_id(raw_trace)
        method = str(scope.get("method") or "")
        path = str(scope.get("path") or "")

        state = AuditRequestState(
            request_id=request_id,
            trace_id=trace_id,
            service_name=self.service_name,
            method=method,
            path=path,
        )
        token = _audit_state_var.set(state)

        async def send_with_request_id(message: Dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                headers = [
                    (hk, hv) for hk, hv in headers if hk.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            _audit_state_var.reset(token)
