"""FastAPI Document Ingestion microservice (`ingest-api`).

Mounts ONLY `src.ingest.routes.ingest_router` (`POST /ingest`) and `GET /health`
on a dedicated write-heavy Cloud Run service (`2 vCPU / 4Gi`, concurrency `4`),
isolating heavy PDF parsing and batch indexing from interactive read/control-plane traffic.
"""

import uvicorn

from src.core.app_factory import create_app
from src.core.config import INGEST_PORT, get_service_port, is_reload_enabled
from src.ingest.routes import ingest, ingest_router

app = create_app(
    title="Document Ingestion API",
    service_name="ingest-api",
    routers=[ingest_router],
)


def main() -> None:
    """Starts the Uvicorn HTTP server for `ingest-api` on `PORT` (default `8000`)."""
    port = get_service_port(INGEST_PORT)
    uvicorn.run(
        "src.ingest.app:app",
        host="0.0.0.0",
        port=port,
        reload=is_reload_enabled(),
    )


if __name__ == "__main__":
    main()

__all__ = ["app", "ingest", "main"]
