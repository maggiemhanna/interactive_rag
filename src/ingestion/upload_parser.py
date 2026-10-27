"""HTTP upload parser, binary PDF validator, and per-request `/tmp` staging manager.

Handles both `multipart/form-data` binary PDF uploads and local-dev `application/json`
`pdf_paths` payloads for `POST /ingest`:
- Blocks `pdf_paths` server filesystem reads inside Cloud Run (`HTTP 403`).
- Validates `%PDF-` magic header bytes, non-empty payloads, `MAX_UPLOAD_MB` (`HTTP 413`),
  `MAX_FILES_PER_REQUEST` (`HTTP 400`), and `MAX_CHUNKS_PER_INGEST` (`HTTP 400`).
- Optionally archives validated PDFs to `gs://<bucket>/users/{uid}/uploads/` and guarantees
  per-request `/tmp/uploads/{uid}/{request_uuid}` cleanup inside a `finally` block.
"""

import contextlib
from dataclasses import dataclass
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple
import uuid

from fastapi import HTTPException, Request
from langchain_core.documents import Document

from src.core.ai_security.guardrails import (
    validate_chunk_count,
    validate_collection_name,
    validate_embedding_model,
    validate_file_count,
)
from src.core.config import get_max_upload_mb, is_cloud_run
from src.core.logger import setup_logging
from src.core.storage import archive_upload, get_bucket_name, get_upload_user_dir
from src.ingestion.splitter import sanitize_filename, split_pdfs

logger = setup_logging(name="ingestion.upload_parser")

_PDF_MAGIC_PREFIX = b"%PDF-"


@dataclass
class IngestPayload:
    """Validated and chunked document ingestion payload ready for Chroma indexing.

    Attributes:
        chunks: Split LangChain `Document` chunks enriched with `source` and optional `gcs_uri`.
        processed_files: Ordered list of sanitized filenames or local paths processed.
        collection_name: Validated target Chroma collection identifier.
        embedding_model: Approved Vertex AI embedding model identifier.
    """

    chunks: List[Document]
    processed_files: List[str]
    collection_name: str
    embedding_model: str


def _parse_json_body(
    body: Dict[str, Any],
) -> Tuple[List[str], Optional[str], Optional[str]]:
    """Extracts `pdf_paths`, `collection_name`, and `embedding_model` from a JSON request body.

    Args:
        body: Parsed JSON dictionary from `await request.json()`.

    Returns:
        Tuple `(raw_server_paths, collection_name, embedding_model)`.

    Raises:
        HTTPException: HTTP 422 if `embedding_model` is provided with a non-string type.
    """
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
    """Validates `collection_name` and `embedding_model`, raising HTTP 422 on invalid input.

    Args:
        collection_name: Candidate Chroma collection identifier.
        embedding_model: Optional candidate Vertex AI embedding model identifier.

    Returns:
        Tuple `(safe_collection_name, safe_embedding_model)`.

    Raises:
        HTTPException: HTTP 422 when either parameter fails guardrail validation.
    """
    try:
        safe_col = validate_collection_name(collection_name)
        safe_emb = validate_embedding_model(embedding_model)
        return safe_col, safe_emb
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


def _enforce_file_ceiling(count: int) -> None:
    """Enforces `MAX_FILES_PER_REQUEST`, mapping `ValueError` to `HTTP 400`."""
    try:
        validate_file_count(count)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _enforce_chunk_ceiling(count: int) -> None:
    """Enforces `MAX_CHUNKS_PER_INGEST`, mapping `ValueError` to `HTTP 400`."""
    try:
        validate_chunk_count(count)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


async def prepare_payload(request: Request, uid: str) -> IngestPayload:
    """Parses JSON or multipart PDF uploads, enforces upload ceilings, splits chunks, and cleans `/tmp`.

    Args:
        request: Incoming FastAPI `Request` (`multipart/form-data` or `application/json`).
        uid: Verified tenant user identifier.

    Returns:
        `IngestPayload` containing validated document chunks and collection metadata.

    Raises:
        HTTPException: HTTP 400/403/413/422 when validation, signature, or ceiling checks fail.
        FileNotFoundError: When a local-dev `pdf_paths` entry does not exist on disk.
    """
    content_type = request.headers.get("content-type", "")
    raw_server_paths: List[str] = []
    uploaded_files_info: List[Tuple[str, str, Optional[str]]] = []  # (local_path, display_name, gcs_uri)
    collection_name: str = ""
    embedding_model: str = ""

    max_upload_mb = get_max_upload_mb()
    max_upload_bytes = max_upload_mb * 1024 * 1024

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
                _enforce_file_ceiling(len(valid_uploads) + len(raw_server_paths))

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

        _enforce_file_ceiling(len(raw_server_paths))

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
            _enforce_chunk_ceiling(len(all_chunks))
            processed_files.append(display_name)

        # 2. Load & split local server paths (local dev mode only)
        for server_path in raw_server_paths:
            file_chunks = split_pdfs([server_path])
            all_chunks.extend(file_chunks)
            _enforce_chunk_ceiling(len(all_chunks))
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


__all__ = ["IngestPayload", "prepare_payload"]
