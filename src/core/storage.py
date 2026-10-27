import contextlib
import json
import os
import random
import re
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional

from google.api_core import exceptions as gcs_exceptions
from google.cloud import storage

from src.core.auth import is_cloud_run, validate_uid
from src.core.logger import setup_logging

logger = setup_logging(name="storage")

VERSION_FILENAME = "version.json"
SQLITE_FILENAME = "chroma.sqlite3"
TRANSIENT_SQLITE_SUFFIXES = ("-wal", "-shm", "-journal")
SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")

_gcs_client_lock = threading.Lock()
_gcs_client: Optional[storage.Client] = None

_state_lock = threading.Lock()
_user_thread_locks: Dict[str, threading.RLock] = {}
_user_last_used: Dict[str, float] = {}
_user_active_refs: Dict[str, int] = {}


class LockTimeoutError(TimeoutError):
    """Raised when a per-user GCS write lock cannot be acquired within the configured timeout."""


def get_bucket_name() -> str:
    """Returns configured GCS bucket name, or empty string when running in local-only disk mode."""
    return os.getenv("GCS_BUCKET_NAME", "").strip()


def get_chroma_root() -> str:
    """Returns the root directory where per-user ChromaDB working directories are stored."""
    configured = os.getenv("LOCAL_CHROMA_ROOT", "").strip()
    if configured:
        return os.path.abspath(configured)
    if is_cloud_run():
        return "/tmp/chroma_db"
    return os.path.abspath(os.path.join("chroma_db", "users"))


def get_user_dir(uid: str) -> str:
    """Returns the validated, isolated local ChromaDB directory path for a specific user."""
    safe_uid = validate_uid(uid)
    root = get_chroma_root()
    user_dir = os.path.abspath(os.path.join(root, safe_uid))
    if os.path.commonpath([root, user_dir]) != root or user_dir == root:
        raise ValueError(f"Unsafe resolved Chroma directory for uid={safe_uid!r}")
    return user_dir


def _get_gcs_client() -> storage.Client:
    """Returns a singleton Google Cloud Storage client."""
    global _gcs_client
    if _gcs_client is not None:
        return _gcs_client
    with _gcs_client_lock:
        if _gcs_client is None:
            project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip() or None
            _gcs_client = storage.Client(project=project)
        return _gcs_client


def _get_bucket() -> storage.Bucket:
    bucket_name = get_bucket_name()
    if not bucket_name:
        raise RuntimeError("GCS_BUCKET_NAME is not configured.")
    return _get_gcs_client().bucket(bucket_name)


def _get_user_thread_lock(uid: str) -> threading.RLock:
    with _state_lock:
        lock = _user_thread_locks.get(uid)
        if lock is None:
            lock = threading.RLock()
            _user_thread_locks[uid] = lock
        return lock


@contextlib.contextmanager
def _track_active_user(uid: str) -> Generator[None, None, None]:
    with _state_lock:
        _user_active_refs[uid] = _user_active_refs.get(uid, 0) + 1
        _user_last_used[uid] = time.time()
    try:
        yield
    finally:
        with _state_lock:
            remaining = max(0, _user_active_refs.get(uid, 1) - 1)
            if remaining == 0:
                _user_active_refs.pop(uid, None)
            else:
                _user_active_refs[uid] = remaining
            _user_last_used[uid] = time.time()


