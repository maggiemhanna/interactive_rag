import os
from typing import Any, Dict, List, Optional

from langchain_chroma import Chroma
from langchain_core.documents import Document

from src.core.chroma import get_vectorstore, require_collection
from src.core.logger import setup_logging
from src.core.security import sanitize_retrieved_documents
from src.core.storage import user_read_lock, user_write_lock

logger = setup_logging(name="ingestion.service")


def index_documents(
    documents: List[Document],
    collection_name: str,
    persist_directory: str = "chroma_db",
    embedding_model: Optional[str] = None,
) -> Chroma:
    """Initializes or loads a Chroma vectorstore and indexes document chunks into it.

    Args:
        documents: List of document chunks to add.
        collection_name: Name of the vector collection.
        persist_directory: Path to store Chroma DB.
        embedding_model: Name of the Vertex AI embedding model.

    Returns:
        Chroma vectorstore instance.
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
    """Mutates the user's isolated ChromaDB inside the GCS distributed write lock."""
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
    """Performs similarity search against a user's Chroma vectorstore collection and masks PII/secrets."""
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

    logger.info(
        f"Query '{query}' by uid='{uid}' against collection '{collection_name}' "
        f"returned {len(results)} results."
    )
    return results
