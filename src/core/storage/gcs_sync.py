"""Google Cloud Storage snapshot synchronization and PDF upload archiving.

Synchronizes per-tenant ChromaDB (`chroma/`) and SQLite FTS5 (`fts/`) snapshots between
Cloud Run's ephemeral `/tmp/chroma_db/{uid}` directory and `gs://<bucket>/users/{uid}/`
using root `version.json` invalidation, and archives raw uploaded PDFs under
`users/{uid}/uploads/` (in GCS when configured, or locally under `<user_dir>/uploads/`).
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
from src.core.config import get_google_cloud_project
from src.core.logger import setup_logging
from src.core.observability.tracing import start_span
from src.core.storage.paths import (
    CHROMA_SUBDIR,
    SAFE_FILENAME_RE,
    SQLITE_FILENAME,
    SYNCED_SUBDIRS,
    TRANSIENT_SQLITE_SUFFIXES,
    VERSION_FILENAME,
    checkpoint_sqlite_wal,
    get_bucket_name,
    get_chroma_root,
    get_user_dir,
    get_user_uploads_dir,
    read_local_version,
    release_client_cache,
    user_gcs_prefix,
    user_uploads_gcs_prefix,
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
            project = get_google_cloud_project() or None
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
    """Downloads `users/{uid}/{version.json, chroma/*, fts/*}` from GCS into local `user_dir` when stale.

    Must be called while holding the user's in-process thread lock. Explicitly scopes downloads
    to `chroma/`, `fts/`, and `version.json` (never downloading `uploads/` or `locks/`), using
    an atomic staging directory (`{uid}.downloading.<hex>`) and `os.replace` so concurrent
    readers never observe partially downloaded SQLite, HNSW, or FTS5 files.

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
        prefix = user_gcs_prefix(uid)
        version_blob = bucket.blob(f"{prefix}{VERSION_FILENAME}")

        if not version_blob.exists():
            # User has no persisted vector/FTS snapshot in GCS yet; remove any stale local copy
            if os.path.exists(user_dir):
                release_client_cache(user_dir)
                shutil.rmtree(user_dir, ignore_errors=True)
            span["attributes"]["cache_hit"] = True
            return user_dir

        version_text = version_blob.download_as_text()
        remote_meta = json.loads(version_text)
        remote_version = remote_meta.get("version")
        local_version = read_local_version(user_dir)

        if remote_version and local_version == remote_version:
            span["attributes"]["cache_hit"] = True
            return user_dir

        span["attributes"]["cache_hit"] = False
        logger.info(
            f"Syncing storage snapshot from GCS for uid='{uid}' (local_version={local_version!r} -> remote_version={remote_version!r})."
        )
        if on_before_download is not None:
            on_before_download(uid)

        root = get_chroma_root()
        os.makedirs(root, exist_ok=True)
        staging_dir = os.path.join(root, f"{uid}.downloading.{uuid.uuid4().hex[:8]}")

        try:
            os.makedirs(staging_dir, exist_ok=True)
            staging_abs = os.path.abspath(staging_dir)
            downloaded_count = 0

            for subdir in SYNCED_SUBDIRS:
                sub_prefix = f"{prefix}{subdir}/"
                for blob in bucket.list_blobs(prefix=sub_prefix):
                    rel_path = blob.name[len(prefix) :]
                    if not rel_path or rel_path.endswith("/"):
                        continue

                    dest_path = os.path.abspath(os.path.join(staging_abs, rel_path))
                    if os.path.commonpath([staging_abs, dest_path]) != staging_abs:
                        raise ValueError(f"Refusing unsafe GCS blob relative path: {rel_path!r}")

                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    blob.download_to_filename(dest_path)
                    downloaded_count += 1

            with open(os.path.join(staging_abs, VERSION_FILENAME), "w", encoding="utf-8") as f:
                f.write(version_text)
            downloaded_count += 1

            release_client_cache(user_dir)
            if os.path.exists(user_dir):
                shutil.rmtree(user_dir)
            os.replace(staging_dir, user_dir)
            span["attributes"]["downloaded_files"] = downloaded_count
            logger.info(f"Downloaded {downloaded_count} snapshot file(s) from GCS into '{user_dir}'.")
            return user_dir
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise


