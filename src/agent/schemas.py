"""Pydantic HTTP schemas and LangGraph `TypedDict` state definitions for the Agentic RAG service.

Validates incoming `POST /agent` and `POST /agent/stream` requests against approved Vertex AI
model catalogs, query character ceilings, and safe collection identifiers, and defines the
structured `AgentResponse` and LangGraph `AgentState` contracts.
"""

from typing import Annotated, Any, Dict, List, NotRequired, Optional, Sequence, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import AfterValidator, BaseModel, BeforeValidator, Field

from src.core.ai_security.guardrails import (
    validate_collection_name,
    validate_embedding_model,
    validate_llm_model,
    validate_query_text,
)
from src.core.compliance.provenance import ComplianceMetadata


class AgentRequest(BaseModel):
    """Validated JSON request payload for `POST /agent` and `POST /agent/stream`."""

    query: Annotated[str, AfterValidator(validate_query_text)] = Field(
        ...,
        description="Query or question for the Agentic RAG assistant",
    )
    collection_name: Annotated[str, AfterValidator(validate_collection_name)] = Field(
        ...,
        description="Name of the Chroma vectorstore collection",
    )
    thread_id: str = Field(default="1", description="LangGraph thread ID for session state")
    model_name: Annotated[str, BeforeValidator(validate_llm_model)] = Field(
        default="",
        validate_default=True,
        description="Approved Vertex AI generative LLM model name (defaults to first entry in ALLOWED_LLM_MODELS)",
    )
    embedding_model: Annotated[str, BeforeValidator(validate_embedding_model)] = Field(
        default="",
        validate_default=True,
        description="Approved Vertex AI embedding model name (defaults to first entry in ALLOWED_EMBEDDING_MODELS)",
    )
    stream: bool = Field(
        default=False,
        description="Whether to stream message events via Server-Sent Events (SSE)",
    )


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
