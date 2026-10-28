"""Google Cloud Model Armor and Cloud DLP screening for prompts, chunks, and outputs (Pillar 3: AI Security).

Provides regional, fail-closed runtime inspection across three RAG stages:
1. **Input Screening (`screen_user_prompt`)**: Blocks prompt injection / jailbreak attempts,
   malicious URIs, RAI violations, and CSAM before invoking Vertex AI Gemini.
2. **Retrieval Sanitization (`sanitize_chunk_text`, `sanitize_retrieved_documents`)**: Masks
   sensitive PII/secrets in retrieved Chroma chunks with `[INFO_TYPE]` placeholders via
   Cloud Sensitive Data Protection (Cloud DLP) while preserving surrounding context.
3. **Output Filtering (`sanitize_model_output`)**: Screens final LLM answers and redacts any
   residual sensitive data prior to JSON or SSE emission.
"""

from concurrent.futures import ThreadPoolExecutor
import contextvars
from dataclasses import dataclass
import os
import re
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

from google.cloud import modelarmor_v1
from langchain_core.documents import Document

from src.core.ai_security.safety import (
    INTERNAL_ERROR_MESSAGE,
    PROMPT_BLOCKED_MESSAGE,
    SAFETY_REFUSAL_MESSAGE,
)
from src.core.compliance.audit import emit_audit_event, hash_query
from src.core.compliance.context import (
    mark_model_armor_active,
    mark_pii_filtered,
    record_guardrail_action,
)
from src.core.compliance.residency import validate_gcp_region
from src.core.config import (
    get_model_armor_location,
    get_model_armor_template_id,
    is_cloud_run,
    is_model_armor_fail_closed,
)
from src.core.logger import setup_logging

logger = setup_logging(name="security")

_TEMPLATE_PATH_RE = re.compile(
    r"^projects/(?P<project>[^/]+)/locations/(?P<location>[^/]+)/templates/(?P<template>[^/]+)$"
)

_client_lock = threading.Lock()
_model_armor_clients: Dict[str, modelarmor_v1.ModelArmorClient] = {}


class SecurityScreeningUnavailableError(RuntimeError):
    """Raised when `MODEL_ARMOR_FAIL_CLOSED=true` and Model Armor or Cloud DLP screening is unavailable."""

    def __init__(self, message: str, reason: str = "model_armor_unavailable") -> None:
        """Initializes the fail-closed screening unavailability exception.

        Args:
            message: Diagnostic description logged server-side (never exposed raw to clients).
            reason: Machine-readable audit reason code (e.g., `'missing_model_armor_config'`, `'model_armor_error'`).
        """
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ScreeningDecision:
    """Structured outcome returned by Model Armor prompt screening and output sanitization.

    Attributes:
        allowed: True when the text may proceed; False when blocked by policy or error.
        text: Sanitized/de-identified text when `allowed=True`, or refusal text when `allowed=False`.
        blocked_reason: Machine-readable identifier of the primary blocking filter, if blocked.
        redacted: True when Cloud DLP / SDP replaced sensitive spans with `[INFO_TYPE]` tokens.
        triggered_filters: Tuple of `(filter_name, action, details_tuple)` entries triggered on this call.
    """

    allowed: bool
    text: str
    blocked_reason: Optional[str] = None
    redacted: bool = False
    triggered_filters: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = ()


