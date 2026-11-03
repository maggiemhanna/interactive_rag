"""ChromaDB similarity search and Cloud DLP sanitization service (`src.query.service`).

Executes read-only similarity searches inside `user_read_lock` with concurrent Model Armor /
Cloud DLP chunk masking and PII-free SHA-256 query logging.
"""

from typing import Any, Dict, List, Optional

from src.core.ai_security.armor import sanitize_retrieved_documents
from src.core.compliance.state import record_retrieved_chunks
from src.core.context import scrub_query_fields
from src.core.logger import setup_logging
from src.core.observability.telemetry import record_retrieval_metrics
from src.core.observability.tracing import start_span
from src.core.storage import (
    get_vectorstore,
    require_collection,
    similarity_search_with_scores,
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
        k: Number of top matching chunks to retrieve (`1..50`).
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
            docs, scored = similarity_search_with_scores(vectorstore, query, k=k)
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

    scrubbed = scrub_query_fields({"query": query})
    logger.info(
        f"Similarity search by uid='{uid}' against collection '{collection_name}' "
        f"(query_sha256='{scrubbed['query_sha256']}', query_chars={scrubbed['query_chars']}) "
        f"returned {len(results)} results."
    )
    return results


__all__ = ["search_collection"]

