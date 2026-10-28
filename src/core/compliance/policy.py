"""Per-user sliding-window rate limiters and Chroma collection count quotas (Pillar 4: Policy Enforcement).

Enforces multi-tenant fairness, cost bounds, and abuse prevention across ingestion and query endpoints:
- **Sliding-Window Rate Limits**: Per-UID 60-second monotonic sliding windows enforcing
  `RATE_LIMIT_INGEST_PER_MINUTE` (default `10`) and `RATE_LIMIT_QUERY_PER_MINUTE` (default `30`),
  returning `HTTP 429` with `Retry-After` headers and `policy.rate_limit_exceeded` audit events.
- **Collection Count Quota**: Enforces `MAX_COLLECTIONS_PER_USER` (default `20`) when creating a
  brand-new collection (`HTTP 403` + `policy.collection_quota_exceeded`), while always permitting
  document appends into collections the user already owns.
"""

from collections import deque
import math
import threading
import time
from typing import Callable, Deque, Dict, Optional, Tuple

from fastapi import HTTPException

from src.core.compliance.audit import emit_audit_event
from src.core.config import (
    get_max_collections_per_user,
    get_rate_limit_ingest_per_minute,
    get_rate_limit_query_per_minute,
)
from src.core.logger import setup_logging
from src.core.storage.chroma import list_collections
from src.core.storage.locks import user_read_lock

logger = setup_logging(name="compliance.policy")

DEFAULT_WINDOW_SECONDS = 60.0


