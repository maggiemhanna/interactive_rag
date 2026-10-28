"""LangGraph event stream deduplication, bounded recursion handling, and SSE payload generators.

Provides both internal Python generators (`stream_events`) and FastAPI `text/event-stream`
generators (`stream_sse`) that pin per-user `/tmp` Chroma caches, enforce `AGENT_RECURSION_LIMIT`,
emit EU AI Act Art. 50 `compliance` frames, and publish PII-free audit events.
"""

import json
import os
from typing import Any, Dict, Generator, Optional

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError

from src.agent.display import print_event, print_header
from src.agent.graph import build_agent
from src.agent.memory import MEMORY_SAVER
from src.agent.schemas import AgentRequest
from src.agent.tools import get_retriever_tool
from src.core.ai_security.armor import SecurityScreeningUnavailableError
from src.core.ai_security.guardrails import get_bounded_recursion_limit
from src.core.ai_security.safety import (
    INTERNAL_ERROR_MESSAGE,
    RECURSION_LIMIT_MESSAGE,
    SCREENING_UNAVAILABLE_MESSAGE,
)
from src.core.compliance.audit import emit_audit_event, hash_query
from src.core.compliance.provenance import build_compliance_metadata
from src.core.config import is_cloud_run
from src.core.logger import setup_logging
from src.core.storage import has_sqlite, user_read_lock

logger = setup_logging(name="agent.stream")

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def extract_text(content: Any) -> str:
    """Extracts plain text from a string or multimodal list `BaseMessage.content` payload.

    Args:
        content: Raw `BaseMessage.content` value.

    Returns:
        Normalized plain-text string.
    """
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict) and "text" in part:
                text_parts.append(part["text"])
        return "\n".join(text_parts)
    return str(content)


def _should_print_console(verbose_console: Optional[bool], is_cli_mode: bool) -> bool:
    """Determines whether Rich message panels should print to stdout.

    Never prints interactive Rich panels inside Cloud Run containers so Cloud Logging
    remains clean and free of raw prompt/document text.

    Args:
        verbose_console: Explicit caller override (`True` or `False`), or `None` for auto-detect.
        is_cli_mode: True when invoked directly from interactive Python/CLI mode.

    Returns:
        True if Rich message trace panels should be rendered to stdout.
    """
    if is_cloud_run():
        return False
    if verbose_console is not None:
        return verbose_console
    if os.getenv("AGENT_CONSOLE_TRACE", "").strip().lower() in {"1", "true", "yes"}:
        return True
    return is_cli_mode


def _stream_events(
    query: str,
    persist_directory: str,
    collection_name: str,
    thread_id: str,
    model_name: Optional[str] = None,
    embedding_model: Optional[str] = None,
    show_console: bool = False,
) -> Generator[Dict[str, Any], None, None]:
    """Executes the compiled LangGraph agent and yields deduplicated message event dicts.

    Catches `GraphRecursionError` when tool-calling exceeds `AGENT_RECURSION_LIMIT` and
    yields a polite `RECURSION_LIMIT_MESSAGE` fallback turn.

    Args:
        query: Validated user question.
        persist_directory: Local filesystem path of the tenant's ChromaDB directory.
        collection_name: Validated Chroma collection identifier.
        thread_id: Tenant-namespaced (`{uid}:{thread_id}`) checkpoint thread identifier.
        model_name: Optional approved Vertex AI generative LLM model identifier.
        embedding_model: Optional approved Vertex AI embedding model identifier.
        show_console: Whether to render Rich message panels to stdout.

    Yields:
        Dictionaries of the form `{'event': 'message', 'message': {...}, 'raw_message': BaseMessage}`.
    """
    retriever_tool = get_retriever_tool(
        collection_name=collection_name,
        persist_directory=persist_directory,
        embedding_model=embedding_model,
    )
    agent = build_agent(retriever_tool, model_name=model_name)

    recursion_limit = get_bounded_recursion_limit()
    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
    }
    input_messages = [HumanMessage(content=query)]
    emitted_keys = set()

    try:
        for event in agent.stream(
            {"messages": input_messages}, config, stream_mode="values"
        ):
            messages = event.get("messages", [])
            if messages:
                last_msg = messages[-1]
                msg_id = getattr(last_msg, "id", None)
                dedup_key = (
                    msg_id
                    if msg_id is not None
                    else (
                        getattr(last_msg, "type", "unknown"),
                        extract_text(getattr(last_msg, "content", "")),
                        repr(getattr(last_msg, "tool_calls", None)),
                    )
                )
                if dedup_key in emitted_keys:
                    continue
                emitted_keys.add(dedup_key)

                if show_console:
                    print_event(last_msg)

                msg_dict = {
                    "type": getattr(last_msg, "type", "unknown"),
                    "content": extract_text(getattr(last_msg, "content", "")),
                }
                if getattr(last_msg, "tool_calls", None):
                    msg_dict["tool_calls"] = last_msg.tool_calls
                if getattr(last_msg, "name", None):
                    msg_dict["name"] = last_msg.name

                yield {
                    "event": "message",
                    "message": msg_dict,
                    "raw_message": last_msg,
                }
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
            query_sha256=hash_query(query),
            query_chars=len(query),
        )
        fallback_msg = AIMessage(content=RECURSION_LIMIT_MESSAGE)
        if show_console:
            print_event(fallback_msg)
        yield {
            "event": "message",
            "message": {
                "type": "ai",
                "content": RECURSION_LIMIT_MESSAGE,
            },
            "raw_message": fallback_msg,
        }


