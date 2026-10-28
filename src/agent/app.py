"""FastAPI LangGraph Agentic RAG microservice (`agent-api`).

Exposes authenticated `POST /agent` (synchronous JSON or optional SSE) and `POST /agent/stream`
(dedicated SSE) endpoints alongside shared `/health`, `/collections`, and `/compliance/my-data`
DSAR routes wired with `MEMORY_SAVER` thread inventory and purge hooks.
"""

import os
from typing import Any

from fastapi import Depends, HTTPException
from fastapi.responses import StreamingResponse
import uvicorn

from src.agent.display import print_header
from src.agent.memory import MEMORY_SAVER, clear_user_memory, list_user_memory_threads
from src.agent.schemas import AgentRequest
from src.agent.stream import SSE_HEADERS, stream_events, stream_sse
from src.core.ai_security.armor import (
    SecurityScreeningUnavailableError,
    ensure_screening_available,
)
from src.core.ai_security.safety import (
    INTERNAL_ERROR_MESSAGE,
    SCREENING_UNAVAILABLE_MESSAGE,
)
from src.core.app_factory import create_app
from src.core.auth import get_current_user
from src.core.compliance.audit import emit_audit_event, hash_query
from src.core.compliance.policy import enforce_query_rate_limit
from src.core.compliance.provenance import build_compliance_metadata
from src.core.config import AGENT_PORT, is_reload_enabled
from src.core.logger import setup_logging
from src.core.storage import LockTimeoutError, require_collection, user_read_lock

logger = setup_logging(name="agent")

app = create_app(
    title="Agentic RAG API",
    service_name="agent-api",
    memory_inventory_fn=list_user_memory_threads,
    on_user_purge=clear_user_memory,
)


@app.post("/agent")
def agent(
    request: AgentRequest,
    uid: str = Depends(get_current_user),
) -> Any:
    """Executes an Agentic RAG turn for `uid`, returning grounded JSON or an SSE `StreamingResponse`.

    Pre-validates per-user query rate limits, Model Armor fail-closed configuration, and the
    tenant's synced ChromaDB collection before starting execution so rate-limited, unscreened,
    or missing-collection requests fail fast across both JSON and SSE streaming modes.

    Args:
        request: Validated `AgentRequest` specifying `query`, `collection_name`, models, and `stream`.
        uid: Verified tenant user identifier injected by `get_current_user`.

    Returns:
        JSON dictionary (conforming to `AgentResponse`) when `stream=False`, or `StreamingResponse` when `stream=True`.
    """
    scoped_thread_id = f"{uid}:{request.thread_id}"
    query_digest = hash_query(request.query)
    query_len = len(request.query)
    try:
        enforce_query_rate_limit(uid)
        ensure_screening_available()

        logger.info(
            f"Received Agentic RAG query from uid='{uid}' for collection '{request.collection_name}' "
            f"(query_sha256='{query_digest}', query_chars={query_len}, stream={request.stream})"
        )

        # Pre-validate user's synced ChromaDB & collection so missing collections return 404 even when stream=True
        with user_read_lock(uid) as persist_directory:
            require_collection(persist_directory, request.collection_name)

            if request.stream:
                return StreamingResponse(
                    stream_sse(
                        uid=uid,
                        request=request,
                        scoped_thread_id=scoped_thread_id,
                    ),
                    media_type="text/event-stream",
                    headers=SSE_HEADERS,
                )

            events = list(
                stream_events(
                    query=request.query,
                    collection_name=request.collection_name,
                    thread_id=scoped_thread_id,
                    model_name=request.model_name,
                    embedding_model=request.embedding_model,
                    persist_directory=persist_directory,
                    verbose_console=False,
                )
            )

        formatted_messages = [evt["message"] for evt in events]
        final_answer = ""
        for msg in reversed(formatted_messages):
            if msg["type"] == "ai" and msg.get("content"):
                final_answer = msg["content"]
                break

        compliance_meta = build_compliance_metadata(
            model_name=request.model_name,
            answer=final_answer,
            messages=formatted_messages,
            scoped_thread_id=scoped_thread_id,
            checkpointer=MEMORY_SAVER,
        ).model_dump()

        emit_audit_event(
            "agent.query",
            uid=uid,
            collection_name=request.collection_name,
            model_name=request.model_name,
            embedding_model=request.embedding_model,
            stream=False,
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

        return {
            "status": "success",
            "query": request.query,
            "answer": final_answer,
            "collection_name": request.collection_name,
            "messages": formatted_messages,
            "compliance": compliance_meta,
        }
    except SecurityScreeningUnavailableError as e:
        logger.error(
            f"Fail-closed screening unavailable on /agent for uid='{uid}' (reason={e.reason})."
        )
        emit_audit_event(
            "agent.query",
            severity="ERROR",
            status="screening_unavailable",
            reason=e.reason,
            http_status=503,
            uid=uid,
            collection_name=request.collection_name,
            stream=request.stream,
            query_sha256=query_digest,
            query_chars=query_len,
        )
        raise HTTPException(status_code=503, detail=SCREENING_UNAVAILABLE_MESSAGE)
    except LockTimeoutError as e:
        emit_audit_event(
            "agent.query",
            severity="WARNING",
            status="lock_timeout",
            uid=uid,
            collection_name=request.collection_name,
            stream=request.stream,
            query_sha256=query_digest,
            query_chars=query_len,
        )
        raise HTTPException(status_code=409, detail=str(e))
    except FileNotFoundError as e:
        logger.error(f"Collection not found for uid='{uid}': {e}")
        emit_audit_event(
            "agent.query",
            severity="WARNING",
            status="not_found",
            uid=uid,
            collection_name=request.collection_name,
            stream=request.stream,
            query_sha256=query_digest,
            query_chars=query_len,
        )
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception:
        logger.exception(f"Error running Agentic RAG for uid='{uid}'")
        emit_audit_event(
            "agent.query",
            severity="ERROR",
            status="error",
            uid=uid,
            collection_name=request.collection_name,
            stream=request.stream,
            query_sha256=query_digest,
            query_chars=query_len,
        )
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_MESSAGE)


@app.post("/agent/stream")
def agent_stream(
    request: AgentRequest,
    uid: str = Depends(get_current_user),
) -> Any:
    """Dedicated Server-Sent Events (`text/event-stream`) endpoint forcing `stream=True`.

    Args:
        request: Validated `AgentRequest` payload.
        uid: Verified tenant user identifier injected by `get_current_user`.

    Returns:
        `StreamingResponse` yielding SSE `message`, `compliance`, and `[DONE]` frames.
    """
    return agent(request=request.model_copy(update={"stream": True}), uid=uid)


def main() -> None:
    """Starts the Uvicorn HTTP server for `agent-api` on `PORT` (default `8001`)."""
    print_header("Agentic RAG Assistant Server Initialized", "magenta")
    port = int(os.getenv("PORT", str(AGENT_PORT)))
    uvicorn.run(
        "src.agent.app:app",
        host="0.0.0.0",
        port=port,
        reload=is_reload_enabled(),
    )


if __name__ == "__main__":
    main()
