from concurrent.futures import ThreadPoolExecutor
import contextvars
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from google.cloud import modelarmor_v1
from langchain_core.documents import Document
from langchain_google_genai import HarmBlockThreshold, HarmCategory

from src.core.audit import (
    emit_audit_event,
    hash_query,
    mark_model_armor_active,
    mark_pii_filtered,
    record_guardrail_action,
)
from src.core.auth import is_cloud_run
from src.core.config import (
    COLLECTION_NAME_PATTERN,
    GCP_REGION_PATTERN,
    get_allowed_embedding_models,
    get_allowed_llm_models,
    get_gemini_safety_threshold,
    get_google_cloud_location,
    get_max_query_chars,
    get_model_armor_location,
    get_model_armor_template_id,
)
from src.core.logger import setup_logging

logger = setup_logging(name="security")

PROMPT_BLOCKED_MESSAGE = (
    "This request cannot be processed because it triggered AI prompt security policies."
)
SAFETY_REFUSAL_MESSAGE = (
    "I cannot fulfill this request due to content safety policies."
)
RECURSION_LIMIT_MESSAGE = (
    "I reached the maximum number of document search steps without finding a complete answer. "
    "Please narrow or rephrase your question."
)
INTERNAL_ERROR_MESSAGE = "An internal server error occurred. Please try again later."

_TEMPLATE_PATH_RE = re.compile(
    r"^projects/(?P<project>[^/]+)/locations/(?P<location>[^/]+)/templates/(?P<template>[^/]+)$"
)

_SAFETY_THRESHOLD_MAP: Dict[str, HarmBlockThreshold] = {
    "BLOCK_LOW_AND_ABOVE": HarmBlockThreshold.BLOCK_LOW_AND_ABOVE,
    "BLOCK_MEDIUM_AND_ABOVE": HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
    "BLOCK_ONLY_HIGH": HarmBlockThreshold.BLOCK_ONLY_HIGH,
}

_SAFETY_BLOCK_REASONS = {
    "SAFETY",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "IMAGE_SAFETY",
}

_client_lock = threading.Lock()
_model_armor_clients: Dict[str, modelarmor_v1.ModelArmorClient] = {}


@dataclass(frozen=True)
class ScreeningDecision:
    """Structured outcome returned by prompt screening and output sanitization."""

    allowed: bool
    text: str
    blocked_reason: Optional[str] = None
    redacted: bool = False
    triggered_filters: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = ()


def validate_gcp_region(location: Optional[str] = None) -> str:
    """Validates that `GOOGLE_CLOUD_LOCATION` is a concrete single GCP region (never 'global' or a multi-region)."""
    raw = location if location is not None else get_google_cloud_location()
    cleaned = (raw or "").strip().lower()
    if not GCP_REGION_PATTERN.match(cleaned):
        raise ValueError(
            f"Invalid GOOGLE_CLOUD_LOCATION '{raw}'. Data residency requires a concrete single GCP "
            f"region (e.g., 'us-central1'), never 'global' or multi-regions ('us', 'eu', 'asia')."
        )
    return cleaned


def validate_collection_name(collection_name: Optional[str]) -> str:
    """Validates that `collection_name` is explicitly provided and matches a strict safe identifier pattern."""
    if collection_name is None or not isinstance(collection_name, str):
        raise ValueError("collection_name is required.")
    cleaned = collection_name.strip()
    if not cleaned:
        raise ValueError("collection_name is required and cannot be empty.")
    if not COLLECTION_NAME_PATTERN.match(cleaned):
        raise ValueError(
            "Invalid collection_name. Use 1-64 characters consisting only of letters, numbers, "
            "hyphens (-), and underscores (_)."
        )
    return cleaned


def validate_llm_model(model_name: Optional[str] = None) -> str:
    """Validates `model_name` against `ALLOWED_LLM_MODELS` (defaulting to the first approved model)."""
    allowed = get_allowed_llm_models()
    cleaned = (model_name or "").strip() or allowed[0]
    if cleaned not in allowed:
        allowed_csv = ", ".join(allowed)
        raise ValueError(
            f"Unsupported model_name '{cleaned}'. Approved LLM models: {allowed_csv}."
        )
    return cleaned


