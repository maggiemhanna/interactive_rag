"""Per-tenant concurrency locks, distributed GCS generation locks, and LRU `/tmp` eviction.

Coordinates concurrent reads and writes across local worker threads and distributed Cloud Run
container revisions:
- In-process `threading.RLock` and active reference counters pinning `/tmp/chroma_db/{uid}` during reads/SSE.
- Distributed GCS write locks (`users/{uid}/locks/chroma.lock`) using atomic `if_generation_match=0`
  preconditions with automatic TTL crash recovery.
- LRU cache eviction (`evict_tmp_cache`) keeping Cloud Run RAM-disk `/tmp` usage bounded.
"""

import contextlib
import json
import os
import random
import shutil
import threading
import time
from typing import Any, Dict, Generator, List, Optional
import uuid

from google.api_core import exceptions as gcs_exceptions
from google.cloud import storage

from src.core.auth import validate_uid
from src.core.logger import setup_logging
from src.core.storage import gcs_sync
from src.core.storage.paths import (
    directory_size_bytes,
    get_bucket_name,
    get_chroma_root,
    get_user_dir,
    release_client_cache,
    user_lock_gcs_blob_name,
)

logger = setup_logging(name="storage.locks")

_state_lock = threading.Lock()
_user_thread_locks: Dict[str, threading.RLock] = {}
_user_last_used: Dict[str, float] = {}
_user_active_refs: Dict[str, int] = {}


class LockTimeoutError(TimeoutError):
    """Raised when a per-user GCS distributed write lock cannot be acquired within timeout."""


def get_user_thread_lock(uid: str) -> threading.RLock:
    """Returns the re-entrant in-process `RLock` serializing local threads for `uid`.

    Args:
        uid: Verified path-safe user identifier.

    Returns:
        Per-user `threading.RLock` instance.
    """
    with _state_lock:
        lock = _user_thread_locks.get(uid)
        if lock is None:
            lock = threading.RLock()
            _user_thread_locks[uid] = lock
        return lock


def clear_user_last_used(uid: str) -> None:
    """Removes `uid` from LRU last-used tracking after local directory erasure.

    Args:
        uid: Verified path-safe user identifier.
    """
    with _state_lock:
        _user_last_used.pop(uid, None)


@contextlib.contextmanager
def track_active_user(uid: str) -> Generator[None, None, None]:
    """Context manager incrementing active reference counts so `evict_tmp_cache` skips `uid`.

    Args:
        uid: Verified path-safe user identifier actively reading or writing `/tmp`.

    Yields:
        `None` while `uid` is pinned as active.
    """
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


def evict_tmp_cache(exclude_uid: Optional[str] = None) -> List[str]:
    """LRU-evicts inactive `/tmp/chroma_db/{uid}` directories when cache size or RAM disk is tight.

    Executes only when `GCS_BUCKET_NAME` is configured (since GCS holds the canonical durable
    copy) and never evicts a tenant directory that currently has an in-flight read or write.

    Args:
        exclude_uid: Optional user ID to exempt from eviction (e.g., the user currently syncing).

    Returns:
        List of `uid` strings whose local `/tmp` Chroma caches were evicted.
    """
    if not get_bucket_name():
        return []

    root = get_chroma_root()
    if not os.path.exists(root):
        return []

    max_cache_bytes = int(os.getenv("CHROMA_TMP_MAX_BYTES", str(512 * 1024 * 1024)))
    min_free_bytes = int(os.getenv("CHROMA_TMP_MIN_FREE_BYTES", str(256 * 1024 * 1024)))

    def _needs_eviction() -> bool:
        """Checks whether `/tmp` Chroma root exceeds byte quota or free-disk threshold."""
        total_used = directory_size_bytes(root)
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

        cand_lock = get_user_thread_lock(cand_uid)
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


def sync_user_dir(uid: str) -> str:
    """Ensures the user's local ChromaDB directory matches the latest snapshot in GCS.

    Args:
        uid: Verified tenant user identifier.

    Returns:
        Local `persist_directory` path (which will not exist on disk yet for brand-new users).
    """
    safe_uid = validate_uid(uid)
    user_lock = get_user_thread_lock(safe_uid)
    with user_lock, track_active_user(safe_uid):
        return gcs_sync._sync_from_gcs_unlocked(
            safe_uid,
            on_before_download=lambda u: evict_tmp_cache(exclude_uid=u),
        )


