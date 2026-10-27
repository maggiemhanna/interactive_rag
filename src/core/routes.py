from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import src.core.config  # noqa: F401 - ensures .env files are loaded
from src.core.audit import AuditCorrelationMiddleware, emit_audit_event
from src.core.auth import get_allowed_origins, get_current_user
from src.core.chroma import list_collections, remove_collection
from src.core.logger import setup_logging
from src.core.security import INTERNAL_ERROR_MESSAGE, validate_collection_name
from src.core.storage import (
    LockTimeoutError,
    user_read_lock,
    user_write_lock,
)

logger = setup_logging(name="routes")

collections_router = APIRouter()


@collections_router.get("/collections")
def get_collections(uid: str = Depends(get_current_user)):
    """Returns a list of existing collection details from Chroma vectorstore."""
    try:
        with user_read_lock(uid) as persist_directory:
            payload = list_collections(persist_directory)
        collections = payload.get("collections", []) if isinstance(payload, dict) else []
        emit_audit_event(
            "collection.list",
            uid=uid,
            collection_count=len(collections),
        )
        return payload
    except HTTPException:
        raise
    except Exception:
        logger.exception(f"Error fetching collections for uid='{uid}'")
        emit_audit_event(
            "collection.list",
            severity="ERROR",
            status="error",
            uid=uid,
        )
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_MESSAGE)


@collections_router.delete("/collections/{collection_name}")
def delete_collection(collection_name: str, uid: str = Depends(get_current_user)):
    """Deletes an existing collection from Chroma vectorstore."""
    try:
        safe_collection = validate_collection_name(collection_name)
        with user_write_lock(uid) as persist_directory:
            remove_collection(persist_directory, safe_collection)
            logger.info(
                f"Successfully deleted Chroma collection '{safe_collection}' for uid='{uid}'."
            )
            emit_audit_event(
                "collection.delete",
                uid=uid,
                collection_name=safe_collection,
            )
            return {
                "status": "success",
                "message": f"Successfully deleted collection '{safe_collection}'.",
                "collection_name": safe_collection,
            }
    except ValueError as e:
        emit_audit_event(
            "collection.delete",
            severity="WARNING",
            status="invalid_request",
            uid=uid,
            reason="invalid_collection_name",
        )
        raise HTTPException(status_code=422, detail=str(e))
    except FileNotFoundError as e:
        emit_audit_event(
            "collection.delete",
            severity="WARNING",
            status="not_found",
            uid=uid,
            collection_name=collection_name,
        )
        raise HTTPException(status_code=404, detail=str(e))
    except LockTimeoutError as e:
        emit_audit_event(
            "collection.delete",
            severity="WARNING",
            status="lock_timeout",
            uid=uid,
            collection_name=collection_name,
        )
        raise HTTPException(status_code=409, detail=str(e))
    except HTTPException:
        raise
    except Exception:
        logger.exception(f"Error deleting collection '{collection_name}' for uid='{uid}'")
        emit_audit_event(
            "collection.delete",
            severity="ERROR",
            status="error",
            uid=uid,
        )
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_MESSAGE)


def create_app(title: str, service_name: str) -> FastAPI:
    """Creates a FastAPI service configured with CORS, correlation middleware, /health probe, and shared /collections routes."""
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
    def health_check():
        """Unauthenticated liveness/readiness probe endpoint for Cloud Run."""
        return {"status": "ok", "service": service_name}

    app.include_router(collections_router)
    return app

