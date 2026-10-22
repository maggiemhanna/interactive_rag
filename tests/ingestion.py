import requests
from dotenv import load_dotenv, find_dotenv
import os 

from langchain_chroma import Chroma
from langchain_google_genai import GoogleGenerativeAIEmbeddings

from src.core.logger import setup_logging

load_dotenv(find_dotenv())
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logger = setup_logging(name="test_ingestion")

if __name__ == "__main__":
    url = "http://localhost:8000/ingest"
    payload = {
        "pdf_paths": [
            "./data/Stock_Market_Performance_2024.pdf",
            "./data/Stock_Market_Performance_2025.pdf",
        ],
        "collection_name": "stock_market_vs",
    }
    logger.info(f"Sending payload to endpoint: {payload}")
    response = requests.post(url, json=payload)
    logger.info(f"Response: {response.json()}")

    # Test the vectorstore with a query using retriever
    collection_name = payload.get("collection_name", "stock_market_vs")
    embeddings = GoogleGenerativeAIEmbeddings(
        model="gemini-embedding-001",
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        vertexai=True,
    )
    vectorstore = Chroma(
        persist_directory="chroma_db",
        embedding_function=embeddings,
        collection_name=collection_name,
    )

    retriever = vectorstore.as_retriever(
        search_type="similarity", search_kwargs={"k": 5}
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