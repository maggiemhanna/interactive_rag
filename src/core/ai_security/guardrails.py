"""Model access controls, XML spotlighting, and input/ingestion guardrails (Pillar 3: AI Security).

Enforces server-side allowlists, indirect prompt injection defenses, and resource ceilings:
- Approved generative LLM and embedding model catalogs (`ALLOWED_LLM_MODELS`, `ALLOWED_EMBEDDING_MODELS`).
- Strict collection identifier validation and Gemini-safe tool naming (`safe_tool_name`).
- XML Spotlighting (`format_retrieved_chunks`) wrapping untrusted chunks in `<retrieved_chunk>` tags
  with angle-bracket escaping to prevent indirect prompt injection tag breakout.
- Query character length ceilings (`MAX_QUERY_CHARS`).
- PDF upload filename sanitization (`sanitize_filename`), binary `%PDF-` magic header and
  `MAX_UPLOAD_MB` payload validation (`validate_pdf_bytes`), Cloud Run server-path blocking
  (`validate_server_pdf_paths`), and file/chunk count ceilings (`validate_file_count`, `validate_chunk_count`).
"""

import os
import re
from typing import Any, Dict, Optional, Sequence

from fastapi import HTTPException
from langchain_core.documents import Document

from src.core.config import (
    COLLECTION_NAME_PATTERN,
    get_allowed_embedding_models,
    get_allowed_llm_models,
    get_max_chunks_per_ingest,
    get_max_files_per_request,
    get_max_query_chars,
    get_max_upload_mb,
    is_cloud_run,
)
from src.core.logger import setup_logging
from src.core.storage.paths import SAFE_FILENAME_RE

logger = setup_logging(name="ai_security.guardrails")

_PDF_MAGIC_PREFIX = b"%PDF-"

EMPTY_RETRIEVAL_MARKER = (
    '<retrieved_context status="empty">'
    "No matching document chunks were found in this collection."
    "</retrieved_context>"
)


def validate_collection_name(collection_name: Optional[str]) -> str:
    """Validates that `collection_name` is explicitly provided and matches a safe identifier pattern.

    Prevents prompt smuggling, shell/SQL metacharacters, and directory traversal via
    Chroma collection names or Gemini tool names.

    Args:
        collection_name: Candidate Chroma collection identifier supplied by the caller.

    Returns:
        Trimmed collection name matching `^[a-zA-Z0-9_-]{1,64}$`.

    Raises:
        ValueError: If `collection_name` is None, blank, or fails pattern validation.
    """
    if collection_name is None or not isinstance(collection_name, str):
        raise ValueError("collection_name is required.")
    cleaned = collection_name.strip()
    if not cleaned:
        raise ValueError("collection_name is required and cannot be empty.")
    if not COLLECTION_NAME_PATTERN.match(cleaned):
        raise ValueError(
            "Invalid collection_name. Use 1-64 characters consisting only of letters, numbers, "
            "hyphens (-), and underscores (_)."
        )
    return cleaned


def validate_llm_model(model_name: Optional[Any] = None) -> str:
    """Validates `model_name` against the server-enforced `ALLOWED_LLM_MODELS` catalog.

    Defaults to the first approved model in `ALLOWED_LLM_MODELS` when `model_name` is omitted
    or blank.

    Args:
        model_name: Optional Vertex AI generative model identifier requested by the client.

    Returns:
        Approved generative LLM model identifier.

    Raises:
        ValueError: If `model_name` is not a string, not in `ALLOWED_LLM_MODELS`, or the catalog env var is unset.
    """
    if model_name is not None and not isinstance(model_name, str):
        raise ValueError("model_name must be a string.")
    allowed = get_allowed_llm_models()
    cleaned = (model_name or "").strip() or allowed[0]
    if cleaned not in allowed:
        allowed_csv = ", ".join(allowed)
        raise ValueError(
            f"Unsupported model_name '{cleaned}'. Approved LLM models: {allowed_csv}."
        )
    return cleaned


