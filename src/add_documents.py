import contextlib
import os
import re
import shutil
import sys
import uuid
from typing import List, Optional, Tuple, Union

from dotenv import load_dotenv, find_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn
from langchain_community.document_loaders import PyPDFLoader
from langchain_core.documents import Document
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma

from utils.auth import get_allowed_origins, get_current_user, is_cloud_run_environment
from utils.chroma_storage import (
    ChromaLockTimeoutError,
    SQLITE_FILENAME,
    archive_user_upload_to_gcs,
    get_gcs_bucket_name,
    user_chroma_read_context,
    user_chroma_write_lock,
)
from utils.logging import setup_logging

# Load environment variables from .env file (searches parent directories and current directory / src)
load_dotenv(find_dotenv())
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logger = setup_logging(name="add_documents")

app = FastAPI(title="Document Ingestion API")

_allowed_origins = get_allowed_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials="*" not in _allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health_check():
    """Unauthenticated liveness/readiness probe endpoint for Cloud Run."""
    return {"status": "ok", "service": "ingestion-api"}


class AddDocumentsRequest(BaseModel):
    pdf_paths: Union[str, List[str]] = Field(
        ..., description="Path or list of paths (or a single path) to PDF files to ingest"
    )
    collection_name: str = Field(
        default="stock_market_vs", description="Name of the Chroma vectorstore collection"
    )
    embedding_model: str = Field(
        default="gemini-embedding-001", description="Name of the Vertex AI embedding model"
    )


class QueryRequest(BaseModel):
    query: str = Field(..., description="Query text to search for in vector store")
    collection_name: str = Field(
        default="stock_market_vs", description="Name of the Chroma vectorstore collection"
    )
    k: int = Field(default=5, description="Number of results to retrieve")
    embedding_model: str = Field(
        default="gemini-embedding-001", description="Name of the Vertex AI embedding model"
    )



def load_and_split_pdfs(
    pdf_paths: Union[str, List[str]], chunk_size: int = 1000, chunk_overlap: int = 200
) -> List[Document]:
    """Loads PDF documents and splits them into text chunks.

    Args:
        pdf_paths: Path or list of paths to PDF files.
        chunk_size: Target character count per chunk.
        chunk_overlap: Overlap character count between consecutive chunks.

    Returns:
        List of document chunks across all PDFs.
    """
    if isinstance(pdf_paths, str):
        pdf_paths = [pdf_paths]

    all_chunks: List[Document] = []
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap
    )

    for pdf_path in pdf_paths:
        if not os.path.exists(pdf_path):
            raise FileNotFoundError(f"PDF file not found: {pdf_path}")

        try:
            pdf_loader = PyPDFLoader(pdf_path)
            pages = pdf_loader.load()
            logger.info(f"Loaded PDF '{pdf_path}' ({len(pages)} pages).")
            chunks = text_splitter.split_documents(pages)
            logger.info(f"Split PDF '{pdf_path}' into {len(chunks)} chunks.")
            all_chunks.extend(chunks)
        except Exception as e:
            logger.error(f"Error loading PDF '{pdf_path}': {e}")
            raise

    return all_chunks


# Alias for backward compatibility
load_and_split_pdf = load_and_split_pdfs


def add_documents_to_vectorstore(
    documents: List[Document],
    persist_directory: str = "chroma_db",
    collection_name: str = "stock_market_vs",
    embedding_model: str = "gemini-embedding-001",
) -> Chroma:
    """Initializes or loads a Chroma vectorstore and adds documents to it.

    Args:
        documents: List of document chunks to add.
        persist_directory: Path to store Chroma DB.
        collection_name: Name of the vector collection.
        embedding_model: Name of the Vertex AI embedding model.

    Returns:
        Chroma vectorstore instance.
    """
    embeddings = GoogleGenerativeAIEmbeddings(
        model=embedding_model,
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        vertexai=True,
    )


    if not os.path.exists(persist_directory):
        os.makedirs(persist_directory)

    try:
        vectorstore = Chroma(
            persist_directory=persist_directory,
            embedding_function=embeddings,
            collection_name=collection_name,
        )

        batch_size = 100
        total_chunks = len(documents)
        for i in range(0, total_chunks, batch_size):
            batch = documents[i : i + batch_size]
            vectorstore.add_documents(documents=batch)
            logger.info(
                f"Ingested batch {i // batch_size + 1} / {(total_chunks - 1) // batch_size + 1} ({len(batch)} chunks) into collection '{collection_name}'."
            )

        logger.info(
            f"Successfully added {total_chunks} documents to collection '{collection_name}' in '{persist_directory}'."
        )
        return vectorstore
    except Exception as e:
        logger.error(f"Error updating vectorstore: {e}")
        raise


