"""Document Ingestion service package (`src.ingest`).

Submodules:
- `app`: Dedicated FastAPI `ingest-api` service exposing `POST /ingest` and `GET /health`.
- `routes`: `ingest_router` hosting `POST /ingest`.
- `schemas`: `IngestRequest` Pydantic request validation model.
- `splitter`: Pure PDF filename sanitization (`sanitize_filename`) and text chunking (`split_pdfs`).
- `upload_parser`: Multipart/JSON upload staging, `%PDF-` verification, and ingestion ceiling enforcement.
- `service`: ChromaDB batch indexing orchestration (`ingest_documents`) inside `user_write_lock`.
"""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.ingest.app import main
    from src.ingest.routes import ingest, ingest_router
    from src.ingest.schemas import IngestRequest
    from src.ingest.service import ingest_documents
    from src.ingest.splitter import sanitize_filename, split_pdfs
    from src.ingest.upload_parser import IngestPayload, prepare_payload

_EXPORT_MAP: dict[str, str] = {
    "IngestPayload": "src.ingest.upload_parser",
    "IngestRequest": "src.ingest.schemas",
    "ingest": "src.ingest.routes",
    "ingest_documents": "src.ingest.service",
    "ingest_router": "src.ingest.routes",
    "main": "src.ingest.app",
    "prepare_payload": "src.ingest.upload_parser",
    "sanitize_filename": "src.ingest.splitter",
    "split_pdfs": "src.ingest.splitter",
}

__all__ = sorted(_EXPORT_MAP.keys())


def __getattr__(name: str) -> Any:
    """Lazily resolves public package exports without eager cross-package import cycles."""
    module_path = _EXPORT_MAP.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_path)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Lists available attributes on `src.ingest` for interactive introspection."""
    return sorted(set(globals().keys()) | set(__all__))