@contextlib.contextmanager
def user_read_lock(uid: str) -> Generator[str, None, None]:
    """Context manager synchronizing a user's Chroma snapshot and pinning `/tmp` against eviction.

    Args:
        uid: Verified tenant user identifier.

    Yields:
        Synced local `persist_directory` path safe for concurrent read queries or SSE streams.
    """
    safe_uid = validate_uid(uid)
    user_lock = get_user_thread_lock(safe_uid)
    with track_active_user(safe_uid):
        with user_lock:
            user_dir = gcs_sync._sync_from_gcs_unlocked(
                safe_uid,
                on_before_download=lambda u: evict_tmp_cache(exclude_uid=u),
            )
        yield user_dir


def acquire_gcs_write_lock(bucket: storage.Bucket, uid: str) -> storage.Blob:
    """Acquires `users/{uid}/locks/chroma.lock` via atomic `if_generation_match=0` with TTL recovery.

    Args:
        bucket: Target GCS `Bucket` hosting lock blobs.
        uid: Verified path-safe user identifier.

    Returns:
        Acquired `storage.Blob` carrying the active lock generation number.

    Raises:
        LockTimeoutError: If another live writer holds the lock beyond `CHROMA_LOCK_TIMEOUT_SECONDS`.
    """
    lock_blob_name = user_lock_gcs_blob_name(uid)
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


def release_gcs_write_lock(lock_blob: Optional[storage.Blob], uid: str) -> None:
    """Releases a previously acquired GCS write lock using `if_generation_match` safety.

    Args:
        lock_blob: Lock `storage.Blob` returned by `acquire_gcs_write_lock`, or `None`.
        uid: Verified path-safe user identifier.
    """
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


@contextlib.contextmanager
def user_write_lock(uid: str) -> Generator[str, None, None]:
    """Transactional context manager for mutating a user's ChromaDB (`ingest` / `delete_collection`).

    1. Acquires per-user thread lock + distributed GCS lock (`if_generation_match=0` with TTL recovery).
    2. Pulls latest user snapshot from GCS into `/tmp/chroma_db/{uid}`.
    3. Yields `/tmp/chroma_db/{uid}` for ChromaDB mutation.
    4. Flushes SQLite/Rust segments, uploads updated files, deletes removed remote blobs, and bumps `version.json`.
    5. Releases the GCS lock and runs LRU `/tmp` eviction if needed.

    Args:
        uid: Verified tenant user identifier.

    Yields:
        Writable local `persist_directory` path.
    """
    safe_uid = validate_uid(uid)
    user_thread_lock = get_user_thread_lock(safe_uid)

    with user_thread_lock, track_active_user(safe_uid):
        bucket_name = get_bucket_name()
        if not bucket_name:
            user_dir = get_user_dir(safe_uid)
            os.makedirs(user_dir, exist_ok=True)
            yield user_dir
            return

        bucket = gcs_sync._get_bucket()
        lock_blob = acquire_gcs_write_lock(bucket, safe_uid)
        try:
            user_dir = gcs_sync._sync_from_gcs_unlocked(
                safe_uid,
                on_before_download=lambda u: evict_tmp_cache(exclude_uid=u),
            )
            os.makedirs(user_dir, exist_ok=True)
            yield user_dir
            gcs_sync._push_user_dir_to_gcs(bucket, safe_uid, user_dir)
        finally:
            release_gcs_write_lock(lock_blob, safe_uid)
            evict_tmp_cache(exclude_uid=safe_uid)


__all__ = [
    "LockTimeoutError",
    "acquire_gcs_write_lock",
    "clear_user_last_used",
    "evict_tmp_cache",
    "get_user_thread_lock",
    "release_gcs_write_lock",
    "sync_user_dir",
    "track_active_user",
    "user_read_lock",
    "user_write_lock",
]