_SAFE_UPLOAD_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _sanitize_upload_filename(raw_filename: str) -> str:
    """Strips path components and enforces a safe .pdf filename."""
    basename = os.path.basename((raw_filename or "").replace("\\", "/")).strip()
    cleaned = _SAFE_UPLOAD_NAME_RE.sub("_", basename).strip("._")
    if not cleaned:
        cleaned = "document.pdf"
    if not cleaned.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail=f"Only PDF uploads are supported (received '{basename or raw_filename}').",
        )
    return cleaned


@app.get("/collections")
def get_collections(uid: str = Depends(get_current_user)):
    """Returns a list of existing collection details from Chroma vectorstore."""
    try:
        with user_chroma_read_context(uid) as persist_directory:
            sqlite_path = os.path.join(persist_directory, SQLITE_FILENAME)
            if not os.path.exists(persist_directory) or not os.path.exists(sqlite_path):
                return {"collections": [], "details": []}

            import chromadb

            client = chromadb.PersistentClient(path=persist_directory)
            collections = client.list_collections()
            collection_names = [col.name for col in collections]
            details = []
            for col in collections:
                try:
                    count = col.count()
                except Exception:
                    count = 0
                details.append({
                    "name": col.name,
                    "count": count,
                })
            return {"collections": collection_names, "details": details}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching collections for uid='{uid}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/collections/{collection_name}")
def delete_collection(collection_name: str, uid: str = Depends(get_current_user)):
    """Deletes an existing collection from Chroma vectorstore."""
    try:
        with user_chroma_write_lock(uid) as persist_directory:
            sqlite_path = os.path.join(persist_directory, SQLITE_FILENAME)
            if not os.path.exists(persist_directory) or not os.path.exists(sqlite_path):
                raise HTTPException(
                    status_code=404,
                    detail=f"Collection '{collection_name}' not found.",
                )

            import chromadb

            client = chromadb.PersistentClient(path=persist_directory)
            # Check if collection exists
            existing = [col.name for col in client.list_collections()]
            if collection_name not in existing:
                raise HTTPException(
                    status_code=404,
                    detail=f"Collection '{collection_name}' not found.",
                )

            client.delete_collection(name=collection_name)
            logger.info(f"Successfully deleted Chroma collection '{collection_name}' for uid='{uid}'.")
            return {
                "status": "success",
                "message": f"Successfully deleted collection '{collection_name}'.",
                "collection_name": collection_name,
            }
    except ChromaLockTimeoutError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting collection '{collection_name}' for uid='{uid}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/add_documents")
