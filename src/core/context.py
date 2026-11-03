"""Neutral per-request correlation context (`RequestContext`) and ASGI middleware.

Maintains thread-safe `contextvars` request identity and trace correlation state
(`request_id`, `trace_id`, `root_span_id`, `active_span_id`, `service_name`, `uid`,
`method`, `path`, `request_start_perf`) alongside a thread-safe `data` store used by
downstream `compliance` and `observability` modules.
"""

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
import hashlib
import re
import threading
import time
from typing import Any, Dict, Mapping, Optional
import uuid

from src.core.config import get_cloud_run_service

# Allow safe, single-line correlation IDs and prevent CRLF / header log injection
REQUEST_ID_PATTERN = re.compile(r"^[a-zA-Z0-9._:-]{1,128}$")
TRACE_ID_PATTERN = re.compile(r"^[a-fA-F0-9]{16,64}$")
SPAN_ID_HEX_PATTERN = re.compile(r"^[a-fA-F0-9]{1,16}$")

# Field names containing raw query/prompt text that must always be hashed in logs/spans/events
RAW_QUERY_KEYS: frozenset[str] = frozenset({"query", "prompt", "raw_query", "user_prompt"})


def hash_query(text: Optional[str]) -> str:
    """Computes a deterministic SHA-256 hex digest of query text without storing raw PII.

    Args:
        text: Raw user query or prompt string.

    Returns:
        64-character lowercase hexadecimal SHA-256 digest of the UTF-8 trimmed text.
    """
    normalized = (text or "").strip().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def scrub_query_fields(fields: Mapping[str, Any]) -> Dict[str, Any]:
    """Replaces raw query/prompt keys with `query_sha256` and `query_chars` and omits `None` values.

    Guarantees that raw user queries or prompts are never persisted in compliance audit records,
    telemetry payloads, structured logs, or trace span attributes.

    Args:
        fields: Arbitrary key-value metadata mapping.

    Returns:
        Sanitized dictionary with raw query keys converted to `query_sha256` and `query_chars`.
    """
    safe_fields: Dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        if key in RAW_QUERY_KEYS:
            text = str(value)
            safe_fields["query_sha256"] = hash_query(text)
            safe_fields["query_chars"] = len(text)
        else:
            safe_fields[key] = value
    return safe_fields


def generate_span_id() -> str:
    """Generates a random 16-character lowercase hexadecimal span identifier."""
    return uuid.uuid4().hex[:16]


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


def extract_span_id(cloud_trace_header: Optional[str]) -> str:
    """Extracts and normalizes the parent span ID from GCP's `X-Cloud-Trace-Context` header.

    GCP's `X-Cloud-Trace-Context: TRACE_ID/SPAN_ID;o=1` encodes `SPAN_ID` as an unsigned
    64-bit decimal integer (or hex string). Converts valid values into a 16-char lowercase
    hex span ID, or returns an empty string if absent/invalid.

    Args:
        cloud_trace_header: Raw `X-Cloud-Trace-Context` HTTP header value.

    Returns:
        16-character lowercase hex span ID, or an empty string if not present.
    """
    if not isinstance(cloud_trace_header, str) or "/" not in cloud_trace_header:
        return ""
    after_slash = cloud_trace_header.strip().split("/", 1)[1]
    raw_span = after_slash.split(";", 1)[0].strip()
    if not raw_span:
        return ""
    if raw_span.isdigit():
        try:
            val = int(raw_span)
            if 0 < val < (1 << 64):
                return f"{val:016x}"
        except ValueError:
            return ""
    if SPAN_ID_HEX_PATTERN.match(raw_span):
        return raw_span.lower().zfill(16)
    return ""


