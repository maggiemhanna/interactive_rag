"""Structured compliance audit logging for InteractiveRAG (Pillar 4: Compliance & Governance).

Publishes PII-free, SHA-256 query-scrubbed audit records to Google Cloud Logging (`interactiverag-audit`)
with automatic Cloud Trace correlation and graceful stdout JSON fallback when running offline.
"""

from datetime import datetime, timezone
import json
import os
import sys
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid

from google.cloud import logging as cloud_logging

from src.core.config import get_google_cloud_location
from src.core.context import get_request_context, get_service_name, hash_query
from src.core.logger import setup_logging

logger = setup_logging(name="audit")

DEFAULT_AUDIT_LOG_NAME = "interactiverag-audit"

_cloud_loggers: Dict[Tuple[str, str], Any] = {}
_cloud_loggers_lock = threading.Lock()
_audit_listeners: List[Callable[[Dict[str, Any]], None]] = []


def get_audit_log_name() -> str:
    """Returns the target GCP Cloud Logging log name (defaults to `'interactiverag-audit'`)."""
    return os.getenv("AUDIT_LOG_NAME", DEFAULT_AUDIT_LOG_NAME).strip() or DEFAULT_AUDIT_LOG_NAME


def _is_cloud_logging_enabled() -> bool:
    """Determines whether audit events should be shipped over the network to GCP Cloud Logging.

    Honors explicit `AUDIT_CLOUD_LOGGING_ENABLED` overrides and disables live network calls
    automatically under `unittest`/`pytest` unless explicitly opted in.

    Returns:
        True when Cloud Logging publishing is active and `GOOGLE_CLOUD_PROJECT` is configured.
    """
    explicit = os.getenv("AUDIT_CLOUD_LOGGING_ENABLED", "").strip().lower()
    if explicit in {"1", "true", "yes"}:
        return bool(os.getenv("GOOGLE_CLOUD_PROJECT", "").strip())
    if explicit in {"0", "false", "no"}:
        return False

    # Keep offline unit test suites fast and side-effect-free unless explicitly enabled
    if "unittest" in sys.modules or "pytest" in sys.modules:
        return False

    return bool(os.getenv("GOOGLE_CLOUD_PROJECT", "").strip())


def _get_cloud_logger(project_id: str, log_name: str) -> Any:
    """Returns a cached `google.cloud.logging.Logger` writing to `projects/<project>/logs/<log_name>`.

    Args:
        project_id: GCP project identifier hosting the Cloud Logging bucket/sink.
        log_name: Logical audit log stream name (e.g., `'interactiverag-audit'`).

    Returns:
        Cached `google.cloud.logging.Logger` instance.
    """
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
    """Registers an in-process audit event observer and returns an unsubscribe function.

    Primarily used by compliance unit tests to inspect structured audit records synchronously.

    Args:
        listener: Callable invoked with a shallow copy of every emitted audit record dictionary.

    Returns:
        Zero-argument callable that removes `listener` when invoked.
    """
    _audit_listeners.append(listener)

    def _remove() -> None:
        """Unregisters the previously attached audit event listener."""
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

    Automatically enriches the record with `request_id`, `trace_id`, `service`, `region`,
    `http_method`, `http_path`, and `uid` from the active `RequestContext`, scrubs any raw
    `query`/`prompt` keyword arguments into `query_sha256` + `query_chars`, and falls back to
    structured stdout JSON when Cloud Logging is disabled or unreachable.

    Args:
        event_type: Dot-namespaced event identifier (e.g., `'document.ingest'`, `'agent.query'`).
        severity: Cloud Logging severity level (`'INFO'`, `'WARNING'`, `'ERROR'`, `'CRITICAL'`).
        uid: Optional explicit tenant `uid` (falls back to context-bound `uid` if omitted).
        status: Outcome status label (`'success'`, `'blocked'`, `'denied'`, `'error'`, etc.).
        **fields: Additional structured metadata fields to attach to the audit payload.

    Returns:
        Complete structured audit record dictionary that was published.
    """
    state = get_request_context()
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


__all__ = [
    "DEFAULT_AUDIT_LOG_NAME",
    "emit_audit_event",
    "get_audit_log_name",
    "hash_query",
    "register_audit_listener",
]
