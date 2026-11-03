"""Live HTTP smoke-test script for the Document Ingestion API (`/ingest`).

Posts sample local PDF paths to a running `ingest-api` service and verifies
similarity retrieval against the resulting ChromaDB collection via
`src.core.storage.chroma.get_vectorstore`.
"""

import requests

from src.core.config import INGEST_PORT, get_service_port
from src.core.logger import setup_logging
from src.core.storage.chroma import get_vectorstore

logger = setup_logging(name="tests.ingestion")


def run_ingestion_smoke_test() -> None:
    """Ingests sample stock market PDFs via `POST /ingest` and runs a similarity search check."""
    port = get_service_port(INGEST_PORT)
    url = f"http://localhost:{port}/ingest"
    payload = {
        "pdf_paths": [
            "./data/Stock_Market_Performance_2024.pdf",
            "./data/Stock_Market_Performance_2025.pdf",
        ],
        "collection_name": "stock_market_vs",
    }
    logger.info(f"Sending payload to '{url}': {payload}")
    response = requests.post(url, json=payload)
    response.raise_for_status()
    logger.info(f"Ingestion response: {response.json()}")

    collection_name = payload["collection_name"]
    vectorstore = get_vectorstore(
        persist_directory="chroma_db",
        collection_name=collection_name,
        embedding_model="gemini-embedding-001",
    )
    retriever = vectorstore.as_retriever(
        search_type="similarity",
        search_kwargs={"k": 5},
    )
    query = "What is Arm Holdings (ARM)?"
    logger.info(f"Testing retriever with query: '{query}'")
    results = retriever.invoke(query)
    logger.info(f"Retrieved {len(results)} results:")
    for idx, doc in enumerate(results, 1):
        preview = (
            doc.page_content[:500] + "..."
            if len(doc.page_content) > 500
            else doc.page_content
        )
        logger.info(f"--- Result {idx} ---\n{preview}")


if __name__ == "__main__":
    run_ingestion_smoke_test()