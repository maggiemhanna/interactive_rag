"""ChromaDB batch indexing and Cloud DLP-sanitized similarity search orchestration.

Coordinates multi-tenant vector storage writes inside `user_write_lock` and executes
similarity searches inside `user_read_lock` with concurrent Model Armor / Cloud DLP
chunk masking and PII-free SHA-256 query logging.
"""

import os
from typing import Any, Dict, List, Optional

from langchain_chroma import Chroma
from langchain_core.documents import Document

from src.core.ai_security.armor import sanitize_retrieved_documents
from src.core.compliance.audit import hash_query
from src.core.logger import setup_logging
from src.core.storage import (
    get_vectorstore,
    require_collection,
    user_read_lock,
    user_write_lock,
)

logger = setup_logging(name="ingestion.service")


def index_documents(
    documents: List[Document],
    collection_name: str,
    persist_directory: str = "chroma_db",
    embedding_model: Optional[str] = None,
) -> Chroma:
    """Initializes or loads a Chroma vectorstore and indexes document chunks in batches of 100.

    Args:
        documents: List of split `Document` chunks to embed and store.
        collection_name: Validated target Chroma collection identifier.
        persist_directory: Local filesystem path of the ChromaDB directory.
        embedding_model: Optional approved Vertex AI embedding model identifier.

    Returns:
        Updated `langchain_chroma.Chroma` vectorstore instance.
    """
    if not os.path.exists(persist_directory):
        os.makedirs(persist_directory)

    try:
        vectorstore = get_vectorstore(
            collection_name=collection_name,
            persist_directory=persist_directory,
            embedding_model=embedding_model,
        )

        batch_size = 100
        total_chunks = len(documents)
        for i in range(0, total_chunks, batch_size):
            batch = documents[i : i + batch_size]
            vectorstore.add_documents(documents=batch)
            logger.info(
                f"Ingested batch {i // batch_size + 1} / {(total_chunks - 1) // batch_size + 1} "
                f"({len(batch)} chunks) into collection '{collection_name}'."
            )

        logger.info(
            f"Successfully added {total_chunks} documents to collection '{collection_name}' in '{persist_directory}'."
        )
        return vectorstore
    except Exception as e:
        logger.error(f"Error updating vectorstore: {e}")
        raise


def ingest_documents(
    uid: str,
    documents: List[Document],
    collection_name: str,
    embedding_model: Optional[str] = None,
) -> None:
    """Indexes document chunks into `uid`'s isolated ChromaDB inside the distributed GCS write lock.

    Args:
        uid: Verified tenant user identifier.
        documents: List of split `Document` chunks to index.
        collection_name: Validated target Chroma collection identifier.
        embedding_model: Optional approved Vertex AI embedding model identifier.
    """
    with user_write_lock(uid) as persist_directory:
        vectorstore = index_documents(
            documents=documents,
            collection_name=collection_name,
            persist_directory=persist_directory,
            embedding_model=embedding_model,
        )
        del vectorstore


def search_collection(
    uid: str,
    query: str,
    collection_name: str,
    k: int = 5,
    embedding_model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Performs top-`k` similarity search on a user's Chroma collection and masks sensitive spans.

    Args:
        uid: Verified tenant user identifier.
        query: Validated user search query string.
        collection_name: Validated Chroma collection identifier.
        k: Number of top matching passages to retrieve (`1..50`).
        embedding_model: Optional approved Vertex AI embedding model identifier.

    Returns:
        List of `{'page_content': ..., 'metadata': ...}` dictionaries after SDP/DLP sanitization.

    Raises:
        FileNotFoundError: If `collection_name` does not exist for `uid`.
    """
    with user_read_lock(uid) as persist_directory:
        require_collection(persist_directory, collection_name)
        vectorstore = get_vectorstore(
            collection_name=collection_name,
            persist_directory=persist_directory,
            embedding_model=embedding_model,
        )
        retriever = vectorstore.as_retriever(
            search_type="similarity", search_kwargs={"k": k}
        )
        docs = retriever.invoke(query)

    safe_docs = sanitize_retrieved_documents(docs)
    results = [
        {
            "page_content": doc.page_content,
            "metadata": doc.metadata,
        }
        for doc in safe_docs
    ]

    query_digest = hash_query(query)
    logger.info(
        f"Similarity search by uid='{uid}' against collection '{collection_name}' "
        f"(query_sha256='{query_digest}', query_chars={len(query)}) returned {len(results)} results."
    )
    return results


__all__ = ["index_documents", "ingest_documents", "search_collection"]
