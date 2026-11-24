"""Per-tenant filesystem path isolation, Chroma handle management, and SQLite utilities.

Provides strict directory containment (`os.path.commonpath`) for `/tmp/chroma_db/{uid}`
and `/tmp/uploads/{uid}`, ChromaDB `SharedSystemClient` cache eviction before/after GCS
snapshots, and SQLite WAL checkpoint helpers.
"""

import json
import os
import re
import sqlite3
from typing import Optional

from src.core.auth import validate_uid
from src.core.config import (
    get_bucket_name,
    get_local_chroma_root,
    get_upload_tmp_root,
    is_cloud_run,
)
from src.core.logger import setup_logging

logger = setup_logging(name="storage.paths")

VERSION_FILENAME = "version.json"
SQLITE_FILENAME = "chroma.sqlite3"
CHROMA_SUBDIR = "chroma"
FTS_SUBDIR = "fts"
UPLOADS_SUBDIR = "uploads"
SYNCED_SUBDIRS = (CHROMA_SUBDIR, FTS_SUBDIR)
TRANSIENT_SQLITE_SUFFIXES = ("-wal", "-shm", "-journal")
SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def get_chroma_root() -> str:
    """Returns the root directory where per-user working directories are stored.

    Resolves `LOCAL_CHROMA_ROOT` when explicitly set, `/tmp/chroma_db` inside Cloud Run,
    or `./chroma_db/users` during local development.

    Returns:
        Absolute filesystem path to the multi-tenant root directory.
    """
    configured = get_local_chroma_root()
    if configured:
        return os.path.abspath(configured)
    if is_cloud_run():
        return "/tmp/chroma_db"
    return os.path.abspath(os.path.join("chroma_db", "users"))


def get_user_dir(uid: str) -> str:
    """Returns the validated, isolated local root directory path for a specific user.

    Contains `version.json`, `chroma/` (ChromaDB SQLite + HNSW segments), `fts/`
    (per-collection SQLite FTS5 indexes), and (in local mode) `uploads/`.

    Args:
        uid: Verified tenant user identifier.

    Returns:
        Absolute path `<root>/<uid>` verified via `os.path.commonpath`.

    Raises:
        HTTPException: HTTP 401 if `uid` fails `validate_uid`.
        ValueError: If the resolved directory escapes `get_chroma_root()`.
    """
    safe_uid = validate_uid(uid)
    root = get_chroma_root()
    user_dir = os.path.abspath(os.path.join(root, safe_uid))
    if os.path.commonpath([root, user_dir]) != root or user_dir == root:
        raise ValueError(f"Unsafe resolved Chroma directory for uid={safe_uid!r}")
    return user_dir


def get_chroma_dir(user_dir: str) -> str:
    """Returns the ChromaDB subdirectory (`<user_dir>/chroma`) inside a tenant's working directory."""
    return os.path.join(user_dir, CHROMA_SUBDIR)


def get_fts_dir(user_dir: str) -> str:
    """Returns the SQLite FTS5 subdirectory (`<user_dir>/fts`) inside a tenant's working directory."""
    return os.path.join(user_dir, FTS_SUBDIR)


def get_user_uploads_dir(user_dir: str) -> str:
    """Returns the archived uploads subdirectory (`<user_dir>/uploads`) inside a tenant's directory."""
    return os.path.join(user_dir, UPLOADS_SUBDIR)


def get_upload_user_dir(uid: str) -> str:
    """Returns the validated, isolated ephemeral PDF upload staging directory for a specific user.

    Args:
        uid: Verified tenant user identifier.

    Returns:
        Absolute path `<upload_root>/<uid>` verified via `os.path.commonpath`.

    Raises:
        HTTPException: HTTP 401 if `uid` fails `validate_uid`.
        ValueError: If the resolved upload directory escapes `UPLOAD_TMP_ROOT`.
    """
    safe_uid = validate_uid(uid)
    upload_root = os.path.abspath(get_upload_tmp_root())
    user_upload_dir = os.path.abspath(os.path.join(upload_root, safe_uid))
    if (
        os.path.commonpath([upload_root, user_upload_dir]) != upload_root
        or user_upload_dir == upload_root
    ):
        raise ValueError(f"Unsafe resolved upload directory for uid={safe_uid!r}")
    return user_upload_dir


def user_gcs_prefix(uid: str) -> str:
    """Returns the canonical root GCS prefix (`users/{uid}/`) for a user's persisted files.

    Args:
        uid: Verified path-safe user identifier.

    Returns:
        Trailing-slash root GCS prefix string for `uid`.
    """
    return f"users/{uid}/"


def user_chroma_gcs_prefix(uid: str) -> str:
    """Returns the canonical GCS object prefix (`users/{uid}/chroma/`) for a user's ChromaDB files.

    Args:
        uid: Verified path-safe user identifier.

    Returns:
        Trailing-slash GCS prefix string for `uid`'s ChromaDB files.
    """
    return f"users/{uid}/{CHROMA_SUBDIR}/"