class SlidingWindowRateLimiter:
    """Thread-safe in-memory per-tenant sliding-window rate limiter."""

    def __init__(
        self,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initializes the sliding-window rate limiter.

        Args:
            window_seconds: Sliding time window duration in seconds (defaults to `60.0`).
            clock: Monotonic clock callable returning seconds (injectable for deterministic tests).
        """
        self._window_seconds = float(window_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: Dict[str, Deque[float]] = {}

    def check_and_record(self, uid: str, limit: int) -> Tuple[bool, int]:
        """Atomically checks whether `uid` is within `limit` and records the request if allowed.

        Rejected requests are not appended to the sliding window so sustained bursts do not
        extend the lockout window indefinitely after a client backs off.

        Args:
            uid: Authenticated tenant user identifier.
            limit: Maximum permitted requests within `window_seconds`.

        Returns:
            Tuple `(allowed, retry_after_seconds)` where `retry_after_seconds` is `0` when
            `allowed=True`, or a positive integer (`>= 1`) when `allowed=False`.
        """
        now = self._clock()
        cutoff = now - self._window_seconds

        with self._lock:
            bucket = self._buckets.get(uid)
            if bucket is None:
                bucket = deque()
                self._buckets[uid] = bucket

            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= limit:
                oldest = bucket[0]
                remaining = self._window_seconds - (now - oldest)
                retry_after = max(1, int(math.ceil(remaining)))
                return False, retry_after

            bucket.append(now)
            return True, 0

    def reset(self, uid: Optional[str] = None) -> None:
        """Clears recorded timestamps for a single `uid` or for all tenants when `uid` is None.

        Args:
            uid: Optional tenant user identifier whose sliding window should be cleared.
        """
        with self._lock:
            if uid is None:
                self._buckets.clear()
            else:
                self._buckets.pop(uid, None)


_INGEST_RATE_LIMITER = SlidingWindowRateLimiter(window_seconds=DEFAULT_WINDOW_SECONDS)
_QUERY_RATE_LIMITER = SlidingWindowRateLimiter(window_seconds=DEFAULT_WINDOW_SECONDS)


def reset_rate_limiters(uid: Optional[str] = None) -> None:
    """Resets both ingestion and query sliding-window rate limiters (used in test teardown).

    Args:
        uid: Optional tenant user identifier to clear; clears all tenants if omitted.
    """
    _INGEST_RATE_LIMITER.reset(uid=uid)
    _QUERY_RATE_LIMITER.reset(uid=uid)


def enforce_ingest_rate_limit(uid: str) -> None:
    """Enforces `RATE_LIMIT_INGEST_PER_MINUTE` for `uid`, raising HTTP 429 with `Retry-After` if exceeded.

    Args:
        uid: Verified tenant user identifier.

    Raises:
        HTTPException: HTTP 429 (`Too Many Requests`) with `Retry-After` header when rate-limited.
    """
    limit = get_rate_limit_ingest_per_minute()
    allowed, retry_after = _INGEST_RATE_LIMITER.check_and_record(uid=uid, limit=limit)
    if allowed:
        return

    logger.warning(
        f"Ingest rate limit exceeded for uid='{uid}' (limit={limit}/min, retry_after={retry_after}s)."
    )
    emit_audit_event(
        "policy.rate_limit_exceeded",
        severity="WARNING",
        status="blocked",
        uid=uid,
        scope="ingest",
        limit_per_minute=limit,
        retry_after_seconds=retry_after,
    )
    raise HTTPException(
        status_code=429,
        detail=(
            f"Ingestion rate limit exceeded ({limit} requests per minute). "
            f"Please retry after {retry_after} seconds."
        ),
        headers={"Retry-After": str(retry_after)},
    )


def enforce_query_rate_limit(uid: str) -> None:
    """Enforces `RATE_LIMIT_QUERY_PER_MINUTE` for `uid`, raising HTTP 429 with `Retry-After` if exceeded.

    Args:
        uid: Verified tenant user identifier.

    Raises:
        HTTPException: HTTP 429 (`Too Many Requests`) with `Retry-After` header when rate-limited.
    """
    limit = get_rate_limit_query_per_minute()
    allowed, retry_after = _QUERY_RATE_LIMITER.check_and_record(uid=uid, limit=limit)
    if allowed:
        return

    logger.warning(
        f"Query rate limit exceeded for uid='{uid}' (limit={limit}/min, retry_after={retry_after}s)."
    )
    emit_audit_event(
        "policy.rate_limit_exceeded",
        severity="WARNING",
        status="blocked",
        uid=uid,
        scope="query",
        limit_per_minute=limit,
        retry_after_seconds=retry_after,
    )
    raise HTTPException(
        status_code=429,
        detail=(
            f"Query rate limit exceeded ({limit} requests per minute). "
            f"Please retry after {retry_after} seconds."
        ),
        headers={"Retry-After": str(retry_after)},
    )


def enforce_collection_quota(
    uid: str,
    collection_name: str,
    persist_directory: Optional[str] = None,
) -> None:
    """Enforces `MAX_COLLECTIONS_PER_USER` when creating a new collection while allowing appends to existing ones.

    Args:
        uid: Verified tenant user identifier.
        collection_name: Validated target ChromaDB collection name.
        persist_directory: Optional already-locked tenant Chroma directory (if omitted, acquires `user_read_lock`).

    Raises:
        HTTPException: HTTP 403 (`Forbidden`) if `collection_name` does not yet exist and `uid` already
            owns `>= MAX_COLLECTIONS_PER_USER` collections.
    """
    max_collections = get_max_collections_per_user()
    if persist_directory is not None:
        inventory = list_collections(persist_directory)
    else:
        with user_read_lock(uid) as synced_dir:
            inventory = list_collections(synced_dir)

    existing = set(inventory.get("collections", []) if isinstance(inventory, dict) else [])
    if collection_name in existing:
        return

    if len(existing) >= max_collections:
        logger.warning(
            f"Collection quota exceeded for uid='{uid}' creating '{collection_name}' "
            f"(current={len(existing)}, max={max_collections})."
        )
        emit_audit_event(
            "policy.collection_quota_exceeded",
            severity="WARNING",
            status="blocked",
            uid=uid,
            collection_name=collection_name,
            current_collections=len(existing),
            max_collections=max_collections,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"Maximum collection limit ({max_collections}) reached. "
                "Delete an unused collection or upload into an existing collection."
            ),
        )


__all__ = [
    "DEFAULT_WINDOW_SECONDS",
    "SlidingWindowRateLimiter",
    "enforce_collection_quota",
    "enforce_ingest_rate_limit",
    "enforce_query_rate_limit",
    "reset_rate_limiters",
]
