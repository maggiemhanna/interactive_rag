"""GDPR Art. 15 Data Inventory & Art. 17 Complete Versioned Erasure (Pillar 4: Compliance & Governance).

Implements tenant-scoped Data Subject Access Request (DSAR) storage operations:
- `get_user_data_inventory(uid)`: Enumerates collections, passage counts, archived PDF uploads,
  live and noncurrent GCS object generations, and disk/cloud byte totals.
- `purge_all_user_data(uid)`: Acquires the user's distributed GCS write lock, wipes local
  `/tmp/chroma_db/{uid}` and `/tmp/uploads/{uid}` directories, and permanently deletes every
  live and noncurrent object revision (`versions=True`) under `gs://<bucket>/users/{uid}/`.
"""

import contextlib
from datetime import datetime, timezone
import os
import re
import shutil
from typing import Any, Dict, List

from google.api_core import exceptions as gcs_exceptions

from src.core.auth import validate_uid
from src.core.compliance.residency import validate_gcp_region
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


def get_user_data_inventory(uid: str) -> Dict[str, Any]:
    """Builds a GDPR Art. 15 personal data inventory for `uid` across ChromaDB and GCS.

    Args:
        uid: Verified tenant user identifier.

    Returns:
        Structured inventory dictionary detailing collections, total passages, archived PDF
        filenames/timestamps, live vs. noncurrent GCS object counts, and storage byte totals.
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
        total_passages = sum(item["count"] for item in collections)
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

    return {
        "uid": safe_uid,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "region": region,
        "storage_backend": "gcs" if bucket_name else "local",
        "gcs_bucket": bucket_name or None,
        "collection_count": len(collections),
        "total_passages": total_passages,
        "collections": collections,
        "uploaded_pdf_count": len(uploaded_pdfs),
        "uploaded_pdfs": uploaded_pdfs,
        "gcs_live_object_count": gcs_live_count,
        "gcs_noncurrent_version_count": gcs_noncurrent_count,
        "gcs_total_bytes": gcs_total_bytes,
        "local_cache_bytes": local_cache_bytes,
        "total_bytes": total_bytes,
    }


def purge_all_user_data(uid: str) -> Dict[str, Any]:
    """Executes GDPR Art. 17 hard erasure of all stored data for `uid`.

    Permanently deletes:
    1. Every live and noncurrent GCS object generation under `users/{uid}/` (`versions=True`).
    2. Local `/tmp/chroma_db/{uid}` (or local working Chroma directory) after releasing Chroma handles.
    3. Local `/tmp/uploads/{uid}` temporary upload staging directory.

    Args:
        uid: Verified tenant user identifier.

    Returns:
        Erasure summary dictionary containing deleted live/noncurrent object counts and local flags.
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
            logger.info(
                f"Purged local user data for uid='{safe_uid}' "
                f"(deleted_local_chroma={had_chroma}, deleted_local_uploads={had_uploads})."
            )
            return {
                "uid": safe_uid,
                "storage_backend": "local",
                "deleted_gcs_live_objects": 0,
                "deleted_gcs_noncurrent_versions": 0,
                "deleted_local_chroma": had_chroma,
                "deleted_local_uploads": had_uploads,
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

        logger.info(
            f"Purged all GCS + local data for uid='{safe_uid}' "
            f"(live_objects={deleted_live}, noncurrent_versions={deleted_noncurrent}, "
            f"local_chroma={had_chroma}, local_uploads={had_uploads})."
        )
        return {
            "uid": safe_uid,
            "storage_backend": "gcs",
            "deleted_gcs_live_objects": deleted_live,
            "deleted_gcs_noncurrent_versions": deleted_noncurrent,
            "deleted_local_chroma": had_chroma,
            "deleted_local_uploads": had_uploads,
        }


__all__ = ["get_user_data_inventory", "purge_all_user_data"]
