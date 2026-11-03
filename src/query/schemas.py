"""Pydantic request schema for Vectorstore Similarity Search (`src.query.schemas`).

Enforces strict collection identifier, query length, top-k (`1..50`), and approved
Vertex AI embedding model guardrails at API deserialization time.
"""

from typing import Any

from pydantic import BaseModel, Field, field_validator

from src.core.ai_security.guardrails import (
    validate_collection_name,
    validate_embedding_model,
    validate_query_text,
)


class QueryRequest(BaseModel):
    """JSON request schema for `POST /query` similarity search over a Chroma collection."""

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
        """Validates `query` is non-empty and within `MAX_QUERY_CHARS`."""
        return validate_query_text(value)

    @field_validator("collection_name")
    @classmethod
    def _check_collection_name(cls, value: str) -> str:
        """Validates `collection_name` against `COLLECTION_NAME_PATTERN`."""
        return validate_collection_name(value)

    @field_validator("embedding_model", mode="before")
    @classmethod
    def _check_embedding_model(cls, value: Any) -> str:
        """Validates `embedding_model` against `ALLOWED_EMBEDDING_MODELS`."""
        if value is not None and not isinstance(value, str):
            raise ValueError("embedding_model must be a string.")
        return validate_embedding_model(value)


__all__ = ["QueryRequest"]
