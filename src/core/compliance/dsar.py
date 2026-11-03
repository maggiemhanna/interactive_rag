"""GDPR Art. 15 Data Inventory & Art. 17 Complete Versioned Erasure (Pillar 4: Compliance & Governance).

Implements tenant-scoped Data Subject Access Request (DSAR) storage and memory operations:
- `list_user_memory_threads(uid, checkpointer)`: Enumerates user-scoped `{uid}:*` conversation threads in `checkpointer`.
- `clear_user_memory(uid, checkpointer)`: Purges all `{uid}:*` conversation checkpoints, writes, and blobs from `checkpointer`.
- `get_user_data_inventory(uid, checkpointer)`: Enumerates collections, chunk counts, archived PDF uploads,
  live and noncurrent GCS object generations, disk/cloud byte totals, and active memory thread count.
- `purge_all_user_data(uid, checkpointer)`: Acquires the user's distributed GCS write lock, wipes local
  `/tmp/chroma_db/{uid}` and `/tmp/uploads/{uid}` directories, permanently deletes every live and
  noncurrent object revision (`versions=True`) under `gs://<bucket>/users/{uid}/`, and purges in-memory threads.
"""

import contextlib
from datetime import datetime, timezone
import os
import re
import shutil
import threading
from typing import Any, Dict, List, Optional, Set

from google.api_core import exceptions as gcs_exceptions

from src.core.auth import validate_uid
from src.core.compliance.residency import validate_gcp_region
from src.core.context import get_request_id
from src.core.logger import setup_logging
from src.core.storage import gcs_sync
from src.core.storage.chroma import list_collections
from src.core.storage.locks import (
    acquire_gcs_write_lock,
    clear_user_last_used,
    get_user_thread_lock,
    release_gcs_write_lock,
    track_active_user,
    user_read_lock,
)
from src.core.storage.paths import (
    directory_size_bytes,
    get_bucket_name,
    get_upload_user_dir,
    get_user_dir,
    release_client_cache,
    user_lock_gcs_blob_name,
)

logger = setup_logging(name="compliance.dsar")

_ARCHIVE_PREFIX_RE = re.compile(r"^\d{8}T\d{6}Z_[0-9a-f]{6}_")
_memory_lock = threading.RLock()


def _collect_matching_thread_ids(prefix: str, checkpointer: Any) -> Set[str]:
    """Collects all internal `checkpointer` thread keys matching `prefix` (`'{uid}:'`).

    Args:
        prefix: Tenant namespace prefix ending with `':'`.
        checkpointer: LangGraph checkpointer (`InMemorySaver`) instance to inspect.

    Returns:
        Set of full `{uid}:{thread_id}` checkpoint keys present across storage, writes, and blobs.
    """
    if checkpointer is None:
        return set()
    matched: Set[str] = set()
    for tid in list(getattr(checkpointer, "storage", {}).keys()):
        if isinstance(tid, str) and tid.startswith(prefix):
            matched.add(tid)
    for key in list(getattr(checkpointer, "writes", {}).keys()):
        if isinstance(key, tuple) and key and isinstance(key[0], str) and key[0].startswith(prefix):
            matched.add(key[0])
    for key in list(getattr(checkpointer, "blobs", {}).keys()):
        if isinstance(key, tuple) and key and isinstance(key[0], str) and key[0].startswith(prefix):
            matched.add(key[0])
    return matched


def list_user_memory_threads(uid: str, checkpointer: Optional[Any] = None) -> List[str]:
    """Returns sorted user-local thread identifiers stored in `checkpointer` for `uid`.

    Args:
        uid: Verified tenant user identifier.
        checkpointer: LangGraph checkpointer (`InMemorySaver`) holding thread history.

    Returns:
        Sorted list of unprefixed client `thread_id` strings belonging to `uid`.
    """
    safe_uid = validate_uid(uid)
    if checkpointer is None:
        return []
    prefix = f"{safe_uid}:"
    with _memory_lock:
        matched = _collect_matching_thread_ids(prefix, checkpointer)
    return sorted(tid[len(prefix) :] for tid in matched)


def clear_user_memory(uid: str, checkpointer: Optional[Any] = None) -> int:
    """Purges all `{uid}:*` conversation checkpoints, writes, and blobs from `checkpointer`.

    Args:
        uid: Verified tenant user identifier.
        checkpointer: LangGraph checkpointer (`InMemorySaver`) holding thread history.

    Returns:
        Number of distinct conversation threads permanently erased for `uid`.
    """
    safe_uid = validate_uid(uid)
    if checkpointer is None:
        return 0
    prefix = f"{safe_uid}:"
    with _memory_lock:
        matched = _collect_matching_thread_ids(prefix, checkpointer)
        for tid in matched:
            checkpointer.delete_thread(tid)

    if matched:
        logger.info(
            f"Purged {len(matched)} conversation thread(s) from checkpointer for uid='{safe_uid}'."
        )
    return len(matched)