def stream_events(
    query: str,
    collection_name: str,
    thread_id: str = "1",
    model_name: Optional[str] = None,
    embedding_model: Optional[str] = None,
    persist_directory: Optional[str] = None,
    verbose_console: Optional[bool] = None,
) -> Generator[Dict[str, Any], None, None]:
    """Streams LangGraph message events, resolving per-user Chroma storage when called from CLI.

    Args:
        query: User question to answer against `collection_name`.
        collection_name: Target Chroma collection identifier.
        thread_id: Session thread identifier (already `{uid}:`-scoped when `persist_directory` is passed).
        model_name: Optional approved Vertex AI generative LLM model identifier.
        embedding_model: Optional approved Vertex AI embedding model identifier.
        persist_directory: Optional pre-locked tenant ChromaDB directory (used by HTTP handlers).
        verbose_console: Optional flag controlling Rich terminal panel output.

    Yields:
        Deduplicated LangGraph message event dictionaries.
    """
    show_console = _should_print_console(
        verbose_console=verbose_console,
        is_cli_mode=(persist_directory is None),
    )
    if persist_directory is not None:
        yield from _stream_events(
            query=query,
            persist_directory=persist_directory,
            collection_name=collection_name,
            thread_id=thread_id,
            model_name=model_name,
            embedding_model=embedding_model,
            show_console=show_console,
        )
        return

    cli_uid = os.getenv("DEV_USER_ID", "local-dev").strip() or "local-dev"
    with user_read_lock(cli_uid) as resolved_dir:
        target_dir = resolved_dir if has_sqlite(resolved_dir) else "chroma_db"
        yield from _stream_events(
            query=query,
            persist_directory=target_dir,
            collection_name=collection_name,
            thread_id=f"{cli_uid}:{thread_id}",
            model_name=model_name,
            embedding_model=embedding_model,
            show_console=show_console,
        )


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
    query_digest = hash_query(request.query)
    query_len = len(request.query)
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
                verbose_console=False,
            ):
                msg = evt["message"]
                streamed_messages.append(msg)
                if msg.get("type") == "ai" and msg.get("content"):
                    final_answer = str(msg["content"])
                data = {
                    "event": evt["event"],
                    "message": msg,
                }
                yield f"data: {json.dumps(data)}\n\n"

        compliance_meta = build_compliance_metadata(
            model_name=request.model_name,
            answer=final_answer,
            messages=streamed_messages,
            scoped_thread_id=scoped_thread_id,
            checkpointer=MEMORY_SAVER,
        ).model_dump()
        compliance_frame = {
            "event": "compliance",
            "compliance": compliance_meta,
            "message": {"type": "compliance", "content": ""},
        }
        yield f"data: {json.dumps(compliance_frame)}\n\n"

        emit_audit_event(
            "agent.query",
            uid=uid,
            collection_name=request.collection_name,
            model_name=request.model_name,
            embedding_model=request.embedding_model,
            stream=True,
            query_sha256=query_digest,
            query_chars=query_len,
            answer_chars=len(final_answer),
            grounded=compliance_meta["grounded"],
            citation_count=compliance_meta["citation_count"],
            pii_filtered=compliance_meta["pii_filtered"],
            guardrail_actions=[
                a["filter"] + ":" + a["action"] for a in compliance_meta["guardrail_actions"]
            ],
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
            query_sha256=query_digest,
            query_chars=query_len,
        )
        err_data = {
            "event": "error",
            "http_status": 503,
            "error": SCREENING_UNAVAILABLE_MESSAGE,
            "message": {
                "type": "ai",
                "content": SCREENING_UNAVAILABLE_MESSAGE,
            },
        }
        yield f"data: {json.dumps(err_data)}\n\n"
        yield "data: [DONE]\n\n"
    except Exception:
        logger.exception(
            f"Unexpected error streaming Agentic RAG SSE for uid='{uid}' (thread_id='{scoped_thread_id}')"
        )
        emit_audit_event(
            "agent.query",
            severity="ERROR",
            status="error",
            uid=uid,
            collection_name=request.collection_name,
            model_name=request.model_name,
            embedding_model=request.embedding_model,
            stream=True,
            query_sha256=query_digest,
            query_chars=query_len,
        )
        err_data = {
            "event": "error",
            "error": INTERNAL_ERROR_MESSAGE,
            "message": {
                "type": "ai",
                "content": INTERNAL_ERROR_MESSAGE,
            },
        }
        yield f"data: {json.dumps(err_data)}\n\n"
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
    logger.info(
        f"Running interactive agent on collection '{collection_name}' "
        f"(query_sha256='{hash_query(query)}', query_chars={len(query)})"
    )
    for _ in stream_events(query=query, collection_name=collection_name, verbose_console=True):
        pass


__all__ = ["SSE_HEADERS", "extract_text", "run_interactive", "stream_events", "stream_sse"]
