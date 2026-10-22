import json
import os
from typing import Any, Dict, Generator, Optional

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from src.agent.display import print_event, print_header
from src.agent.graph import build_agent
from src.agent.schemas import AgentRequest
from src.agent.tools import get_retriever_tool
from src.core.chroma import has_sqlite
from src.core.config import (
    DEFAULT_COLLECTION,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_LLM_MODEL,
)
from src.core.logger import setup_logging
from src.core.storage import user_read_lock

logger = setup_logging(name="agent.stream")

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def extract_text(content) -> str:
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


def _stream_events(
    query: str,
    persist_directory: str,
    collection_name: str,
    thread_id: str,
    model_name: str,
    embedding_model: str,
) -> Generator[Dict[str, Any], None, None]:
    retriever_tool = get_retriever_tool(
        collection_name=collection_name,
        persist_directory=persist_directory,
        embedding_model=embedding_model,
    )
    agent = build_agent(retriever_tool, model_name=model_name)

    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    input_messages = [HumanMessage(content=query)]

    for event in agent.stream(
        {"messages": input_messages}, config, stream_mode="values"
    ):
        messages = event.get("messages", [])
        if messages:
            last_msg = messages[-1]
            # Print all components of the message using src/agent/display.py
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


def stream_events(
    query: str,
    collection_name: str = DEFAULT_COLLECTION,
    thread_id: str = "1",
    model_name: str = DEFAULT_LLM_MODEL,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    persist_directory: Optional[str] = None,
) -> Generator[Dict[str, Any], None, None]:
    """Streams graph message output, printing each message component using print_event."""
    if persist_directory is not None:
        yield from _stream_events(
            query=query,
            persist_directory=persist_directory,
            collection_name=collection_name,
            thread_id=thread_id,
            model_name=model_name,
            embedding_model=embedding_model,
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
        )


def stream_sse(
    uid: str,
    request: AgentRequest,
    scoped_thread_id: str,
) -> Generator[str, None, None]:
    """Generator yielding Server-Sent Events (SSE) payloads while pinning user Chroma cache."""
    with user_read_lock(uid) as stream_persist_dir:
        for evt in stream_events(
            query=request.query,
            collection_name=request.collection_name,
            thread_id=scoped_thread_id,
            model_name=request.model_name,
            embedding_model=request.embedding_model,
            persist_directory=stream_persist_dir,
        ):
            data = {
                "event": evt["event"],
                "message": evt["message"],
            }
            yield f"data: {json.dumps(data)}\n\n"
        yield "data: [DONE]\n\n"


def run_interactive(
    query: str = "What is ARM Holdings?",
    collection_name: str = DEFAULT_COLLECTION,
) -> None:
    """Interactive runner to stream message output and pretty print message components."""
    print_header("Agentic RAG Assistant Initialized", "magenta")
    logger.info(f"Running agent with query: '{query}'")
    for _ in stream_events(query=query, collection_name=collection_name):
        pass