def validate_embedding_model(embedding_model: Optional[str] = None) -> str:
    """Validates `embedding_model` against `ALLOWED_EMBEDDING_MODELS` (defaulting to the first approved model)."""
    allowed = get_allowed_embedding_models()
    cleaned = (embedding_model or "").strip() or allowed[0]
    if cleaned not in allowed:
        allowed_csv = ", ".join(allowed)
        raise ValueError(
            f"Unsupported embedding_model '{cleaned}'. Approved embedding models: {allowed_csv}."
        )
    return cleaned


def validate_query_text(query: Optional[str]) -> str:
    """Validates that a user query is non-empty and within `MAX_QUERY_CHARS`."""
    if query is None or not isinstance(query, str):
        raise ValueError("Query text must be a non-empty string.")
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("Query text cannot be empty or whitespace only.")
    max_chars = get_max_query_chars()
    if len(cleaned) > max_chars:
        raise ValueError(
            f"Query exceeds the maximum length of {max_chars} characters (received {len(cleaned)})."
        )
    return cleaned


def get_gemini_safety_settings() -> Dict[HarmCategory, HarmBlockThreshold]:
    """Builds explicit Gemini HarmCategory -> HarmBlockThreshold settings from configuration."""
    threshold_name = get_gemini_safety_threshold()
    threshold = _SAFETY_THRESHOLD_MAP.get(threshold_name)
    if threshold is None:
        logger.warning(
            f"Unrecognized GEMINI_SAFETY_THRESHOLD='{threshold_name}'; falling back to BLOCK_LOW_AND_ABOVE."
        )
        threshold = HarmBlockThreshold.BLOCK_LOW_AND_ABOVE

    return {
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: threshold,
        HarmCategory.HARM_CATEGORY_HARASSMENT: threshold,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: threshold,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: threshold,
    }


def is_safety_blocked(response: Any) -> bool:
    """Returns True when a Gemini AIMessage indicates generation was blocked by safety filters."""
    metadata = getattr(response, "response_metadata", None) or {}
    finish_reason = str(metadata.get("finish_reason", "")).strip().upper()
    if finish_reason in _SAFETY_BLOCK_REASONS:
        return True

    prompt_feedback = metadata.get("prompt_feedback") or {}
    if isinstance(prompt_feedback, dict):
        block_reason = str(prompt_feedback.get("block_reason", "")).strip().upper()
        if block_reason and block_reason not in {"BLOCK_REASON_UNSPECIFIED", "0", "NONE"}:
            return True

    return False


def _resolve_model_armor_target() -> Optional[Tuple[str, str]]:
    """Resolves `(template_resource_name, location)` for Model Armor, or None when unconfigured."""
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
    """Returns a cached regional ModelArmorClient pointing to `modelarmor.<location>.rep.googleapis.com`."""
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
    """Safely checks whether a proto-plus message or mock object populates `field_name`."""
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
    """Extracts matched RAI filter category names (e.g. HARASSMENT, HATE_SPEECH, DANGEROUS, SEXUALLY_EXPLICIT)."""
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
    """Evaluates per-filter Model Armor results, blocking threats while preserving SDP de-identified text."""
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


def _is_model_armor_fail_closed() -> bool:
    """Returns True when unconfigured Model Armor should fail closed (`MODEL_ARMOR_FAIL_CLOSED=true`)."""
    return os.getenv("MODEL_ARMOR_FAIL_CLOSED", "").strip().lower() in {"1", "true", "yes"}


