"""Vectorstore similarity search package (`src.query`).

Submodules:
- `schemas`: `QueryRequest` Pydantic model with query length, `k`, and model allowlist guardrails.
- `service`: `search_collection` top-`k` ChromaDB search with Cloud DLP / SDP chunk masking.
- `routes`: `query_router` exposing `POST /query` mounted on `agent-api`.
"""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.query.routes import query, query_router
    from src.query.schemas import QueryRequest
    from src.query.service import search_collection

_EXPORT_MAP: dict[str, str] = {
    "QueryRequest": "src.query.schemas",
    "query": "src.query.routes",
    "query_router": "src.query.routes",
    "search_collection": "src.query.service",
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
    """Lists available attributes on `src.query` for interactive introspection."""
    return sorted(set(globals().keys()) | set(__all__))
