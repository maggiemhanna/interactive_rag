"""ChromaDB batch indexing orchestration (`src.ingest.service`).

Coordinates multi-tenant vector storage writes inside `user_write_lock` with
collection quota enforcement and Vertex AI batch embedding spans.
"""

import os
from typing import List, Optional

from langchain_core.documents import Document

from src.core.ai_security.guardrails import validate_embedding_model
from src.core.compliance.policy import enforce_collection_quota
from src.core.logger import setup_logging
from src.core.observability.tracing import start_span
from src.core.storage import (
    get_vectorstore,
    resolve_collection_embedding_model,
    user_write_lock,
)

logger = setup_logging(name="ingest.service")


def ingest_documents(
    uid: str,
    documents: List[Document],
    collection_name: str,
    embedding_model: Optional[str] = None,
) -> None:
    """Indexes document chunks into `uid`'s isolated ChromaDB inside the distributed GCS write lock.

    Re-verifies `MAX_COLLECTIONS_PER_USER` inside `user_write_lock` to prevent concurrent
    multi-instance new-collection uploads from bypassing the tenant collection quota, and
    indexes chunks in batches of 100.

    Args:
        uid: Verified tenant user identifier.
        documents: List of split `Document` chunks to embed and store.
        collection_name: Validated target Chroma collection identifier.
        embedding_model: Optional approved Vertex AI embedding model identifier.
    """
    safe_embedding_model = validate_embedding_model(embedding_model)
    with user_write_lock(uid) as persist_directory:
        enforce_collection_quota(
            uid=uid,
            collection_name=collection_name,
            persist_directory=persist_directory,
        )
        if not os.path.exists(persist_directory):
            os.makedirs(persist_directory)

        resolved_embedding_model = resolve_collection_embedding_model(
            persist_directory=persist_directory,
            collection_name=collection_name,
            embedding_model=safe_embedding_model,
        )

        with start_span(
            "ingest.ingest_documents",
            stage="embedding",
            collection_name=collection_name,
            chunks_count=len(documents),
        ):
            vectorstore = None
            try:
                vectorstore = get_vectorstore(
                    collection_name=collection_name,
                    persist_directory=persist_directory,
                    embedding_model=resolved_embedding_model,
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

                try:
                    raw_col = getattr(vectorstore, "_collection", None)
                    if raw_col is not None and hasattr(raw_col, "modify"):
                        existing_meta = getattr(raw_col, "metadata", None) or {}
                        raw_col.modify(
                            metadata={
                                **(existing_meta if isinstance(existing_meta, dict) else {}),
                                "embedding_model": resolved_embedding_model,
                            }
                        )
                except Exception:
                    pass

                logger.info(
                    f"Successfully added {total_chunks} documents to collection '{collection_name}' in '{persist_directory}'."
                )
            except Exception as e:
                logger.error(f"Error updating vectorstore: {e}")
                raise
            finally:
                del vectorstore


__all__ = ["ingest_documents"]
