import re

from langchain_core.tools.retriever import create_retriever_tool

from src.core.chroma import get_vectorstore, require_collection
from src.core.config import DEFAULT_COLLECTION, DEFAULT_EMBEDDING_MODEL


def safe_tool_name(collection_name: str) -> str:
    """Constructs a Vertex AI / Gemini-compatible function tool name for the collection."""
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", collection_name or "collection").strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"col_{cleaned}"
    return f"{cleaned}_retriever"[:64]


def get_retriever_tool(
    collection_name: str = DEFAULT_COLLECTION,
    persist_directory: str = "chroma_db",
    k: int = 5,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    description: str = "Search and retrieve relevant document chunks from collection '{collection_name}'.",
):
    """Loads a Chroma vectorstore collection and creates a LangChain retriever tool."""
    require_collection(persist_directory, collection_name)

    vectorstore = get_vectorstore(
        persist_directory=persist_directory,
        collection_name=collection_name,
        embedding_model=embedding_model,
    )

    retriever = vectorstore.as_retriever(
        search_type="similarity", search_kwargs={"k": k}
    )

    tool_description = (
        description.format(collection_name=collection_name)
        if "{collection_name}" in description
        else description
    )

    return create_retriever_tool(
        retriever,
        name=safe_tool_name(collection_name),
        description=tool_description,
    )
