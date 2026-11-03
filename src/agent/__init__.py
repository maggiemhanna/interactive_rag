"""LangGraph Agentic RAG & Interactive Read/Control-Plane service package (`src.agent`).

Submodules:
- `app`: FastAPI `agent-api` service mounting `agent_router`, `query_router`, and `manage_router`.
- `routes`: `agent_router` hosting `POST /agent` and `POST /agent/stream`.
- `schemas`: Pure Pydantic request/response models (`AgentRequest`, `AgentResponse`) and `AgentState`.
- `graph`: Hardened LangGraph `StateGraph` (`input_guard -> model_call <-> tools -> output_guard`).
- `tools`: XML-spotlighted (`<retrieved_chunk>`) and DLP-sanitized Chroma retriever tool builder.
- `memory`: Tenant-namespaced (`{uid}:{thread_id}`) `InMemorySaver` checkpoint store and DSAR purge hooks.
- `service`: LangGraph event deduplication, bounded recursion handling, and SSE frame generators.
- `display`: Rich terminal panel rendering for interactive CLI execution.
"""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.agent.app import main
    from src.agent.routes import agent, agent_router, agent_stream
    from src.agent.schemas import AgentRequest, AgentResponse, AgentState

_EXPORT_MAP: dict[str, str] = {
    "AgentRequest": "src.agent.schemas",
    "AgentResponse": "src.agent.schemas",
    "AgentState": "src.agent.schemas",
    "agent": "src.agent.routes",
    "agent_router": "src.agent.routes",
    "agent_stream": "src.agent.routes",
    "main": "src.agent.app",
}

__all__ = sorted(_EXPORT_MAP.keys())


def __getattr__(name: str) -> Any:
    """Lazily resolves public package exports without eager cross-package import cycles."""
    module_path = _EXPORT_MAP.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_path)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Lists available attributes on `src.agent` for interactive introspection."""
    return sorted(set(globals().keys()) | set(__all__))