def validate_embedding_model(embedding_model: Optional[Any] = None) -> str:
    """Validates `embedding_model` against the server-enforced `ALLOWED_EMBEDDING_MODELS` catalog.

    Defaults to the first approved model in `ALLOWED_EMBEDDING_MODELS` when `embedding_model`
    is omitted or blank.

    Args:
        embedding_model: Optional Vertex AI embedding model identifier requested by the client.

    Returns:
        Approved embedding model identifier.

    Raises:
        ValueError: If `embedding_model` is not a string, not in `ALLOWED_EMBEDDING_MODELS`, or the catalog is unset.
    """
    if embedding_model is not None and not isinstance(embedding_model, str):
        raise ValueError("embedding_model must be a string.")
    allowed = get_allowed_embedding_models()
    cleaned = (embedding_model or "").strip() or allowed[0]
    if cleaned not in allowed:
        allowed_csv = ", ".join(allowed)
        raise ValueError(
            f"Unsupported embedding_model '{cleaned}'. Approved embedding models: {allowed_csv}."
        )
    return cleaned


def validate_query_text(query: Optional[str]) -> str:
    """Validates that a user search or agent query is non-empty and within `MAX_QUERY_CHARS`.

    Args:
        query: Raw user query string from a REST or LangGraph invocation.

    Returns:
        Trimmed, length-validated query string.

    Raises:
        ValueError: If `query` is missing, whitespace-only, or exceeds `MAX_QUERY_CHARS`.
    """
    if query is None or not isinstance(query, str):
        raise ValueError("Query text must be a non-empty string.")
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("Query text cannot be empty or whitespace only.")
    max_chars = get_max_query_chars()
    if len(cleaned) > max_chars:
        raise ValueError(
            f"Query exceeds the maximum length of {max_chars} characters (received {len(cleaned)})."
        )
    return cleaned


def validate_file_count(file_count: int) -> int:
    """Enforces the `MAX_FILES_PER_REQUEST` ceiling on PDF ingestion requests.

    Args:
        file_count: Total number of uploaded PDFs and/or local PDF paths in the request.

    Returns:
        Validated `file_count`.

    Raises:
        HTTPException: HTTP 400 if `file_count` exceeds `MAX_FILES_PER_REQUEST`.
    """
    max_files = get_max_files_per_request()
    if file_count > max_files:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Too many PDF files in a single request (received {file_count}, "
                f"maximum allowed is {max_files})."
            ),
        )
    return file_count


def validate_chunk_count(chunk_count: int) -> int:
    """Enforces the `MAX_CHUNKS_PER_INGEST` ceiling on split document chunks per request.

    Args:
        chunk_count: Cumulative number of text chunks produced across files in the request.

    Returns:
        Validated `chunk_count`.

    Raises:
        HTTPException: HTTP 400 if `chunk_count` exceeds `MAX_CHUNKS_PER_INGEST`.
    """
    max_chunks = get_max_chunks_per_ingest()
    if chunk_count > max_chunks:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Ingestion exceeded the maximum allowed {max_chunks} chunks per request "
                f"(already reached {chunk_count} chunks). Please upload smaller or fewer PDFs."
            ),
        )
    return chunk_count


def sanitize_filename(raw_filename: str) -> str:
    """Strips directory traversal components and enforces a safe `.pdf` filename.

    Args:
        raw_filename: Client-supplied multipart upload filename.

    Returns:
        Sanitized basename containing only safe characters and ending with `.pdf`.

    Raises:
        HTTPException: HTTP 400 if the filename does not have a `.pdf` extension.
    """
    basename = os.path.basename((raw_filename or "").replace("\\", "/")).strip()
    cleaned = SAFE_FILENAME_RE.sub("_", basename).strip("._")
    if not cleaned:
        cleaned = "document.pdf"
    if not cleaned.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail=f"Only PDF uploads are supported (received '{basename or raw_filename}').",
        )
    return cleaned


def validate_pdf_bytes(content: bytes, filename: str) -> bytes:
    """Validates that uploaded file bytes are non-empty, within `MAX_UPLOAD_MB`, and carry `%PDF-` magic bytes.

    Args:
        content: Raw bytes read from the uploaded multipart file (up to `max_upload_bytes + 1`).
        filename: Sanitized display filename for error reporting.

    Returns:
        Validated `content` bytes.

    Raises:
        HTTPException: HTTP 400 if empty or missing `%PDF-` header; HTTP 413 if exceeding `MAX_UPLOAD_MB`.
    """
    max_upload_mb = get_max_upload_mb()
    max_upload_bytes = max_upload_mb * 1024 * 1024
    if not content:
        raise HTTPException(
            status_code=400,
            detail=f"Uploaded PDF '{filename}' is empty.",
        )
    if len(content) > max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Uploaded PDF '{filename}' exceeds the maximum allowed size "
                f"of {max_upload_mb} MB."
            ),
        )
    if not content.lstrip().startswith(_PDF_MAGIC_PREFIX):
        raise HTTPException(
            status_code=400,
            detail=f"Uploaded file '{filename}' is not a valid PDF document.",
        )
    return content


