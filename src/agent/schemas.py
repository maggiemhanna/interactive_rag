from typing import Annotated, Sequence, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

from src.core.config import (
    DEFAULT_COLLECTION,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_LLM_MODEL,
)


class AgentRequest(BaseModel):
    query: str = Field(..., description="Query or question for the Agentic RAG assistant")
    collection_name: str = Field(
        default=DEFAULT_COLLECTION,
        description="Name of the Chroma vectorstore collection",
    )
    thread_id: str = Field(default="1", description="LangGraph thread ID for session state")
    model_name: str = Field(
        default=DEFAULT_LLM_MODEL,
        description="Vertex AI model name",
    )
    embedding_model: str = Field(
        default=DEFAULT_EMBEDDING_MODEL,
        description="Name of the Vertex AI embedding model",
    )
    stream: bool = Field(
        default=False,
        description="Whether to stream message events via Server-Sent Events (SSE)",
    )


class AgentState(TypedDict):
    """State definition for the RAG Agent workflow."""

    messages: Annotated[Sequence[BaseMessage], add_messages]
