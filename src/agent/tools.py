"""XML-spotlighted and Cloud DLP-sanitized LangChain retriever tool builder.

Constructs a Gemini-compatible `StructuredTool` over a tenant's Chroma collection that:
1. Retrieves top-`k` chunks via similarity search.
2. Masks sensitive PII/secrets concurrently through Cloud Model Armor + Cloud DLP.
3. Records retrieved chunk counts on the active request context for EU AI Act grounding provenance.
4. Encloses untrusted document text inside `<retrieved_chunk source="..." page="...">` XML tags
   with angle-bracket escaping to prevent indirect prompt injection tag breakout.
"""

from typing import Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from src.core.ai_security.armor import sanitize_retrieved_documents
from src.core.ai_security.guardrails import (
    EMPTY_RETRIEVAL_MARKER,
    format_retrieved_chunks,
    safe_tool_name,
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


class RetrieverInput(BaseModel):
    """Input schema for the XML-spotlighted collection retriever tool."""

    query: str = Field(
        ...,
        description="Search query used to retrieve relevant chunks from the document collection.",
    )


def get_retriever_tool(
    collection_name: str,
    persist_directory: str = "chroma_db",
    k: int = 5,
    embedding_model: Optional[str] = None,
    description: str = (
        "Search and retrieve relevant document chunks from collection '{collection_name}'. "
        "Chunks are returned inside <retrieved_chunk source=\"...\" page=\"...\"> XML tags."
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

