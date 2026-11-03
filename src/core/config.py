"""Centralized environment configuration and runtime settings for InteractiveRAG.

Loads `.env` files once on startup and exposes typed, validated runtime getters for
approved Vertex AI model catalogs, regional GCP location pinning, Cloud Run runtime
detection, CORS allowlists, upload/query ceilings, and Model Armor configuration.
"""

import os
import re
import sys
from typing import List, Tuple

from dotenv import find_dotenv, load_dotenv


def load_env() -> None:
    """Loads environment variables from repository root `.env` if present."""
    load_dotenv(find_dotenv())


# Load environment variables once on core config import before deriving defaults
load_env()

INGEST_PORT = 8000
AGENT_PORT = 8001

DEFAULT_AUDIT_LOG_NAME = "interactiverag-audit"
DEFAULT_TELEMETRY_LOG_NAME = "interactiverag-telemetry"

# Strict collection name pattern preventing prompt/schema smuggling via collection labels
COLLECTION_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Concrete single-region GCP identifier pattern (rejects 'global' and multi-regions like 'us'/'eu')
GCP_REGION_PATTERN = re.compile(r"^[a-z]+-[a-z]+[0-9]+$")

TRUE_ENV_VALUES = frozenset({"1", "true", "yes"})
FALSE_ENV_VALUES = frozenset({"0", "false", "no"})


def parse_bool_env(env_var: str, default: bool = False) -> bool:
    """Parses a boolean flag from an environment variable with a safe fallback.

    Recognizes truthy values (`'1'`, `'true'`, `'yes'`) and falsy values (`'0'`, `'false'`, `'no'`).

    Args:
        env_var: Name of the boolean environment variable to read.
        default: Fallback boolean returned when unset or unrecognized.

    Returns:
        Parsed boolean value from the environment or `default`.
    """
    raw = os.getenv(env_var, "").strip().lower()
    if raw in TRUE_ENV_VALUES:
        return True
    if raw in FALSE_ENV_VALUES:
        return False
    return default


def _parse_required_csv_models(env_var: str) -> Tuple[str, ...]:
    """Parses a required comma-separated environment variable into a deduplicated model tuple.

    Args:
        env_var: Name of the environment variable containing approved model identifiers.

    Returns:
        Deduplicated tuple of non-empty model identifier strings preserving declaration order.

    Raises:
        ValueError: If the environment variable is unset or contains no valid model names.
    """
    raw = os.getenv(env_var, "").strip()
    parsed = tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))
    if not parsed:
        raise ValueError(
            f"{env_var} environment variable must be set to a non-empty comma-separated list of approved models."
        )
    return parsed