def screen_user_prompt(prompt: str) -> ScreeningDecision:
    """Screens an incoming user prompt via Google Cloud Model Armor (`sanitize_user_prompt`)."""
    prompt_digest = hash_query(prompt)
    prompt_len = len(prompt)
    target = _resolve_model_armor_target()
    if target is None:
        if _is_model_armor_fail_closed():
            logger.error("MODEL_ARMOR_TEMPLATE_ID is unset with MODEL_ARMOR_FAIL_CLOSED=true; failing closed.")
            record_guardrail_action("missing_model_armor_config", "blocked", stage="input")
            emit_audit_event(
                "security.prompt_blocked",
                severity="ERROR",
                status="blocked",
                reason="missing_model_armor_config",
                query_sha256=prompt_digest,
                query_chars=prompt_len,
            )
            return ScreeningDecision(
                allowed=False,
                text=PROMPT_BLOCKED_MESSAGE,
                blocked_reason="missing_model_armor_config",
                triggered_filters=(("missing_model_armor_config", "blocked", ()),),
            )
        if is_cloud_run():
            logger.warning("MODEL_ARMOR_TEMPLATE_ID is unset in Cloud Run; skipping Model Armor prompt check.")
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
    except Exception as e:
        logger.error(f"Model Armor prompt screening failed: {e}")
        record_guardrail_action("model_armor_error", "blocked", stage="input")
        emit_audit_event(
            "security.prompt_screening_error",
            severity="ERROR",
            status="error",
            reason="model_armor_error",
            query_sha256=prompt_digest,
            query_chars=prompt_len,
        )
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
    """Screens and redacts PII/secrets in LLM output via Model Armor + Cloud DLP (`sanitize_model_response`)."""
    if not output_text or not output_text.strip():
        return ScreeningDecision(allowed=True, text=output_text)

    output_len = len(output_text)
    target = _resolve_model_armor_target()
    if target is None:
        if _is_model_armor_fail_closed():
            logger.error("MODEL_ARMOR_TEMPLATE_ID is unset with MODEL_ARMOR_FAIL_CLOSED=true; failing closed.")
            record_guardrail_action("missing_model_armor_config", "blocked", stage="output")
            emit_audit_event(
                "security.output_blocked",
                severity="ERROR",
                status="blocked",
                reason="missing_model_armor_config",
                output_chars=output_len,
            )
            return ScreeningDecision(
                allowed=False,
                text=SAFETY_REFUSAL_MESSAGE,
                blocked_reason="missing_model_armor_config",
                triggered_filters=(("missing_model_armor_config", "blocked", ()),),
            )
        if is_cloud_run():
            logger.warning("MODEL_ARMOR_TEMPLATE_ID is unset in Cloud Run; skipping Model Armor output check.")
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
    except Exception as e:
        logger.error(f"Model Armor output sanitization failed: {e}")
        record_guardrail_action("model_armor_error", "blocked", stage="output")
        emit_audit_event(
            "security.output_screening_error",
            severity="ERROR",
            status="error",
            reason="model_armor_error",
            output_chars=output_len,
        )
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
    """Extracts Cloud DLP `[INFO_TYPE]`-masked text from `sdp_filter_result` while preserving surrounding chunk text."""
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
    """Masks sensitive spans in a retrieved chunk with `[INFO_TYPE]` placeholders via Model Armor SDP + Cloud DLP."""
    if not chunk_text or not chunk_text.strip():
        return chunk_text

    chunk_len = len(chunk_text)
    target = _resolve_model_armor_target()
    if target is None:
        if _is_model_armor_fail_closed():
            logger.error("MODEL_ARMOR_TEMPLATE_ID is unset with MODEL_ARMOR_FAIL_CLOSED=true; failing closed.")
            emit_audit_event(
                "security.chunk_screening_error",
                severity="ERROR",
                status="error",
                reason="missing_model_armor_config",
                chunk_chars=chunk_len,
            )
            raise RuntimeError("Model Armor chunk sanitization failed (missing_model_armor_config).")
        if is_cloud_run():
            logger.warning("MODEL_ARMOR_TEMPLATE_ID is unset in Cloud Run; skipping Model Armor chunk sanitization.")
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
    except Exception as e:
        logger.error(f"Model Armor chunk sanitization failed: {e}")
        emit_audit_event(
            "security.chunk_screening_error",
            severity="ERROR",
            status="error",
            reason="model_armor_error",
            chunk_chars=chunk_len,
        )
        raise RuntimeError("Model Armor chunk sanitization failed (model_armor_error).") from e


def sanitize_retrieved_documents(docs: Sequence[Document]) -> List[Document]:
    """Sanitizes a sequence of retrieved Document chunks concurrently while preserving chunk order and metadata."""
    if not docs:
        return []

    target = _resolve_model_armor_target()
    if target is None and not _is_model_armor_fail_closed():
        if is_cloud_run():
            logger.warning(
                "MODEL_ARMOR_TEMPLATE_ID is unset in Cloud Run; skipping Model Armor chunk sanitization."
            )
        return list(docs)

    if len(docs) == 1 or target is None:
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


