"""Google Cloud Storage snapshot synchronization and PDF upload archiving.

Synchronizes per-tenant ChromaDB SQLite/HNSW snapshots between Cloud Run's ephemeral
`/tmp/chroma_db/{uid}` directory and `gs://<bucket>/users/{uid}/chroma_db/` using
`version.json` invalidation, and archives raw uploaded PDFs under `users/{uid}/uploads/`.
"""

import base64
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
import threading
from typing import Any, Callable, Dict, Optional
import uuid

from google.api_core import exceptions as gcs_exceptions
from google.cloud import storage

from src.core.auth import validate_uid
from src.core.logger import setup_logging
from src.core.observability.tracing import start_span
from src.core.storage.paths import (
    SAFE_FILENAME_RE,
    SQLITE_FILENAME,
    TRANSIENT_SQLITE_SUFFIXES,
    VERSION_FILENAME,
    checkpoint_sqlite_wal,
    get_bucket_name,
    get_chroma_root,
    get_user_dir,
    read_local_version,
    release_client_cache,
    user_chroma_gcs_prefix,
)

logger = setup_logging(name="storage.gcs_sync")

_gcs_client_lock = threading.Lock()
_gcs_client: Optional[storage.Client] = None


def _compute_file_md5_base64(file_path: str) -> str:
    """Computes the base64-encoded MD5 digest of a local file matching GCS `blob.md5_hash` format.

    Args:
        file_path: Local path to the file to hash.

    Returns:
        Base64-encoded MD5 string.
    """
    hasher = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return base64.b64encode(hasher.digest()).decode("ascii")


def _get_gcs_client() -> storage.Client:
    """Returns a thread-safe singleton Google Cloud Storage `Client`.

    Returns:
        Initialized `google.cloud.storage.Client` bound to `GOOGLE_CLOUD_PROJECT` if set.
    """
    global _gcs_client
    if _gcs_client is not None:
        return _gcs_client
    with _gcs_client_lock:
        if _gcs_client is None:
            project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip() or None
            _gcs_client = storage.Client(project=project)
        return _gcs_client


def _get_bucket() -> storage.Bucket:
    """Returns the configured GCS `Bucket` handle for multi-tenant persistence.

    Returns:
        `google.cloud.storage.Bucket` corresponding to `GCS_BUCKET_NAME`.

    Raises:
        RuntimeError: If `GCS_BUCKET_NAME` is not configured.
    """
    bucket_name = get_bucket_name()
    if not bucket_name:
        raise RuntimeError("GCS_BUCKET_NAME is not configured.")
    return _get_gcs_client().bucket(bucket_name)


