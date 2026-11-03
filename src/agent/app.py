"""FastAPI Interactive Read & Control-Plane microservice (`agent-api`).

Mounts three cohesive read/control-plane routers onto a high-concurrency (`32`) Cloud Run service
sharing a single warm `/tmp/chroma_db/{uid}` cache and `_QUERY_RATE_LIMITER`:
- `src.agent.routes.agent_router`: `POST /agent` and `POST /agent/stream` (LangGraph Agentic RAG + SSE).
- `src.query.routes.query_router`: `POST /query` (top-`k` vectorstore similarity search).
- `src.manage.routes.manage_router`: `GET /collections`, `DELETE /collections/{collection_name}`, and
  `GET`/`DELETE /compliance/my-data` wired with `MEMORY_SAVER` thread inventory and purge hooks.
"""

import uvicorn

from src.agent.display import print_header
from src.agent.routes import agent, agent_router, agent_stream
from src.core.app_factory import create_app
from src.core.config import AGENT_PORT, get_service_port, is_reload_enabled
from src.manage.routes import manage_router
from src.query.routes import query_router

app = create_app(
    title="Agentic RAG API",
    service_name="agent-api",
    routers=[
        agent_router,
        query_router,
        manage_router,
    ],
)


def main() -> None:
    """Starts the Uvicorn HTTP server for `agent-api` on `PORT` (default `8001`)."""
    print_header("Agentic RAG Assistant Server Initialized", "magenta")
    port = get_service_port(AGENT_PORT)
    uvicorn.run(
        "src.agent.app:app",
        host="0.0.0.0",
        port=port,
        reload=is_reload_enabled(),
    )


if __name__ == "__main__":
    main()

__all__ = ["agent", "agent_router", "agent_stream", "app", "main"]