def _resolve_model_armor_target() -> Optional[Tuple[str, str]]:
    """Resolves `(template_resource_name, location)` for Model Armor, or `None` when unconfigured.

    Accepts either a full resource path (`projects/.../locations/.../templates/...`) or a short
    template ID combined with `GOOGLE_CLOUD_PROJECT` and `MODEL_ARMOR_LOCATION`, enforcing single-region
    data residency on the resolved location.

    Returns:
        Tuple of `(full_template_name, validated_gcp_region)`, or `None` if `MODEL_ARMOR_TEMPLATE_ID` is empty.

    Raises:
        ValueError: If the template location is `'global'` or a multi-region identifier.
        RuntimeError: If a short template ID is provided without `GOOGLE_CLOUD_PROJECT`.
    """
    raw_template = get_model_armor_template_id()
    if not raw_template:
        return None

    match = _TEMPLATE_PATH_RE.match(raw_template)
    if match:
        return raw_template, validate_gcp_region(match.group("location"))

    project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    location = validate_gcp_region(get_model_armor_location())
    if not project:
        raise RuntimeError(
            "GOOGLE_CLOUD_PROJECT must be set when MODEL_ARMOR_TEMPLATE_ID is a short template ID."
        )
    return f"projects/{project}/locations/{location}/templates/{raw_template}", location


def _get_model_armor_client(location: str) -> modelarmor_v1.ModelArmorClient:
    """Returns a cached regional `ModelArmorClient` bound to `modelarmor.<location>.rep.googleapis.com`.

    Args:
        location: Validated single-region GCP identifier (e.g., `'us-central1'`).

    Returns:
        Thread-safe cached `modelarmor_v1.ModelArmorClient` for `location`.
    """
    client = _model_armor_clients.get(location)
    if client is not None:
        return client

    with _client_lock:
        client = _model_armor_clients.get(location)
        if client is None:
            endpoint = f"modelarmor.{location}.rep.googleapis.com"
            client = modelarmor_v1.ModelArmorClient(
                client_options={"api_endpoint": endpoint}
            )
            _model_armor_clients[location] = client
            logger.info(f"Initialized ModelArmorClient for regional endpoint '{endpoint}'.")
        return client


def _has_proto_field(msg: Any, field_name: str) -> bool:
    """Safely checks whether a proto-plus message or test double populates `field_name`.

    Args:
        msg: Protobuf / proto-plus message wrapper or mock object.
        field_name: Name of the submessage field to check.

    Returns:
        True when `field_name` is present and non-None on `msg`.
    """
    if "_pb" in getattr(msg, "__dict__", {}):
        return field_name in msg
    try:
        if field_name in msg:
            return True
    except TypeError:
        pass
    val = getattr(msg, field_name, None)
    return val is not None


def _extract_matched_rai_types(rai_res: Any) -> Tuple[str, ...]:
    """Extracts matched Responsible AI subcategory names from a Model Armor `RaiFilterResult`.

    Args:
        rai_res: `modelarmor_v1.RaiFilterResult` message from `filter_results`.

    Returns:
        Sorted deduplicated tuple of matched category names (e.g., `('DANGEROUS', 'HARASSMENT')`).
    """
    match_found = modelarmor_v1.FilterMatchState.MATCH_FOUND
    type_results = getattr(rai_res, "rai_filter_type_results", None) or {}
    matched: List[str] = []
    if hasattr(type_results, "items"):
        for key, entry in type_results.items():
            if getattr(entry, "match_state", None) == match_found:
                ft = getattr(entry, "filter_type", None)
                raw_name = getattr(ft, "name", None) or str(key)
                cleaned = raw_name.strip().upper()
                if cleaned and cleaned not in {"RAI_FILTER_TYPE_UNSPECIFIED", "0", "NONE"}:
                    matched.append(cleaned)
    return tuple(sorted(set(matched)))


