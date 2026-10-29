"""Centralized environment configuration and runtime settings for InteractiveRAG.

Loads `.env` files once on startup and exposes typed, validated runtime getters for
approved Vertex AI model catalogs, regional GCP location pinning, Cloud Run runtime
detection, CORS allowlists, upload/query ceilings, and Model Armor configuration.
"""

import os
import re
from typing import List, Tuple

from dotenv import find_dotenv, load_dotenv

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env() -> None:
    """Loads environment variables from repository root `.env` and `src/.env` if present."""
    load_dotenv(find_dotenv())
    load_dotenv(os.path.join(SRC_DIR, ".env"))


# Load environment variables once on core config import before deriving defaults
load_env()

INGESTION_PORT = 8000
AGENT_PORT = 8001

# Strict collection name pattern preventing prompt/schema smuggling via collection labels
COLLECTION_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Concrete single-region GCP identifier pattern (rejects 'global' and multi-regions like 'us'/'eu')
GCP_REGION_PATTERN = re.compile(r"^[a-z]+-[a-z]+[0-9]+$")


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


def is_cloud_run() -> bool:
    """Checks whether the process is executing inside a Google Cloud Run container.

    Returns:
        True when Cloud Run's standard `K_SERVICE` environment variable is populated.
    """
    return bool(os.getenv("K_SERVICE", "").strip())


def is_structured_logging_enabled() -> bool:
    """Returns True when application logs should be emitted as single-line GCP JSON on stdout.

    Honors explicit `STRUCTURED_LOGGING` (`true`/`false`), and defaults to `True` on
    Cloud Run (`K_SERVICE` set) and `False` during local terminal development.
    """
    raw = os.getenv("STRUCTURED_LOGGING", "").strip().lower()
    if raw in {"1", "true", "yes"}:
        return True
    if raw in {"0", "false", "no"}:
        return False
    return is_cloud_run()


def is_cloud_trace_enabled() -> bool:
    """Returns True when completed spans should be exported to Google Cloud Trace.

    Honors explicit `CLOUD_TRACE_ENABLED` (`true`/`false`), and defaults to `True` on
    Cloud Run (`K_SERVICE` set) and `False` during local development and offline tests.
    """
    raw = os.getenv("CLOUD_TRACE_ENABLED", "").strip().lower()
    if raw in {"1", "true", "yes"}:
        return True
    if raw in {"0", "false", "no"}:
        return False
    return is_cloud_run()


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
    return os.getenv("MODEL_ARMOR_FAIL_CLOSED", "false").strip().lower() in {"1", "true", "yes"}


def get_rate_limit_ingest_per_minute() -> int:
    """Returns the maximum PDF ingestion requests permitted per user per 60-second sliding window."""
    return _parse_positive_int("RATE_LIMIT_INGEST_PER_MINUTE", 10)


def get_rate_limit_query_per_minute() -> int:
    """Returns the maximum search or agent query requests permitted per user per 60-second sliding window."""
    return _parse_positive_int("RATE_LIMIT_QUERY_PER_MINUTE", 30)


def get_max_collections_per_user() -> int:
    """Returns the maximum number of distinct ChromaDB collections an authenticated user may create."""
    return _parse_positive_int("MAX_COLLECTIONS_PER_USER", 20)


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


def is_reload_enabled() -> bool:
    """Returns True when `UVICORN_RELOAD` hot-reloading is enabled in the environment."""
    return os.getenv("UVICORN_RELOAD", "").strip().lower() in {"1", "true", "yes"}