def _list_bucket_blobs(bucket: Any, prefix: str, versions: bool = False) -> List[Any]:
    """Lists GCS blobs under `prefix`, optionally enumerating all live and noncurrent versions.

    Args:
        bucket: GCS `Bucket` (or unversioned test double).
        prefix: Object key prefix (e.g., `'users/alice/'`).
        versions: When True, requests `versions=True` and falls back cleanly on unversioned doubles.

    Returns:
        List of matching GCS `Blob` objects.
    """
    if not versions:
        return list(bucket.list_blobs(prefix=prefix))
    try:
        return list(bucket.list_blobs(prefix=prefix, versions=True))
    except TypeError:
        return list(bucket.list_blobs(prefix=prefix))


def _delete_blob_revision(bucket: Any, blob: Any) -> None:
    """Permanently deletes a specific GCS blob generation (or live blob on unversioned doubles).

    Args:
        bucket: GCS `Bucket` supporting `delete_blob(..., generation=...)` or basic `blob.delete()`.
        blob: Target `Blob` revision to destroy permanently.
    """
    generation = getattr(blob, "generation", None)
    with contextlib.suppress(gcs_exceptions.NotFound):
        if hasattr(bucket, "delete_blob"):
            bucket.delete_blob(blob.name, generation=generation)
        else:
            blob.delete()


def get_user_data_inventory(uid: str, checkpointer: Optional[Any] = None) -> Dict[str, Any]:
    """Builds a GDPR Art. 15 personal data inventory for `uid` across ChromaDB, GCS, and agent memory.

    Args:
        uid: Verified tenant user identifier.
        checkpointer: Optional LangGraph checkpointer (`InMemorySaver`) to count user memory threads.

    Returns:
        Structured inventory dictionary detailing collections, total chunks, archived PDF
        filenames/timestamps, live vs. noncurrent GCS object counts, storage byte totals,
        and active conversation memory thread count.
    """
    safe_uid = validate_uid(uid)
    region = validate_gcp_region()
    bucket_name = get_bucket_name()

    with user_read_lock(safe_uid) as persist_directory:
        col_payload = list_collections(persist_directory)
        raw_details = col_payload.get("details", []) if isinstance(col_payload, dict) else []
        collections = [
            {"name": str(item.get("name", "")), "count": int(item.get("count", 0) or 0)}
            for item in raw_details
            if isinstance(item, dict) and item.get("name")
        ]
        total_chunks = sum(item["count"] for item in collections)
        local_chroma_bytes = directory_size_bytes(persist_directory)

    local_uploads_bytes = directory_size_bytes(get_upload_user_dir(safe_uid))
    local_cache_bytes = local_chroma_bytes + local_uploads_bytes

    uploaded_pdfs: List[Dict[str, Any]] = []
    gcs_live_count = 0
    gcs_noncurrent_count = 0
    gcs_total_bytes = 0

    if bucket_name:
        bucket = gcs_sync._get_bucket()
        user_prefix = f"users/{safe_uid}/"
        uploads_prefix = f"users/{safe_uid}/uploads/"
        locks_prefix = f"users/{safe_uid}/locks/"

        for blob in _list_bucket_blobs(bucket, prefix=user_prefix, versions=True):
            blob_name = getattr(blob, "name", "")
            if not blob_name or blob_name.endswith("/") or blob_name.startswith(locks_prefix):
                continue

            size_bytes = int(getattr(blob, "size", 0) or 0)
            is_noncurrent = getattr(blob, "time_deleted", None) is not None
            gcs_total_bytes += size_bytes

            if is_noncurrent:
                gcs_noncurrent_count += 1
                continue

            gcs_live_count += 1
            if blob_name.startswith(uploads_prefix):
                raw_base = blob_name[len(uploads_prefix) :]
                display_name = _ARCHIVE_PREFIX_RE.sub("", raw_base) or raw_base
                updated = getattr(blob, "updated", None)
                updated_iso = (
                    updated.isoformat() if hasattr(updated, "isoformat") else None
                )
                uploaded_pdfs.append(
                    {
                        "filename": display_name,
                        "blob_name": blob_name,
                        "size_bytes": size_bytes,
                        "updated_at": updated_iso,
                    }
                )

    uploaded_pdfs.sort(key=lambda item: (item["filename"].lower(), item["blob_name"]))
    total_bytes = gcs_total_bytes if bucket_name else local_cache_bytes
    memory_threads = list_user_memory_threads(safe_uid, checkpointer=checkpointer)

    return {
        "uid": safe_uid,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "region": region,
        "storage_backend": "gcs" if bucket_name else "local",
        "gcs_bucket": bucket_name or None,
        "collection_count": len(collections),
        "total_chunks": total_chunks,
        "collections": collections,
        "uploaded_pdf_count": len(uploaded_pdfs),
        "uploaded_pdfs": uploaded_pdfs,
        "gcs_live_object_count": gcs_live_count,
        "gcs_noncurrent_version_count": gcs_noncurrent_count,
        "gcs_total_bytes": gcs_total_bytes,
        "local_cache_bytes": local_cache_bytes,
        "total_bytes": total_bytes,
        "memory_thread_count": len(memory_threads),
        "request_id": get_request_id(),
    }


