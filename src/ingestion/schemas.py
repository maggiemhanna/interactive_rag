from typing import List, Union

from pydantic import BaseModel, Field, field_validator

from src.core.config import DEFAULT_COLLECTION, DEFAULT_EMBEDDING_MODEL
from src.core.security import (
    validate_collection_name,
    validate_embedding_model,
    validate_query_text,
)


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
        description="Name of the approved Vertex AI embedding model",
    )

    @field_validator("collection_name")
    @classmethod
    def _check_collection_name(cls, value: str) -> str:
        return validate_collection_name(value)

    @field_validator("embedding_model")
    @classmethod
    def _check_embedding_model(cls, value: str) -> str:
        return validate_embedding_model(value)


class QueryRequest(BaseModel):
    query: str = Field(..., description="Query text to search for in vector store")
    collection_name: str = Field(
        default=DEFAULT_COLLECTION,
        description="Name of the Chroma vectorstore collection",
    )
    k: int = Field(default=5, description="Number of results to retrieve")
    embedding_model: str = Field(
        default=DEFAULT_EMBEDDING_MODEL,
        description="Name of the approved Vertex AI embedding model",
    )

    @field_validator("query")
    @classmethod
    def _check_query(cls, value: str) -> str:
        return validate_query_text(value)

    @field_validator("collection_name")
    @classmethod
    def _check_collection_name(cls, value: str) -> str:
        return validate_collection_name(value)

    @field_validator("embedding_model")
    @classmethod
    def _check_embedding_model(cls, value: str) -> str:
        return validate_embedding_model(value)
