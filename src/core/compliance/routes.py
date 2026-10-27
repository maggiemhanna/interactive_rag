"""Self-service GDPR Data Subject Access Request (DSAR) FastAPI router (Pillar 4: Compliance & Governance).

Exposes authenticated `/compliance/my-data` `GET` (Art. 15 Right of Access) and `DELETE`
(Art. 17 Right to Erasure) endpoints with optional callbacks for inspecting and purging
in-memory LangGraph conversation checkpoints on the agent service.
"""

from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException

from src.core.ai_security.safety import INTERNAL_ERROR_MESSAGE
from src.core.auth import get_current_user
from src.core.compliance.audit import emit_audit_event
from src.core.compliance.context import get_request_id
from src.core.compliance.dsar import get_user_data_inventory, purge_all_user_data
from src.core.logger import setup_logging
from src.core.storage.locks import LockTimeoutError

logger = setup_logging(name="compliance.routes")


def build_compliance_router(
    memory_inventory_fn: Optional[Callable[[str], List[str]]] = None,
    on_user_purge: Optional[Callable[[str], int]] = None,
) -> APIRouter:
    """Constructs the FastAPI router serving `GET` and `DELETE /compliance/my-data`.

    Args:
        memory_inventory_fn: Optional callback `(uid) -> list[thread_id]` reporting active
            in-memory conversation threads for the tenant (supplied by `agent-api`).
        on_user_purge: Optional callback `(uid) -> deleted_thread_count` wiping in-memory
            conversation checkpoints during erasure (supplied by `agent-api`).

    Returns:
        Configured `APIRouter` instance exposing DSAR endpoints.
    """
    router = APIRouter()

    @router.get("/compliance/my-data")
    def export_my_data_inventory(uid: str = Depends(get_current_user)) -> Dict[str, Any]:
        """GDPR Art. 15 (Right of Access) — returns per-user inventory of collections, PDFs, bytes, and threads."""
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
    def erase_my_data(uid: str = Depends(get_current_user)) -> Dict[str, Any]:
        """GDPR Art. 17 (Right to Erasure) — permanently deletes all live & noncurrent GCS versions, caches, and memory."""
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


__all__ = ["build_compliance_router"]
