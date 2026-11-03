"""LangGraph event stream deduplication, bounded recursion handling, and SSE payload generators.

Provides both internal Python generators (`stream_events`) and FastAPI `text/event-stream`
generators (`stream_sse`) that pin per-user `/tmp` Chroma caches, enforce `AGENT_RECURSION_LIMIT`,
emit EU AI Act Art. 50 `compliance` frames, and publish PII-free audit events.
"""

import json
from typing import Any, Dict, Generator, Optional

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError

from src.agent.display import print_event, print_header
from src.agent.graph import build_agent, extract_message_text
from src.agent.memory import MEMORY_SAVER
from src.agent.schemas import AgentRequest
from src.agent.tools import get_retriever_tool
from src.core.ai_security.armor import SecurityScreeningUnavailableError
from src.core.ai_security.safety import (
    INTERNAL_ERROR_MESSAGE,
    RECURSION_LIMIT_MESSAGE,
    SCREENING_UNAVAILABLE_MESSAGE,
)
from src.core.compliance.audit import emit_audit_event
from src.core.compliance.provenance import build_compliance_metadata
from src.core.config import (
    get_agent_recursion_limit,
    get_dev_user_id,
    is_agent_console_trace_enabled,
    is_cloud_run,
)
from src.core.context import scrub_query_fields
from src.core.logger import setup_logging
from src.core.observability.state import record_ttft
from src.core.observability.telemetry import emit_telemetry_event
from src.core.storage import has_sqlite, user_read_lock

logger = setup_logging(name="agent.service")

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _sse_frame(payload: Dict[str, Any]) -> str:
    """Formats a dictionary payload as a Server-Sent Events (`data: <json>\\n\\n`) frame."""
    return f"data: {json.dumps(payload)}\n\n"


def emit_agent_query_events(
    *,
    uid: str,
    request: AgentRequest,
    stream: bool,
    final_answer: str,
    compliance_meta: Dict[str, Any],
) -> None:
    """Publishes the canonical `'agent.query'` compliance audit and AI telemetry records for a completed turn.

    Args:
        uid: Verified tenant user identifier.
        request: Validated `AgentRequest` payload.
        stream: True when executed via SSE streaming, False for synchronous JSON.
        final_answer: Sanitized final assistant response text.
        compliance_meta: Serialized `ComplianceMetadata` dictionary.
    """
    guardrail_actions = [
        f"{action['filter']}:{action['action']}"
        for action in compliance_meta.get("guardrail_actions", [])
    ]
    emit_audit_event(
        "agent.query",
        uid=uid,
        collection_name=request.collection_name,
        model_name=request.model_name,
        embedding_model=request.embedding_model,
        stream=stream,
        query=request.query,
        answer_chars=len(final_answer),
        grounded=compliance_meta["grounded"],
        citation_count=compliance_meta["citation_count"],
        pii_filtered=compliance_meta["pii_filtered"],
        guardrail_actions=guardrail_actions,
    )
    emit_telemetry_event(
        "agent.query",
        uid=uid,
        collection_name=request.collection_name,
        model_name=request.model_name,
        embedding_model=request.embedding_model,
        stream=stream,
        query=request.query,
        answer_chars=len(final_answer),
        grounded=compliance_meta["grounded"],
        citation_count=compliance_meta["citation_count"],
    )


def stream_events(
    query: str,
    collection_name: str,
    thread_id: str = "1",
    model_name: Optional[str] = None,
    embedding_model: Optional[str] = None,
    persist_directory: str = "chroma_db",
    verbose_console: bool = False,
) -> Generator[Dict[str, Any], None, None]:
    """Executes the compiled LangGraph agent and yields deduplicated message event dictionaries.

    Catches `GraphRecursionError` when tool-calling exceeds `AGENT_RECURSION_LIMIT` and
    yields a polite `RECURSION_LIMIT_MESSAGE` fallback turn.

    Args:
        query: Validated user question to answer against `collection_name`.
        collection_name: Validated Chroma collection identifier.
        thread_id: Tenant-namespaced (`{uid}:{thread_id}`) checkpoint thread identifier.
        model_name: Optional approved Vertex AI generative LLM model identifier.
        embedding_model: Optional approved Vertex AI embedding model identifier.
        persist_directory: Local filesystem path of the tenant's ChromaDB directory.
        verbose_console: Whether to render Rich message panels to stdout in local dev.

    Yields:
        Dictionaries of the form `{'event': 'message', 'message': {...}}`.
    """
    show_console = not is_cloud_run() and (
        verbose_console or is_agent_console_trace_enabled()
    )
    retriever_tool = get_retriever_tool(
        collection_name=collection_name,
        persist_directory=persist_directory,
        embedding_model=embedding_model,
    )
    agent = build_agent(retriever_tool, model_name=model_name)

    recursion_limit = get_agent_recursion_limit()
    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
    }
    emitted_keys = set()

    try:
        for event in agent.stream(
            {"messages": [HumanMessage(content=query)]}, config, stream_mode="values"
        ):
            messages = event.get("messages", [])
            if not messages:
                continue

            last_msg = messages[-1]
            msg_id = getattr(last_msg, "id", None)
            dedup_key = (
                msg_id
                if msg_id is not None
                else (
                    getattr(last_msg, "type", "unknown"),
                    extract_message_text(getattr(last_msg, "content", "")),
                    repr(getattr(last_msg, "tool_calls", None)),
                )
            )
            if dedup_key in emitted_keys:
                continue
            emitted_keys.add(dedup_key)

            if show_console:
                print_event(last_msg)

            msg_dict: Dict[str, Any] = {
                "type": getattr(last_msg, "type", "unknown"),
                "content": extract_message_text(getattr(last_msg, "content", "")),
            }
            if getattr(last_msg, "tool_calls", None):
                msg_dict["tool_calls"] = last_msg.tool_calls
            if getattr(last_msg, "name", None):
                msg_dict["name"] = last_msg.name

            yield {"event": "message", "message": msg_dict}

    except GraphRecursionError:
        logger.warning(
            f"Agent reached recursion_limit={recursion_limit} for thread_id='{thread_id}'."
        )
        emit_audit_event(
            "agent.recursion_limit",
            severity="WARNING",
            status="truncated",
            collection_name=collection_name,
            recursion_limit=recursion_limit,
            query=query,
        )
        if show_console:
            print_event(AIMessage(content=RECURSION_LIMIT_MESSAGE))
        yield {
            "event": "message",
            "message": {
                "type": "ai",
                "content": RECURSION_LIMIT_MESSAGE,
            },
        }


