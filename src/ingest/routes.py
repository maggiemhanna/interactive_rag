"""FastAPI `/ingest` router (`src.ingest.routes`).

Exposes authenticated `POST /ingest` on the dedicated `ingest-api` Cloud Run service
so heavy PDF parsing, chunking, Vertex AI embedding, and GCS snapshot sync are
completely isolated from interactive `/query`, `/collections`, and `/agent` requests.
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from src.core.ai_security.armor import SecurityScreeningUnavailableError
from src.core.ai_security.safety import (
    INTERNAL_ERROR_MESSAGE,
    SCREENING_UNAVAILABLE_MESSAGE,
)
from src.core.auth import get_current_user
from src.core.compliance.audit import emit_audit_event
from src.core.logger import setup_logging
from src.core.observability.telemetry import emit_telemetry_event
from src.core.storage import LockTimeoutError
from src.ingest.service import ingest_documents
from src.ingest.upload_parser import prepare_payload

logger = setup_logging(name="ingest.routes")

ingest_router = APIRouter()


@ingest_router.post("/ingest")
async def ingest(
    request: Request,
    uid: str = Depends(get_current_user),
) -> Dict[str, Any]:
    """Loads, chunks, and indexes uploaded PDF documents into the user's Chroma collection.

    Args:
        request: Raw FastAPI `Request` carrying multipart PDF uploads or local JSON `pdf_paths`.
        uid: Verified tenant user identifier injected by `get_current_user`.

    Returns:
        JSON confirmation containing `chunks_added`, `collection_name`, and `processed_files`.
    """
    payload = None
    try:
        payload = await prepare_payload(request=request, uid=uid)

        logger.info(
            f"Ingesting {len(payload.chunks)} chunks for uid='{uid}' into collection '{payload.collection_name}' "
            f"(embedding_model='{payload.embedding_model}', files={payload.processed_files})."
        )

        ingest_documents(
            uid=uid,
            documents=payload.chunks,
            collection_name=payload.collection_name,
            embedding_model=payload.embedding_model,
        )

        emit_audit_event(
            "document.ingest",
            uid=uid,
            collection_name=payload.collection_name,
            embedding_model=payload.embedding_model,
            chunks_added=len(payload.chunks),
            file_count=len(payload.processed_files),
            processed_files=payload.processed_files,
        )
        emit_telemetry_event(
            "document.ingest",
            uid=uid,
            collection_name=payload.collection_name,
            embedding_model=payload.embedding_model,
            chunks_added=len(payload.chunks),
            file_count=len(payload.processed_files),
        )

        return {
            "status": "success",
            "message": f"Successfully added {len(payload.chunks)} document chunks to collection '{payload.collection_name}'.",
            "chunks_added": len(payload.chunks),
            "collection_name": payload.collection_name,
            "processed_files": payload.processed_files,
        }
    except SecurityScreeningUnavailableError as e:
        emit_audit_event(
            "document.ingest",
            severity="ERROR",
            status="screening_unavailable",
            reason=e.reason,
            http_status=503,
            uid=uid,
            collection_name=payload.collection_name if payload else None,
        )
        raise HTTPException(status_code=503, detail=SCREENING_UNAVAILABLE_MESSAGE)
    except LockTimeoutError as e:
        emit_audit_event(
            "document.ingest",
            severity="WARNING",
            status="lock_timeout",
            http_status=409,
            uid=uid,
            collection_name=payload.collection_name if payload else None,
        )
        raise HTTPException(status_code=409, detail=str(e))
    except FileNotFoundError as e:
        logger.error(f"File not found error during ingestion for uid='{uid}': {e}")
        emit_audit_event(
            "document.ingest",
            severity="WARNING",
            status="not_found",
            http_status=404,
            uid=uid,
            collection_name=payload.collection_name if payload else None,
        )
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException as e:
        emit_audit_event(
            "document.ingest",
            severity="WARNING",
            status="rejected",
            uid=uid,
            http_status=e.status_code,
            collection_name=payload.collection_name if payload else None,
        )
        raise
    except Exception:
        logger.exception(f"Failed to ingest documents for uid='{uid}'.")
        emit_audit_event(
            "document.ingest",
            severity="ERROR",
            status="error",
            http_status=500,
            uid=uid,
            collection_name=payload.collection_name if payload else None,
        )
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_MESSAGE)


__all__ = ["ingest", "ingest_router"]