def _push_user_dir_to_gcs(bucket: storage.Bucket, uid: str, user_dir: str) -> None:
    """Flushes local Chroma/FTS state and incrementally uploads changed snapshot files + `version.json` to GCS.

    Scopes sync strictly to `chroma/` and `fts/` under `users/{uid}/` so `uploads/` and `locks/`
    are never modified or pruned. Compares local file MD5 digests against existing remote GCS
    blob `md5_hash` metadata so unchanged HNSW segments and untouched collection FTS5 files are
    skipped. Uploads new/modified data files first, deletes obsolete remote blobs under `chroma/`
    and `fts/`, and writes `users/{uid}/version.json` last so readers only switch after every
    data blob is durable in GCS.

    Args:
        bucket: Target GCS `Bucket` instance.
        uid: Verified path-safe user identifier.
        user_dir: Local tenant working directory containing `chroma/` and `fts/`.
    """
    with start_span("storage.gcs_push", stage="gcs_sync", uid=uid) as span:
        release_client_cache(user_dir)
        checkpoint_sqlite_wal(user_dir)

        prefix = user_gcs_prefix(uid)
        local_rel_files: Dict[str, str] = {}

        for subdir in SYNCED_SUBDIRS:
            sub_root = os.path.join(user_dir, subdir)
            if not os.path.exists(sub_root):
                continue
            for dirpath, _, filenames in os.walk(sub_root):
                for fname in filenames:
                    if fname.endswith(TRANSIENT_SQLITE_SUFFIXES):
                        fpath = os.path.join(dirpath, fname)
                        if os.path.exists(fpath) and os.path.getsize(fpath) == 0:
                            continue
                    full_path = os.path.join(dirpath, fname)
                    rel_path = os.path.relpath(full_path, user_dir).replace(os.sep, "/")
                    local_rel_files[rel_path] = full_path

        # 1. Index existing remote snapshot blobs under chroma/ and fts/ (never touching uploads/ or locks/)
        remote_blobs_by_rel: Dict[str, storage.Blob] = {}
        for subdir in SYNCED_SUBDIRS:
            sub_prefix = f"{prefix}{subdir}/"
            for remote_blob in bucket.list_blobs(prefix=sub_prefix):
                rel_path = remote_blob.name[len(prefix) :]
                if not rel_path or rel_path.endswith("/"):
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

        # 3. Delete remote snapshot blobs that were removed locally (e.g., deleted collection HNSW/FTS files)
        deleted_count = 0
        for rel_path, remote_blob in remote_blobs_by_rel.items():
            if rel_path not in local_rel_files:
                remote_blob.delete()
                deleted_count += 1

        span["attributes"]["uploaded_files"] = uploaded_count
        span["attributes"]["skipped_files"] = skipped_count
        span["attributes"]["deleted_files"] = deleted_count

        # 4. Write & upload version.json LAST so readers only switch after all data files exist in GCS
        chroma_sqlite_rel = f"{CHROMA_SUBDIR}/{SQLITE_FILENAME}"
        local_version_path = os.path.join(user_dir, VERSION_FILENAME)
        if chroma_sqlite_rel in local_rel_files:
            new_version = {
                "version": uuid.uuid4().hex,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "file_count": len(local_rel_files),
            }
            version_json_str = json.dumps(new_version, indent=2)
            with open(local_version_path, "w", encoding="utf-8") as f:
                f.write(version_json_str)

            version_blob = bucket.blob(f"{prefix}{VERSION_FILENAME}")
            version_blob.upload_from_string(version_json_str, content_type="application/json")
            logger.info(
                f"Synced snapshot to GCS for uid='{uid}': uploaded={uploaded_count}, "
                f"skipped={skipped_count}, deleted={deleted_count} (version={new_version['version']})."
            )
        else:
            # User directory contains no Chroma SQLite database; ensure version.json is removed too
            with contextlib.suppress(OSError):
                if os.path.exists(local_version_path):
                    os.remove(local_version_path)
            version_blob = bucket.blob(f"{prefix}{VERSION_FILENAME}")
            with contextlib.suppress(gcs_exceptions.NotFound):
                version_blob.delete()


def archive_upload(uid: str, local_file_path: str, original_filename: str) -> Optional[str]:
    """Archives an uploaded PDF to GCS (`users/{uid}/uploads/`) or local `<user_dir>/uploads/`.

    When `GCS_BUCKET_NAME` is configured, uploads the PDF to GCS and returns its `gs://` URI.
    In local mode (when `GCS_BUCKET_NAME` is unset), copies the validated PDF into
    `<user_dir>/uploads/` so local disk mirrors the GCS tenant layout, returning `None`.

    Args:
        uid: Verified tenant user identifier.
        local_file_path: Staged ephemeral `/tmp` path of the validated PDF binary.
        original_filename: Sanitized display filename of the uploaded PDF.

    Returns:
        `gs://<bucket>/users/{uid}/uploads/<timestamp>_<nonce>_<filename>` URI in GCS mode,
        or `None` in local mode.
    """
    safe_uid = validate_uid(uid)
    cleaned_name = SAFE_FILENAME_RE.sub("_", os.path.basename(original_filename or "document.pdf")).strip("_")
    if not cleaned_name.lower().endswith(".pdf"):
        cleaned_name = f"{cleaned_name}.pdf"

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_filename = f"{timestamp}_{uuid.uuid4().hex[:6]}_{cleaned_name}"

    bucket_name = get_bucket_name()
    if not bucket_name:
        user_dir = get_user_dir(safe_uid)
        uploads_dir = os.path.abspath(get_user_uploads_dir(user_dir))
        os.makedirs(uploads_dir, exist_ok=True)
        dest_path = os.path.abspath(os.path.join(uploads_dir, archive_filename))
        if os.path.commonpath([uploads_dir, dest_path]) != uploads_dir:
            raise ValueError(f"Refusing unsafe local upload archive path: {archive_filename!r}")
        shutil.copy2(local_file_path, dest_path)
        logger.info(f"Archived uploaded PDF for uid='{safe_uid}' locally to '{dest_path}'.")
        return None

    with start_span("storage.archive_upload", stage="gcs_sync", uid=safe_uid):
        blob_name = f"{user_uploads_gcs_prefix(safe_uid)}{archive_filename}"
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
