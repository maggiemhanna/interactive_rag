"""Shared FastAPI application factory for InteractiveRAG microservices.

Constructs standardized FastAPI instances (`ingest-api` and `agent-api`) wired with
CORS origin policies, `RequestCorrelationMiddleware` (`X-Request-Id` / `X-Cloud-Trace-Context`),
unauthenticated `/health` readiness probes, and explicit domain routers supplied by each service.
"""

from typing import Dict, Optional, Sequence

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

import src.core.config  # noqa: F401 - ensures .env files are loaded
from src.core.config import get_allowed_origins
from src.core.context import RequestCorrelationMiddleware


def create_app(
    title: str,
    service_name: str,
    *,
    routers: Optional[Sequence[APIRouter]] = None,
) -> FastAPI:
    """Creates a FastAPI service configured with CORS, correlation middleware, health probe, and domain routers.

    Args:
        title: OpenAPI specification title for the FastAPI application.
        service_name: Logical microservice identifier (`'ingest-api'` or `'agent-api'`).
        routers: Optional sequence of FastAPI `APIRouter` instances to mount on this service.

    Returns:
        Configured `FastAPI` application instance.
    """
    app = FastAPI(title=title)

    allowed_origins = get_allowed_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials="*" not in allowed_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id"],
    )
    app.add_middleware(RequestCorrelationMiddleware, service_name=service_name)

    @app.get("/health")
    def health_check() -> Dict[str, str]:
        """Unauthenticated liveness and readiness probe endpoint for Cloud Run."""
        return {"status": "ok", "service": service_name}

    if routers:
        for router in routers:
            app.include_router(router)
    return app


__all__ = ["create_app"]