def _inspect_sanitization_result(
    sanitization_result: Any,
    original_text: str,
    block_message_for_pi: str,
) -> ScreeningDecision:
    """Evaluates per-filter Model Armor results, blocking threats while preserving SDP de-identified text.

    Args:
        sanitization_result: `modelarmor_v1.SanitizationResult` returned by Model Armor.
        original_text: Unmodified input prompt or model output string.
        block_message_for_pi: User-facing refusal string used if prompt-injection or URI filters match.

    Returns:
        `ScreeningDecision` describing whether the text is allowed, redacted, or blocked.
    """
    match_found = modelarmor_v1.FilterMatchState.MATCH_FOUND
    filter_results = getattr(sanitization_result, "filter_results", None) or {}

    pi_matched = False
    uri_matched = False
    rai_matched = False
    rai_types: List[str] = []
    csam_matched = False
    deidentified_text: Optional[str] = None
    sdp_inspect_only_matched = False

    for filter_result in filter_results.values():
        if _has_proto_field(filter_result, "pi_and_jailbreak_filter_result"):
            pi_res = filter_result.pi_and_jailbreak_filter_result
            if getattr(pi_res, "match_state", None) == match_found:
                pi_matched = True

        if _has_proto_field(filter_result, "malicious_uri_filter_result"):
            uri_res = filter_result.malicious_uri_filter_result
            if getattr(uri_res, "match_state", None) == match_found:
                uri_matched = True

        if _has_proto_field(filter_result, "rai_filter_result"):
            rai_res = filter_result.rai_filter_result
            if getattr(rai_res, "match_state", None) == match_found:
                rai_matched = True
                rai_types.extend(_extract_matched_rai_types(rai_res))

        if _has_proto_field(filter_result, "csam_filter_filter_result"):
            csam_res = filter_result.csam_filter_filter_result
            if getattr(csam_res, "match_state", None) == match_found:
                csam_matched = True

        if _has_proto_field(filter_result, "sdp_filter_result"):
            sdp_res = filter_result.sdp_filter_result
            if _has_proto_field(sdp_res, "deidentify_result"):
                deid = sdp_res.deidentify_result
                if getattr(deid, "match_state", None) == match_found:
                    candidate = getattr(getattr(deid, "data", None), "text", None)
                    if candidate is not None:
                        deidentified_text = candidate
            elif _has_proto_field(sdp_res, "inspect_result"):
                insp = sdp_res.inspect_result
                if getattr(insp, "match_state", None) == match_found:
                    sdp_inspect_only_matched = True

    triggered: List[Tuple[str, str, Tuple[str, ...]]] = []
    if pi_matched:
        triggered.append(("pi_and_jailbreak", "blocked", ()))
    if uri_matched:
        triggered.append(("malicious_uri", "blocked", ()))
    if rai_matched:
        triggered.append(("rai", "blocked", tuple(sorted(set(rai_types)))))
    if csam_matched:
        triggered.append(("csam", "blocked", ()))
    if deidentified_text is not None and deidentified_text != original_text:
        triggered.append(("sdp", "redacted", ()))
    elif sdp_inspect_only_matched and deidentified_text is None:
        triggered.append(("sdp", "blocked", ()))

    blocking_filters = [flt for flt, act, _ in triggered if act == "blocked"]
    if blocking_filters:
        primary_reason = blocking_filters[0]
        refusal_text = (
            block_message_for_pi
            if primary_reason in {"pi_and_jailbreak", "malicious_uri"}
            else SAFETY_REFUSAL_MESSAGE
        )
        return ScreeningDecision(
            allowed=False,
            text=refusal_text,
            blocked_reason=primary_reason,
            triggered_filters=tuple(triggered),
        )

    if deidentified_text is not None:
        return ScreeningDecision(
            allowed=True,
            text=deidentified_text,
            redacted=(deidentified_text != original_text),
            triggered_filters=tuple(triggered),
        )

    # Fallback if top-level match_found was set without populated per-filter details
    if getattr(sanitization_result, "filter_match_state", None) == match_found:
        return ScreeningDecision(
            allowed=False,
            text=block_message_for_pi,
            blocked_reason="model_armor_policy",
            triggered_filters=(("model_armor_policy", "blocked", ()),),
        )

    return ScreeningDecision(allowed=True, text=original_text)