def release_client_cache(persist_directory: str) -> None:
    """Stops and evicts ChromaDB's cached System instance for a specific persist_directory.

    ChromaDB's PersistentClient caches RustBindingsAPI/SegmentAPI instances per directory path.
    Releasing this handle is mandatory before replacing local SQLite/HNSW files on GCS refresh
    and before snapshotting SQLite files back to GCS.
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


def _checkpoint_sqlite_wal(user_dir: str) -> None:
    """Folds any SQLite WAL frames cleanly into chroma.sqlite3 prior to GCS upload."""
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


def _directory_size_bytes(path: str) -> int:
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


def evict_tmp_cache(exclude_uid: Optional[str] = None) -> List[str]:
    """LRU-evicts inactive `/tmp/chroma_db/{uid}` directories when cache size or free RAM disk is tight.

    Runs ONLY when `GCS_BUCKET_NAME` is configured (since GCS holds the canonical persistent copy).
    Never evicts a user directory that currently has an active read or write operation in flight.
    """
    if not get_bucket_name():
        return []

    root = get_chroma_root()
    if not os.path.exists(root):
        return []

    max_cache_bytes = int(os.getenv("CHROMA_TMP_MAX_BYTES", str(512 * 1024 * 1024)))
    min_free_bytes = int(os.getenv("CHROMA_TMP_MIN_FREE_BYTES", str(256 * 1024 * 1024)))

    def _needs_eviction() -> bool:
        total_used = _directory_size_bytes(root)
        if max_cache_bytes > 0 and total_used > max_cache_bytes:
            return True
        if min_free_bytes > 0:
            try:
                free_space = shutil.disk_usage(root).free
                if free_space < min_free_bytes:
                    return True
            except OSError:
                pass
        return False

    if not _needs_eviction():
        return []

    candidates = []
    with _state_lock:
        for entry in os.scandir(root):
            if not entry.is_dir():
                continue
            cand_uid = entry.name
            if cand_uid == exclude_uid or ".downloading." in cand_uid:
                continue
            if _user_active_refs.get(cand_uid, 0) > 0:
                continue
            try:
                fallback_mtime = entry.stat().st_mtime
            except OSError:
                fallback_mtime = 0.0
            last_used = _user_last_used.get(cand_uid, fallback_mtime)
            candidates.append((last_used, cand_uid, entry.path))

    # Sort oldest-used first (LRU)
    candidates.sort(key=lambda item: item[0])

    evicted: List[str] = []
    for _, cand_uid, cand_path in candidates:
        if not _needs_eviction():
            break

        cand_lock = _get_user_thread_lock(cand_uid)
        if not cand_lock.acquire(blocking=False):
            continue
        try:
            with _state_lock:
                if _user_active_refs.get(cand_uid, 0) > 0:
                    continue
            release_client_cache(cand_path)
            shutil.rmtree(cand_path, ignore_errors=True)
            with _state_lock:
                _user_last_used.pop(cand_uid, None)
            evicted.append(cand_uid)
            logger.info(f"LRU-evicted inactive local Chroma cache for uid='{cand_uid}' ('{cand_path}').")
        finally:
            cand_lock.release()

    return evicted


def _user_chroma_gcs_prefix(uid: str) -> str:
    return f"users/{uid}/chroma_db/"


def _user_lock_gcs_blob_name(uid: str) -> str:
    return f"users/{uid}/locks/chroma.lock"


def _read_local_version(user_dir: str) -> Optional[str]:
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


def _sync_from_gcs_unlocked(uid: str) -> str:
    """Downloads `gs://<bucket>/users/{uid}/chroma_db/` into local `user_dir` if missing or stale."""
    user_dir = get_user_dir(uid)
    if not get_bucket_name():
        return user_dir

    bucket = _get_bucket()
    prefix = _user_chroma_gcs_prefix(uid)
    version_blob = bucket.blob(f"{prefix}{VERSION_FILENAME}")

    if not version_blob.exists():
        # User has no persisted vector store in GCS yet; remove any stale local copy
        if os.path.exists(user_dir):
            release_client_cache(user_dir)
            shutil.rmtree(user_dir, ignore_errors=True)
        return user_dir

    remote_meta = json.loads(version_blob.download_as_text())
    remote_version = remote_meta.get("version")
    local_version = _read_local_version(user_dir)

    if remote_version and local_version == remote_version:
        return user_dir

    logger.info(
        f"Syncing ChromaDB from GCS for uid='{uid}' (local_version={local_version!r} -> remote_version={remote_version!r})."
    )
    evict_tmp_cache(exclude_uid=uid)

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
        logger.info(f"Downloaded {downloaded_count} ChromaDB file(s) from GCS into '{user_dir}'.")
        return user_dir
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def sync_user_dir(uid: str) -> str:
    """Ensures the user's local ChromaDB directory matches the latest snapshot in GCS (Read Path).

    Returns the user's `persist_directory` path. If the user has not created any collections yet,
    the returned directory path will not exist on disk (allowing callers to return empty collections / 404).
    """
    safe_uid = validate_uid(uid)
    user_lock = _get_user_thread_lock(safe_uid)
    with user_lock, _track_active_user(safe_uid):
        return _sync_from_gcs_unlocked(safe_uid)


@contextlib.contextmanager
def user_read_lock(uid: str) -> Generator[str, None, None]:
    """Context manager for read operations that pins the user's `/tmp` cache against LRU eviction."""
    safe_uid = validate_uid(uid)
    user_lock = _get_user_thread_lock(safe_uid)
    with _track_active_user(safe_uid):
        with user_lock:
            user_dir = _sync_from_gcs_unlocked(safe_uid)
        yield user_dir