def _sync_from_gcs_unlocked(
    uid: str,
    on_before_download: Optional[Callable[[str], Any]] = None,
) -> str:
    """Downloads `gs://<bucket>/users/{uid}/chroma_db/` into local `user_dir` when missing or stale.

    Must be called while holding the user's in-process thread lock. Uses an atomic staging
    directory (`{uid}.downloading.<hex>`) and `os.replace` so concurrent readers never observe
    partially downloaded SQLite or HNSW segment files.

    Args:
        uid: Verified path-safe user identifier.
        on_before_download: Optional callback (e.g., `evict_tmp_cache`) invoked with `uid`
            before staging a fresh GCS snapshot download.

    Returns:
        Local filesystem path `get_user_dir(uid)`.

    Raises:
        ValueError: If any remote GCS blob contains a path-traversal relative key.
    """
    user_dir = get_user_dir(uid)
    if not get_bucket_name():
        return user_dir

    with start_span("storage.gcs_pull", stage="gcs_sync", uid=uid) as span:
        bucket = _get_bucket()
        prefix = user_chroma_gcs_prefix(uid)
        version_blob = bucket.blob(f"{prefix}{VERSION_FILENAME}")

        if not version_blob.exists():
            # User has no persisted vector store in GCS yet; remove any stale local copy
            if os.path.exists(user_dir):
                release_client_cache(user_dir)
                shutil.rmtree(user_dir, ignore_errors=True)
            span["attributes"]["cache_hit"] = True
            return user_dir

        remote_meta = json.loads(version_blob.download_as_text())
        remote_version = remote_meta.get("version")
        local_version = read_local_version(user_dir)

        if remote_version and local_version == remote_version:
            span["attributes"]["cache_hit"] = True
            return user_dir

        span["attributes"]["cache_hit"] = False
        logger.info(
            f"Syncing ChromaDB from GCS for uid='{uid}' (local_version={local_version!r} -> remote_version={remote_version!r})."
        )
        if on_before_download is not None:
            on_before_download(uid)

        root = get_chroma_root()
        os.makedirs(root, exist_ok=True)
        staging_dir = os.path.join(root, f"{uid}.downloading.{uuid.uuid4().hex[:8]}")

        try:
            os.makedirs(staging_dir, exist_ok=True)
            blobs = list(bucket.list_blobs(prefix=prefix))
            downloaded_count = 0

            for blob in blobs:
                rel_path = blob.name[len(prefix) :]
                if not rel_path or rel_path.endswith("/"):
                    continue

                dest_path = os.path.abspath(os.path.join(staging_dir, rel_path))
                if os.path.commonpath([staging_dir, dest_path]) != staging_dir:
                    raise ValueError(f"Refusing unsafe GCS blob relative path: {rel_path!r}")

                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                blob.download_to_filename(dest_path)
                downloaded_count += 1

            release_client_cache(user_dir)
            if os.path.exists(user_dir):
                shutil.rmtree(user_dir)
            os.replace(staging_dir, user_dir)
            span["attributes"]["downloaded_files"] = downloaded_count
            logger.info(f"Downloaded {downloaded_count} ChromaDB file(s) from GCS into '{user_dir}'.")
            return user_dir
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise


