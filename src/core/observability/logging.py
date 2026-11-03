"""Structured GCP JSON application logging and request/trace correlation filter (Pillar 5: Observability).

Provides:
- `RequestContextLogFilter`: Injects `request_id`, `trace_id`, `span_id`, `service`, `uid`,
  `http_method`, and `http_path` from `src.core.context` onto every `logging.LogRecord` and
  scrubs any accidental raw user query/prompt strings into `query_sha256` and `query_chars`.
- `GCPJsonFormatter`: Emits single-line JSON log records conforming to Google Cloud Logging's
  structured payload schema (`severity`, `message`, `timestamp`, `logging.googleapis.com/trace`,
  `logging.googleapis.com/spanId`, `logging.googleapis.com/sourceLocation`).
"""

from datetime import datetime, timezone
import json
import logging
import re
from typing import Any, Dict, Set

from src.core.config import get_google_cloud_project
from src.core.context import (
    RAW_QUERY_KEYS,
    get_request_context,
    get_service_name,
    scrub_query_fields,
)

_INLINE_QUERY_RE = re.compile(
    r"""\b(?P<key>query|prompt|raw_query|user_prompt)\s*=\s*(?P<quote>['"])(?P<val>.*?)(?P=quote)(?=\s*[,)\]}\s]|$)""",
    re.DOTALL,
)

_STANDARD_LOG_RECORD_ATTRS: Set[str] = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "hostname",
    "http_method",
    "http_path",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "request_id",
    "service",
    "span_id",
    "stack_info",
    "taskName",
    "thread",
    "threadName",
    "trace_id",
    "uid",
    "username",
}


def _scrub_inline_query_text(text: str) -> str:
    """Replaces inline `query='...'` or `prompt="..."` substrings with SHA-256 hash metadata."""
    if not text or not any(k in text for k in ("query", "prompt")):
        return text

    def _replacer(match: re.Match[str]) -> str:
        scrubbed = scrub_query_fields({match.group("key"): match.group("val")})
        return f"query_sha256='{scrubbed['query_sha256']}', query_chars={scrubbed['query_chars']}"

    return _INLINE_QUERY_RE.sub(_replacer, text)


class RequestContextLogFilter(logging.Filter):
    """Enriches `LogRecord` instances with `RequestContext` fields and scrubs raw query strings."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Attaches request/trace correlation attributes and scrubs any raw query fields.

        Args:
            record: Standard Python `logging.LogRecord` being processed.

        Returns:
            Always `True` after enriching and scrubbing `record` in place.
        """
        ctx = get_request_context()
        if ctx is not None:
            record.request_id = getattr(record, "request_id", "") or ctx.request_id
            record.trace_id = getattr(record, "trace_id", "") or ctx.trace_id
            record.span_id = getattr(record, "span_id", "") or ctx.active_span_id
            record.service = getattr(record, "service", "") or ctx.service_name
            record.uid = getattr(record, "uid", "") or (ctx.uid or "")
            record.http_method = getattr(record, "http_method", "") or ctx.method
            record.http_path = getattr(record, "http_path", "") or ctx.path
        else:
            record.request_id = getattr(record, "request_id", "")
            record.trace_id = getattr(record, "trace_id", "")
            record.span_id = getattr(record, "span_id", "")
            record.service = getattr(record, "service", "") or get_service_name()
            record.uid = getattr(record, "uid", "")
            record.http_method = getattr(record, "http_method", "")
            record.http_path = getattr(record, "http_path", "")

        # Scrub any raw query/prompt fields passed via `extra={...}` using `scrub_query_fields`
        raw_query_extras = {
            key: record.__dict__.pop(key)
            for key in RAW_QUERY_KEYS
            if key in record.__dict__
        }
        if raw_query_extras:
            record.__dict__.update(scrub_query_fields(raw_query_extras))

        # Scrub inline `query='...'` or `prompt="..."` patterns in string log messages
        if isinstance(record.msg, str) and not record.args:
            record.msg = _scrub_inline_query_text(record.msg)
        elif isinstance(record.msg, dict):
            record.msg = scrub_query_fields(record.msg)

        return True


class GCPJsonFormatter(logging.Formatter):
    """Formats `LogRecord` instances as single-line Google Cloud Logging structured JSON."""

    def __init__(self) -> None:
        super().__init__()
        self._context_filter = RequestContextLogFilter()

    def format(self, record: logging.LogRecord) -> str:
        """Serializes `record` into a single-line JSON string with GCP trace & sourceLocation fields.

        Args:
            record: Standard Python `logging.LogRecord` to format.

        Returns:
            Single-line JSON string ready for stdout ingestion by Cloud Run / Cloud Logging.
        """
        self._context_filter.filter(record)

        message = _scrub_inline_query_text(record.getMessage())
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        project_id = get_google_cloud_project()

        payload: Dict[str, Any] = {
            "timestamp": timestamp,
            "severity": record.levelname,
            "message": message,
            "logger": record.name,
            "service": getattr(record, "service", "") or get_service_name(),
            "logging.googleapis.com/sourceLocation": {
                "file": record.filename,
                "line": str(record.lineno),
                "function": record.funcName,
            },
        }

        request_id = getattr(record, "request_id", "")
        if request_id:
            payload["request_id"] = request_id

        uid = getattr(record, "uid", "")
        if uid:
            payload["uid"] = uid

        http_method = getattr(record, "http_method", "")
        if http_method:
            payload["http_method"] = http_method

        http_path = getattr(record, "http_path", "")
        if http_path:
            payload["http_path"] = http_path

        trace_id = getattr(record, "trace_id", "")
        if trace_id:
            payload["trace_id"] = trace_id
            trace_project = project_id or "local"
            payload["logging.googleapis.com/trace"] = f"projects/{trace_project}/traces/{trace_id}"
            payload["logging.googleapis.com/trace_sampled"] = True

        span_id = getattr(record, "span_id", "")
        if span_id:
            payload["span_id"] = span_id
            payload["logging.googleapis.com/spanId"] = span_id

        # Attach any extra structured fields (such as scrubbed query_sha256/query_chars)
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOG_RECORD_ATTRS or key.startswith("_"):
                continue
            if value is not None:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, sort_keys=True, default=str)


__all__ = [
    "GCPJsonFormatter",
    "RequestContextLogFilter",
]