def ensure_screening_available(
    *,
    stage: Optional[str] = None,
    audit_event_type: str = "security.screening_unavailable",
    **audit_fields: Any,
) -> Optional[Tuple[str, str]]:
    """Resolves the Model Armor target and enforces fail-closed availability when `MODEL_ARMOR_FAIL_CLOSED=true`.

    Called prior to opening SSE streams and reused across prompt, retrieval chunk, and output
    screening functions so missing or invalid Model Armor configuration fails closed immediately
    with HTTP 503 (`SecurityScreeningUnavailableError`).

    Args:
        stage: Optional pipeline stage (`'input'`, `'retrieval'`, or `'output'`) recorded
            in guardrail actions when fail-closed configuration is missing or invalid.
        audit_event_type: Structured audit event name emitted when screening is unavailable.
        **audit_fields: Additional PII-free metadata fields included in the audit event.

    Returns:
        Tuple of `(full_template_name, validated_gcp_region)`, or `None` when
        `MODEL_ARMOR_TEMPLATE_ID` is unset and `MODEL_ARMOR_FAIL_CLOSED=false`.

    Raises:
        SecurityScreeningUnavailableError: If `MODEL_ARMOR_FAIL_CLOSED=true` and
            `MODEL_ARMOR_TEMPLATE_ID` is unset or misconfigured.
        ValueError: If `MODEL_ARMOR_FAIL_CLOSED=false` and the configured location is invalid.
        RuntimeError: If `MODEL_ARMOR_FAIL_CLOSED=false` and `GOOGLE_CLOUD_PROJECT` is missing for a short ID.
    """
    fail_closed = is_model_armor_fail_closed()
    try:
        target = _resolve_model_armor_target()
    except Exception as e:
        if not fail_closed:
            raise
        logger.error(f"Invalid Model Armor configuration with MODEL_ARMOR_FAIL_CLOSED=true: {e}")
        if stage:
            record_guardrail_action("invalid_model_armor_config", "blocked", stage=stage)
        emit_audit_event(
            audit_event_type,
            severity="ERROR",
            status="screening_unavailable",
            reason="invalid_model_armor_config",
            **audit_fields,
        )
        raise SecurityScreeningUnavailableError(
            "Model Armor configuration is invalid while MODEL_ARMOR_FAIL_CLOSED=true.",
            reason="invalid_model_armor_config",
        ) from e

    if target is None:
        if fail_closed:
            logger.error(
                "MODEL_ARMOR_TEMPLATE_ID is unset with MODEL_ARMOR_FAIL_CLOSED=true; failing closed."
            )
            if stage:
                record_guardrail_action("missing_model_armor_config", "blocked", stage=stage)
            emit_audit_event(
                audit_event_type,
                severity="ERROR",
                status="screening_unavailable",
                reason="missing_model_armor_config",
                **audit_fields,
            )
            raise SecurityScreeningUnavailableError(
                "MODEL_ARMOR_TEMPLATE_ID is unset while MODEL_ARMOR_FAIL_CLOSED=true.",
                reason="missing_model_armor_config",
            )
        if is_cloud_run():
            logger.warning(
                f"MODEL_ARMOR_TEMPLATE_ID is unset in Cloud Run; skipping Model Armor {stage or 'security'} check."
            )
        return None

    return target


