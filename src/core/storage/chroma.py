"""Regional Vertex AI embeddings and ChromaDB collection lifecycle helpers.

Pins `GoogleGenerativeAIEmbeddings` to the validated single GCP region (`GOOGLE_CLOUD_LOCATION`),
validates embedding model and collection identifiers, and provides collection inspection,
loading, and deletion operations over a tenant's `persist_directory`.
"""

import os
from typing import Any, Dict, Optional

import chromadb
from langchain_chroma import Chroma
from langchain_google_genai import GoogleGenerativeAIEmbeddings

from src.core.ai_security.guardrails import (
    validate_collection_name,
    validate_embedding_model,
)
from src.core.compliance.residency import validate_gcp_region
from src.core.storage.paths import SQLITE_FILENAME


def get_embeddings(
    embedding_model: Optional[str] = None,
) -> GoogleGenerativeAIEmbeddings:
    """Builds a regionally pinned Vertex AI `GoogleGenerativeAIEmbeddings` instance.

    Args:
        embedding_model: Optional approved embedding model name; defaults to primary catalog model.

    Returns:
        Configured `GoogleGenerativeAIEmbeddings` client bound to `GOOGLE_CLOUD_LOCATION`.

    Raises:
        ValueError: If `embedding_model` is unapproved or `GOOGLE_CLOUD_LOCATION` is non-regional.
    """
    safe_model = validate_embedding_model(embedding_model)
    safe_location = validate_gcp_region()
    return GoogleGenerativeAIEmbeddings(
        model=safe_model,
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        location=safe_location,
        vertexai=True,
    )


def get_vectorstore(
    collection_name: str,
    persist_directory: str = "chroma_db",
    embedding_model: Optional[str] = None,
) -> Chroma:
    """Initializes or loads a `Chroma` vectorstore instance for `persist_directory` and `collection_name`.

    Args:
        collection_name: Validated Chroma collection identifier.
        persist_directory: Local filesystem path of the tenant's ChromaDB directory.
        embedding_model: Optional approved Vertex AI embedding model identifier.

    Returns:
        Initialized `langchain_chroma.Chroma` vectorstore instance.
    """
    safe_collection = validate_collection_name(collection_name)
    return Chroma(
        persist_directory=persist_directory,
        embedding_function=get_embeddings(embedding_model=embedding_model),
        collection_name=safe_collection,
    )


def has_sqlite(persist_directory: str) -> bool:
    """Checks whether a Chroma SQLite database file exists inside `persist_directory`.

    Args:
        persist_directory: Local tenant ChromaDB directory path.

    Returns:
        True when `<persist_directory>/chroma.sqlite3` exists on disk.
    """
    sqlite_path = os.path.join(persist_directory, SQLITE_FILENAME)
    return os.path.exists(persist_directory) and os.path.exists(sqlite_path)


def require_collection(persist_directory: str, collection_name: str) -> None:
    """Verifies that the tenant's Chroma SQLite database and `collection_name` exist.

    Args:
        persist_directory: Local tenant ChromaDB directory path.
        collection_name: Target Chroma collection name to check.

    Raises:
        FileNotFoundError: If the SQLite database or `collection_name` does not exist.
    """
    if not has_sqlite(persist_directory):
        raise FileNotFoundError(f"Collection '{collection_name}' not found.")

    client = chromadb.PersistentClient(path=persist_directory)
    existing = [col.name for col in client.list_collections()]
    if collection_name not in existing:
        raise FileNotFoundError(f"Collection '{collection_name}' not found.")


def list_collections(persist_directory: str) -> Dict[str, Any]:
    """Lists all Chroma collections and their document chunk counts inside `persist_directory`.

    Args:
        persist_directory: Local tenant ChromaDB directory path.

    Returns:
        Dictionary containing `'collections'` (list of names) and `'details'` (`[{'name', 'count'}]`).
    """
    if not has_sqlite(persist_directory):
        return {"collections": [], "details": []}

    client = chromadb.PersistentClient(path=persist_directory)
    collections = client.list_collections()
    collection_names = [col.name for col in collections]
    details = []
    for col in collections:
        try:
            count = col.count()
        except Exception:
            count = 0
        details.append({"name": col.name, "count": count})
    return {"collections": collection_names, "details": details}


def remove_collection(persist_directory: str, collection_name: str) -> None:
    """Deletes `collection_name` from `persist_directory`, raising `FileNotFoundError` if absent.

    Args:
        persist_directory: Local tenant ChromaDB directory path.
        collection_name: Target Chroma collection name to delete.

    Raises:
        FileNotFoundError: If `collection_name` does not exist in `persist_directory`.
    """
    require_collection(persist_directory, collection_name)
    client = chromadb.PersistentClient(path=persist_directory)
    client.delete_collection(name=collection_name)


__all__ = [
    "get_embeddings",
    "get_vectorstore",
    "has_sqlite",
    "list_collections",
    "remove_collection",
    "require_collection",
]
