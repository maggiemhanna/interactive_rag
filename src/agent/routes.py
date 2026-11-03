"""FastAPI `/agent` and `/agent/stream` router (`src.agent.routes`).

Exposes authenticated `POST /agent` (grounded JSON or Server-Sent Events) and
`POST /agent/stream` (dedicated SSE stream) on `agent-api`.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from src.agent.memory import MEMORY_SAVER
from src.agent.schemas import AgentRequest
from src.agent.service import (
    SSE_HEADERS,
    emit_agent_query_events,
    stream_events,
    stream_sse,
)
from src.core.ai_security.armor import (
    SecurityScreeningUnavailableError,
    ensure_screening_available,
)
from src.core.ai_security.safety import (
    INTERNAL_ERROR_MESSAGE,
    SCREENING_UNAVAILABLE_MESSAGE,
)
from src.core.auth import get_current_user
from src.core.compliance.audit import emit_audit_event
from src.core.compliance.policy import enforce_query_rate_limit
from src.core.compliance.provenance import build_compliance_metadata
from src.core.context import scrub_query_fields
from src.core.logger import setup_logging
from src.core.storage import LockTimeoutError, require_collection, user_read_lock

logger = setup_logging(name="agent.routes")

agent_router = APIRouter()


@agent_router.post("/agent")
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
    try:
        enforce_query_rate_limit(uid)
        ensure_screening_available()

        scrubbed = scrub_query_fields({"query": request.query})
        logger.info(
            f"Received Agentic RAG query from uid='{uid}' for collection '{request.collection_name}' "
            f"(query_sha256='{scrubbed['query_sha256']}', query_chars={scrubbed['query_chars']}, stream={request.stream})."
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

        emit_agent_query_events(
            uid=uid,
            request=request,
            stream=False,
            final_answer=final_answer,
            compliance_meta=compliance_meta,
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
            model_name=request.model_name,
            embedding_model=request.embedding_model,
            stream=request.stream,
            query=request.query,
        )
        raise HTTPException(status_code=503, detail=SCREENING_UNAVAILABLE_MESSAGE)
    except LockTimeoutError as e:
        emit_audit_event(
            "agent.query",
            severity="WARNING",
            status="lock_timeout",
            http_status=409,
            uid=uid,
            collection_name=request.collection_name,
            model_name=request.model_name,
            embedding_model=request.embedding_model,
            stream=request.stream,
            query=request.query,
        )
        raise HTTPException(status_code=409, detail=str(e))
    except FileNotFoundError as e:
        logger.error(f"Collection not found for uid='{uid}': {e}")
        emit_audit_event(
            "agent.query",
            severity="WARNING",
            status="not_found",
            http_status=404,
            uid=uid,
            collection_name=request.collection_name,
            model_name=request.model_name,
            embedding_model=request.embedding_model,
            stream=request.stream,
            query=request.query,
        )
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception:
        logger.exception(f"Error running Agentic RAG for uid='{uid}'.")
        emit_audit_event(
            "agent.query",
            severity="ERROR",
            status="error",
            http_status=500,
            uid=uid,
            collection_name=request.collection_name,
            model_name=request.model_name,
            embedding_model=request.embedding_model,
            stream=request.stream,
            query=request.query,
        )
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_MESSAGE)


@agent_router.post("/agent/stream")
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


__all__ = ["agent", "agent_router", "agent_stream"]

