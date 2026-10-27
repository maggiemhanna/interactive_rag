from typing import Callable, List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import src.core.config  # noqa: F401 - ensures .env files are loaded
from src.core.audit import AuditCorrelationMiddleware, emit_audit_event, get_request_id
from src.core.auth import get_allowed_origins, get_current_user
from src.core.chroma import list_collections, remove_collection
from src.core.logger import setup_logging
from src.core.security import INTERNAL_ERROR_MESSAGE, validate_collection_name
from src.core.storage import (
    LockTimeoutError,
    get_user_data_inventory,
    purge_all_user_data,
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


def _build_compliance_router(
    memory_inventory_fn: Optional[Callable[[str], List[str]]] = None,
    on_user_purge: Optional[Callable[[str], int]] = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/compliance/my-data")
    def export_my_data_inventory(uid: str = Depends(get_current_user)):
        """GDPR Art. 15 (Right of Access) — returns per-user inventory of collections, uploaded PDFs, byte counts, and memory threads."""
        try:
            inventory = get_user_data_inventory(uid)
            memory_threads = memory_inventory_fn(uid) if memory_inventory_fn else []
            inventory["memory_thread_count"] = len(memory_threads)
            inventory["request_id"] = get_request_id()

            emit_audit_event(
                "dsar.export_inventory",
                uid=uid,
                collection_count=inventory["collection_count"],
                total_passages=inventory["total_passages"],
                uploaded_pdf_count=inventory["uploaded_pdf_count"],
                gcs_live_object_count=inventory["gcs_live_object_count"],
                gcs_noncurrent_version_count=inventory["gcs_noncurrent_version_count"],
                total_bytes=inventory["total_bytes"],
                memory_thread_count=len(memory_threads),
            )
            return inventory
        except HTTPException:
            raise
        except Exception:
            logger.exception(f"Error building DSAR data inventory for uid='{uid}'")
            emit_audit_event(
                "dsar.export_inventory",
                severity="ERROR",
                status="error",
                uid=uid,
            )
            raise HTTPException(status_code=500, detail=INTERNAL_ERROR_MESSAGE)

    @router.delete("/compliance/my-data")
    def erase_my_data(uid: str = Depends(get_current_user)):
        """GDPR Art. 17 (Right to Erasure) — permanently deletes all live & noncurrent GCS versions, local caches, and conversation memory."""
        try:
            purge_result = purge_all_user_data(uid)
            deleted_memory_threads = on_user_purge(uid) if on_user_purge else 0
            request_id = get_request_id()

            emit_audit_event(
                "dsar.erase_user_data",
                uid=uid,
                storage_backend=purge_result["storage_backend"],
                deleted_gcs_live_objects=purge_result["deleted_gcs_live_objects"],
                deleted_gcs_noncurrent_versions=purge_result["deleted_gcs_noncurrent_versions"],
                deleted_local_chroma=purge_result["deleted_local_chroma"],
                deleted_local_uploads=purge_result["deleted_local_uploads"],
                deleted_memory_threads=deleted_memory_threads,
            )
            return {
                "status": "success",
                "message": (
                    "All personal data, vector collections, archived PDFs, and conversation "
                    "history have been permanently erased."
                ),
                "request_id": request_id,
                **purge_result,
                "deleted_memory_threads": deleted_memory_threads,
            }
        except LockTimeoutError as e:
            emit_audit_event(
                "dsar.erase_user_data",
                severity="WARNING",
                status="lock_timeout",
                uid=uid,
            )
            raise HTTPException(status_code=409, detail=str(e))
        except HTTPException:
            raise
        except Exception:
            logger.exception(f"Error erasing DSAR user data for uid='{uid}'")
            emit_audit_event(
                "dsar.erase_user_data",
                severity="ERROR",
                status="error",
                uid=uid,
            )
            raise HTTPException(status_code=500, detail=INTERNAL_ERROR_MESSAGE)

    return router


def create_app(
    title: str,
    service_name: str,
    *,
    memory_inventory_fn: Optional[Callable[[str], List[str]]] = None,
    on_user_purge: Optional[Callable[[str], int]] = None,
) -> FastAPI:
    """Creates a FastAPI service configured with CORS, correlation middleware, /health probe, /collections, and /compliance/my-data DSAR routes."""
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
    app.include_router(
        _build_compliance_router(
            memory_inventory_fn=memory_inventory_fn,
            on_user_purge=on_user_purge,
        )
    )
    return app