@dataclass
class RequestContext:
    """Neutral per-request correlation context shared across async handlers, worker threads, and SSE.

    Attributes:
        request_id: Validated client `X-Request-Id` or auto-generated `req-<uuid>` correlation ID.
        trace_id: Extracted hex trace identifier from `X-Cloud-Trace-Context`, if present.
        root_span_id: Root 16-char hex span ID for the HTTP request.
        active_span_id: Currently active 16-char hex span ID (updated as child spans enter/exit).
        service_name: Logical service identifier (`'ingest-api'` or `'agent-api'`).
        uid: Verified tenant user identifier bound during JWT authentication.
        method: HTTP request method (e.g., `'POST'`).
        path: HTTP request path (e.g., `'/agent/stream'`).
        request_start_perf: `time.perf_counter()` timestamp recorded when the request started.
        data: Thread-safe dictionary used by domain packages (`compliance`, `observability`) to
            attach per-request state without coupling `src/core/context.py` to domain schemas.
        lock: Per-request lock synchronizing concurrent worker thread updates to `data`.
    """

    request_id: str
    trace_id: str = ""
    root_span_id: str = field(default_factory=generate_span_id)
    active_span_id: str = ""
    service_name: str = "interactiverag"
    uid: Optional[str] = None
    method: str = ""
    path: str = ""
    request_start_perf: float = field(default_factory=time.perf_counter)
    data: Dict[str, Any] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Initializes `active_span_id` to `root_span_id` when not explicitly provided."""
        if not self.active_span_id:
            self.active_span_id = self.root_span_id


_request_context_var: ContextVar[Optional[RequestContext]] = ContextVar(
    "interactiverag_request_context", default=None
)


def bind_request_context(state: RequestContext) -> Token[Optional[RequestContext]]:
    """Binds a fresh `RequestContext` to the current execution context.

    Args:
        state: Initialized `RequestContext` for the incoming HTTP request.

    Returns:
        ContextVar reset token to restore once the HTTP response finishes.
    """
    return _request_context_var.set(state)


def reset_request_context(token: Token[Optional[RequestContext]]) -> None:
    """Restores the `RequestContext` ContextVar using the token from `bind_request_context`.

    Args:
        token: Token previously returned by `bind_request_context`.
    """
    _request_context_var.reset(token)


def get_request_context() -> Optional[RequestContext]:
    """Returns the active per-request `RequestContext` if executing inside a bound context."""
    return _request_context_var.get()


def get_request_id() -> str:
    """Returns the active request correlation ID, generating an ephemeral ID outside HTTP context."""
    state = _request_context_var.get()
    if state is not None and state.request_id:
        return state.request_id
    return f"req-{uuid.uuid4().hex}"


def get_trace_id() -> str:
    """Returns the GCP Cloud Trace ID bound to the active request, or an empty string."""
    state = _request_context_var.get()
    return state.trace_id if state is not None else ""


def get_active_span_id() -> str:
    """Returns the currently active 16-char hex span ID, or an empty string outside a request."""
    state = _request_context_var.get()
    if state is None:
        return ""
    return state.active_span_id or state.root_span_id


def set_active_span_id(span_id: str) -> Optional[str]:
    """Sets the active span ID on the current `RequestContext` and returns the previous span ID.

    Args:
        span_id: New 16-character hex span identifier.

    Returns:
        Previous `active_span_id` string if a `RequestContext` is bound, or `None`.
    """
    state = _request_context_var.get()
    if state is None:
        return None
    with state.lock:
        previous = state.active_span_id
        state.active_span_id = span_id
        return previous


def get_service_name() -> str:
    """Returns the logical service name associated with the active request or container."""
    state = _request_context_var.get()
    if state is not None and state.service_name:
        return state.service_name
    return get_cloud_run_service() or "interactiverag"


def set_request_uid(uid: Optional[str]) -> None:
    """Binds the verified tenant `uid` to the active request's context.

    Args:
        uid: Verified path-safe user identifier.
    """
    state = _request_context_var.get()
    if state is not None and uid:
        with state.lock:
            state.uid = uid


def get_request_uid() -> Optional[str]:
    """Returns the verified tenant `uid` bound to the active request, if any."""
    state = _request_context_var.get()
    return state.uid if state is not None else None


class RequestCorrelationMiddleware:
    """Pure ASGI middleware propagating `X-Request-Id` and `X-Cloud-Trace-Context` across REST and SSE routes.

    Implemented as a raw ASGI middleware (rather than `BaseHTTPMiddleware`) so `ContextVar`
    state stays bound for the full lifetime of streaming `StreamingResponse` generators.
    """

    def __init__(self, app: Any, service_name: str) -> None:
        """Initializes the correlation middleware for a FastAPI/ASGI service.

        Args:
            app: Downstream ASGI application.
            service_name: Logical service name (`'ingest-api'` or `'agent-api'`).
        """
        self.app = app
        self.service_name = service_name

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        """Intercepts HTTP requests, binds `RequestContext`, and injects `X-Request-Id` on responses."""
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
        incoming_span_id = extract_span_id(raw_trace) or generate_span_id()
        method = str(scope.get("method") or "")
        path = str(scope.get("path") or "")

        state = RequestContext(
            request_id=request_id,
            trace_id=trace_id,
            root_span_id=incoming_span_id,
            active_span_id=incoming_span_id,
            service_name=self.service_name,
            method=method,
            path=path,
        )
        token = bind_request_context(state)

        async def send_with_request_id(message: Dict[str, Any]) -> None:
            """Injects the canonical `X-Request-Id` header into `http.response.start` frames."""
            if message.get("type") == "http.response.start":
                headers = [
                    (hk, hv)
                    for hk, hv in list(message.get("headers") or [])
                    if hk.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            reset_request_context(token)


__all__ = [
    "RAW_QUERY_KEYS",
    "REQUEST_ID_PATTERN",
    "SPAN_ID_HEX_PATTERN",
    "TRACE_ID_PATTERN",
    "RequestContext",
    "RequestCorrelationMiddleware",
    "bind_request_context",
    "extract_span_id",
    "extract_trace_id",
    "generate_span_id",
    "get_active_span_id",
    "get_request_context",
    "get_request_id",
    "get_request_uid",
    "get_service_name",
    "get_trace_id",
    "hash_query",
    "reset_request_context",
    "sanitize_request_id",
    "scrub_query_fields",
    "set_active_span_id",
    "set_request_uid",
]

