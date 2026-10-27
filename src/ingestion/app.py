"""FastAPI Document Ingestion & Vector Search microservice (`ingestion-api`).

Exposes authenticated `POST /ingest` and `POST /query` endpoints alongside the shared
`/health`, `/collections`, and `/compliance/my-data` routes wired by `create_app`.
"""

import os
from typing import Any, Dict

from fastapi import Depends, HTTPException, Request
import uvicorn

from src.core.ai_security.safety import INTERNAL_ERROR_MESSAGE
from src.core.app_factory import create_app
from src.core.auth import get_current_user
from src.core.compliance.audit import emit_audit_event, hash_query
from src.core.config import INGESTION_PORT, is_reload_enabled
from src.core.logger import setup_logging
from src.core.storage import LockTimeoutError
from src.ingestion.schemas import QueryRequest
from src.ingestion.service import ingest_documents, search_collection
from src.ingestion.upload_parser import prepare_payload

logger = setup_logging(name="ingestion")

app = create_app(title="Document Ingestion API", service_name="ingestion-api")


@app.post("/ingest")
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
    try:
        payload = await prepare_payload(request=request, uid=uid)

        logger.info(
            f"Ingesting {len(payload.chunks)} chunks for uid='{uid}' into collection '{payload.collection_name}' "
            f"(embedding_model='{payload.embedding_model}', files={payload.processed_files})"
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

        return {
            "status": "success",
            "message": f"Successfully added {len(payload.chunks)} document chunks to collection '{payload.collection_name}'.",
            "chunks_added": len(payload.chunks),
            "collection_name": payload.collection_name,
            "processed_files": payload.processed_files,
        }
    except LockTimeoutError as e:
        emit_audit_event(
            "document.ingest",
            severity="WARNING",
            status="lock_timeout",
            uid=uid,
        )
        raise HTTPException(status_code=409, detail=str(e))
    except FileNotFoundError as e:
        logger.error(f"File not found error: {e}")
        emit_audit_event(
            "document.ingest",
            severity="WARNING",
            status="not_found",
            uid=uid,
        )
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException as e:
        emit_audit_event(
            "document.ingest",
            severity="WARNING",
            status="rejected",
            uid=uid,
            http_status=e.status_code,
        )
        raise
    except Exception:
        logger.exception(f"Failed to ingest documents for uid='{uid}'")
        emit_audit_event(
            "document.ingest",
            severity="ERROR",
            status="error",
            uid=uid,
        )
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_MESSAGE)


@app.post("/query")
def query(
    request: QueryRequest,
    uid: str = Depends(get_current_user),
) -> Dict[str, Any]:
    """Executes top-`k` vector similarity search over a user's Chroma collection.

    Args:
        request: Validated `QueryRequest` specifying `query`, `collection_name`, `k`, and `embedding_model`.
        uid: Verified tenant user identifier injected by `get_current_user`.

    Returns:
        JSON response containing sanitized passage `results` and `results_count`.
    """
    query_digest = hash_query(request.query)
    query_len = len(request.query)
    try:
        results = search_collection(
            uid=uid,
            query=request.query,
            collection_name=request.collection_name,
            k=request.k,
            embedding_model=request.embedding_model,
        )
        emit_audit_event(
            "vectorstore.query",
            uid=uid,
            collection_name=request.collection_name,
            embedding_model=request.embedding_model,
            k=request.k,
            results_count=len(results),
            query_sha256=query_digest,
            query_chars=query_len,
        )
        return {
            "status": "success",
            "query": request.query,
            "collection_name": request.collection_name,
            "results_count": len(results),
            "results": results,
        }
    except FileNotFoundError as e:
        emit_audit_event(
            "vectorstore.query",
            severity="WARNING",
            status="not_found",
            uid=uid,
            collection_name=request.collection_name,
            query_sha256=query_digest,
            query_chars=query_len,
        )
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            f"Failed to query vectorstore collection '{request.collection_name}' for uid='{uid}'"
        )
        emit_audit_event(
            "vectorstore.query",
            severity="ERROR",
            status="error",
            uid=uid,
            collection_name=request.collection_name,
            query_sha256=query_digest,
            query_chars=query_len,
        )
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_MESSAGE)


def main() -> None:
    """Starts the Uvicorn HTTP server for `ingestion-api` on `PORT` (default `8000`)."""
    port = int(os.getenv("PORT", str(INGESTION_PORT)))
    uvicorn.run(
        "src.ingestion.app:app",
        host="0.0.0.0",
        port=port,
        reload=is_reload_enabled(),
    )


if __name__ == "__main__":
    main()
