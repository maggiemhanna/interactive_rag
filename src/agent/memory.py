"""Tenant-namespaced in-memory LangGraph conversation checkpointer (`src.agent.memory`).

Maintains a shared `TenantMemorySaver` (`MEMORY_SAVER`) keyed by `{uid}:{thread_id}` across
warm requests with:
- Cross-instance `workspace_id` invalidation: detects when a tenant's workspace was erased
  (or recreated after DSAR Art. 17 erasure) on another Cloud Run instance and immediately
  purges stale `{uid}:*` RAM checkpoints so erased history can never resurrect.
- Bounded idle TTL eviction (`MEMORY_TTL_SECONDS`, default 30 minutes) so inactive threads
  are automatically evicted from container memory.
"""

import os
import threading
import time
from typing import Any, Callable, Dict, Optional

from langgraph.checkpoint.memory import InMemorySaver

from src.core.config import get_memory_ttl_seconds
from src.core.logger import setup_logging
from src.core.storage.paths import read_local_workspace_id

logger = setup_logging(name="agent.memory")


class TenantMemorySaver(InMemorySaver):
    """Thread-safe `InMemorySaver` with per-tenant workspace invalidation and idle TTL eviction."""

    def __init__(
        self,
        *args: Any,
        clock: Callable[[], float] = time.monotonic,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._lock = threading.RLock()
        self._clock = clock
        self._thread_last_used: Dict[str, float] = {}
        self._user_workspaces: Dict[str, str] = {}

    def get_tuple(self, config: Any) -> Any:
        """Returns the checkpoint tuple for `thread_id`, evicting idle expired threads first."""
        self.evict_expired()
        thread_id = (config or {}).get("configurable", {}).get("thread_id")
        with self._lock:
            if not isinstance(thread_id, str) or thread_id not in self.storage:
                return None
            result = super().get_tuple(config)
            if result is not None:
                self._thread_last_used[thread_id] = self._clock()
            return result

    def put(
        self,
        config: Any,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any,
    ) -> Any:
        """Saves a checkpoint and updates the thread's idle-TTL timestamp."""
        self.evict_expired()
        thread_id = (config or {}).get("configurable", {}).get("thread_id")
        with self._lock:
            result = super().put(config, checkpoint, metadata, new_versions)
            if isinstance(thread_id, str) and thread_id:
                self._thread_last_used[thread_id] = self._clock()
            return result

    def delete_thread(self, thread_id: str) -> None:
        """Deletes all checkpoints, writes, blobs, and TTL metadata for `thread_id`."""
        with self._lock:
            super().delete_thread(thread_id)
            self._thread_last_used.pop(thread_id, None)

    def evict_expired(self, ttl_seconds: Optional[float] = None) -> int:
        """Evicts in-memory threads idle for at least `ttl_seconds` (`MEMORY_TTL_SECONDS`).

        Args:
            ttl_seconds: Optional TTL override in seconds (`<= 0` disables eviction).

        Returns:
            Number of expired conversation threads evicted from RAM.
        """
        ttl = float(get_memory_ttl_seconds()) if ttl_seconds is None else float(ttl_seconds)
        if ttl <= 0:
            return 0
        now = self._clock()
        with self._lock:
            expired = [
                tid
                for tid, last_used in list(self._thread_last_used.items())
                if (now - last_used) >= ttl
            ]
            for tid in expired:
                super().delete_thread(tid)
                self._thread_last_used.pop(tid, None)
        if expired:
            logger.info(
                f"Evicted {len(expired)} idle conversation thread(s) after {ttl:.0f}s TTL."
            )
        return len(expired)

    def clear_user_threads(self, uid: str) -> int:
        """Purges all `{uid}:*` checkpoints, writes, blobs, and TTL metadata for `uid`."""
        prefix = f"{uid}:"
        with self._lock:
            matched = {
                tid
                for tid in list(self.storage.keys())
                if isinstance(tid, str) and tid.startswith(prefix)
            }
            matched.update(
                k[0]
                for k in list(self.writes.keys())
                if isinstance(k, tuple) and k and isinstance(k[0], str) and k[0].startswith(prefix)
            )
            matched.update(
                k[0]
                for k in list(self.blobs.keys())
                if isinstance(k, tuple) and k and isinstance(k[0], str) and k[0].startswith(prefix)
            )
            matched.update(
                tid
                for tid in list(self._thread_last_used.keys())
                if isinstance(tid, str) and tid.startswith(prefix)
            )
            for tid in matched:
                self.delete_thread(tid)
            return len(matched)

    def clear_user_workspace(self, uid: str) -> None:
        """Removes the cached `workspace_id` binding for `uid`."""
        with self._lock:
            self._user_workspaces.pop(uid, None)

    def sync_user_workspace(self, uid: str, persist_directory: str) -> int:
        """Invalidates `{uid}:*` RAM threads if `uid`'s synced workspace was erased or replaced.

        Args:
            uid: Verified tenant user identifier.
            persist_directory: Synced local tenant directory yielded by `user_read_lock(uid)`.

        Returns:
            Number of stale conversation threads purged for `uid`.
        """
        self.evict_expired()
        has_workspace = os.path.exists(persist_directory)
        current_ws = read_local_workspace_id(persist_directory) if has_workspace else None

        with self._lock:
            prev_ws = self._user_workspaces.get(uid)
            workspace_changed = (not has_workspace) or (
                prev_ws is not None and current_ws != prev_ws
            )
            purged = self.clear_user_threads(uid) if workspace_changed else 0
            if current_ws is not None:
                self._user_workspaces[uid] = current_ws
            elif not has_workspace:
                self._user_workspaces.pop(uid, None)

        if purged:
            logger.info(
                f"Invalidated {purged} stale conversation thread(s) for uid='{uid}' after workspace reset."
            )
        return purged

    def reset(self) -> None:
        """Clears all checkpoints, writes, blobs, TTL timestamps, and workspace bindings."""
        with self._lock:
            self.storage.clear()
            self.writes.clear()
            self.blobs.clear()
            self._thread_last_used.clear()
            self._user_workspaces.clear()


# Module-scoped checkpointer persists multi-turn conversation history across warm requests;
# every HTTP request namespaces thread_id as f"{uid}:{request.thread_id}".
MEMORY_SAVER = TenantMemorySaver()

__all__ = ["MEMORY_SAVER", "TenantMemorySaver"]




