import os
from typing import Any, Dict, List

from langchain_chroma import Chroma
from langchain_core.documents import Document

from src.core.chroma import get_vectorstore, verify_collection_exists
from src.core.config import DEFAULT_COLLECTION_NAME, DEFAULT_EMBEDDING_MODEL
from src.core.logger import setup_logging
from src.core.storage import user_chroma_read_context, user_chroma_write_lock

logger = setup_logging(name="ingestion.service")


def add_documents_to_vectorstore(
    documents: List[Document],
    persist_directory: str = "chroma_db",
    collection_name: str = DEFAULT_COLLECTION_NAME,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
) -> Chroma:
    """Initializes or loads a Chroma vectorstore and adds documents to it.

    Args:
        documents: List of document chunks to add.
        persist_directory: Path to store Chroma DB.
        collection_name: Name of the vector collection.
        embedding_model: Name of the Vertex AI embedding model.

    Returns:
        Chroma vectorstore instance.
    """
    if not os.path.exists(persist_directory):
        os.makedirs(persist_directory)

    try:
        vectorstore = get_vectorstore(
            persist_directory=persist_directory,
            collection_name=collection_name,
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


def ingest_user_documents(
    uid: str,
    documents: List[Document],
    collection_name: str = DEFAULT_COLLECTION_NAME,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
) -> None:
    """Mutates the user's isolated ChromaDB inside the GCS distributed write lock."""
    with user_chroma_write_lock(uid) as persist_directory:
        vectorstore = add_documents_to_vectorstore(
            documents=documents,
            persist_directory=persist_directory,
            collection_name=collection_name,
            embedding_model=embedding_model,
        )
        del vectorstore


def query_user_collection(
    uid: str,
    query: str,
    collection_name: str = DEFAULT_COLLECTION_NAME,
    k: int = 5,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
) -> List[Dict[str, Any]]:
    """Performs similarity search against a user's Chroma vectorstore collection."""
    with user_chroma_read_context(uid) as persist_directory:
        verify_collection_exists(persist_directory, collection_name)
        vectorstore = get_vectorstore(
            persist_directory=persist_directory,
            collection_name=collection_name,
            embedding_model=embedding_model,
        )
        retriever = vectorstore.as_retriever(
            search_type="similarity", search_kwargs={"k": k}
        )
        docs = retriever.invoke(query)

        results = [
            {
                "page_content": doc.page_content,
                "metadata": doc.metadata,
            }
            for doc in docs
        ]

        logger.info(
            f"Query '{query}' by uid='{uid}' against collection '{collection_name}' "
            f"returned {len(results)} results."
        )
        return results
