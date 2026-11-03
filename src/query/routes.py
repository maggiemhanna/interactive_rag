"""FastAPI `/query` router (`src.query.routes`).

Exposes authenticated `POST /query` similarity search mounted on `agent-api` so read-path
vector queries share the warm `/tmp/chroma_db/{uid}` cache and `_QUERY_RATE_LIMITER` with
`/agent` and `/collections` and never contend with CPU-bound PDF ingestion.
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from src.core.ai_security.armor import SecurityScreeningUnavailableError
from src.core.ai_security.safety import (
    INTERNAL_ERROR_MESSAGE,
    SCREENING_UNAVAILABLE_MESSAGE,
)
from src.core.auth import get_current_user
from src.core.compliance.audit import emit_audit_event
from src.core.compliance.policy import enforce_query_rate_limit
from src.core.logger import setup_logging
from src.core.observability.telemetry import emit_telemetry_event
from src.query.schemas import QueryRequest
from src.query.service import search_collection

logger = setup_logging(name="query.routes")

query_router = APIRouter()


@query_router.post("/query")
def query(
    request: QueryRequest,
    uid: str = Depends(get_current_user),
) -> Dict[str, Any]:
    """Executes top-`k` vector similarity search over a user's Chroma collection.

    Args:
        request: Validated `QueryRequest` specifying `query`, `collection_name`, `k`, and `embedding_model`.
        uid: Verified tenant user identifier injected by `get_current_user`.

    Returns:
        JSON response containing sanitized chunk `results` and `results_count`.
    """
    try:
        enforce_query_rate_limit(uid)
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
            query=request.query,
        )
        emit_telemetry_event(
            "vectorstore.query",
            uid=uid,
            collection_name=request.collection_name,
            embedding_model=request.embedding_model,
            k=request.k,
            results_count=len(results),
            query=request.query,
        )
        return {
            "status": "success",
            "query": request.query,
            "collection_name": request.collection_name,
            "results_count": len(results),
            "results": results,
        }
    except SecurityScreeningUnavailableError as e:
        emit_audit_event(
            "vectorstore.query",
            severity="ERROR",
            status="screening_unavailable",
            reason=e.reason,
            http_status=503,
            uid=uid,
            collection_name=request.collection_name,
            query=request.query,
        )
        raise HTTPException(status_code=503, detail=SCREENING_UNAVAILABLE_MESSAGE)
    except FileNotFoundError as e:
        emit_audit_event(
            "vectorstore.query",
            severity="WARNING",
            status="not_found",
            http_status=404,
            uid=uid,
            collection_name=request.collection_name,
            query=request.query,
        )
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            f"Failed to query vectorstore collection '{request.collection_name}' for uid='{uid}'."
        )
        emit_audit_event(
            "vectorstore.query",
            severity="ERROR",
            status="error",
            http_status=500,
            uid=uid,
            collection_name=request.collection_name,
            query=request.query,
        )
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR_MESSAGE)


__all__ = ["query", "query_router"]

