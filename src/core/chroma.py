import os
from typing import Any, Dict

import chromadb
from langchain_chroma import Chroma
from langchain_google_genai import GoogleGenerativeAIEmbeddings

from src.core.config import DEFAULT_COLLECTION_NAME, DEFAULT_EMBEDDING_MODEL
from src.core.storage import SQLITE_FILENAME


def get_embeddings(
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
) -> GoogleGenerativeAIEmbeddings:
    """Builds a Vertex AI-backed GoogleGenerativeAIEmbeddings instance."""
    return GoogleGenerativeAIEmbeddings(
        model=embedding_model,
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        vertexai=True,
    )


def get_vectorstore(
    persist_directory: str = "chroma_db",
    collection_name: str = DEFAULT_COLLECTION_NAME,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
) -> Chroma:
    """Initializes or loads a Chroma vectorstore instance for a given directory and collection."""
    return Chroma(
        persist_directory=persist_directory,
        embedding_function=get_embeddings(embedding_model=embedding_model),
        collection_name=collection_name,
    )


def has_chroma_sqlite(persist_directory: str) -> bool:
    """Returns True if a Chroma SQLite database exists inside `persist_directory`."""
    sqlite_path = os.path.join(persist_directory, SQLITE_FILENAME)
    return os.path.exists(persist_directory) and os.path.exists(sqlite_path)


def verify_collection_exists(persist_directory: str, collection_name: str) -> None:
    """Verifies that the user's Chroma SQLite database and requested collection exist."""
    if not has_chroma_sqlite(persist_directory):
        raise FileNotFoundError(f"Collection '{collection_name}' not found.")

    client = chromadb.PersistentClient(path=persist_directory)
    existing = [col.name for col in client.list_collections()]
    if collection_name not in existing:
        raise FileNotFoundError(f"Collection '{collection_name}' not found.")


def list_collections(persist_directory: str) -> Dict[str, Any]:
    """Lists all collections and their chunk counts in `persist_directory`."""
    if not has_chroma_sqlite(persist_directory):
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
    """Deletes `collection_name` from `persist_directory`, raising FileNotFoundError if absent."""
    verify_collection_exists(persist_directory, collection_name)
    client = chromadb.PersistentClient(path=persist_directory)
    client.delete_collection(name=collection_name)
