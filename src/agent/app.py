import os

import uvicorn
from fastapi import Depends, HTTPException
from fastapi.responses import StreamingResponse

from src.agent.display import print_header
from src.agent.schemas import AgentRequest
from src.agent.stream import (
    SSE_HEADERS,
    stream_events,
    stream_sse,
)
from src.core.auth import get_current_user
from src.core.chroma import require_collection
from src.core.config import AGENT_PORT, is_reload_enabled
from src.core.logger import setup_logging
from src.core.routes import create_app
from src.core.storage import user_read_lock

logger = setup_logging(name="agent")

app = create_app(title="Agentic RAG API", service_name="agent-api")


@app.post("/agent")
def agent(
    request: AgentRequest,
    uid: str = Depends(get_current_user),
):
    """FastAPI endpoint to run the Agentic RAG assistant with a query."""
    scoped_thread_id = f"{uid}:{request.thread_id}"
    try:
        logger.info(
            f"Received Agentic RAG query from uid='{uid}': '{request.query}' "
            f"for collection '{request.collection_name}' (stream={request.stream})"
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
                )
            )

        formatted_messages = [evt["message"] for evt in events]
        final_answer = ""
        for msg in reversed(formatted_messages):
            if msg["type"] == "ai" and msg.get("content"):
                final_answer = msg["content"]
                break

        return {
            "status": "success",
            "query": request.query,
            "answer": final_answer,
            "collection_name": request.collection_name,
            "messages": formatted_messages,
        }
    except FileNotFoundError as e:
        logger.error(f"Collection not found for uid='{uid}': {e}")
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error running Agentic RAG for uid='{uid}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/agent/stream")
def agent_stream(
    request: AgentRequest,
    uid: str = Depends(get_current_user),
):
    """Dedicated streaming endpoint using Server-Sent Events (SSE)."""
    request.stream = True
    return agent(request=request, uid=uid)


def main() -> None:
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