def stream_sse(
    uid: str,
    request: AgentRequest,
    scoped_thread_id: str,
) -> Generator[str, None, None]:
    """Yields Server-Sent Events (`data: ...\n\n`) frames while pinning the user's Chroma cache.

    Emits each message event frame followed by a terminal `event="compliance"` provenance frame
    and `data: [DONE]`, or a sanitized `event="error"` frame if screening or execution fails.

    Args:
        uid: Verified tenant user identifier.
        request: Validated `AgentRequest` payload.
        scoped_thread_id: Tenant-namespaced `{uid}:{request.thread_id}` checkpoint key.

    Yields:
        SSE-formatted UTF-8 event strings (`data: <json>\n\n`).
    """
    final_answer = ""
    streamed_messages = []
    try:
        with user_read_lock(uid) as stream_persist_dir:
            for evt in stream_events(
                query=request.query,
                collection_name=request.collection_name,
                thread_id=scoped_thread_id,
                model_name=request.model_name,
                embedding_model=request.embedding_model,
                persist_directory=stream_persist_dir,
            ):
                msg = evt["message"]
                streamed_messages.append(msg)
                if msg.get("type") != "human":
                    record_ttft()
                if msg.get("type") == "ai" and msg.get("content"):
                    final_answer = str(msg["content"])
                yield _sse_frame(evt)

        compliance_meta = build_compliance_metadata(
            model_name=request.model_name,
            answer=final_answer,
            messages=streamed_messages,
            scoped_thread_id=scoped_thread_id,
            checkpointer=MEMORY_SAVER,
        ).model_dump()
        yield _sse_frame(
            {
                "event": "compliance",
                "compliance": compliance_meta,
                "message": {"type": "compliance", "content": ""},
            }
        )

        emit_agent_query_events(
            uid=uid,
            request=request,
            stream=True,
            final_answer=final_answer,
            compliance_meta=compliance_meta,
        )
        yield "data: [DONE]\n\n"
    except SecurityScreeningUnavailableError as e:
        logger.error(
            f"Fail-closed screening unavailable during SSE stream for uid='{uid}' "
            f"(thread_id='{scoped_thread_id}', reason={e.reason})."
        )
        emit_audit_event(
            "agent.query",
            severity="ERROR",
            status="screening_unavailable",
            reason=e.reason,
            http_status=503,
            uid=uid,
            collection_name=request.collection_name,
            model_name=request.model_name,
            embedding_model=request.embedding_model,
            stream=True,
            query=request.query,
        )
        yield _sse_frame(
            {
                "event": "error",
                "http_status": 503,
                "error": SCREENING_UNAVAILABLE_MESSAGE,
                "message": {"type": "ai", "content": SCREENING_UNAVAILABLE_MESSAGE},
            }
        )
        yield "data: [DONE]\n\n"
    except Exception:
        logger.exception(
            f"Unexpected error streaming Agentic RAG SSE for uid='{uid}' (thread_id='{scoped_thread_id}')."
        )
        emit_audit_event(
            "agent.query",
            severity="ERROR",
            status="error",
            http_status=500,
            uid=uid,
            collection_name=request.collection_name,
            model_name=request.model_name,
            embedding_model=request.embedding_model,
            stream=True,
            query=request.query,
        )
        yield _sse_frame(
            {
                "event": "error",
                "error": INTERNAL_ERROR_MESSAGE,
                "message": {"type": "ai", "content": INTERNAL_ERROR_MESSAGE},
            }
        )
        yield "data: [DONE]\n\n"


def run_interactive(
    collection_name: str,
    query: str = "What is ARM Holdings?",
) -> None:
    """Runs an interactive local CLI query and renders Rich message panels to the terminal.

    Args:
        collection_name: Target Chroma collection identifier.
        query: Sample or custom question to execute against the collection.
    """
    print_header("Agentic RAG Assistant Initialized", "magenta")
    scrubbed = scrub_query_fields({"query": query})
    logger.info(
        f"Running interactive agent on collection '{collection_name}' "
        f"(query_sha256='{scrubbed['query_sha256']}', query_chars={scrubbed['query_chars']})."
    )
    cli_uid = get_dev_user_id()
    with user_read_lock(cli_uid) as resolved_dir:
        target_dir = resolved_dir if has_sqlite(resolved_dir) else "chroma_db"
        for _ in stream_events(
            query=query,
            collection_name=collection_name,
            thread_id=f"{cli_uid}:1",
            persist_directory=target_dir,
            verbose_console=True,
        ):
            pass


__all__ = [
    "SSE_HEADERS",
    "emit_agent_query_events",
    "run_interactive",
    "stream_events",
    "stream_sse",
]