def purge_all_user_data(uid: str, checkpointer: Optional[Any] = None) -> Dict[str, Any]:
    """Executes GDPR Art. 17 hard erasure of all stored data and conversation memory for `uid`.

    Permanently deletes:
    1. Every live and noncurrent GCS object generation under `users/{uid}/` (`versions=True`).
    2. Local `/tmp/chroma_db/{uid}` (or local working Chroma directory) after releasing Chroma handles.
    3. Local `/tmp/uploads/{uid}` temporary upload staging directory.
    4. All `{uid}:*` multi-turn conversation checkpoints in `checkpointer` (when provided).

    Args:
        uid: Verified tenant user identifier.
        checkpointer: Optional LangGraph checkpointer (`InMemorySaver`) whose `{uid}:*` threads should be erased.

    Returns:
        Erasure summary dictionary containing deleted live/noncurrent object counts, local flags,
        and `deleted_memory_threads`.
    """
    safe_uid = validate_uid(uid)
    user_thread_lock = get_user_thread_lock(safe_uid)

    def _wipe_local_user_dirs() -> tuple[bool, bool]:
        """Stops Chroma handles and deletes local Chroma and upload directories for `safe_uid`."""
        user_dir = get_user_dir(safe_uid)
        upload_dir = get_upload_user_dir(safe_uid)

        release_client_cache(user_dir)
        had_chroma = os.path.exists(user_dir)
        if had_chroma:
            shutil.rmtree(user_dir, ignore_errors=True)

        had_uploads = os.path.exists(upload_dir)
        if had_uploads:
            shutil.rmtree(upload_dir, ignore_errors=True)

        clear_user_last_used(safe_uid)
        return had_chroma, had_uploads

    with user_thread_lock, track_active_user(safe_uid):
        bucket_name = get_bucket_name()
        if not bucket_name:
            had_chroma, had_uploads = _wipe_local_user_dirs()
            deleted_memory_threads = clear_user_memory(safe_uid, checkpointer=checkpointer)
            logger.info(
                f"Purged local user data for uid='{safe_uid}' "
                f"(deleted_local_chroma={had_chroma}, deleted_local_uploads={had_uploads}, "
                f"deleted_memory_threads={deleted_memory_threads})."
            )
            return {
                "uid": safe_uid,
                "storage_backend": "local",
                "deleted_gcs_live_objects": 0,
                "deleted_gcs_noncurrent_versions": 0,
                "deleted_local_chroma": had_chroma,
                "deleted_local_uploads": had_uploads,
                "deleted_memory_threads": deleted_memory_threads,
            }

        bucket = gcs_sync._get_bucket()
        user_prefix = f"users/{safe_uid}/"
        locks_prefix = f"users/{safe_uid}/locks/"
        lock_name = user_lock_gcs_blob_name(safe_uid)
        lock_blob = acquire_gcs_write_lock(bucket, safe_uid)
        active_lock_gen = getattr(lock_blob, "generation", None)

        deleted_live = 0
        deleted_noncurrent = 0
        had_chroma = False
        had_uploads = False

        try:
            had_chroma, had_uploads = _wipe_local_user_dirs()

            for blob in _list_bucket_blobs(bucket, prefix=user_prefix, versions=True):
                blob_name = getattr(blob, "name", "")
                blob_gen = getattr(blob, "generation", None)
                is_noncurrent = getattr(blob, "time_deleted", None) is not None

                if (
                    blob_name == lock_name
                    and not is_noncurrent
                    and blob_gen == active_lock_gen
                ):
                    continue

                _delete_blob_revision(bucket, blob)
                if not blob_name.startswith(locks_prefix):
                    if is_noncurrent:
                        deleted_noncurrent += 1
                    else:
                        deleted_live += 1
        finally:
            release_gcs_write_lock(lock_blob, safe_uid)
            for lock_ver in _list_bucket_blobs(bucket, prefix=locks_prefix, versions=True):
                _delete_blob_revision(bucket, lock_ver)

        deleted_memory_threads = clear_user_memory(safe_uid, checkpointer=checkpointer)
        logger.info(
            f"Purged all GCS + local data for uid='{safe_uid}' "
            f"(live_objects={deleted_live}, noncurrent_versions={deleted_noncurrent}, "
            f"local_chroma={had_chroma}, local_uploads={had_uploads}, "
            f"deleted_memory_threads={deleted_memory_threads})."
        )
        return {
            "uid": safe_uid,
            "storage_backend": "gcs",
            "deleted_gcs_live_objects": deleted_live,
            "deleted_gcs_noncurrent_versions": deleted_noncurrent,
            "deleted_local_chroma": had_chroma,
            "deleted_local_uploads": had_uploads,
            "deleted_memory_threads": deleted_memory_threads,
        }


__all__ = [
    "clear_user_memory",
    "get_user_data_inventory",
    "list_user_memory_threads",
    "purge_all_user_data",
]


