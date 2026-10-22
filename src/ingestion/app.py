import os

import uvicorn
from fastapi import Depends, HTTPException, Request

from src.core.auth import get_current_user
from src.core.config import INGESTION_PORT, is_reload_enabled
from src.core.logger import setup_logging
from src.core.routes import create_app
from src.core.storage import LockTimeoutError
from src.ingestion.loader import prepare_payload
from src.ingestion.schemas import QueryRequest
from src.ingestion.service import ingest_documents, search_collection

logger = setup_logging(name="ingestion")

app = create_app(title="Document Ingestion API", service_name="ingestion-api")


@app.post("/ingest")
async def ingest(request: Request, uid: str = Depends(get_current_user)):
    """FastAPI endpoint to load, split, and ingest PDF documents (via file upload or file paths) into Chroma vectorstore."""
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

        return {
            "status": "success",
            "message": f"Successfully added {len(payload.chunks)} document chunks to collection '{payload.collection_name}'.",
            "chunks_added": len(payload.chunks),
            "collection_name": payload.collection_name,
            "processed_files": payload.processed_files,
        }
    except LockTimeoutError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except FileNotFoundError as e:
        logger.error(f"File not found error: {e}")
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to ingest documents for uid='{uid}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/query")
def query(request: QueryRequest, uid: str = Depends(get_current_user)):
    """FastAPI endpoint to search/query documents in a specific Chroma vectorstore collection."""
    try:
        results = search_collection(
            uid=uid,
            query=request.query,
            collection_name=request.collection_name,
            k=request.k,
            embedding_model=request.embedding_model,
        )
        return {
            "status": "success",
            "query": request.query,
            "collection_name": request.collection_name,
            "results_count": len(results),
            "results": results,
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to query vectorstore collection '{request.collection_name}' for uid='{uid}': {e}"
        )
        raise HTTPException(status_code=500, detail=str(e))


def main() -> None:
    port = int(os.getenv("PORT", str(INGESTION_PORT)))
    uvicorn.run(
        "src.ingestion.app:app",
        host="0.0.0.0",
        port=port,
        reload=is_reload_enabled(),
    )


if __name__ == "__main__":
    main()
