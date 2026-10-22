import os

from dotenv import find_dotenv, load_dotenv

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_COLLECTION = "stock_market_vs"
DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"
DEFAULT_LLM_MODEL = "gemini-3.8-flash"
DEFAULT_LOCATION = "global"

INGESTION_PORT = 8000
AGENT_PORT = 8001


def load_env() -> None:
    """Loads environment variables from root .env and src/.env files if present."""
    load_dotenv(find_dotenv())
    load_dotenv(os.path.join(SRC_DIR, ".env"))


def is_reload_enabled() -> bool:
    """Returns True when UVICORN_RELOAD is enabled in the environment."""
    return os.getenv("UVICORN_RELOAD", "").strip().lower() in {"1", "true", "yes"}


# Load environment variables once on core config import
load_env()