def user_fts_gcs_prefix(uid: str) -> str:
    """Returns the canonical GCS object prefix (`users/{uid}/fts/`) for a user's FTS5 indexes.

    Args:
        uid: Verified path-safe user identifier.

    Returns:
        Trailing-slash GCS prefix string for `uid`'s FTS5 inverted index files.
    """
    return f"users/{uid}/{FTS_SUBDIR}/"


def user_uploads_gcs_prefix(uid: str) -> str:
    """Returns the canonical GCS object prefix (`users/{uid}/uploads/`) for a user's archived PDFs.

    Args:
        uid: Verified path-safe user identifier.

    Returns:
        Trailing-slash GCS prefix string for `uid`'s archived uploads.
    """
    return f"users/{uid}/{UPLOADS_SUBDIR}/"


def user_lock_gcs_blob_name(uid: str) -> str:
    """Returns the canonical GCS object name (`users/{uid}/locks/write.lock`) for tenant write locking.

    Args:
        uid: Verified path-safe user identifier.

    Returns:
        GCS blob path used for atomic `if_generation_match` distributed write locks.
    """
    return f"users/{uid}/locks/write.lock"


def release_client_cache(persist_directory: str) -> None:
    """Stops and evicts ChromaDB's cached `System` instance(s) at or inside `persist_directory`.

    ChromaDB's `PersistentClient` caches `RustBindingsAPI`/`SegmentAPI` handles per directory
    path (`<user_dir>/chroma`). Releasing matching handles is mandatory before replacing local
    SQLite/HNSW files on GCS refresh and before snapshotting SQLite files back to GCS.

    Args:
        persist_directory: Local filesystem path of the tenant directory or its `chroma/` subdirectory.
    """
    try:
        from chromadb.api.client import SharedSystemClient
    except ImportError:
        return

    target_abs = os.path.abspath(persist_directory)
    candidates = {persist_directory, target_abs}
    for key in list(getattr(SharedSystemClient, "_identifier_to_system", {}).keys()):
        key_str = str(key)
        key_abs = os.path.abspath(key_str)
        is_match = key in candidates or key_abs in candidates
        if not is_match:
            try:
                is_match = os.path.commonpath([target_abs, key_abs]) == target_abs
            except ValueError:
                is_match = False
        if is_match:
            system = SharedSystemClient._identifier_to_system.pop(key, None)
            SharedSystemClient._identifier_to_refcount.pop(key, None)
            if system is not None:
                try:
                    system.stop()
                except Exception as e:
                    logger.warning(f"Error stopping cached Chroma system for '{key}': {e}")


def checkpoint_sqlite_wal(user_dir: str) -> None:
    """Folds any SQLite Write-Ahead Log frames cleanly into `chroma/chroma.sqlite3` before GCS upload.

    Args:
        user_dir: Local tenant working directory containing `chroma/chroma.sqlite3`.
    """
    sqlite_path = os.path.join(get_chroma_dir(user_dir), SQLITE_FILENAME)
    if not os.path.exists(sqlite_path):
        return
    try:
        conn = sqlite3.connect(sqlite_path, timeout=10.0)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"SQLite WAL checkpoint warning for '{sqlite_path}': {e}")


def directory_size_bytes(path: str) -> int:
    """Computes total disk usage in bytes across all files nested inside `path`.

    Args:
        path: Root directory path to measure.

    Returns:
        Sum of file sizes in bytes (`0` if `path` does not exist).
    """
    total = 0
    if not os.path.exists(path):
        return 0
    for dirpath, _, filenames in os.walk(path):
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            try:
                total += os.path.getsize(fpath)
            except OSError:
                pass
    return total


def read_local_version(user_dir: str) -> Optional[str]:
    """Reads the local snapshot UUID from `<user_dir>/version.json` when `chroma/chroma.sqlite3` is present.

    Args:
        user_dir: Local tenant working directory.

    Returns:
        Version UUID string if both `version.json` and `chroma/chroma.sqlite3` exist, otherwise `None`.
    """
    version_path = os.path.join(user_dir, VERSION_FILENAME)
    sqlite_path = os.path.join(get_chroma_dir(user_dir), SQLITE_FILENAME)
    if not os.path.exists(version_path) or not os.path.exists(sqlite_path):
        return None
    try:
        with open(version_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("version")
    except Exception:
        return None


__all__ = [
    "CHROMA_SUBDIR",
    "FTS_SUBDIR",
    "SAFE_FILENAME_RE",
    "SQLITE_FILENAME",
    "SYNCED_SUBDIRS",
    "TRANSIENT_SQLITE_SUFFIXES",
    "UPLOADS_SUBDIR",
    "VERSION_FILENAME",
    "checkpoint_sqlite_wal",
    "directory_size_bytes",
    "get_bucket_name",
    "get_chroma_dir",
    "get_chroma_root",
    "get_fts_dir",
    "get_upload_user_dir",
    "get_user_dir",
    "get_user_uploads_dir",
    "read_local_version",
    "release_client_cache",
    "user_chroma_gcs_prefix",
    "user_fts_gcs_prefix",
    "user_gcs_prefix",
    "user_lock_gcs_blob_name",
    "user_uploads_gcs_prefix",
]
