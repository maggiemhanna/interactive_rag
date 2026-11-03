"""Regional Vertex AI embeddings and ChromaDB collection lifecycle helpers.

Pins `GoogleGenerativeAIEmbeddings` to the validated single GCP region (`GOOGLE_CLOUD_LOCATION`),
validates embedding model and collection identifiers, and provides collection inspection,
loading, and deletion operations over a tenant's `persist_directory`.
"""

import os
from typing import Any, Dict, List, Optional, Tuple

import chromadb
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_google_genai import GoogleGenerativeAIEmbeddings

from src.core.ai_security.guardrails import (
    validate_collection_name,
    validate_embedding_model,
)
from src.core.compliance.residency import validate_gcp_region
from src.core.config import (
    get_allowed_embedding_models,
    get_google_cloud_project,
)
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
        project=get_google_cloud_project() or None,
        location=safe_location,
        vertexai=True,
    )


def _infer_collection_embedding_model(col: Any) -> Optional[str]:
    """Resolves a Chroma collection's approved embedding model from metadata or vector dimension."""
    try:
        allowed = get_allowed_embedding_models()
    except Exception:
        allowed = ()

    col_meta = getattr(col, "metadata", None) or {}
    if isinstance(col_meta, dict):
        raw_model = col_meta.get("embedding_model")
        if isinstance(raw_model, str) and raw_model.strip():
            candidate = raw_model.strip()
            if not allowed or candidate in allowed:
                return candidate

    try:
        if hasattr(col, "get"):
            sample = col.get(limit=1, include=["embeddings"])
            embeddings = sample.get("embeddings") if isinstance(sample, dict) else None
            if embeddings is not None and len(embeddings) > 0 and embeddings[0] is not None:
                dim = len(embeddings[0])
                if dim == 3072 and (not allowed or "gemini-embedding-001" in allowed):
                    return "gemini-embedding-001"
    except Exception:
        pass

    return None


def get_collection_embedding_model(
    persist_directory: str,
    collection_name: str,
) -> Optional[str]:
    """Looks up the stored or inferred embedding model for `collection_name` in `persist_directory`.

    Args:
        persist_directory: Local tenant ChromaDB directory path.
        collection_name: Validated Chroma collection identifier.

    Returns:
        Approved embedding model identifier string if stored/inferred on the collection, else `None`.
    """
    safe_collection = validate_collection_name(collection_name)
    if not has_sqlite(persist_directory):
        return None
    try:
        client = chromadb.PersistentClient(path=persist_directory)
        for col in client.list_collections():
            if getattr(col, "name", None) == safe_collection:
                return _infer_collection_embedding_model(col)
    except Exception:
        return None
    return None


def resolve_collection_embedding_model(
    persist_directory: str,
    collection_name: str,
    embedding_model: Optional[str] = None,
) -> str:
    """Resolves the effective embedding model for `collection_name`, preferring the collection's stored model.

    Always validates `embedding_model` against the approved embedding model catalog first so unapproved
    model identifiers are rejected consistently, then returns the collection's stored embedding model
    when `collection_name` already exists in `persist_directory`.

    Args:
        persist_directory: Local tenant ChromaDB directory path.
        collection_name: Target Chroma collection identifier.
        embedding_model: Optional caller-requested embedding model identifier.

    Returns:
        Validated embedding model identifier bound to `collection_name`.
    """
    fallback_model = validate_embedding_model(embedding_model)
    stored_model = get_collection_embedding_model(persist_directory, collection_name)
    return stored_model or fallback_model


def get_vectorstore(
    collection_name: str,
    persist_directory: str = "chroma_db",
    embedding_model: Optional[str] = None,
) -> Chroma:
    """Initializes or loads a `Chroma` vectorstore instance for `persist_directory` and `collection_name`.

    Automatically binds `embedding_function` to the collection's stored embedding model when the
    collection already exists in `persist_directory`, ensuring query and retrieval embeddings
    remain in the same vector space as the indexed documents.

    Args:
        collection_name: Validated Chroma collection identifier.
        persist_directory: Local filesystem path of the tenant's ChromaDB directory.
        embedding_model: Optional approved Vertex AI embedding model identifier (used when creating a new collection).

    Returns:
        Initialized `langchain_chroma.Chroma` vectorstore instance.
    """
    safe_collection = validate_collection_name(collection_name)
    resolved_model = resolve_collection_embedding_model(
        persist_directory=persist_directory,
        collection_name=safe_collection,
        embedding_model=embedding_model,
    )
    return Chroma(
        persist_directory=persist_directory,
        embedding_function=get_embeddings(embedding_model=resolved_model),
        collection_name=safe_collection,
    )


def similarity_search_with_scores(
    vectorstore: Any,
    query: str,
    k: int = 5,
) -> Tuple[List[Document], List[Any]]:
    """Executes top-k similarity search and converts raw distances into normalized `[0.0, 1.0]` scores.

    Falls back to `vectorstore.as_retriever(search_type="similarity", search_kwargs={"k": k})`
    when `vectorstore` is a lightweight test double that does not implement
    `similarity_search_with_score`.

    Args:
        vectorstore: Initialized `Chroma` vectorstore (or retriever-compatible mock).
        query: Validated search query string.
        k: Maximum number of chunks to retrieve.

    Returns:
        Tuple of `(docs, scored_items)` where `docs` is `List[Document]` and `scored_items`
        is suitable for `record_retrieval_metrics(...)`.
    """
    if hasattr(vectorstore, "similarity_search_with_score"):
        raw_pairs = vectorstore.similarity_search_with_score(query, k=k)
        docs = [doc for doc, _ in raw_pairs]
        scored: List[Any] = [
            (doc, round(1.0 / (1.0 + max(0.0, float(dist))), 4))
            for doc, dist in raw_pairs
        ]
        return docs, scored

    retriever = vectorstore.as_retriever(
        search_type="similarity",
        search_kwargs={"k": k},
    )
    docs = list(retriever.invoke(query))
    return docs, docs


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
        Dictionary containing `'collections'` (list of names) and `'details'` (`[{'name', 'count', 'embedding_model'}]`).
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
        emb_model = _infer_collection_embedding_model(col)
        entry: Dict[str, Any] = {"name": col.name, "count": count}
        if isinstance(emb_model, str) and emb_model.strip():
            entry["embedding_model"] = emb_model.strip()
        details.append(entry)
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
    "get_collection_embedding_model",
    "get_embeddings",
    "get_vectorstore",
    "has_sqlite",
    "list_collections",
    "remove_collection",
    "require_collection",
    "resolve_collection_embedding_model",
    "similarity_search_with_scores",
]

