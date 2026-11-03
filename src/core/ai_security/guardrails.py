"""Model access controls and input/ingestion ceilings (Pillar 3: AI Security).

Enforces server-side allowlists and resource guardrails across all API endpoints:
- Approved generative LLM and embedding model catalogs (`ALLOWED_LLM_MODELS`, `ALLOWED_EMBEDDING_MODELS`).
- Strict collection identifier validation preventing prompt smuggling or path traversal.
- Query character length ceilings (`MAX_QUERY_CHARS`).
- Ingestion file-count (`MAX_FILES_PER_REQUEST`) and chunk-count (`MAX_CHUNKS_PER_INGEST`) ceilings.
"""

from typing import Any, Optional

from src.core.config import (
    COLLECTION_NAME_PATTERN,
    get_allowed_embedding_models,
    get_allowed_llm_models,
    get_max_chunks_per_ingest,
    get_max_files_per_request,
    get_max_query_chars,
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
        ValueError: If `file_count` exceeds `MAX_FILES_PER_REQUEST`.
    """
    max_files = get_max_files_per_request()
    if file_count > max_files:
        raise ValueError(
            f"Too many PDF files in a single request (received {file_count}, "
            f"maximum allowed is {max_files})."
        )
    return file_count


def validate_chunk_count(chunk_count: int) -> int:
    """Enforces the `MAX_CHUNKS_PER_INGEST` ceiling on split document chunks per request.

    Args:
        chunk_count: Cumulative number of text chunks produced across files in the request.

    Returns:
        Validated `chunk_count`.

    Raises:
        ValueError: If `chunk_count` exceeds `MAX_CHUNKS_PER_INGEST`.
    """
    max_chunks = get_max_chunks_per_ingest()
    if chunk_count > max_chunks:
        raise ValueError(
            f"Ingestion exceeded the maximum allowed {max_chunks} chunks per request "
            f"(already reached {chunk_count} chunks). Please upload smaller or fewer PDFs."
        )
    return chunk_count


__all__ = [
    "validate_chunk_count",
    "validate_collection_name",
    "validate_embedding_model",
    "validate_file_count",
    "validate_llm_model",
    "validate_query_text",
]