def _parse_positive_int(env_var: str, default: int) -> int:
    """Parses a positive integer ceiling from an environment variable with a safe fallback.

    Args:
        env_var: Name of the integer environment variable to read.
        default: Fallback positive integer returned when unset, non-numeric, or <= 0.

    Returns:
        Positive integer value from the environment or `default`.
    """
    raw = os.getenv(env_var, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        return value if value > 0 else default
    except ValueError:
        return default


def _parse_non_negative_int(env_var: str, default: int) -> int:
    """Parses a non-negative integer (`>= 0`) from an environment variable with a safe fallback.

    Allows `0` so callers can explicitly disable optional byte/free-disk thresholds in tests.

    Args:
        env_var: Name of the integer environment variable to read.
        default: Fallback non-negative integer returned when unset, non-numeric, or `< 0`.

    Returns:
        Non-negative integer value from the environment or `default`.
    """
    raw = os.getenv(env_var, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        return value if value >= 0 else default
    except ValueError:
        return default


def _parse_positive_float(env_var: str, default: float) -> float:
    """Parses a positive float (`> 0.0`) from an environment variable with a safe fallback.

    Args:
        env_var: Name of the floating-point environment variable to read.
        default: Fallback positive float returned when unset, non-numeric, or `<= 0.0`.

    Returns:
        Positive float value from the environment or `default`.
    """
    raw = os.getenv(env_var, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
        return value if value > 0.0 else default
    except ValueError:
        return default


def get_cloud_run_service() -> str:
    """Returns the Cloud Run service name (`K_SERVICE`), or an empty string when running locally."""
    return os.getenv("K_SERVICE", "").strip()


def get_cloud_run_revision() -> str:
    """Returns the Cloud Run revision identifier (`K_REVISION`), defaulting to `'local'`."""
    return os.getenv("K_REVISION", "local").strip() or "local"


def is_cloud_run() -> bool:
    """Checks whether the process is executing inside a Google Cloud Run container.

    Returns:
        True when Cloud Run's standard `K_SERVICE` environment variable is populated.
    """
    return bool(get_cloud_run_service())


def get_service_port(default_port: int) -> int:
    """Returns the HTTP listener port (`PORT`), falling back to `default_port`."""
    return _parse_positive_int("PORT", default_port)


def get_google_cloud_project() -> str:
    """Returns the configured `GOOGLE_CLOUD_PROJECT` identifier (or empty string if unset)."""
    return os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()


def get_firebase_project_id() -> str:
    """Returns the project ID used by Firebase Admin token verification (`GOOGLE_CLOUD_PROJECT` or `FIREBASE_PROJECT_ID`)."""
    return get_google_cloud_project() or os.getenv("FIREBASE_PROJECT_ID", "").strip()


def get_dev_user_id() -> str:
    """Returns the local development fallback user ID (`DEV_USER_ID`, defaulting to `'local-dev'`)."""
    return os.getenv("DEV_USER_ID", "local-dev").strip() or "local-dev"


def is_auth_disabled() -> bool:
    """Returns True when `AUTH_DISABLED` is truthy in the environment (still gated by `is_cloud_run()` in auth)."""
    return parse_bool_env("AUTH_DISABLED", default=False)


def is_structured_logging_enabled() -> bool:
    """Returns True when application logs should be emitted as single-line GCP JSON on stdout.

    Honors explicit `STRUCTURED_LOGGING` (`true`/`false`), and defaults to `True` on
    Cloud Run (`K_SERVICE` set) and `False` during local terminal development.
    """
    return parse_bool_env("STRUCTURED_LOGGING", default=is_cloud_run())


def is_cloud_trace_enabled() -> bool:
    """Returns True when completed spans should be exported to Google Cloud Trace.

    Honors explicit `CLOUD_TRACE_ENABLED` (`true`/`false`), and defaults to `True` on
    Cloud Run (`K_SERVICE` set) and `False` during local development and offline tests.
    """
    return parse_bool_env("CLOUD_TRACE_ENABLED", default=is_cloud_run())


def get_audit_log_name() -> str:
    """Returns the target GCP Cloud Logging audit log name (defaults to `'interactiverag-audit'`)."""
    return os.getenv("AUDIT_LOG_NAME", DEFAULT_AUDIT_LOG_NAME).strip() or DEFAULT_AUDIT_LOG_NAME


def is_audit_cloud_logging_enabled() -> bool:
    """Returns True when audit events should be shipped over the network to GCP Cloud Logging.

    Honors explicit `AUDIT_CLOUD_LOGGING_ENABLED` overrides, disables live network calls
    automatically under `unittest`/`pytest` unless explicitly opted in, and requires `GOOGLE_CLOUD_PROJECT`.
    """
    default_enabled = not ("unittest" in sys.modules or "pytest" in sys.modules)
    return parse_bool_env("AUDIT_CLOUD_LOGGING_ENABLED", default=default_enabled) and bool(
        get_google_cloud_project()
    )


def get_telemetry_log_name() -> str:
    """Returns the target GCP Cloud Logging telemetry log name (defaults to `'interactiverag-telemetry'`)."""
    return (
        os.getenv("TELEMETRY_LOG_NAME", DEFAULT_TELEMETRY_LOG_NAME).strip()
        or DEFAULT_TELEMETRY_LOG_NAME
    )


def is_telemetry_cloud_logging_enabled() -> bool:
    """Returns True when telemetry events should be shipped over the network to GCP Cloud Logging.

    Honors explicit `TELEMETRY_CLOUD_LOGGING_ENABLED` overrides, disables live network calls
    automatically under `unittest`/`pytest` unless explicitly opted in, and requires `GOOGLE_CLOUD_PROJECT`.
    """
    default_enabled = not ("unittest" in sys.modules or "pytest" in sys.modules)
    return parse_bool_env("TELEMETRY_CLOUD_LOGGING_ENABLED", default=default_enabled) and bool(
        get_google_cloud_project()
    )


def get_bucket_name() -> str:
    """Returns the configured `GCS_BUCKET_NAME`, or an empty string in local-only disk mode."""
    return os.getenv("GCS_BUCKET_NAME", "").strip()


def get_local_chroma_root() -> str:
    """Returns the optional `LOCAL_CHROMA_ROOT` directory override (or empty string if unset)."""
    return os.getenv("LOCAL_CHROMA_ROOT", "").strip()


def get_upload_tmp_root() -> str:
    """Returns the temporary PDF upload root directory (`UPLOAD_TMP_ROOT`, defaulting to `'/tmp/uploads'`)."""
    return os.getenv("UPLOAD_TMP_ROOT", "/tmp/uploads").strip() or "/tmp/uploads"


def get_chroma_lock_ttl_seconds() -> float:
    """Returns the GCS distributed write lock lease TTL in seconds (`CHROMA_LOCK_TTL_SECONDS`, default `300.0`)."""
    return _parse_positive_float("CHROMA_LOCK_TTL_SECONDS", 300.0)


def get_chroma_lock_timeout_seconds() -> float:
    """Returns the GCS distributed write lock acquisition timeout in seconds (`CHROMA_LOCK_TIMEOUT_SECONDS`, default `60.0`)."""
    return _parse_positive_float("CHROMA_LOCK_TIMEOUT_SECONDS", 60.0)


def is_archive_uploads_enabled() -> bool:
    """Returns True when uploaded PDFs should be archived to GCS (`ARCHIVE_UPLOADS_TO_GCS`, default `True`)."""
    return parse_bool_env("ARCHIVE_UPLOADS_TO_GCS", default=True)


def is_agent_console_trace_enabled() -> bool:
    """Returns True when `AGENT_CONSOLE_TRACE` enables Rich console trace panels during local agent runs."""
    return parse_bool_env("AGENT_CONSOLE_TRACE", default=False)


def get_allowed_origins() -> List[str]:
    """Parses `ALLOWED_ORIGINS` from the environment for FastAPI CORS middleware.

    Defaults to standard localhost React/Vite development ports when running locally,
    and fails closed (`[]`) inside Cloud Run if `ALLOWED_ORIGINS` is omitted.

    Returns:
        List of normalized origin URLs (with trailing slashes stripped) or `["*"]`.
    """
    raw = os.getenv("ALLOWED_ORIGINS", "").strip()
    if raw:
        origins: List[str] = []
        for item in raw.split(","):
            cleaned = item.strip()
            if not cleaned:
                continue
            origins.append("*" if cleaned == "*" else cleaned.rstrip("/"))
        return origins

    if is_cloud_run():
        return []

    return [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def get_allowed_llm_models() -> Tuple[str, ...]:
    """Returns the server-approved tuple of Vertex AI generative LLM model names.

    Returns:
        Ordered tuple of model names parsed from `ALLOWED_LLM_MODELS`.

    Raises:
        ValueError: If `ALLOWED_LLM_MODELS` is missing or empty.
    """
    return _parse_required_csv_models("ALLOWED_LLM_MODELS")


def get_allowed_embedding_models() -> Tuple[str, ...]:
    """Returns the server-approved tuple of Vertex AI embedding model names.

    Returns:
        Ordered tuple of embedding model names parsed from `ALLOWED_EMBEDDING_MODELS`.

    Raises:
        ValueError: If `ALLOWED_EMBEDDING_MODELS` is missing or empty.
    """
    return _parse_required_csv_models("ALLOWED_EMBEDDING_MODELS")


def get_default_llm_model() -> str:
    """Returns the primary approved generative LLM model (first entry in `ALLOWED_LLM_MODELS`)."""
    return get_allowed_llm_models()[0]


def get_default_embedding_model() -> str:
    """Returns the primary approved embedding model (first entry in `ALLOWED_EMBEDDING_MODELS`)."""
    return get_allowed_embedding_models()[0]


def get_max_query_chars() -> int:
    """Returns the maximum character length permitted for search and agent queries."""
    return _parse_positive_int("MAX_QUERY_CHARS", 4000)


def get_agent_recursion_limit() -> int:
    """Returns the LangGraph `recursion_limit` ceiling for agent tool-call cycles."""
    return _parse_positive_int("AGENT_RECURSION_LIMIT", 6)


def get_max_upload_mb() -> int:
    """Returns the maximum file size (in MiB) allowed for a single uploaded PDF."""
    return _parse_positive_int("MAX_UPLOAD_MB", 20)


def get_max_files_per_request() -> int:
    """Returns the maximum number of PDF files accepted in one ingestion request."""
    return _parse_positive_int("MAX_FILES_PER_REQUEST", 10)


def get_max_chunks_per_ingest() -> int:
    """Returns the maximum total document chunks permitted per ingestion request."""
    return _parse_positive_int("MAX_CHUNKS_PER_INGEST", 1000)


def get_gemini_safety_threshold() -> str:
    """Returns the configured Vertex AI Gemini `HarmBlockThreshold` name."""
    return os.getenv("GEMINI_SAFETY_THRESHOLD", "BLOCK_LOW_AND_ABOVE").strip().upper() or "BLOCK_LOW_AND_ABOVE"


def get_google_cloud_location() -> str:
    """Returns the unified single-region GCP location (`GOOGLE_CLOUD_LOCATION`, defaulting to `'us-central1'`)."""
    return os.getenv("GOOGLE_CLOUD_LOCATION", "").strip() or "us-central1"


def get_model_armor_template_id() -> str:
    """Returns the configured Google Cloud Model Armor template resource ID (or empty string if unset)."""
    return os.getenv("MODEL_ARMOR_TEMPLATE_ID", "").strip()


def get_model_armor_location() -> str:
    """Returns the regional GCP location hosting the Model Armor template (defaults to `GOOGLE_CLOUD_LOCATION`)."""
    return os.getenv("MODEL_ARMOR_LOCATION", "").strip() or get_google_cloud_location()


def is_model_armor_fail_closed() -> bool:
    """Returns True when missing or unavailable Model Armor / Cloud DLP screening must fail closed with HTTP 503."""
    return parse_bool_env("MODEL_ARMOR_FAIL_CLOSED", default=False)


def get_rate_limit_ingest_per_minute() -> int:
    """Returns the maximum PDF ingestion requests permitted per user per 60-second sliding window."""
    return _parse_positive_int("RATE_LIMIT_INGEST_PER_MINUTE", 10)


def get_rate_limit_query_per_minute() -> int:
    """Returns the maximum search or agent query requests permitted per user per 60-second sliding window."""
    return _parse_positive_int("RATE_LIMIT_QUERY_PER_MINUTE", 30)


def get_max_collections_per_user() -> int:
    """Returns the maximum number of distinct ChromaDB collections an authenticated user may create."""
    return _parse_positive_int("MAX_COLLECTIONS_PER_USER", 20)


def get_chroma_tmp_max_bytes() -> int:
    """Returns the maximum total bytes permitted in `/tmp/chroma_db` before LRU tenant eviction runs."""
    return _parse_non_negative_int("CHROMA_TMP_MAX_BYTES", 512 * 1024 * 1024)


def get_chroma_tmp_min_free_bytes() -> int:
    """Returns the minimum free bytes required on the `/tmp` RAM disk before LRU tenant eviction runs."""
    return _parse_non_negative_int("CHROMA_TMP_MIN_FREE_BYTES", 256 * 1024 * 1024)


# Module-level snapshots (for direct constant imports; runtime validators call get_* helpers)
GOOGLE_CLOUD_LOCATION: str = get_google_cloud_location()
MAX_QUERY_CHARS: int = get_max_query_chars()
AGENT_RECURSION_LIMIT: int = get_agent_recursion_limit()
MAX_UPLOAD_MB: int = get_max_upload_mb()
MAX_FILES_PER_REQUEST: int = get_max_files_per_request()
MAX_CHUNKS_PER_INGEST: int = get_max_chunks_per_ingest()
GEMINI_SAFETY_THRESHOLD: str = get_gemini_safety_threshold()
MODEL_ARMOR_TEMPLATE_ID: str = get_model_armor_template_id()
MODEL_ARMOR_LOCATION: str = get_model_armor_location()
MODEL_ARMOR_FAIL_CLOSED: bool = is_model_armor_fail_closed()
RATE_LIMIT_INGEST_PER_MINUTE: int = get_rate_limit_ingest_per_minute()
RATE_LIMIT_QUERY_PER_MINUTE: int = get_rate_limit_query_per_minute()
MAX_COLLECTIONS_PER_USER: int = get_max_collections_per_user()
CHROMA_TMP_MAX_BYTES: int = get_chroma_tmp_max_bytes()
CHROMA_TMP_MIN_FREE_BYTES: int = get_chroma_tmp_min_free_bytes()


def is_reload_enabled() -> bool:
    """Returns True when `UVICORN_RELOAD` hot-reloading is enabled in the environment."""
    return parse_bool_env("UVICORN_RELOAD", default=False)


__all__ = [
    "AGENT_PORT",
    "AGENT_RECURSION_LIMIT",
    "CHROMA_TMP_MAX_BYTES",
    "CHROMA_TMP_MIN_FREE_BYTES",
    "COLLECTION_NAME_PATTERN",
    "DEFAULT_AUDIT_LOG_NAME",
    "DEFAULT_TELEMETRY_LOG_NAME",
    "FALSE_ENV_VALUES",
    "GCP_REGION_PATTERN",
    "GEMINI_SAFETY_THRESHOLD",
    "GOOGLE_CLOUD_LOCATION",
    "INGEST_PORT",
    "MAX_CHUNKS_PER_INGEST",
    "MAX_COLLECTIONS_PER_USER",
    "MAX_FILES_PER_REQUEST",
    "MAX_QUERY_CHARS",
    "MAX_UPLOAD_MB",
    "MODEL_ARMOR_FAIL_CLOSED",
    "MODEL_ARMOR_LOCATION",
    "MODEL_ARMOR_TEMPLATE_ID",
    "RATE_LIMIT_INGEST_PER_MINUTE",
    "RATE_LIMIT_QUERY_PER_MINUTE",
    "SRC_DIR",
    "TRUE_ENV_VALUES",
    "get_agent_recursion_limit",
    "get_allowed_embedding_models",
    "get_allowed_llm_models",
    "get_allowed_origins",
    "get_audit_log_name",
    "get_bucket_name",
    "get_chroma_lock_timeout_seconds",
    "get_chroma_lock_ttl_seconds",
    "get_chroma_tmp_max_bytes",
    "get_chroma_tmp_min_free_bytes",
    "get_cloud_run_revision",
    "get_cloud_run_service",
    "get_default_embedding_model",
    "get_default_llm_model",
    "get_dev_user_id",
    "get_firebase_project_id",
    "get_gemini_safety_threshold",
    "get_google_cloud_location",
    "get_google_cloud_project",
    "get_local_chroma_root",
    "get_max_chunks_per_ingest",
    "get_max_collections_per_user",
    "get_max_files_per_request",
    "get_max_query_chars",
    "get_max_upload_mb",
    "get_model_armor_location",
    "get_model_armor_template_id",
    "get_rate_limit_ingest_per_minute",
    "get_rate_limit_query_per_minute",
    "get_service_port",
    "get_telemetry_log_name",
    "get_upload_tmp_root",
    "is_agent_console_trace_enabled",
    "is_archive_uploads_enabled",
    "is_audit_cloud_logging_enabled",
    "is_auth_disabled",
    "is_cloud_run",
    "is_cloud_trace_enabled",
    "is_model_armor_fail_closed",
    "is_reload_enabled",
    "is_structured_logging_enabled",
    "is_telemetry_cloud_logging_enabled",
    "load_env",
    "parse_bool_env",
]

