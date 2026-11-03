"""Tenant collection management and GDPR DSAR control-plane package (`src.manage`).

Provides lightweight control-plane endpoints mounted on `agent-api`:
- `collections`: `collections_router` (`GET /collections` and `DELETE /collections/{collection_name}`).
- `compliance`: `compliance_router` (`GET /compliance/my-data` and `DELETE /compliance/my-data`).
- `routes`: `manage_router` combining `collections_router` and `compliance_router`.
"""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.manage.collections import collections_router, delete_collection, get_collections
    from src.manage.compliance import (
        compliance_router,
        erase_my_data,
        export_my_data_inventory,
    )
    from src.manage.routes import manage_router

_EXPORT_MAP: dict[str, str] = {
    "collections_router": "src.manage.collections",
    "delete_collection": "src.manage.collections",
    "get_collections": "src.manage.collections",
    "compliance_router": "src.manage.compliance",
    "erase_my_data": "src.manage.compliance",
    "export_my_data_inventory": "src.manage.compliance",
    "manage_router": "src.manage.routes",
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
    """Lists available attributes on `src.manage` for interactive introspection."""
    return sorted(set(globals().keys()) | set(__all__))
