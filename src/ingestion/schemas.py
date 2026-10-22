from typing import List, Union

from pydantic import BaseModel, Field

from src.core.config import DEFAULT_COLLECTION, DEFAULT_EMBEDDING_MODEL


class IngestRequest(BaseModel):
    pdf_paths: Union[str, List[str]] = Field(
        ..., description="Path or list of paths (or a single path) to PDF files to ingest"
    )
    collection_name: str = Field(
        default=DEFAULT_COLLECTION,
        description="Name of the Chroma vectorstore collection",
    )
    embedding_model: str = Field(
        default=DEFAULT_EMBEDDING_MODEL,
        description="Name of the Vertex AI embedding model",
    )


class QueryRequest(BaseModel):
    query: str = Field(..., description="Query text to search for in vector store")
    collection_name: str = Field(
        default=DEFAULT_COLLECTION,
        description="Name of the Chroma vectorstore collection",
    )
    k: int = Field(default=5, description="Number of results to retrieve")
    embedding_model: str = Field(
        default=DEFAULT_EMBEDDING_MODEL,
        description="Name of the Vertex AI embedding model",
    )
