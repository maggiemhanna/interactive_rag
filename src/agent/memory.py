"""Tenant-namespaced in-memory LangGraph conversation checkpointer (`src.agent.memory`).

Maintains a shared `InMemorySaver` (`MEMORY_SAVER`) keyed by `{uid}:{thread_id}` across
warm requests.
"""

from langgraph.checkpoint.memory import InMemorySaver

# Module-scoped checkpointer persists multi-turn conversation history across warm requests;
# every HTTP request namespaces thread_id as f"{uid}:{request.thread_id}".
MEMORY_SAVER = InMemorySaver()

__all__ = ["MEMORY_SAVER"]




