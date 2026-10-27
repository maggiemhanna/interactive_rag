"""Shared FastAPI application factory for InteractiveRAG microservices.

Constructs standardized FastAPI instances (`ingestion-api` and `agent-api`) wired with
CORS origin policies, `AuditCorrelationMiddleware` (`X-Request-Id` / `X-Cloud-Trace-Context`),
unauthenticated `/health` readiness probes, shared `/collections` endpoints, and GDPR
`/compliance/my-data` DSAR routes.
"""

from typing import Callable, Dict, List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import src.core.config  # noqa: F401 - ensures .env files are loaded
from src.core.compliance.audit import AuditCorrelationMiddleware
from src.core.compliance.routes import build_compliance_router
from src.core.config import get_allowed_origins
from src.core.storage.routes import collections_router


def create_app(
    title: str,
    service_name: str,
    *,
    memory_inventory_fn: Optional[Callable[[str], List[str]]] = None,
    on_user_purge: Optional[Callable[[str], int]] = None,
) -> FastAPI:
    """Creates a FastAPI service configured with CORS, correlation middleware, health probe, and shared routers.

    Args:
        title: OpenAPI specification title for the FastAPI application.
        service_name: Logical microservice identifier (`'ingestion-api'` or `'agent-api'`).
        memory_inventory_fn: Optional callback returning active conversation thread IDs for DSAR exports.
        on_user_purge: Optional callback purging conversation memory threads on DSAR erasure.

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
    app.add_middleware(AuditCorrelationMiddleware, service_name=service_name)

    @app.get("/health")
    def health_check() -> Dict[str, str]:
        """Unauthenticated liveness and readiness probe endpoint for Cloud Run."""
        return {"status": "ok", "service": service_name}

    app.include_router(collections_router)
    app.include_router(
        build_compliance_router(
            memory_inventory_fn=memory_inventory_fn,
            on_user_purge=on_user_purge,
        )
    )
    return app


__all__ = ["create_app"]