def validate_server_pdf_paths(raw_server_paths: Sequence[str], uid: str) -> None:
    """Blocks server-side filesystem `pdf_paths` ingestion when executing inside Cloud Run (`K_SERVICE`).

    Args:
        raw_server_paths: Candidate local filesystem paths supplied in the request.
        uid: Verified tenant user identifier for security logging.

    Raises:
        HTTPException: HTTP 403 if `raw_server_paths` is non-empty on Cloud Run.
    """
    if raw_server_paths and is_cloud_run():
        logger.warning(
            f"Blocked server-filesystem pdf_paths ingestion attempt in Cloud Run by uid='{uid}'."
        )
        raise HTTPException(
            status_code=403,
            detail="Server-side filesystem 'pdf_paths' ingestion is disabled in Cloud Run. Please upload PDF files directly.",
        )


def safe_tool_name(collection_name: str) -> str:
    """Constructs a Vertex AI / Gemini-compatible function tool identifier for `collection_name`.

    Args:
        collection_name: Validated Chroma collection name.

    Returns:
        Safe tool name (`<= 64` chars, starting with a letter/underscore) ending in `_retriever`.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", collection_name or "collection").strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"col_{cleaned}"
    return f"{cleaned}_retriever"[:64]


def _escape_xml_attr(value: str) -> str:
    """Escapes special XML characters inside `source` and `page` attribute values.

    Args:
        value: Raw attribute string.

    Returns:
        XML-escaped attribute string.
    """
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _resolve_one_indexed_page(metadata: Dict[str, Any]) -> str:
    """Converts PyPDF's 0-indexed `page` metadata into a human-readable 1-indexed page string.

    Args:
        metadata: Document chunk metadata dictionary from ChromaDB.

    Returns:
        1-indexed page label string (defaults to `'1'` when absent).
    """
    raw_page = metadata.get("page")
    if raw_page is not None and not isinstance(raw_page, bool):
        try:
            page_num = int(raw_page)
            return str(page_num + 1 if page_num >= 0 else 1)
        except (TypeError, ValueError):
            pass

    page_label = str(metadata.get("page_label") or "").strip()
    if page_label:
        return page_label

    return "1"


def format_retrieved_chunks(docs: Sequence[Document]) -> str:
    """Formats retrieved documents inside `<retrieved_chunk source="..." page="...">` spotlighting tags.

    Args:
        docs: Sequence of sanitized LangChain `Document` chunks.

    Returns:
        XML-spotlighted chunk string, or `EMPTY_RETRIEVAL_MARKER` when `docs` is empty.
    """
    if not docs:
        return EMPTY_RETRIEVAL_MARKER

    rendered_blocks = []
    for doc in docs:
        metadata = doc.metadata or {}
        raw_source = str(metadata.get("source") or "unknown").strip()
        source_name = os.path.basename(raw_source.replace("\\", "/")) or raw_source or "unknown"
        safe_source = _escape_xml_attr(source_name)
        safe_page = _escape_xml_attr(_resolve_one_indexed_page(metadata))
        safe_content = (doc.page_content or "").replace("<", "&lt;").replace(">", "&gt;")
        rendered_blocks.append(
            f'<retrieved_chunk source="{safe_source}" page="{safe_page}">\n'
            f"{safe_content}\n"
            f"</retrieved_chunk>"
        )

    return "\n\n".join(rendered_blocks)


__all__ = [
    "EMPTY_RETRIEVAL_MARKER",
    "format_retrieved_chunks",
    "safe_tool_name",
    "sanitize_filename",
    "validate_chunk_count",
    "validate_collection_name",
    "validate_embedding_model",
    "validate_file_count",
    "validate_llm_model",
    "validate_pdf_bytes",
    "validate_query_text",
    "validate_server_pdf_paths",
]

