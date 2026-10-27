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
from src.core.config import is_cloud_run
from src.core.logger import setup_logging

logger = setup_logging(name="storage.paths")

VERSION_FILENAME = "version.json"
SQLITE_FILENAME = "chroma.sqlite3"
TRANSIENT_SQLITE_SUFFIXES = ("-wal", "-shm", "-journal")
SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def get_bucket_name() -> str:
    """Returns the configured `GCS_BUCKET_NAME`, or an empty string in local-only disk mode."""
    return os.getenv("GCS_BUCKET_NAME", "").strip()


def get_chroma_root() -> str:
    """Returns the root directory where per-user ChromaDB working directories are stored.

    Resolves `LOCAL_CHROMA_ROOT` when explicitly set, `/tmp/chroma_db` inside Cloud Run,
    or `./chroma_db/users` during local development.

    Returns:
        Absolute filesystem path to the multi-tenant ChromaDB root directory.
    """
    configured = os.getenv("LOCAL_CHROMA_ROOT", "").strip()
    if configured:
        return os.path.abspath(configured)
    if is_cloud_run():
        return "/tmp/chroma_db"
    return os.path.abspath(os.path.join("chroma_db", "users"))


def get_user_dir(uid: str) -> str:
    """Returns the validated, isolated local ChromaDB directory path for a specific user.

    Args:
        uid: Verified tenant user identifier.

    Returns:
        Absolute path `<chroma_root>/<uid>` verified via `os.path.commonpath`.

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


def get_upload_user_dir(uid: str) -> str:
    """Returns the validated, isolated temporary PDF upload directory for a specific user.

    Args:
        uid: Verified tenant user identifier.

    Returns:
        Absolute path `<upload_root>/<uid>` verified via `os.path.commonpath`.

    Raises:
        HTTPException: HTTP 401 if `uid` fails `validate_uid`.
        ValueError: If the resolved upload directory escapes `UPLOAD_TMP_ROOT`.
    """
    safe_uid = validate_uid(uid)
    upload_root = os.path.abspath(os.getenv("UPLOAD_TMP_ROOT", "/tmp/uploads"))
    user_upload_dir = os.path.abspath(os.path.join(upload_root, safe_uid))
    if (
        os.path.commonpath([upload_root, user_upload_dir]) != upload_root
        or user_upload_dir == upload_root
    ):
        raise ValueError(f"Unsafe resolved upload directory for uid={safe_uid!r}")
    return user_upload_dir


def user_chroma_gcs_prefix(uid: str) -> str:
    """Returns the canonical GCS object prefix (`users/{uid}/chroma_db/`) for a user's vector store.

    Args:
        uid: Verified path-safe user identifier.

    Returns:
        Trailing-slash GCS prefix string for `uid`'s ChromaDB files.
    """
    return f"users/{uid}/chroma_db/"


def user_lock_gcs_blob_name(uid: str) -> str:
    """Returns the canonical GCS object name (`users/{uid}/locks/chroma.lock`) for write locking.

    Args:
        uid: Verified path-safe user identifier.

    Returns:
        GCS blob path used for atomic `if_generation_match` distributed write locks.
    """
    return f"users/{uid}/locks/chroma.lock"


def release_client_cache(persist_directory: str) -> None:
    """Stops and evicts ChromaDB's cached `System` instance for `persist_directory`.

    ChromaDB's `PersistentClient` caches `RustBindingsAPI`/`SegmentAPI` handles per directory
    path. Releasing this handle is mandatory before replacing local SQLite/HNSW files on GCS
    refresh and before snapshotting SQLite files back to GCS.

    Args:
        persist_directory: Local filesystem path of the ChromaDB tenant directory.
    """
    try:
        from chromadb.api.client import SharedSystemClient
    except ImportError:
        return

    candidates = {persist_directory, os.path.abspath(persist_directory)}
    for key in list(getattr(SharedSystemClient, "_identifier_to_system", {}).keys()):
        if key in candidates or os.path.abspath(str(key)) in candidates:
            system = SharedSystemClient._identifier_to_system.pop(key, None)
            SharedSystemClient._identifier_to_refcount.pop(key, None)
            if system is not None:
                try:
                    system.stop()
                except Exception as e:
                    logger.warning(f"Error stopping cached Chroma system for '{key}': {e}")


def checkpoint_sqlite_wal(user_dir: str) -> None:
    """Folds any SQLite Write-Ahead Log frames cleanly into `chroma.sqlite3` before GCS upload.

    Args:
        user_dir: Local ChromaDB tenant directory containing `chroma.sqlite3`.
    """
    sqlite_path = os.path.join(user_dir, SQLITE_FILENAME)
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
    """Reads the local snapshot UUID from `<user_dir>/version.json` when SQLite is present.

    Args:
        user_dir: Local ChromaDB tenant directory.

    Returns:
        Version UUID string if both `version.json` and `chroma.sqlite3` exist, otherwise `None`.
    """
    version_path = os.path.join(user_dir, VERSION_FILENAME)
    sqlite_path = os.path.join(user_dir, SQLITE_FILENAME)
    if not os.path.exists(version_path) or not os.path.exists(sqlite_path):
        return None
    try:
        with open(version_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("version")
    except Exception:
        return None


__all__ = [
    "SAFE_FILENAME_RE",
    "SQLITE_FILENAME",
    "TRANSIENT_SQLITE_SUFFIXES",
    "VERSION_FILENAME",
    "checkpoint_sqlite_wal",
    "directory_size_bytes",
    "get_bucket_name",
    "get_chroma_root",
    "get_upload_user_dir",
    "get_user_dir",
    "read_local_version",
    "release_client_cache",
    "user_chroma_gcs_prefix",
    "user_lock_gcs_blob_name",
]
