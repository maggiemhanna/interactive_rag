from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import src.core.config  # noqa: F401 - ensures .env files are loaded
from src.core.auth import get_allowed_origins, get_current_user
from src.core.chroma import list_collections, remove_collection
from src.core.logger import setup_logging
from src.core.storage import (
    ChromaLockTimeoutError,
    user_chroma_read_context,
    user_chroma_write_lock,
)

logger = setup_logging(name="routes")

collections_router = APIRouter()


@collections_router.get("/collections")
def get_collections(uid: str = Depends(get_current_user)):
    """Returns a list of existing collection details from Chroma vectorstore."""
    try:
        with user_chroma_read_context(uid) as persist_directory:
            return list_collections(persist_directory)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching collections for uid='{uid}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


@collections_router.delete("/collections/{collection_name}")
def delete_collection(collection_name: str, uid: str = Depends(get_current_user)):
    """Deletes an existing collection from Chroma vectorstore."""
    try:
        with user_chroma_write_lock(uid) as persist_directory:
            remove_collection(persist_directory, collection_name)
            logger.info(
                f"Successfully deleted Chroma collection '{collection_name}' for uid='{uid}'."
            )
            return {
                "status": "success",
                "message": f"Successfully deleted collection '{collection_name}'.",
                "collection_name": collection_name,
            }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ChromaLockTimeoutError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting collection '{collection_name}' for uid='{uid}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


def create_app(title: str, service_name: str) -> FastAPI:
    """Creates a FastAPI service configured with CORS, /health probe, and shared /collections routes."""
    app = FastAPI(title=title)

    allowed_origins = get_allowed_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials="*" not in allowed_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health_check():
        """Unauthenticated liveness/readiness probe endpoint for Cloud Run."""
        return {"status": "ok", "service": service_name}

    app.include_router(collections_router)
    return app