def screen_user_prompt(prompt: str) -> ScreeningDecision:
    """Screens an incoming user prompt via Google Cloud Model Armor (`sanitize_user_prompt`).

    Records per-request guardrail actions and PII-free SHA-256 audit events when a prompt
    is blocked or when Model Armor encounters a runtime failure.

    Args:
        prompt: Validated user prompt string prior to LLM invocation.

    Returns:
        `ScreeningDecision` indicating whether the prompt is allowed or blocked.

    Raises:
        SecurityScreeningUnavailableError: When `MODEL_ARMOR_FAIL_CLOSED=true` and Model Armor
            is unconfigured or returns an upstream API error.
    """
    prompt_digest = hash_query(prompt)
    prompt_len = len(prompt)
    fail_closed = is_model_armor_fail_closed()
    target = ensure_screening_available(
        stage="input",
        audit_event_type="security.prompt_screening_error",
        query_sha256=prompt_digest,
        query_chars=prompt_len,
    )
    if target is None:
        return ScreeningDecision(allowed=True, text=prompt)

    template_name, location = target
    try:
        client = _get_model_armor_client(location)
        request = modelarmor_v1.SanitizeUserPromptRequest(
            name=template_name,
            user_prompt_data=modelarmor_v1.DataItem(text=prompt),
        )
        response = client.sanitize_user_prompt(request=request)
        mark_model_armor_active(True)
        decision = _inspect_sanitization_result(
            sanitization_result=response.sanitization_result,
            original_text=prompt,
            block_message_for_pi=PROMPT_BLOCKED_MESSAGE,
        )
        for flt, act, details in decision.triggered_filters:
            record_guardrail_action(flt, act, stage="input", details=list(details))
        if decision.redacted:
            mark_pii_filtered(True)
        if not decision.allowed:
            logger.warning(
                f"Model Armor blocked user prompt (reason={decision.blocked_reason})."
            )
            emit_audit_event(
                "security.prompt_blocked",
                severity="WARNING",
                status="blocked",
                reason=decision.blocked_reason,
                query_sha256=prompt_digest,
                query_chars=prompt_len,
            )
        return decision
    except SecurityScreeningUnavailableError:
        raise
    except Exception as e:
        logger.error(f"Model Armor prompt screening failed: {e}")
        record_guardrail_action("model_armor_error", "blocked", stage="input")
        emit_audit_event(
            "security.prompt_screening_error",
            severity="ERROR",
            status="screening_unavailable" if fail_closed else "error",
            reason="model_armor_error",
            query_sha256=prompt_digest,
            query_chars=prompt_len,
        )
        if fail_closed:
            raise SecurityScreeningUnavailableError(
                "Model Armor prompt screening failed.",
                reason="model_armor_error",
            ) from e
        return ScreeningDecision(
            allowed=False,
            text=INTERNAL_ERROR_MESSAGE,
            blocked_reason="model_armor_error",
            triggered_filters=(("model_armor_error", "blocked", ()),),
        )


def sanitize_model_output(
    output_text: str,
    user_prompt: Optional[str] = None,
) -> ScreeningDecision:
    """Screens and redacts PII/secrets in LLM output via Model Armor + Cloud DLP (`sanitize_model_response`).

    Args:
        output_text: Candidate final response text generated by the LLM.
        user_prompt: Optional original user prompt supplied to Model Armor for context.

    Returns:
        `ScreeningDecision` containing clean/redacted text or a policy refusal message.

    Raises:
        SecurityScreeningUnavailableError: When `MODEL_ARMOR_FAIL_CLOSED=true` and Model Armor
            or Cloud DLP is unconfigured or returns an upstream API error.
    """
    if not output_text or not output_text.strip():
        return ScreeningDecision(allowed=True, text=output_text)

    output_len = len(output_text)
    fail_closed = is_model_armor_fail_closed()
    target = ensure_screening_available(
        stage="output",
        audit_event_type="security.output_screening_error",
        output_chars=output_len,
    )
    if target is None:
        return ScreeningDecision(allowed=True, text=output_text)

    template_name, location = target
    try:
        client = _get_model_armor_client(location)
        request_kwargs: Dict[str, Any] = {
            "name": template_name,
            "model_response_data": modelarmor_v1.DataItem(text=output_text),
        }
        if user_prompt:
            request_kwargs["user_prompt"] = user_prompt

        request = modelarmor_v1.SanitizeModelResponseRequest(**request_kwargs)
        response = client.sanitize_model_response(request=request)
        mark_model_armor_active(True)
        decision = _inspect_sanitization_result(
            sanitization_result=response.sanitization_result,
            original_text=output_text,
            block_message_for_pi=SAFETY_REFUSAL_MESSAGE,
        )
        for flt, act, details in decision.triggered_filters:
            record_guardrail_action(flt, act, stage="output", details=list(details))
        if not decision.allowed:
            logger.warning(
                f"Model Armor blocked model response (reason={decision.blocked_reason})."
            )
            emit_audit_event(
                "security.output_blocked",
                severity="WARNING",
                status="blocked",
                reason=decision.blocked_reason,
                output_chars=output_len,
            )
        elif decision.redacted:
            logger.info("Model Armor + Cloud DLP redacted sensitive data in model response.")
            mark_pii_filtered(True)
            emit_audit_event(
                "security.output_dlp_redacted",
                severity="INFO",
                status="redacted",
                output_chars=output_len,
            )
        return decision
    except SecurityScreeningUnavailableError:
        raise
    except Exception as e:
        logger.error(f"Model Armor output sanitization failed: {e}")
        record_guardrail_action("model_armor_error", "blocked", stage="output")
        emit_audit_event(
            "security.output_screening_error",
            severity="ERROR",
            status="screening_unavailable" if fail_closed else "error",
            reason="model_armor_error",
            output_chars=output_len,
        )
        if fail_closed:
            raise SecurityScreeningUnavailableError(
                "Model Armor output sanitization failed.",
                reason="model_armor_error",
            ) from e
        return ScreeningDecision(
            allowed=False,
            text=INTERNAL_ERROR_MESSAGE,
            blocked_reason="model_armor_error",
            triggered_filters=(("model_armor_error", "blocked", ()),),
        )