def _push_user_dir_to_gcs(bucket: storage.Bucket, uid: str, user_dir: str) -> None:
    """Flushes local ChromaDB state and incrementally uploads changed snapshot files + `version.json` to GCS.

    Compares local file MD5 digests against existing remote GCS blob `md5_hash` metadata so
    unchanged HNSW segment files from untouched collections are skipped. Uploads new/modified
    data files first, removes obsolete remote blobs (e.g., deleted collections), and writes
    `version.json` last so readers only switch after every data blob is durable in GCS.

    Args:
        bucket: Target GCS `Bucket` instance.
        uid: Verified path-safe user identifier.
        user_dir: Local ChromaDB working directory to snapshot.
    """
    with start_span("storage.gcs_push", stage="gcs_sync", uid=uid) as span:
        release_client_cache(user_dir)
        checkpoint_sqlite_wal(user_dir)

        prefix = user_chroma_gcs_prefix(uid)
        local_rel_files: Dict[str, str] = {}

        if os.path.exists(user_dir):
            for dirpath, _, filenames in os.walk(user_dir):
                for fname in filenames:
                    if fname == VERSION_FILENAME:
                        continue
                    if fname.endswith(TRANSIENT_SQLITE_SUFFIXES):
                        fpath = os.path.join(dirpath, fname)
                        if os.path.exists(fpath) and os.path.getsize(fpath) == 0:
                            continue
                    full_path = os.path.join(dirpath, fname)
                    rel_path = os.path.relpath(full_path, user_dir).replace(os.sep, "/")
                    local_rel_files[rel_path] = full_path

        # 1. Index existing remote blobs to support incremental MD5 delta uploads and orphan deletion
        existing_blobs = list(bucket.list_blobs(prefix=prefix))
        remote_blobs_by_rel: Dict[str, storage.Blob] = {}
        for remote_blob in existing_blobs:
            rel_path = remote_blob.name[len(prefix) :]
            if not rel_path or rel_path == VERSION_FILENAME:
                continue
            remote_blobs_by_rel[rel_path] = remote_blob

        # 2. Upload only new or modified data files (skipping blobs with identical MD5 digest)
        uploaded_count = 0
        skipped_count = 0
        for rel_path, full_path in local_rel_files.items():
            existing_blob = remote_blobs_by_rel.get(rel_path)
            remote_md5 = getattr(existing_blob, "md5_hash", None) if existing_blob is not None else None
            if remote_md5 and remote_md5 == _compute_file_md5_base64(full_path):
                skipped_count += 1
                continue
            blob = bucket.blob(f"{prefix}{rel_path}")
            blob.upload_from_filename(full_path)
            uploaded_count += 1

        # 3. Delete remote blobs that were removed locally (e.g., deleted collection HNSW folders)
        deleted_count = 0
        for rel_path, remote_blob in remote_blobs_by_rel.items():
            if rel_path not in local_rel_files:
                remote_blob.delete()
                deleted_count += 1

        span["attributes"]["uploaded_files"] = uploaded_count
        span["attributes"]["skipped_files"] = skipped_count
        span["attributes"]["deleted_files"] = deleted_count

        # 4. Write & upload version.json LAST so readers only switch after all data files exist in GCS
        if SQLITE_FILENAME in local_rel_files:
            new_version = {
                "version": uuid.uuid4().hex,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "file_count": len(local_rel_files),
            }
            version_json_str = json.dumps(new_version, indent=2)
            local_version_path = os.path.join(user_dir, VERSION_FILENAME)
            with open(local_version_path, "w", encoding="utf-8") as f:
                f.write(version_json_str)

            version_blob = bucket.blob(f"{prefix}{VERSION_FILENAME}")
            version_blob.upload_from_string(version_json_str, content_type="application/json")
            logger.info(
                f"Synced ChromaDB to GCS for uid='{uid}': uploaded={uploaded_count}, "
                f"skipped={skipped_count}, deleted={deleted_count} (version={new_version['version']})."
            )
        else:
            # User directory contains no SQLite database; ensure version.json is removed too
            version_blob = bucket.blob(f"{prefix}{VERSION_FILENAME}")
            with contextlib.suppress(gcs_exceptions.NotFound):
                version_blob.delete()


def archive_upload(uid: str, local_file_path: str, original_filename: str) -> Optional[str]:
    """Archives an uploaded PDF to `gs://<bucket>/users/{uid}/uploads/` when GCS is configured.

    Args:
        uid: Verified tenant user identifier.
        local_file_path: Staged `/tmp` path of the validated PDF binary.
        original_filename: Sanitized display filename of the uploaded PDF.

    Returns:
        `gs://<bucket>/users/{uid}/uploads/<timestamp>_<nonce>_<filename>` URI, or `None` in local mode.
    """
    bucket_name = get_bucket_name()
    if not bucket_name:
        return None

    safe_uid = validate_uid(uid)
    with start_span("storage.archive_upload", stage="gcs_sync", uid=safe_uid):
        cleaned_name = SAFE_FILENAME_RE.sub("_", os.path.basename(original_filename or "document.pdf")).strip("_")
        if not cleaned_name.lower().endswith(".pdf"):
            cleaned_name = f"{cleaned_name}.pdf"

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        blob_name = f"users/{safe_uid}/uploads/{timestamp}_{uuid.uuid4().hex[:6]}_{cleaned_name}"
        bucket = _get_bucket()
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(local_file_path, content_type="application/pdf")
        gcs_uri = f"gs://{bucket_name}/{blob_name}"
        logger.info(f"Archived uploaded PDF for uid='{safe_uid}' to '{gcs_uri}'.")
        return gcs_uri


__all__ = [
    "_compute_file_md5_base64",
    "_get_bucket",
    "_get_gcs_client",
    "_push_user_dir_to_gcs",
    "_sync_from_gcs_unlocked",
    "archive_upload",
]
