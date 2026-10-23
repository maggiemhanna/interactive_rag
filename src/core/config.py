import os
import re
from typing import Tuple

from dotenv import find_dotenv, load_dotenv

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env() -> None:
    """Loads environment variables from root .env and src/.env files if present."""
    load_dotenv(find_dotenv())
    load_dotenv(os.path.join(SRC_DIR, ".env"))


# Load environment variables once on core config import before deriving defaults
load_env()

DEFAULT_COLLECTION = "stock_market_vs"
DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"
DEFAULT_LLM_MODEL = "gemini-3.8-flash"
DEFAULT_LOCATION = "global"

INGESTION_PORT = 8000
AGENT_PORT = 8001

# Strict collection name pattern preventing prompt/schema smuggling via collection labels
COLLECTION_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Default approved Vertex AI model catalogs (multiple vetted options per capability)
DEFAULT_ALLOWED_LLM_MODELS: Tuple[str, ...] = (
    DEFAULT_LLM_MODEL,
    "gemini-2.5-flash",
    "gemini-2.5-pro",
)
DEFAULT_ALLOWED_EMBEDDING_MODELS: Tuple[str, ...] = (
    DEFAULT_EMBEDDING_MODEL,
    "text-embedding-005",
)


def _parse_csv_models(env_var: str, fallback: Tuple[str, ...]) -> Tuple[str, ...]:
    raw = os.getenv(env_var, "").strip()
    if not raw:
        return fallback
    parsed = tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))
    return parsed or fallback


def _parse_positive_int(env_var: str, default: int) -> int:
    raw = os.getenv(env_var, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        return value if value > 0 else default
    except ValueError:
        return default


def get_allowed_llm_models() -> Tuple[str, ...]:
    """Returns the server-approved tuple of Vertex AI generative LLM model names."""
    return _parse_csv_models("ALLOWED_LLM_MODELS", DEFAULT_ALLOWED_LLM_MODELS)


def get_allowed_embedding_models() -> Tuple[str, ...]:
    """Returns the server-approved tuple of Vertex AI embedding model names."""
    return _parse_csv_models("ALLOWED_EMBEDDING_MODELS", DEFAULT_ALLOWED_EMBEDDING_MODELS)


def get_max_query_chars() -> int:
    """Returns the maximum character length permitted for search and agent queries."""
    return _parse_positive_int("MAX_QUERY_CHARS", 4000)


def get_agent_recursion_limit() -> int:
    """Returns the LangGraph recursion_limit ceiling for agent tool-call cycles."""
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
    """Returns the configured Gemini HarmBlockThreshold name."""
    return os.getenv("GEMINI_SAFETY_THRESHOLD", "BLOCK_LOW_AND_ABOVE").strip().upper() or "BLOCK_LOW_AND_ABOVE"


def get_model_armor_template_id() -> str:
    """Returns the configured Google Cloud Model Armor template resource ID (or empty if unset)."""
    return os.getenv("MODEL_ARMOR_TEMPLATE_ID", "").strip()


def get_model_armor_location() -> str:
    """Returns the regional GCP location hosting the Model Armor template."""
    return os.getenv("MODEL_ARMOR_LOCATION", "us-central1").strip() or "us-central1"


# Module-level snapshots (for direct constant imports; runtime validators call get_* helpers)
ALLOWED_LLM_MODELS: Tuple[str, ...] = get_allowed_llm_models()
ALLOWED_EMBEDDING_MODELS: Tuple[str, ...] = get_allowed_embedding_models()
MAX_QUERY_CHARS: int = get_max_query_chars()
AGENT_RECURSION_LIMIT: int = get_agent_recursion_limit()
MAX_UPLOAD_MB: int = get_max_upload_mb()
MAX_FILES_PER_REQUEST: int = get_max_files_per_request()
MAX_CHUNKS_PER_INGEST: int = get_max_chunks_per_ingest()
GEMINI_SAFETY_THRESHOLD: str = get_gemini_safety_threshold()
MODEL_ARMOR_TEMPLATE_ID: str = get_model_armor_template_id()
MODEL_ARMOR_LOCATION: str = get_model_armor_location()


def is_reload_enabled() -> bool:
    """Returns True when UVICORN_RELOAD is enabled in the environment."""
    return os.getenv("UVICORN_RELOAD", "").strip().lower() in {"1", "true", "yes"}
