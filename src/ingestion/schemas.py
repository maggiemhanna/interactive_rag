from typing import Any, List, Union

from pydantic import BaseModel, Field, field_validator

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
        ...,
        description="Name of the Chroma vectorstore collection",
    )
    embedding_model: str = Field(
        default="",
        validate_default=True,
        description="Approved Vertex AI embedding model name (defaults to first entry in ALLOWED_EMBEDDING_MODELS)",
    )

    @field_validator("collection_name")
    @classmethod
    def _check_collection_name(cls, value: str) -> str:
        return validate_collection_name(value)

    @field_validator("embedding_model", mode="before")
    @classmethod
    def _check_embedding_model(cls, value: Any) -> str:
        if value is not None and not isinstance(value, str):
            raise ValueError("embedding_model must be a string.")
        return validate_embedding_model(value)


class QueryRequest(BaseModel):
    query: str = Field(..., description="Query text to search for in vector store")
    collection_name: str = Field(
        ...,
        description="Name of the Chroma vectorstore collection",
    )
    k: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Number of results to retrieve (1-50)",
    )
    embedding_model: str = Field(
        default="",
        validate_default=True,
        description="Approved Vertex AI embedding model name (defaults to first entry in ALLOWED_EMBEDDING_MODELS)",
    )

    @field_validator("query")
    @classmethod
    def _check_query(cls, value: str) -> str:
        return validate_query_text(value)

    @field_validator("collection_name")
    @classmethod
    def _check_collection_name(cls, value: str) -> str:
        return validate_collection_name(value)

    @field_validator("embedding_model", mode="before")
    @classmethod
    def _check_embedding_model(cls, value: Any) -> str:
        if value is not None and not isinstance(value, str):
            raise ValueError("embedding_model must be a string.")
        return validate_embedding_model(value)