def _extract_sdp_deidentified_text(
    sanitization_result: Any,
    original_text: str,
) -> str:
    """Extracts Cloud DLP `[INFO_TYPE]`-masked text from `sdp_filter_result` while preserving surrounding chunk text.

    Fails closed by raising `RuntimeError` if SDP execution was skipped or matched sensitive
    data under an inspect-only template without returning de-identified text.

    Args:
        sanitization_result: `modelarmor_v1.SanitizationResult` returned for a retrieved chunk.
        original_text: Unmodified retrieved document chunk text.

    Returns:
        De-identified chunk text with `[INFO_TYPE]` placeholders, or `original_text` when no PII matched.

    Raises:
        RuntimeError: If SDP execution was skipped or matched PII without de-identification output.
    """
    match_found = modelarmor_v1.FilterMatchState.MATCH_FOUND
    exec_skipped = modelarmor_v1.FilterExecutionState.EXECUTION_SKIPPED
    filter_results = getattr(sanitization_result, "filter_results", None) or {}

    for filter_result in filter_results.values():
        if not _has_proto_field(filter_result, "sdp_filter_result"):
            continue

        sdp_res = filter_result.sdp_filter_result
        if _has_proto_field(sdp_res, "deidentify_result"):
            deid = sdp_res.deidentify_result
            if getattr(deid, "execution_state", None) == exec_skipped:
                raise RuntimeError("Model Armor SDP de-identification execution was skipped.")
            if getattr(deid, "match_state", None) == match_found:
                candidate = getattr(getattr(deid, "data", None), "text", None)
                if candidate is None:
                    raise RuntimeError(
                        "Model Armor SDP matched sensitive data without returning de-identified text."
                    )
                return candidate
            return original_text

        if _has_proto_field(sdp_res, "inspect_result"):
            insp = sdp_res.inspect_result
            if getattr(insp, "execution_state", None) == exec_skipped:
                raise RuntimeError("Model Armor SDP inspection execution was skipped.")
            if getattr(insp, "match_state", None) == match_found:
                raise RuntimeError(
                    "Model Armor SDP matched sensitive data without a de-identify template configured."
                )
            return original_text

    return original_text


