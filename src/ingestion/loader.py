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
from src.core.config import DEFAULT_COLLECTION, DEFAULT_EMBEDDING_MODEL
from src.core.logger import setup_logging
from src.core.storage import archive_upload, get_bucket_name

logger = setup_logging(name="ingestion.loader")

_SAFE_UPLOAD_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


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
    default_collection: str,
    default_embedding: str,
) -> Tuple[List[str], str, str]:
    raw_server_paths: List[str] = []
    raw_paths = body.get("pdf_paths")
    if isinstance(raw_paths, str) and raw_paths.strip():
        raw_server_paths.append(raw_paths.strip())
    elif isinstance(raw_paths, list):
        raw_server_paths.extend([str(p).strip() for p in raw_paths if str(p).strip()])

    collection_name = (
        str(body.get("collection_name") or default_collection).strip() or default_collection
    )
    embedding_model = (
        str(body.get("embedding_model") or default_embedding).strip() or default_embedding
    )
    return raw_server_paths, collection_name, embedding_model


async def prepare_payload(request: Request, uid: str) -> IngestPayload:
    """Parses JSON or multipart PDF uploads, stages/archives files safely, splits chunks, and cleans /tmp."""
    content_type = request.headers.get("content-type", "")
    raw_server_paths: List[str] = []
    uploaded_files_info: List[Tuple[str, str, Optional[str]]] = []  # (local_path, display_name, gcs_uri)
    collection_name = DEFAULT_COLLECTION
    embedding_model = DEFAULT_EMBEDDING_MODEL

    upload_tmp_root = os.path.abspath(os.getenv("UPLOAD_TMP_ROOT", "/tmp/uploads"))
    user_upload_dir = os.path.join(upload_tmp_root, uid)
    request_upload_dir: Optional[str] = None

    try:
        if "application/json" in content_type:
            body = await request.json()
            raw_server_paths, collection_name, embedding_model = _parse_json_body(
                body, collection_name, embedding_model
            )
        elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
            async with request.form() as form:
                raw_col = form.get("collection_name", collection_name)
                if isinstance(raw_col, list):
                    raw_col = raw_col[0]
                collection_name = str(raw_col).strip() if raw_col else DEFAULT_COLLECTION

                raw_embedding = form.get("embedding_model", embedding_model)
                if isinstance(raw_embedding, list):
                    raw_embedding = raw_embedding[0]
                embedding_model = (
                    str(raw_embedding).strip() if raw_embedding else DEFAULT_EMBEDDING_MODEL
                )

                # Handle uploaded files in an isolated per-user, per-request /tmp directory
                files = form.getlist("files")
                if not files and "file" in form:
                    files = form.getlist("file")

                valid_uploads = [f for f in files if hasattr(f, "filename") and f.filename]
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
                            archive_upload(uid, save_path, safe_name)
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
                raw_server_paths, collection_name, embedding_model = _parse_json_body(
                    body, collection_name, embedding_model
                )
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="Unsupported Content-Type. Please send application/json or multipart/form-data.",
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

        if not uploaded_files_info and not raw_server_paths:
            raise HTTPException(
                status_code=400,
                detail="No PDF files uploaded or PDF paths provided.",
            )

        all_chunks: List[Document] = []
        processed_files: List[str] = []

        # 1. Load & split uploaded files (with sanitized source metadata)
        for save_path, display_name, gcs_uri in uploaded_files_info:
            file_chunks = split_pdfs([save_path])
            for chunk in file_chunks:
                chunk.metadata["source"] = display_name
                if gcs_uri:
                    chunk.metadata["gcs_uri"] = gcs_uri
            all_chunks.extend(file_chunks)
            processed_files.append(display_name)

        # 2. Load & split local server paths (local dev mode only)
        if raw_server_paths:
            all_chunks.extend(split_pdfs(raw_server_paths))
            processed_files.extend(raw_server_paths)

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
