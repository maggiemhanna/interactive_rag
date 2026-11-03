"""Tenant-namespaced in-memory LangGraph conversation checkpointer and DSAR lifecycle helpers.

Maintains a shared `InMemorySaver` keyed by `{uid}:{thread_id}` across warm requests and
provides thread-safe enumeration (`list_user_memory_threads`) and GDPR Art. 17 erasure
(`clear_user_memory`) scoped strictly to a single tenant `uid`.
"""

import threading
from typing import List, Set

from langgraph.checkpoint.memory import InMemorySaver

from src.core.auth import validate_uid
from src.core.logger import setup_logging

logger = setup_logging(name="agent.memory")

# Module-scoped checkpointer persists multi-turn conversation history across warm requests;
# every HTTP request namespaces thread_id as f"{uid}:{request.thread_id}".
MEMORY_SAVER = InMemorySaver()
_memory_lock = threading.RLock()


def _collect_matching_thread_ids(prefix: str) -> Set[str]:
    """Collects all internal `MEMORY_SAVER` thread keys matching `prefix` (`'{uid}:'`).

    Args:
        prefix: Tenant namespace prefix ending with `':'`.

    Returns:
        Set of full `{uid}:{thread_id}` checkpoint keys present across storage, writes, and blobs.
    """
    matched: Set[str] = set()
    for tid in list(getattr(MEMORY_SAVER, "storage", {}).keys()):
        if isinstance(tid, str) and tid.startswith(prefix):
            matched.add(tid)
    for key in list(getattr(MEMORY_SAVER, "writes", {}).keys()):
        if isinstance(key, tuple) and key and isinstance(key[0], str) and key[0].startswith(prefix):
            matched.add(key[0])
    for key in list(getattr(MEMORY_SAVER, "blobs", {}).keys()):
        if isinstance(key, tuple) and key and isinstance(key[0], str) and key[0].startswith(prefix):
            matched.add(key[0])
    return matched


def list_user_memory_threads(uid: str) -> List[str]:
    """Returns sorted user-local thread identifiers stored in `MEMORY_SAVER` for `uid`.

    Args:
        uid: Verified tenant user identifier.

    Returns:
        Sorted list of unprefixed client `thread_id` strings belonging to `uid`.
    """
    safe_uid = validate_uid(uid)
    prefix = f"{safe_uid}:"
    with _memory_lock:
        matched = _collect_matching_thread_ids(prefix)
    return sorted(tid[len(prefix) :] for tid in matched)


def clear_user_memory(uid: str) -> int:
    """Purges all `{uid}:*` conversation checkpoints, writes, and blobs from `MEMORY_SAVER`.

    Args:
        uid: Verified tenant user identifier.

    Returns:
        Number of distinct conversation threads permanently erased for `uid`.
    """
    safe_uid = validate_uid(uid)
    prefix = f"{safe_uid}:"
    with _memory_lock:
        matched = _collect_matching_thread_ids(prefix)
        for tid in matched:
            MEMORY_SAVER.delete_thread(tid)

    if matched:
        logger.info(
            f"Purged {len(matched)} conversation thread(s) from MEMORY_SAVER for uid='{safe_uid}'."
        )
    return len(matched)


__all__ = [
    "MEMORY_SAVER",
    "clear_user_memory",
    "list_user_memory_threads",
]

