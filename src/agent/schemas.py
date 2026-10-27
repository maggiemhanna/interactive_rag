"""Pydantic HTTP schemas and LangGraph `TypedDict` state definitions for the Agentic RAG service.

Validates incoming `POST /agent` and `POST /agent/stream` requests against approved Vertex AI
model catalogs, query character ceilings, and safe collection identifiers, and defines the
structured `AgentResponse` and LangGraph `AgentState` contracts.
"""

from typing import Annotated, Any, Dict, List, NotRequired, Optional, Sequence, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, field_validator

from src.core.ai_security.guardrails import (
    validate_collection_name,
    validate_embedding_model,
    validate_llm_model,
    validate_query_text,
)
from src.core.compliance.provenance import ComplianceMetadata


class AgentRequest(BaseModel):
    """Validated JSON request payload for `POST /agent` and `POST /agent/stream`."""

    query: str = Field(..., description="Query or question for the Agentic RAG assistant")
    collection_name: str = Field(
        ...,
        description="Name of the Chroma vectorstore collection",
    )
    thread_id: str = Field(default="1", description="LangGraph thread ID for session state")
    model_name: str = Field(
        default="",
        validate_default=True,
        description="Approved Vertex AI generative LLM model name (defaults to first entry in ALLOWED_LLM_MODELS)",
    )
    embedding_model: str = Field(
        default="",
        validate_default=True,
        description="Approved Vertex AI embedding model name (defaults to first entry in ALLOWED_EMBEDDING_MODELS)",
    )
    stream: bool = Field(
        default=False,
        description="Whether to stream message events via Server-Sent Events (SSE)",
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

    @field_validator("model_name", mode="before")
    @classmethod
    def _check_model_name(cls, value: Any) -> str:
        """Validates `model_name` against `ALLOWED_LLM_MODELS`."""
        if value is not None and not isinstance(value, str):
            raise ValueError("model_name must be a string.")
        return validate_llm_model(value)

    @field_validator("embedding_model", mode="before")
    @classmethod
    def _check_embedding_model(cls, value: Any) -> str:
        """Validates `embedding_model` against `ALLOWED_EMBEDDING_MODELS`."""
        if value is not None and not isinstance(value, str):
            raise ValueError("embedding_model must be a string.")
        return validate_embedding_model(value)


class AgentResponse(BaseModel):
    """Structured JSON response returned by `POST /agent` when `stream=False`."""

    status: str = Field(default="success")
    query: str
    answer: str
    collection_name: str
    messages: List[Dict[str, Any]]
    compliance: ComplianceMetadata


class AgentState(TypedDict):
    """LangGraph workflow state shared across `input_guard`, `model_call`, `tools`, and `output_guard`."""

    messages: Annotated[Sequence[BaseMessage], add_messages]
    pending_response: NotRequired[Optional[BaseMessage]]
    input_blocked: NotRequired[bool]


__all__ = ["AgentRequest", "AgentResponse", "AgentState"]