def _acquire_gcs_write_lock(bucket: storage.Bucket, uid: str) -> storage.Blob:
    """Acquires `users/{uid}/locks/chroma.lock` using atomic `if_generation_match=0` with TTL recovery."""
    lock_blob_name = _user_lock_gcs_blob_name(uid)
    lock_ttl_seconds = float(os.getenv("CHROMA_LOCK_TTL_SECONDS", "300"))
    acquire_timeout_seconds = float(os.getenv("CHROMA_LOCK_TIMEOUT_SECONDS", "60"))
    owner_id = f"{os.getenv('K_REVISION', 'local')}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    deadline = time.monotonic() + acquire_timeout_seconds
    backoff = 0.25

    while True:
        now = time.time()
        payload = json.dumps(
            {
                "uid": uid,
                "owner": owner_id,
                "acquired_at": now,
                "expires_at": now + lock_ttl_seconds,
            }
        )
        lock_blob = bucket.blob(lock_blob_name)

        try:
            lock_blob.upload_from_string(
                payload,
                content_type="application/json",
                if_generation_match=0,
            )
            logger.info(f"Acquired GCS Chroma write lock for uid='{uid}' (generation={lock_blob.generation}).")
            return lock_blob
        except gcs_exceptions.PreconditionFailed:
            # Lock already exists — inspect whether it has expired (container crash recovery)
            try:
                existing_blob = bucket.blob(lock_blob_name)
                existing_text = existing_blob.download_as_text()
                existing_gen = existing_blob.generation
                existing_data: Dict[str, Any] = json.loads(existing_text)
                expires_at = float(existing_data.get("expires_at", 0))

                if existing_gen is not None and time.time() > expires_at:
                    logger.warning(
                        f"Recovering expired GCS Chroma lock for uid='{uid}' "
                        f"(expired_owner={existing_data.get('owner')!r}, gen={existing_gen})."
                    )
                    recovered_now = time.time()
                    recovered_payload = json.dumps(
                        {
                            "uid": uid,
                            "owner": owner_id,
                            "acquired_at": recovered_now,
                            "expires_at": recovered_now + lock_ttl_seconds,
                        }
                    )
                    existing_blob.upload_from_string(
                        recovered_payload,
                        content_type="application/json",
                        if_generation_match=existing_gen,
                    )
                    logger.info(
                        f"Recovered stale GCS Chroma write lock for uid='{uid}' (generation={existing_blob.generation})."
                    )
                    return existing_blob
            except (gcs_exceptions.NotFound, gcs_exceptions.PreconditionFailed, ValueError):
                # Lock was just released or recovered by another contender; retry immediately
                pass

        if time.monotonic() >= deadline:
            raise LockTimeoutError(
                f"Timed out waiting {acquire_timeout_seconds:.0f}s for ChromaDB write lock for user '{uid}'."
            )

        sleep_for = min(backoff, max(0.05, deadline - time.monotonic())) + random.uniform(0.0, 0.1)
        time.sleep(sleep_for)
        backoff = min(backoff * 1.6, 2.0)


