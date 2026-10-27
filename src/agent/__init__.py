"""LangGraph Agentic RAG & SSE Streaming service package (`src.agent`).

Submodules:
- `app`: FastAPI `agent-api` service exposing `POST /agent` and `POST /agent/stream`.
- `schemas`: Pure Pydantic request/response models (`AgentRequest`, `AgentResponse`) and `AgentState`.
- `graph`: Hardened LangGraph `StateGraph` (`input_guard -> model_call <-> tools -> output_guard`).
- `tools`: XML-spotlighted (`<retrieved_chunk>`) and DLP-sanitized Chroma retriever tool builder.
- `memory`: Tenant-namespaced (`{uid}:{thread_id}`) `InMemorySaver` checkpoint store and DSAR purge hooks.
- `stream`: LangGraph event deduplication, bounded recursion handling, and SSE frame generators.
- `display`: Rich terminal panel rendering for interactive CLI execution.
"""
