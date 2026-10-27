import re
from typing import Annotated, Any, Dict, List, NotRequired, Optional, Sequence, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, field_validator

from src.agent.memory import MEMORY_SAVER
from src.core.audit import (
    get_guardrail_actions,
    get_request_id,
    get_retrieved_chunks_count,
    was_model_armor_active,
    was_pii_filtered,
)
from src.core.security import (
    validate_collection_name,
    validate_embedding_model,
    validate_gcp_region,
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


MODEL_ARMOR_DEFAULT_FILTERS: List[str] = [
    "pi_and_jailbreak",
    "malicious_uri",
    "rai",
    "sdp",
]

_CITATION_BRACKET_RE = re.compile(
    r"\[[^\[\]\n]+?,\s*(?:pp?\.?|pages?)\s*\d+[^\[\]\n]*\]"
    r"|\([^\(\)\n]+?\.(?:pdf|docx?|txt|md|csv|html?)\s*,\s*(?:pp?\.?|pages?)\s*\d+[^\(\)\n]*\)",
    re.IGNORECASE,
)
_PAGE_REF_RE = re.compile(r"(?:pp?\.?|pages?)\s*\d+", re.IGNORECASE)


def _count_inline_citations(answer: Optional[str]) -> int:
    """Counts inline `[source, p.X]` / `(source.pdf, p.X)` document citations in an answer."""
    if not answer or not isinstance(answer, str):
        return 0
    total = 0
    for match in _CITATION_BRACKET_RE.findall(answer):
        refs = _PAGE_REF_RE.findall(match)
        total += max(len(refs), 1)
    return total


def _has_thread_retrieved_chunks(
    messages: Optional[Sequence[Dict[str, Any]]] = None,
    scoped_thread_id: Optional[str] = None,
) -> bool:
    """Checks whether retrieved document chunks exist in the current turn events or thread memory."""
    if messages:
        for msg in messages:
            if isinstance(msg, dict) and "<retrieved_chunk " in str(msg.get("content") or ""):
                return True

    if scoped_thread_id:
        try:
            tup = MEMORY_SAVER.get_tuple({"configurable": {"thread_id": scoped_thread_id}})
            if tup and tup.checkpoint:
                channel_msgs = (tup.checkpoint.get("channel_values") or {}).get("messages") or []
                for msg in channel_msgs:
                    content = getattr(msg, "content", "")
                    if "<retrieved_chunk " in str(content or ""):
                        return True
        except Exception:
            pass

    return False


class GuardrailAction(BaseModel):
    """Structured record of a Cloud Model Armor or Gemini safety action triggered during the turn."""

    filter: str = Field(..., description="Filter identifier (pi_and_jailbreak, malicious_uri, rai, sdp, gemini_safety)")
    label: str = Field(..., description="Human-readable filter name")
    action: str = Field(..., description="Action taken by the guardrail ('blocked' or 'redacted')")
    stage: str = Field(..., description="Pipeline stage ('input', 'retrieval', 'model', or 'output')")
    details: List[str] = Field(
        default_factory=list,
        description="Optional matched subcategories (e.g. HARASSMENT, DANGEROUS for RAI)",
    )


class ComplianceMetadata(BaseModel):
    """EU AI Act Art. 50 AI transparency, grounding & Model Armor provenance metadata attached to agent responses."""

    ai_generated: bool = Field(
        default=True,
        description="True when generated by the LLM; False when replaced by a deterministic safety/security refusal",
    )
    model: str = Field(..., description="Approved regional Vertex AI LLM model identifier")
    region: str = Field(..., description="Pinned single GCP region where inference and retrieval executed")
    grounding_enforced: bool = Field(
        default=True,
        description="Indicates system-level grounding and citation guardrails were enforced",
    )
    grounded: bool = Field(
        default=False,
        description="True when the answer cites retrieved document passages and was not blocked by guardrails",
    )
    citation_count: int = Field(
        default=0,
        description="Number of inline document page citations present in the final answer",
    )
    retrieved_passages: int = Field(
        default=0,
        description="Number of vectorstore passages retrieved during this turn",
    )
    pii_filtered: bool = Field(
        default=False,
        description="True when Sensitive Data Protection / Model Armor redacted PII during the request",
    )
    model_armor_active: bool = Field(
        default=False,
        description="True when Google Cloud Model Armor actively screened this turn",
    )
    filters_checked: List[str] = Field(
        default_factory=list,
        description="Cloud Model Armor filter suites evaluated on this turn",
    )
    guardrail_actions: List[GuardrailAction] = Field(
        default_factory=list,
        description="All Cloud Model Armor and Gemini safety actions (blocks or redactions) triggered on this turn",
    )
    request_id: str = Field(..., description="End-to-end audit correlation identifier")


class AgentResponse(BaseModel):
    """Structured JSON response returned by `POST /agent` when `stream=False`."""

    status: str = Field(default="success")
    query: str
    answer: str
    collection_name: str
    messages: List[Dict[str, Any]]
    compliance: ComplianceMetadata


def build_compliance_metadata(
    model_name: Optional[str] = None,
    answer: Optional[str] = None,
    messages: Optional[Sequence[Dict[str, Any]]] = None,
    scoped_thread_id: Optional[str] = None,
) -> ComplianceMetadata:
    """Constructs EU AI Act Art. 50 provenance, grounding, and Model Armor metadata for the active request."""
    raw_actions = get_guardrail_actions()
    was_blocked = any(item.get("action") == "blocked" for item in raw_actions)
    retrieved_passages = get_retrieved_chunks_count()
    citation_count = 0 if was_blocked else _count_inline_citations(answer)
    has_evidence = (retrieved_passages > 0) or _has_thread_retrieved_chunks(
        messages=messages,
        scoped_thread_id=scoped_thread_id,
    )
    grounded = (not was_blocked) and (citation_count > 0) and has_evidence
    armor_active = was_model_armor_active()

    return ComplianceMetadata(
        ai_generated=not was_blocked,
        model=validate_llm_model(model_name),
        region=validate_gcp_region(),
        grounding_enforced=True,
        grounded=grounded,
        citation_count=citation_count,
        retrieved_passages=retrieved_passages,
        pii_filtered=was_pii_filtered(),
        model_armor_active=armor_active,
        filters_checked=list(MODEL_ARMOR_DEFAULT_FILTERS) if armor_active else [],
        guardrail_actions=[GuardrailAction(**item) for item in raw_actions],
        request_id=get_request_id(),
    )


class AgentState(TypedDict):
    """State definition for the RAG Agent workflow."""

    messages: Annotated[Sequence[BaseMessage], add_messages]
    pending_response: NotRequired[Optional[BaseMessage]]
    input_blocked: NotRequired[bool]

