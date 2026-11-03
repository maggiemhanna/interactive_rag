"""XML-spotlighted and Cloud DLP-sanitized LangChain retriever tool builder.

Constructs a Gemini-compatible `StructuredTool` over a tenant's Chroma collection that:
1. Retrieves top-`k` passages via similarity search.
2. Masks sensitive PII/secrets concurrently through Cloud Model Armor + Cloud DLP.
3. Records retrieved passage counts on the active request context for EU AI Act grounding provenance.
4. Encloses untrusted document text inside `<retrieved_chunk source="..." page="...">` XML tags
   with angle-bracket escaping to prevent indirect prompt injection tag breakout.
"""

import os
import re
from typing import Any, Dict, Optional, Sequence

from langchain_core.documents import Document
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from src.core.ai_security.armor import sanitize_retrieved_documents
from src.core.ai_security.guardrails import (
    validate_collection_name,
    validate_embedding_model,
)
from src.core.compliance.state import record_retrieved_chunks
from src.core.observability.telemetry import record_retrieval_metrics
from src.core.observability.tracing import start_span
from src.core.storage import (
    get_vectorstore,
    require_collection,
    similarity_search_with_scores,
)

EMPTY_RETRIEVAL_MARKER = (
    '<retrieved_context status="empty">'
    "No matching document chunks were found in this collection."
    "</retrieved_context>"
)


class RetrieverInput(BaseModel):
    """Input schema for the XML-spotlighted collection retriever tool."""

    query: str = Field(
        ...,
        description="Search query used to retrieve relevant passages from the document collection.",
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


def _escape_chunk_text(text: str) -> str:
    """Escapes `<` and `>` inside retrieved document text to prevent `</retrieved_chunk>` breakout.

    Args:
        text: Sanitized chunk body text.

    Returns:
        Chunk body text with angle brackets escaped as `&lt;` and `&gt;`.
    """
    return text.replace("<", "&lt;").replace(">", "&gt;")


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
        XML-spotlighted passage string, or `EMPTY_RETRIEVAL_MARKER` when `docs` is empty.
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
        safe_content = _escape_chunk_text(doc.page_content or "")
        rendered_blocks.append(
            f'<retrieved_chunk source="{safe_source}" page="{safe_page}">\n'
            f"{safe_content}\n"
            f"</retrieved_chunk>"
        )

    return "\n\n".join(rendered_blocks)


def get_retriever_tool(
    collection_name: str,
    persist_directory: str = "chroma_db",
    k: int = 5,
    embedding_model: Optional[str] = None,
    description: str = (
        "Search and retrieve relevant document chunks from collection '{collection_name}'. "
        "Passages are returned inside <retrieved_chunk source=\"...\" page=\"...\"> XML tags."
    ),
) -> StructuredTool:
    """Loads a Chroma collection and creates an XML-spotlighted LangChain `StructuredTool`.

    Args:
        collection_name: Validated Chroma collection identifier.
        persist_directory: Local filesystem path of the tenant's ChromaDB directory.
        k: Number of top matching chunks retrieved per tool call (defaults to `5`).
        embedding_model: Optional approved Vertex AI embedding model identifier.
        description: Tool description template exposed to the Gemini tool-calling model.

    Returns:
        Configured `StructuredTool` ready to bind to `ChatGoogleGenerativeAI`.

    Raises:
        FileNotFoundError: If `collection_name` does not exist in `persist_directory`.
    """
    safe_collection = validate_collection_name(collection_name)
    safe_embedding = validate_embedding_model(embedding_model)
    require_collection(persist_directory, safe_collection)

    vectorstore = get_vectorstore(
        persist_directory=persist_directory,
        collection_name=safe_collection,
        embedding_model=safe_embedding,
    )

    tool_description = (
        description.format(collection_name=safe_collection)
        if "{collection_name}" in description
        else description
    )

    def _retrieve(query: str) -> str:
        """Executes similarity search, sanitizes chunks via DLP, and formats XML spotlight blocks."""
        with start_span(
            "agent.retriever_tool",
            stage="retrieval",
            collection_name=safe_collection,
            k=k,
        ) as span:
            docs, scored = similarity_search_with_scores(vectorstore, query, k=k)
            metrics = record_retrieval_metrics(scored, is_tool_call=True)
            safe_docs = sanitize_retrieved_documents(docs)
            record_retrieved_chunks(len(safe_docs))
            span["attributes"]["retrieved_chunks"] = len(safe_docs)
            if metrics.get("top_similarity_score") is not None:
                span["attributes"]["top_similarity_score"] = metrics["top_similarity_score"]
            return format_retrieved_chunks(safe_docs)

    return StructuredTool.from_function(
        func=_retrieve,
        name=safe_tool_name(safe_collection),
        description=tool_description,
        args_schema=RetrieverInput,
    )


__all__ = [
    "EMPTY_RETRIEVAL_MARKER",
    "RetrieverInput",
    "format_retrieved_chunks",
    "get_retriever_tool",
    "safe_tool_name",
]

