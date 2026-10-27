import os
import re
from typing import Any, Dict, Optional, Sequence

from langchain_core.documents import Document
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from src.core.audit import record_retrieved_chunks
from src.core.chroma import get_vectorstore, require_collection
from src.core.security import (
    sanitize_retrieved_documents,
    validate_collection_name,
    validate_embedding_model,
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
    """Constructs a Vertex AI / Gemini-compatible function tool name for the collection."""
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", collection_name or "collection").strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"col_{cleaned}"
    return f"{cleaned}_retriever"[:64]


def _escape_xml_attr(value: str) -> str:
    """Escapes special characters inside XML attribute values."""
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _escape_chunk_text(text: str) -> str:
    """Escapes `<` and `>` inside retrieved document text to prevent `</retrieved_chunk>` tag breakout."""
    return text.replace("<", "&lt;").replace(">", "&gt;")


def _resolve_one_indexed_page(metadata: Dict[str, Any]) -> str:
    """Converts PyPDF's 0-indexed `page` metadata into a human-readable 1-indexed page string."""
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
    """Formats retrieved documents inside `<retrieved_chunk source="..." page="...">` spotlighting tags."""
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
):
    """Loads a Chroma vectorstore collection and creates an XML-spotlighted LangChain retriever tool."""
    safe_collection = validate_collection_name(collection_name)
    safe_embedding = validate_embedding_model(embedding_model)
    require_collection(persist_directory, safe_collection)

    vectorstore = get_vectorstore(
        persist_directory=persist_directory,
        collection_name=safe_collection,
        embedding_model=safe_embedding,
    )

    retriever = vectorstore.as_retriever(
        search_type="similarity", search_kwargs={"k": k}
    )

    tool_description = (
        description.format(collection_name=safe_collection)
        if "{collection_name}" in description
        else description
    )

    def _retrieve(query: str) -> str:
        docs = retriever.invoke(query)
        safe_docs = sanitize_retrieved_documents(docs)
        record_retrieved_chunks(len(safe_docs))
        return format_retrieved_chunks(safe_docs)

    return StructuredTool.from_function(
        func=_retrieve,
        name=safe_tool_name(safe_collection),
        description=tool_description,
        args_schema=RetrieverInput,
    )
