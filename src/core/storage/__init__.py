"""Multi-tenant vector and blob persistence package (`src.core.storage`).

Organizes per-user ChromaDB and Google Cloud Storage persistence into modular layers:
- `paths`: Tenant directory isolation (`/tmp/chroma_db/{uid}`, `/tmp/uploads/{uid}`), Chroma cache eviction, and SQLite WAL helpers.
- `gcs_sync`: GCS client/bucket lookup, atomic snapshot pull/push, and PDF upload archiving.
- `locks`: Per-user thread RLocks, distributed `if_generation_match` GCS locks, and `/tmp` LRU cache eviction.
- `chroma`: Regional Vertex AI embeddings and ChromaDB collection operations.
- `routes`: Shared FastAPI `/collections` router.
"""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.core.storage.chroma import (
        get_embeddings,
        get_vectorstore,
        has_sqlite,
        list_collections,
        remove_collection,
        require_collection,
    )
    from src.core.storage.gcs_sync import archive_upload
    from src.core.storage.locks import (
        LockTimeoutError,
        evict_tmp_cache,
        sync_user_dir,
        user_read_lock,
        user_write_lock,
    )
    from src.core.storage.paths import (
        SQLITE_FILENAME,
        VERSION_FILENAME,
        checkpoint_sqlite_wal,
        directory_size_bytes,
        get_bucket_name,
        get_chroma_root,
        get_upload_user_dir,
        get_user_dir,
        release_client_cache,
    )

_EXPORT_MAP: dict[str, str] = {
    "get_embeddings": "src.core.storage.chroma",
    "get_vectorstore": "src.core.storage.chroma",
    "has_sqlite": "src.core.storage.chroma",
    "list_collections": "src.core.storage.chroma",
    "remove_collection": "src.core.storage.chroma",
    "require_collection": "src.core.storage.chroma",
    "archive_upload": "src.core.storage.gcs_sync",
    "LockTimeoutError": "src.core.storage.locks",
    "evict_tmp_cache": "src.core.storage.locks",
    "sync_user_dir": "src.core.storage.locks",
    "user_read_lock": "src.core.storage.locks",
    "user_write_lock": "src.core.storage.locks",
    "SQLITE_FILENAME": "src.core.storage.paths",
    "VERSION_FILENAME": "src.core.storage.paths",
    "checkpoint_sqlite_wal": "src.core.storage.paths",
    "directory_size_bytes": "src.core.storage.paths",
    "get_bucket_name": "src.core.storage.paths",
    "get_chroma_root": "src.core.storage.paths",
    "get_upload_user_dir": "src.core.storage.paths",
    "get_user_dir": "src.core.storage.paths",
    "release_client_cache": "src.core.storage.paths",
}

__all__ = sorted(_EXPORT_MAP.keys())


def __getattr__(name: str) -> Any:
    """Lazily resolves public package exports without eager cross-package import cycles.

    Args:
        name: Exported attribute name requested on `src.core.storage`.

    Returns:
        The resolved attribute from its defining submodule.

    Raises:
        AttributeError: If `name` is not part of the package public API.
    """
    module_path = _EXPORT_MAP.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_path)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Lists available attributes on `src.core.storage` for interactive introspection."""
    return sorted(set(globals().keys()) | set(__all__))
