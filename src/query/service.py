"""ChromaDB similarity search and Cloud DLP sanitization service (`src.query.service`).

Executes read-only similarity searches inside `user_read_lock` with concurrent Model Armor /
Cloud DLP chunk masking and PII-free SHA-256 query logging.
"""

from typing import Any, Dict, List, Optional

from src.core.ai_security.armor import sanitize_retrieved_documents
from src.core.compliance.audit import hash_query
from src.core.compliance.state import record_retrieved_chunks
from src.core.logger import setup_logging
from src.core.observability.telemetry import record_retrieval_metrics
from src.core.observability.tracing import start_span
from src.core.storage import (
    get_vectorstore,
    require_collection,
    user_read_lock,
)

logger = setup_logging(name="query.service")


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
        with start_span(
            "query.search_collection",
            stage="retrieval",
            collection_name=collection_name,
            k=k,
        ) as span:
            require_collection(persist_directory, collection_name)
            vectorstore = get_vectorstore(
                collection_name=collection_name,
                persist_directory=persist_directory,
                embedding_model=embedding_model,
            )
            if hasattr(vectorstore, "similarity_search_with_score"):
                raw_pairs = vectorstore.similarity_search_with_score(query, k=k)
                docs = [doc for doc, _ in raw_pairs]
                scored = [
                    (doc, round(1.0 / (1.0 + max(0.0, float(dist))), 4))
                    for doc, dist in raw_pairs
                ]
            else:
                retriever = vectorstore.as_retriever(
                    search_type="similarity", search_kwargs={"k": k}
                )
                docs = retriever.invoke(query)
                scored = docs
            metrics = record_retrieval_metrics(scored, is_tool_call=False)

            safe_docs = sanitize_retrieved_documents(docs)
            record_retrieved_chunks(len(safe_docs))
            results = [
                {
                    "page_content": doc.page_content,
                    "metadata": doc.metadata,
                }
                for doc in safe_docs
            ]
            span["attributes"]["results_count"] = len(results)
            if metrics.get("top_similarity_score") is not None:
                span["attributes"]["top_similarity_score"] = metrics["top_similarity_score"]

    query_digest = hash_query(query)
    logger.info(
        f"Similarity search by uid='{uid}' against collection '{collection_name}' "
        f"(query_sha256='{query_digest}', query_chars={len(query)}) returned {len(results)} results."
    )
    return results


__all__ = ["search_collection"]