def _release_gcs_write_lock(lock_blob: Optional[storage.Blob], uid: str) -> None:
    if lock_blob is None:
        return
    try:
        if lock_blob.generation is not None:
            lock_blob.delete(if_generation_match=lock_blob.generation)
        else:
            lock_blob.delete()
        logger.info(f"Released GCS Chroma write lock for uid='{uid}'.")
    except (gcs_exceptions.NotFound, gcs_exceptions.PreconditionFailed):
        logger.warning(f"GCS Chroma write lock for uid='{uid}' was already released or superseded.")
    except Exception as e:
        logger.error(f"Unexpected error releasing GCS write lock for uid='{uid}': {e}")


def _push_user_dir_to_gcs(bucket: storage.Bucket, uid: str, user_dir: str) -> None:
    """Flushes ChromaDB state and uploads the local user directory snapshot + new version.json to GCS."""
    release_client_cache(user_dir)
    _checkpoint_sqlite_wal(user_dir)

    prefix = _user_chroma_gcs_prefix(uid)
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

    # 1. Upload all current data files first
    for rel_path, full_path in local_rel_files.items():
        blob = bucket.blob(f"{prefix}{rel_path}")
        blob.upload_from_filename(full_path)

    # 2. Delete remote blobs that were removed locally (e.g., deleted collection HNSW folders)
    existing_blobs = list(bucket.list_blobs(prefix=prefix))
    deleted_count = 0
    for remote_blob in existing_blobs:
        rel_path = remote_blob.name[len(prefix) :]
        if not rel_path or rel_path == VERSION_FILENAME:
            continue
        if rel_path not in local_rel_files:
            remote_blob.delete()
            deleted_count += 1

    # 3. Write & upload version.json LAST so readers only switch after all data files exist in GCS
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
            f"Synced {len(local_rel_files)} file(s) to GCS (deleted {deleted_count} obsolete blob(s)) "
            f"for uid='{uid}' (version={new_version['version']})."
        )
    else:
        # User directory contains no SQLite database; ensure version.json is removed too
        version_blob = bucket.blob(f"{prefix}{VERSION_FILENAME}")
        with contextlib.suppress(gcs_exceptions.NotFound):
            version_blob.delete()


@contextlib.contextmanager
def user_write_lock(uid: str) -> Generator[str, None, None]:
    """Transactional context manager for mutating a user's ChromaDB (`ingest` / `delete_collection`).

    1. Acquires per-user thread lock + distributed GCS lock (`if_generation_match=0` with TTL recovery).
    2. Pulls latest user snapshot from GCS into `/tmp/chroma_db/{uid}`.
    3. Yields `/tmp/chroma_db/{uid}` for ChromaDB mutation.
    4. Flushes SQLite/Rust segments, uploads updated files, deletes removed remote blobs, and bumps `version.json`.
    5. Releases the GCS lock and runs LRU `/tmp` eviction if needed.
    """
    safe_uid = validate_uid(uid)
    user_thread_lock = _get_user_thread_lock(safe_uid)

    with user_thread_lock, _track_active_user(safe_uid):
        bucket_name = get_bucket_name()
        if not bucket_name:
            user_dir = get_user_dir(safe_uid)
            os.makedirs(user_dir, exist_ok=True)
            yield user_dir
            return

        bucket = _get_bucket()
        lock_blob = _acquire_gcs_write_lock(bucket, safe_uid)
        try:
            user_dir = _sync_from_gcs_unlocked(safe_uid)
            os.makedirs(user_dir, exist_ok=True)
            yield user_dir
            _push_user_dir_to_gcs(bucket, safe_uid, user_dir)
        finally:
            _release_gcs_write_lock(lock_blob, safe_uid)
            evict_tmp_cache(exclude_uid=safe_uid)


