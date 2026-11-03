"""Pydantic request schema for Document Ingestion (`src.ingest.schemas`).

Enforces strict collection identifier and approved Vertex AI embedding model
guardrails at API deserialization time.
"""

from typing import Annotated, List, Union

from pydantic import AfterValidator, BaseModel, BeforeValidator, Field

from src.core.ai_security.guardrails import (
    validate_collection_name,
    validate_embedding_model,
)


class IngestRequest(BaseModel):
    """JSON request schema for local-development `POST /ingest` calls using `pdf_paths`."""

    pdf_paths: Union[str, List[str]] = Field(
        ...,
        description="Path or list of paths (or a single path) to PDF files to ingest",
    )
    collection_name: Annotated[str, AfterValidator(validate_collection_name)] = Field(
        ...,
        description="Name of the Chroma vectorstore collection",
    )
    embedding_model: Annotated[str, BeforeValidator(validate_embedding_model)] = Field(
        default="",
        validate_default=True,
        description="Approved Vertex AI embedding model name (defaults to first entry in ALLOWED_EMBEDDING_MODELS)",
    )


__all__ = ["IngestRequest"]

