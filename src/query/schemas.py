"""Pydantic request schema for Vectorstore Similarity Search (`src.query.schemas`).

Enforces strict collection identifier, query length, top-k (`1..50`), and approved
Vertex AI embedding model guardrails at API deserialization time.
"""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, BeforeValidator, Field

from src.core.ai_security.guardrails import (
    validate_collection_name,
    validate_embedding_model,
    validate_query_text,
)


class QueryRequest(BaseModel):
    """JSON request schema for `POST /query` similarity search over a Chroma collection."""

    query: Annotated[str, AfterValidator(validate_query_text)] = Field(
        ...,
        description="Query text to search for in vector store",
    )
    collection_name: Annotated[str, AfterValidator(validate_collection_name)] = Field(
        ...,
        description="Name of the Chroma vectorstore collection",
    )
    k: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Number of results to retrieve (1-50)",
    )
    embedding_model: Annotated[str, BeforeValidator(validate_embedding_model)] = Field(
        default="",
        validate_default=True,
        description="Approved Vertex AI embedding model name (defaults to first entry in ALLOWED_EMBEDDING_MODELS)",
    )


__all__ = ["QueryRequest"]

