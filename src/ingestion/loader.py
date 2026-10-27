import contextlib
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

from fastapi import HTTPException, Request
from langchain_community.document_loaders import PyPDFLoader
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from src.core.auth import is_cloud_run
from src.core.config import (
    get_max_chunks_per_ingest,
    get_max_files_per_request,
    get_max_upload_mb,
)
from src.core.logger import setup_logging
from src.core.security import validate_collection_name, validate_embedding_model
from src.core.storage import archive_upload, get_bucket_name, get_upload_user_dir

logger = setup_logging(name="ingestion.loader")

_SAFE_UPLOAD_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_PDF_MAGIC_PREFIX = b"%PDF-"


@dataclass
class IngestPayload:
    chunks: List[Document]
    processed_files: List[str]
    collection_name: str
    embedding_model: str


def sanitize_filename(raw_filename: str) -> str:
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


def split_pdfs(
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


def _parse_json_body(
    body: dict,
) -> Tuple[List[str], Optional[str], Optional[str]]:
    raw_server_paths: List[str] = []
    raw_paths = body.get("pdf_paths")
    if isinstance(raw_paths, str) and raw_paths.strip():
        raw_server_paths.append(raw_paths.strip())
    elif isinstance(raw_paths, list):
        raw_server_paths.extend([str(p).strip() for p in raw_paths if str(p).strip()])

    raw_col = body.get("collection_name")
    collection_name = raw_col if isinstance(raw_col, str) else None
    raw_emb = body.get("embedding_model")
    if raw_emb is not None and not isinstance(raw_emb, str):
        raise HTTPException(status_code=422, detail="embedding_model must be a string.")
    return raw_server_paths, collection_name, raw_emb


def _validate_ingest_params(
    collection_name: Optional[str],
    embedding_model: Optional[str],
) -> Tuple[str, str]:
    """Validates `collection_name` and `embedding_model`, raising HTTP 422 on invalid input."""
    try:
        safe_col = validate_collection_name(collection_name)
        safe_emb = validate_embedding_model(embedding_model)
        return safe_col, safe_emb
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


async def prepare_payload(request: Request, uid: str) -> IngestPayload:
    """Parses JSON or multipart PDF uploads, enforces upload ceilings, splits chunks, and cleans /tmp."""
    content_type = request.headers.get("content-type", "")
    raw_server_paths: List[str] = []
    uploaded_files_info: List[Tuple[str, str, Optional[str]]] = []  # (local_path, display_name, gcs_uri)
    collection_name: str = ""
    embedding_model: str = ""

    max_files = get_max_files_per_request()
    max_upload_mb = get_max_upload_mb()
    max_upload_bytes = max_upload_mb * 1024 * 1024
    max_chunks = get_max_chunks_per_ingest()

    user_upload_dir = get_upload_user_dir(uid)
    request_upload_dir: Optional[str] = None

    try:
        if "application/json" in content_type:
            body = await request.json()
            raw_server_paths, raw_col, raw_emb = _parse_json_body(body)
            collection_name, embedding_model = _validate_ingest_params(
                raw_col, raw_emb
            )
        elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
            async with request.form() as form:
                raw_col = form.get("collection_name")
                if isinstance(raw_col, list):
                    raw_col = raw_col[0] if raw_col else None
                col_str = raw_col if isinstance(raw_col, str) else None

                raw_embedding = form.get("embedding_model")
                if isinstance(raw_embedding, list):
                    raw_embedding = raw_embedding[0] if raw_embedding else None
                emb_str = raw_embedding if isinstance(raw_embedding, str) else None

                collection_name, embedding_model = _validate_ingest_params(
                    col_str, emb_str
                )

                # Handle pdf_paths field if provided in form
                paths_param = form.getlist("pdf_paths")
                for p in paths_param:
                    if isinstance(p, str) and p.strip():
                        for single_p in p.replace("\r\n", "\n").split("\n"):
                            single_p = single_p.strip()
                            if single_p:
                                raw_server_paths.append(single_p)

                # Handle uploaded files in an isolated per-user, per-request /tmp directory
                files = form.getlist("files")
                if not files and "file" in form:
                    files = form.getlist("file")

                valid_uploads = [f for f in files if hasattr(f, "filename") and f.filename]
                total_requested_files = len(valid_uploads) + len(raw_server_paths)
                if total_requested_files > max_files:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Too many PDF files in a single request (received {total_requested_files}, "
                            f"maximum allowed is {max_files})."
                        ),
                    )

                if valid_uploads:
                    request_upload_dir = os.path.join(user_upload_dir, uuid.uuid4().hex)
                    os.makedirs(request_upload_dir, exist_ok=True)
                    should_archive = bool(get_bucket_name()) and (
                        os.getenv("ARCHIVE_UPLOADS_TO_GCS", "true").strip().lower()
                        in {"1", "true", "yes"}
                    )

                    used_names = set()
                    for idx, file_item in enumerate(valid_uploads):
                        safe_name = sanitize_filename(file_item.filename)
                        save_name = f"{idx}_{safe_name}" if safe_name in used_names else safe_name
                        used_names.add(save_name)

                        # Bounded read: read at most max_upload_bytes + 1 so oversized uploads fail fast
                        content = await file_item.read(max_upload_bytes + 1)
                        if not content:
                            raise HTTPException(
                                status_code=400,
                                detail=f"Uploaded PDF '{safe_name}' is empty.",
                            )
                        if len(content) > max_upload_bytes:
                            raise HTTPException(
                                status_code=413,
                                detail=(
                                    f"Uploaded PDF '{safe_name}' exceeds the maximum allowed size "
                                    f"of {max_upload_mb} MB."
                                ),
                            )
                        if not content.lstrip().startswith(_PDF_MAGIC_PREFIX):
                            raise HTTPException(
                                status_code=400,
                                detail=f"Uploaded file '{safe_name}' is not a valid PDF document.",
                            )

                        save_path = os.path.join(request_upload_dir, save_name)
                        with open(save_path, "wb") as f:
                            f.write(content)

                        gcs_uri = (
                            archive_upload(uid, save_path, safe_name)
                            if should_archive
                            else None
                        )
                        uploaded_files_info.append((save_path, safe_name, gcs_uri))
        else:
            try:
                body = await request.json()
                raw_server_paths, raw_col, raw_emb = _parse_json_body(body)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="Unsupported Content-Type. Please send application/json or multipart/form-data.",
                )
            collection_name, embedding_model = _validate_ingest_params(
                raw_col, raw_emb
            )

        # Security guard: never allow arbitrary container filesystem reads in Cloud Run
        if raw_server_paths and is_cloud_run():
            logger.warning(
                f"Blocked server-filesystem pdf_paths ingestion attempt in Cloud Run by uid='{uid}'."
            )
            raise HTTPException(
                status_code=403,
                detail="Server-side filesystem 'pdf_paths' ingestion is disabled in Cloud Run. Please upload PDF files directly.",
            )

        if len(raw_server_paths) > max_files:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Too many PDF files in a single request (received {len(raw_server_paths)}, "
                    f"maximum allowed is {max_files})."
                ),
            )

        if not uploaded_files_info and not raw_server_paths:
            raise HTTPException(
                status_code=400,
                detail="No PDF files uploaded or PDF paths provided.",
            )

        all_chunks: List[Document] = []
        processed_files: List[str] = []

        # 1. Load & split uploaded files (preserving source, page, and gcs_uri metadata)
        for save_path, display_name, gcs_uri in uploaded_files_info:
            file_chunks = split_pdfs([save_path])
            for chunk in file_chunks:
                chunk.metadata["source"] = display_name
                if gcs_uri:
                    chunk.metadata["gcs_uri"] = gcs_uri
            all_chunks.extend(file_chunks)
            if len(all_chunks) > max_chunks:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Ingestion exceeded the maximum allowed {max_chunks} chunks per request "
                        f"(already reached {len(all_chunks)} chunks). Please upload smaller or fewer PDFs."
                    ),
                )
            processed_files.append(display_name)

        # 2. Load & split local server paths (local dev mode only)
        for server_path in raw_server_paths:
            file_chunks = split_pdfs([server_path])
            all_chunks.extend(file_chunks)
            if len(all_chunks) > max_chunks:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Ingestion exceeded the maximum allowed {max_chunks} chunks per request "
                        f"(already reached {len(all_chunks)} chunks). Please upload smaller or fewer PDFs."
                    ),
                )
            processed_files.append(server_path)

        return IngestPayload(
            chunks=all_chunks,
            processed_files=processed_files,
            collection_name=collection_name,
            embedding_model=embedding_model,
        )
    finally:
        if request_upload_dir and os.path.exists(request_upload_dir):
            shutil.rmtree(request_upload_dir, ignore_errors=True)
        with contextlib.suppress(OSError):
            if os.path.exists(user_upload_dir) and not os.listdir(user_upload_dir):
                os.rmdir(user_upload_dir)
