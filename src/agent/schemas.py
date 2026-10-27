from typing import Annotated, Any, NotRequired, Optional, Sequence, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, field_validator

from src.core.security import (
    validate_collection_name,
    validate_embedding_model,
    validate_llm_model,
    validate_query_text,
)


class AgentRequest(BaseModel):
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
        return validate_query_text(value)

    @field_validator("collection_name")
    @classmethod
    def _check_collection_name(cls, value: str) -> str:
        return validate_collection_name(value)

    @field_validator("model_name", mode="before")
    @classmethod
    def _check_model_name(cls, value: Any) -> str:
        if value is not None and not isinstance(value, str):
            raise ValueError("model_name must be a string.")
        return validate_llm_model(value)

    @field_validator("embedding_model", mode="before")
    @classmethod
    def _check_embedding_model(cls, value: Any) -> str:
        if value is not None and not isinstance(value, str):
            raise ValueError("embedding_model must be a string.")
        return validate_embedding_model(value)


class AgentState(TypedDict):
    """State definition for the RAG Agent workflow."""

    messages: Annotated[Sequence[BaseMessage], add_messages]
    pending_response: NotRequired[Optional[BaseMessage]]
    input_blocked: NotRequired[bool]
