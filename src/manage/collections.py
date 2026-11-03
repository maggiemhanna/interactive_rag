"""Authenticated `/collections` FastAPI router (`src.manage.collections`).

Exposes tenant-scoped `GET /collections` and `DELETE /collections/{collection_name}`
endpoints backed by GCS-synced per-user ChromaDB locks and compliance audit logging.
Mounted on `agent-api` so collection listings share the warm `/tmp/chroma_db/{uid}` cache
with `/query` and `/agent` and remain isolated from heavy PDF ingestion workers.
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from src.core.ai_security.guardrails import validate_collection_name
from src.core.ai_security.safety import INTERNAL_ERROR_MESSAGE
from src.core.auth import get_current_user
from src.core.compliance.audit import emit_audit_event
from src.core.logger import setup_logging
from src.core.storage.chroma import list_collections, remove_collection
from src.core.storage.locks import LockTimeoutError, user_read_lock, user_write_lock

logger = setup_logging(name="manage.collections")

collections_router = APIRouter()


@collections_router.get("/collections")
def get_collections(uid: str = Depends(get_current_user)) -> Dict[str, Any]:
    """Lists all existing ChromaDB collections and chunk counts for the authenticated user.

    Args:
        uid: Verified tenant user identifier injected by `get_current_user`.

    Returns:
        JSON payload with `'collections'` names and `'details'` per-collection passage counts.
    """
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
        logger.exception(f"Error fetching collections for uid='{uid}'.")
        emit_audit_event(
            "collection.list",
            severity="ERROR",
            status="error",
            http_status=500,
            uid=uid,
        )
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_MESSAGE)


@collections_router.delete("/collections/{collection_name}")
def delete_collection(
    collection_name: str,
    uid: str = Depends(get_current_user),
) -> Dict[str, Any]:
    """Deletes a specific ChromaDB vector collection belonging to the authenticated user.

    Args:
        collection_name: URL path parameter naming the collection to remove.
        uid: Verified tenant user identifier injected by `get_current_user`.

    Returns:
        Confirmation JSON dictionary containing `'status'`, `'message'`, and `'collection_name'`.
    """
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
            http_status=422,
            uid=uid,
            reason="invalid_collection_name",
        )
        raise HTTPException(status_code=422, detail=str(e))
    except FileNotFoundError as e:
        emit_audit_event(
            "collection.delete",
            severity="WARNING",
            status="not_found",
            http_status=404,
            uid=uid,
            collection_name=collection_name,
        )
        raise HTTPException(status_code=404, detail=str(e))
    except LockTimeoutError as e:
        emit_audit_event(
            "collection.delete",
            severity="WARNING",
            status="lock_timeout",
            http_status=409,
            uid=uid,
            collection_name=collection_name,
        )
        raise HTTPException(status_code=409, detail=str(e))
    except HTTPException:
        raise
    except Exception:
        logger.exception(f"Error deleting collection '{collection_name}' for uid='{uid}'.")
        emit_audit_event(
            "collection.delete",
            severity="ERROR",
            status="error",
            http_status=500,
            uid=uid,
        )
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_MESSAGE)


__all__ = ["collections_router", "delete_collection", "get_collections"]