def sanitize_chunk_text(chunk_text: str) -> str:
    """Masks sensitive spans in a retrieved chunk with `[INFO_TYPE]` placeholders via Model Armor SDP + Cloud DLP.

    Args:
        chunk_text: Raw text content of a single retrieved Chroma `Document` chunk.

    Returns:
        Sanitized chunk string safe for LLM context and `/query` responses.

    Raises:
        SecurityScreeningUnavailableError: If `MODEL_ARMOR_FAIL_CLOSED=true` and Model Armor / Cloud DLP
            is unconfigured or unavailable.
        RuntimeError: If `MODEL_ARMOR_FAIL_CLOSED=false`, Model Armor is configured, and chunk screening fails.
    """
    if not chunk_text or not chunk_text.strip():
        return chunk_text

    chunk_len = len(chunk_text)
    fail_closed = is_model_armor_fail_closed()
    target = ensure_screening_available(
        stage="retrieval",
        audit_event_type="security.chunk_screening_error",
        chunk_chars=chunk_len,
    )
    if target is None:
        return chunk_text

    template_name, location = target
    try:
        client = _get_model_armor_client(location)
        request = modelarmor_v1.SanitizeModelResponseRequest(
            name=template_name,
            model_response_data=modelarmor_v1.DataItem(text=chunk_text),
        )
        response = client.sanitize_model_response(request=request)
        mark_model_armor_active(True)
        redacted_text = _extract_sdp_deidentified_text(
            sanitization_result=response.sanitization_result,
            original_text=chunk_text,
        )
        if redacted_text != chunk_text:
            logger.info("Model Armor + Cloud DLP masked sensitive infoTypes in retrieved document chunk.")
            mark_pii_filtered(True)
            record_guardrail_action("sdp", "redacted", stage="retrieval")
            emit_audit_event(
                "security.chunk_dlp_masked",
                severity="INFO",
                status="redacted",
                chunk_chars=chunk_len,
            )
        return redacted_text
    except SecurityScreeningUnavailableError:
        raise
    except Exception as e:
        logger.error(f"Model Armor chunk sanitization failed: {e}")
        emit_audit_event(
            "security.chunk_screening_error",
            severity="ERROR",
            status="screening_unavailable" if fail_closed else "error",
            reason="model_armor_error",
            chunk_chars=chunk_len,
        )
        if fail_closed:
            raise SecurityScreeningUnavailableError(
                "Model Armor chunk sanitization failed (model_armor_error).",
                reason="model_armor_error",
            ) from e
        raise RuntimeError("Model Armor chunk sanitization failed (model_armor_error).") from e


def sanitize_retrieved_documents(docs: Sequence[Document]) -> List[Document]:
    """Sanitizes a sequence of retrieved `Document` chunks concurrently while preserving order and metadata.

    Propagates active `contextvars` into worker threads so `request_id`, `uid`, `pii_filtered`,
    and `guardrail_actions` correlate accurately back to the parent HTTP request.

    Args:
        docs: Sequence of LangChain `Document` objects returned by Chroma similarity search.

    Returns:
        List of new `Document` instances with sensitive spans replaced by `[INFO_TYPE]` tokens.

    Raises:
        SecurityScreeningUnavailableError: When `MODEL_ARMOR_FAIL_CLOSED=true` and Model Armor / Cloud DLP
            is unconfigured or unavailable.
    """
    target = ensure_screening_available(stage="retrieval")
    if not docs:
        return []
    if target is None:
        return list(docs)

    if len(docs) == 1:
        sanitized_texts = [sanitize_chunk_text(doc.page_content or "") for doc in docs]
    else:
        max_workers = min(len(docs), 8)
        contexts = [contextvars.copy_context() for _ in docs]
        raw_texts = [doc.page_content or "" for doc in docs]
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            sanitized_texts = list(
                executor.map(
                    lambda item: item[0].run(sanitize_chunk_text, item[1]),
                    zip(contexts, raw_texts),
                )
            )

    return [
        Document(page_content=text, metadata=dict(doc.metadata or {}))
        for doc, text in zip(docs, sanitized_texts)
    ]


__all__ = [
    "ScreeningDecision",
    "SecurityScreeningUnavailableError",
    "ensure_screening_available",
    "sanitize_chunk_text",
    "sanitize_model_output",
    "sanitize_retrieved_documents",
    "screen_user_prompt",
]
