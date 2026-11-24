"""Multi-tenant vector, FTS5 lexical, and blob persistence package (`src.core.storage`).

Organizes per-user ChromaDB, SQLite FTS5, and Google Cloud Storage persistence into modular layers:
- `paths`: Tenant directory isolation (`users/{uid}/{version.json, uploads/, chroma/, fts/}`), Chroma cache eviction, and SQLite WAL helpers.
- `fts`: Persisted per-collection SQLite FTS5 inverted index (`fts/<collection_name>.sqlite3`) for sub-linear BM25 retrieval.
- `gcs_sync`: GCS client/bucket lookup, atomic snapshot pull/push (`chroma/` + `fts/` + `version.json`), incremental MD5 delta sync, and PDF upload archiving.
- `locks`: Per-user thread RLocks, distributed `if_generation_match` GCS locks, and `/tmp` LRU cache eviction.
- `chroma`: Regional Vertex AI embeddings, ChromaDB collection operations, and hybrid dense + FTS5/BM25 RRF search.
"""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.core.storage.chroma import (
        get_collection_embedding_model,
        get_embeddings,
        get_vectorstore,
        has_sqlite,
        list_collections,
        remove_collection,
        require_collection,
        resolve_collection_embedding_model,
        similarity_search_with_scores,
    )
    from src.core.storage.fts import (
        FTS_TABLE_NAME,
        add_documents_to_fts,
        fts_index_path,
        has_fts_index,
        remove_fts_index,
        search_fts,
        tokenize_lexical,
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
        CHROMA_SUBDIR,
        FTS_SUBDIR,
        SAFE_FILENAME_RE,
        SQLITE_FILENAME,
        SYNCED_SUBDIRS,
        UPLOADS_SUBDIR,
        VERSION_FILENAME,
        checkpoint_sqlite_wal,
        directory_size_bytes,
        get_bucket_name,
        get_chroma_dir,
        get_chroma_root,
        get_fts_dir,
        get_upload_user_dir,
        get_user_dir,
        get_user_uploads_dir,
        read_local_workspace_id,
        release_client_cache,
        user_chroma_gcs_prefix,
        user_fts_gcs_prefix,
        user_gcs_prefix,
        user_uploads_gcs_prefix,
    )

_EXPORT_MAP: dict[str, str] = {
    "get_collection_embedding_model": "src.core.storage.chroma",
    "get_embeddings": "src.core.storage.chroma",
    "get_vectorstore": "src.core.storage.chroma",
    "has_sqlite": "src.core.storage.chroma",
    "list_collections": "src.core.storage.chroma",
    "remove_collection": "src.core.storage.chroma",
    "require_collection": "src.core.storage.chroma",
    "resolve_collection_embedding_model": "src.core.storage.chroma",
    "similarity_search_with_scores": "src.core.storage.chroma",
    "FTS_TABLE_NAME": "src.core.storage.fts",
    "add_documents_to_fts": "src.core.storage.fts",
    "fts_index_path": "src.core.storage.fts",
    "has_fts_index": "src.core.storage.fts",
    "remove_fts_index": "src.core.storage.fts",
    "search_fts": "src.core.storage.fts",
    "tokenize_lexical": "src.core.storage.fts",
    "archive_upload": "src.core.storage.gcs_sync",
    "LockTimeoutError": "src.core.storage.locks",
    "evict_tmp_cache": "src.core.storage.locks",
    "sync_user_dir": "src.core.storage.locks",
    "user_read_lock": "src.core.storage.locks",
    "user_write_lock": "src.core.storage.locks",
    "CHROMA_SUBDIR": "src.core.storage.paths",
    "FTS_SUBDIR": "src.core.storage.paths",
    "SAFE_FILENAME_RE": "src.core.storage.paths",
    "SQLITE_FILENAME": "src.core.storage.paths",
    "SYNCED_SUBDIRS": "src.core.storage.paths",
    "UPLOADS_SUBDIR": "src.core.storage.paths",
    "VERSION_FILENAME": "src.core.storage.paths",
    "checkpoint_sqlite_wal": "src.core.storage.paths",
    "directory_size_bytes": "src.core.storage.paths",
    "get_bucket_name": "src.core.storage.paths",
    "get_chroma_dir": "src.core.storage.paths",
    "get_chroma_root": "src.core.storage.paths",
    "get_fts_dir": "src.core.storage.paths",
    "get_upload_user_dir": "src.core.storage.paths",
    "get_user_dir": "src.core.storage.paths",
    "get_user_uploads_dir": "src.core.storage.paths",
    "read_local_workspace_id": "src.core.storage.paths",
    "release_client_cache": "src.core.storage.paths",
    "user_chroma_gcs_prefix": "src.core.storage.paths",
    "user_fts_gcs_prefix": "src.core.storage.paths",
    "user_gcs_prefix": "src.core.storage.paths",
    "user_uploads_gcs_prefix": "src.core.storage.paths",
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