def archive_upload(uid: str, local_file_path: str, original_filename: str) -> Optional[str]:
    """Uploads a user's raw PDF to `gs://<bucket>/users/{uid}/uploads/` when GCS is configured."""
    bucket_name = get_bucket_name()
    if not bucket_name:
        return None

    safe_uid = validate_uid(uid)
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


_ARCHIVE_PREFIX_RE = re.compile(r"^\d{8}T\d{6}Z_[0-9a-f]{6}_")


def get_upload_user_dir(uid: str) -> str:
    """Returns the validated, isolated temporary PDF upload directory for a specific user."""
    safe_uid = validate_uid(uid)
    upload_root = os.path.abspath(os.getenv("UPLOAD_TMP_ROOT", "/tmp/uploads"))
    user_upload_dir = os.path.abspath(os.path.join(upload_root, safe_uid))
    if (
        os.path.commonpath([upload_root, user_upload_dir]) != upload_root
        or user_upload_dir == upload_root
    ):
        raise ValueError(f"Unsafe resolved upload directory for uid={safe_uid!r}")
    return user_upload_dir


def _list_bucket_blobs(bucket: Any, prefix: str, versions: bool = False) -> List[Any]:
    """Lists blobs under `prefix`, optionally enumerating all live and noncurrent versions."""
    if not versions:
        return list(bucket.list_blobs(prefix=prefix))
    try:
        return list(bucket.list_blobs(prefix=prefix, versions=True))
    except TypeError:
        return list(bucket.list_blobs(prefix=prefix))


def _delete_blob_revision(bucket: Any, blob: Any) -> None:
    """Permanently deletes a specific GCS blob generation (or live blob on unversioned doubles)."""
    generation = getattr(blob, "generation", None)
    with contextlib.suppress(gcs_exceptions.NotFound):
        if hasattr(bucket, "delete_blob"):
            bucket.delete_blob(blob.name, generation=generation)
        else:
            blob.delete()


def get_user_data_inventory(uid: str) -> Dict[str, Any]:
    """Builds a GDPR Art. 15 personal data inventory for `uid` across ChromaDB and GCS."""
    from src.core.chroma import list_collections
    from src.core.security import validate_gcp_region

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
        local_chroma_bytes = _directory_size_bytes(persist_directory)

    local_uploads_bytes = _directory_size_bytes(get_upload_user_dir(safe_uid))
    local_cache_bytes = local_chroma_bytes + local_uploads_bytes

    uploaded_pdfs: List[Dict[str, Any]] = []
    gcs_live_count = 0
    gcs_noncurrent_count = 0
    gcs_total_bytes = 0

    if bucket_name:
        bucket = _get_bucket()
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
    2. Local `/tmp/chroma_db/{uid}` (or local working Chroma directory) after releasing Chroma client handles.
    3. Local `/tmp/uploads/{uid}` temporary upload directory.
    """
    safe_uid = validate_uid(uid)
    user_thread_lock = _get_user_thread_lock(safe_uid)

    def _wipe_local_user_dirs() -> tuple[bool, bool]:
        user_dir = get_user_dir(safe_uid)
        upload_dir = get_upload_user_dir(safe_uid)

        release_client_cache(user_dir)
        had_chroma = os.path.exists(user_dir)
        if had_chroma:
            shutil.rmtree(user_dir, ignore_errors=True)

        had_uploads = os.path.exists(upload_dir)
        if had_uploads:
            shutil.rmtree(upload_dir, ignore_errors=True)

        with _state_lock:
            _user_last_used.pop(safe_uid, None)
        return had_chroma, had_uploads

    with user_thread_lock, _track_active_user(safe_uid):
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

        bucket = _get_bucket()
        user_prefix = f"users/{safe_uid}/"
        locks_prefix = f"users/{safe_uid}/locks/"
        lock_name = _user_lock_gcs_blob_name(safe_uid)
        lock_blob = _acquire_gcs_write_lock(bucket, safe_uid)
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
            _release_gcs_write_lock(lock_blob, safe_uid)
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