async def add_documents(request: Request, uid: str = Depends(get_current_user)):
    """FastAPI endpoint to load, split, and add PDF documents (via file upload or file paths) to Chroma vectorstore."""
    content_type = request.headers.get("content-type", "")
    raw_server_paths: List[str] = []
    uploaded_files_info: List[Tuple[str, str, Optional[str]]] = []  # (local_path, display_name, gcs_uri)
    collection_name = "stock_market_vs"
    embedding_model = "gemini-embedding-001"

    upload_tmp_root = os.path.abspath(os.getenv("UPLOAD_TMP_ROOT", "/tmp/uploads"))
    user_upload_dir = os.path.join(upload_tmp_root, uid)
    request_upload_dir: Optional[str] = None

    try:
        if "application/json" in content_type:
            body = await request.json()
            raw_paths = body.get("pdf_paths")
            if isinstance(raw_paths, str) and raw_paths.strip():
                raw_server_paths.append(raw_paths.strip())
            elif isinstance(raw_paths, list):
                raw_server_paths.extend([str(p).strip() for p in raw_paths if str(p).strip()])
            collection_name = str(body.get("collection_name") or collection_name).strip() or "stock_market_vs"
            embedding_model = str(body.get("embedding_model") or embedding_model).strip() or "gemini-embedding-001"
        elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
            async with request.form() as form:
                raw_col = form.get("collection_name", collection_name)
                if isinstance(raw_col, list):
                    raw_col = raw_col[0]
                collection_name = str(raw_col).strip() if raw_col else "stock_market_vs"

                raw_embedding = form.get("embedding_model", embedding_model)
                if isinstance(raw_embedding, list):
                    raw_embedding = raw_embedding[0]
                embedding_model = str(raw_embedding).strip() if raw_embedding else "gemini-embedding-001"

                # Handle uploaded files in an isolated per-user, per-request /tmp directory
                files = form.getlist("files")
                if not files and "file" in form:
                    files = form.getlist("file")

                valid_uploads = [f for f in files if hasattr(f, "filename") and f.filename]
                if valid_uploads:
                    request_upload_dir = os.path.join(user_upload_dir, uuid.uuid4().hex)
                    os.makedirs(request_upload_dir, exist_ok=True)
                    should_archive = bool(get_gcs_bucket_name()) and (
                        os.getenv("ARCHIVE_UPLOADS_TO_GCS", "true").strip().lower() in {"1", "true", "yes"}
                    )

                    used_names = set()
                    for idx, file_item in enumerate(valid_uploads):
                        safe_name = _sanitize_upload_filename(file_item.filename)
                        save_name = f"{idx}_{safe_name}" if safe_name in used_names else safe_name
                        used_names.add(save_name)

                        save_path = os.path.join(request_upload_dir, save_name)
                        content = await file_item.read()
                        if not content:
                            raise HTTPException(
                                status_code=400,
                                detail=f"Uploaded PDF '{safe_name}' is empty.",
                            )
                        with open(save_path, "wb") as f:
                            f.write(content)

                        gcs_uri = (
                            archive_user_upload_to_gcs(uid, save_path, safe_name)
                            if should_archive
                            else None
                        )
                        uploaded_files_info.append((save_path, safe_name, gcs_uri))

                # Handle pdf_paths field if provided in form
                paths_param = form.getlist("pdf_paths")
                for p in paths_param:
                    if isinstance(p, str) and p.strip():
                        for single_p in p.replace("\r\n", "\n").split("\n"):
                            single_p = single_p.strip()
                            if single_p:
                                raw_server_paths.append(single_p)
        else:
            try:
                body = await request.json()
                raw_paths = body.get("pdf_paths")
                if isinstance(raw_paths, str) and raw_paths.strip():
                    raw_server_paths.append(raw_paths.strip())
                elif isinstance(raw_paths, list):
                    raw_server_paths.extend([str(p).strip() for p in raw_paths if str(p).strip()])
                collection_name = str(body.get("collection_name") or collection_name).strip() or "stock_market_vs"
                embedding_model = str(body.get("embedding_model") or embedding_model).strip() or "gemini-embedding-001"
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="Unsupported Content-Type. Please send application/json or multipart/form-data.",
                )

        # Security guard: never allow arbitrary container filesystem reads in Cloud Run
        if raw_server_paths and is_cloud_run_environment():
            logger.warning(f"Blocked server-filesystem pdf_paths ingestion attempt in Cloud Run by uid='{uid}'.")
            raise HTTPException(
                status_code=403,
                detail="Server-side filesystem 'pdf_paths' ingestion is disabled in Cloud Run. Please upload PDF files directly.",
            )

        if not uploaded_files_info and not raw_server_paths:
            raise HTTPException(
                status_code=400,
                detail="No PDF files uploaded or PDF paths provided.",
            )

        all_chunks: List[Document] = []
        processed_files: List[str] = []

        # 1. Load & split uploaded files (with sanitized source metadata)
        for save_path, display_name, gcs_uri in uploaded_files_info:
            file_chunks = load_and_split_pdfs([save_path])
            for chunk in file_chunks:
                chunk.metadata["source"] = display_name
                if gcs_uri:
                    chunk.metadata["gcs_uri"] = gcs_uri
            all_chunks.extend(file_chunks)
            processed_files.append(display_name)

        # 2. Load & split local server paths (local dev mode only)
        if raw_server_paths:
            all_chunks.extend(load_and_split_pdfs(raw_server_paths))
            processed_files.extend(raw_server_paths)

        logger.info(
            f"Ingesting {len(all_chunks)} chunks for uid='{uid}' into collection '{collection_name}' "
            f"(embedding_model='{embedding_model}', files={processed_files})"
        )

        # 3. Mutate the user's isolated ChromaDB inside the GCS distributed write lock
        with user_chroma_write_lock(uid) as persist_directory:
            vectorstore = add_documents_to_vectorstore(
                documents=all_chunks,
                persist_directory=persist_directory,
                collection_name=collection_name,
                embedding_model=embedding_model,
            )
            del vectorstore

        return {
            "status": "success",
            "message": f"Successfully added {len(all_chunks)} document chunks to collection '{collection_name}'.",
            "chunks_added": len(all_chunks),
            "collection_name": collection_name,
            "processed_files": processed_files,
        }
    except ChromaLockTimeoutError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except FileNotFoundError as e:
        logger.error(f"File not found error: {e}")
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to add documents for uid='{uid}': {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if request_upload_dir and os.path.exists(request_upload_dir):
            shutil.rmtree(request_upload_dir, ignore_errors=True)
        with contextlib.suppress(OSError):
            if os.path.exists(user_upload_dir) and not os.listdir(user_upload_dir):
                os.rmdir(user_upload_dir)


@app.post("/query")
def query_collection(request: QueryRequest, uid: str = Depends(get_current_user)):
    """FastAPI endpoint to search/query documents in a specific Chroma vectorstore collection."""
    try:
        with user_chroma_read_context(uid) as persist_directory:
            sqlite_path = os.path.join(persist_directory, SQLITE_FILENAME)
            if not os.path.exists(persist_directory) or not os.path.exists(sqlite_path):
                raise HTTPException(
                    status_code=404,
                    detail=f"Collection '{request.collection_name}' not found.",
                )

            import chromadb

            client = chromadb.PersistentClient(path=persist_directory)
            existing = [col.name for col in client.list_collections()]
            if request.collection_name not in existing:
                raise HTTPException(
                    status_code=404,
                    detail=f"Collection '{request.collection_name}' not found.",
                )

            embeddings = GoogleGenerativeAIEmbeddings(
                model=request.embedding_model,
                project=os.getenv("GOOGLE_CLOUD_PROJECT"),
                vertexai=True,
            )
            vectorstore = Chroma(
                persist_directory=persist_directory,
                embedding_function=embeddings,
                collection_name=request.collection_name,
            )

            retriever = vectorstore.as_retriever(
                search_type="similarity", search_kwargs={"k": request.k}
            )
            docs = retriever.invoke(request.query)

            results = []
            for doc in docs:
                results.append({
                    "page_content": doc.page_content,
                    "metadata": doc.metadata,
                })

            logger.info(
                f"Query '{request.query}' by uid='{uid}' against collection '{request.collection_name}' "
                f"returned {len(results)} results."
            )

            return {
                "status": "success",
                "query": request.query,
                "collection_name": request.collection_name,
                "results_count": len(results),
                "results": results,
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to query vectorstore collection '{request.collection_name}' for uid='{uid}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


def main():
    port = int(os.getenv("PORT", "8000"))
    reload_enabled = os.getenv("UVICORN_RELOAD", "").strip().lower() in {"1", "true", "yes"}
    uvicorn.run("src.add_documents:app", host="0.0.0.0", port=port, reload=reload_enabled)


if __name__ == "__main__":
    main()