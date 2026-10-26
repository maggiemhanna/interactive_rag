from concurrent.futures import ThreadPoolExecutor
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from google.cloud import modelarmor_v1
from langchain_core.documents import Document
from langchain_google_genai import HarmBlockThreshold, HarmCategory

from src.core.auth import is_cloud_run
from src.core.config import (
    COLLECTION_NAME_PATTERN,
    DEFAULT_COLLECTION,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_LLM_MODEL,
    get_allowed_embedding_models,
    get_allowed_llm_models,
    get_gemini_safety_threshold,
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


def validate_collection_name(collection_name: Optional[str]) -> str:
    """Validates `collection_name` against a strict safe identifier pattern."""
    cleaned = (collection_name if collection_name is not None else DEFAULT_COLLECTION).strip()
    if not cleaned:
        cleaned = DEFAULT_COLLECTION
    if not COLLECTION_NAME_PATTERN.match(cleaned):
        raise ValueError(
            "Invalid collection_name. Use 1-64 characters consisting only of letters, numbers, "
            "hyphens (-), and underscores (_)."
        )
    return cleaned


def validate_llm_model(model_name: Optional[str]) -> str:
    """Validates `model_name` against the server-approved generative LLM allowlist."""
    cleaned = (model_name or "").strip() or DEFAULT_LLM_MODEL
    allowed = get_allowed_llm_models()
    if cleaned not in allowed:
        allowed_csv = ", ".join(allowed)
        raise ValueError(
            f"Unsupported model_name '{cleaned}'. Approved LLM models: {allowed_csv}."
        )
    return cleaned


def validate_embedding_model(embedding_model: Optional[str]) -> str:
    """Validates `embedding_model` against the server-approved embedding model allowlist."""
    cleaned = (embedding_model or "").strip() or DEFAULT_EMBEDDING_MODEL
    allowed = get_allowed_embedding_models()
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
        return raw_template, match.group("location")

    project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    location = get_model_armor_location()
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


def _inspect_sanitization_result(
    sanitization_result: Any,
    original_text: str,
    block_message_for_pi: str,
) -> ScreeningDecision:
    """Evaluates per-filter Model Armor results, blocking threats while preserving SDP de-identified text."""
    match_found = modelarmor_v1.FilterMatchState.MATCH_FOUND
    filter_results = getattr(sanitization_result, "filter_results", None) or {}

    deidentified_text: Optional[str] = None
    sdp_inspect_only_matched = False

    for filter_result in filter_results.values():
        if _has_proto_field(filter_result, "pi_and_jailbreak_filter_result"):
            pi_res = filter_result.pi_and_jailbreak_filter_result
            if getattr(pi_res, "match_state", None) == match_found:
                return ScreeningDecision(
                    allowed=False,
                    text=block_message_for_pi,
                    blocked_reason="pi_and_jailbreak",
                )

        if _has_proto_field(filter_result, "malicious_uri_filter_result"):
            uri_res = filter_result.malicious_uri_filter_result
            if getattr(uri_res, "match_state", None) == match_found:
                return ScreeningDecision(
                    allowed=False,
                    text=block_message_for_pi,
                    blocked_reason="malicious_uri",
                )

        if _has_proto_field(filter_result, "rai_filter_result"):
            rai_res = filter_result.rai_filter_result
            if getattr(rai_res, "match_state", None) == match_found:
                return ScreeningDecision(
                    allowed=False,
                    text=SAFETY_REFUSAL_MESSAGE,
                    blocked_reason="rai",
                )

        if _has_proto_field(filter_result, "csam_filter_filter_result"):
            csam_res = filter_result.csam_filter_filter_result
            if getattr(csam_res, "match_state", None) == match_found:
                return ScreeningDecision(
                    allowed=False,
                    text=SAFETY_REFUSAL_MESSAGE,
                    blocked_reason="csam",
                )

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

    if sdp_inspect_only_matched and deidentified_text is None:
        return ScreeningDecision(
            allowed=False,
            text=SAFETY_REFUSAL_MESSAGE,
            blocked_reason="sdp",
        )

    if deidentified_text is not None:
        return ScreeningDecision(
            allowed=True,
            text=deidentified_text,
            redacted=(deidentified_text != original_text),
        )

    # Fallback if top-level match_found was set without populated per-filter details
    if getattr(sanitization_result, "filter_match_state", None) == match_found:
        return ScreeningDecision(
            allowed=False,
            text=block_message_for_pi,
            blocked_reason="model_armor_policy",
        )

    return ScreeningDecision(allowed=True, text=original_text)


def _is_model_armor_fail_closed() -> bool:
    """Returns True when unconfigured Model Armor should fail closed (`MODEL_ARMOR_FAIL_CLOSED=true`)."""
    return os.getenv("MODEL_ARMOR_FAIL_CLOSED", "").strip().lower() in {"1", "true", "yes"}


def screen_user_prompt(prompt: str) -> ScreeningDecision:
    """Screens an incoming user prompt via Google Cloud Model Armor (`sanitize_user_prompt`)."""
    target = _resolve_model_armor_target()
    if target is None:
        if _is_model_armor_fail_closed():
            logger.error("MODEL_ARMOR_TEMPLATE_ID is unset with MODEL_ARMOR_FAIL_CLOSED=true; failing closed.")
            return ScreeningDecision(
                allowed=False,
                text=PROMPT_BLOCKED_MESSAGE,
                blocked_reason="missing_model_armor_config",
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
        decision = _inspect_sanitization_result(
            sanitization_result=response.sanitization_result,
            original_text=prompt,
            block_message_for_pi=PROMPT_BLOCKED_MESSAGE,
        )
        if not decision.allowed:
            logger.warning(
                f"Model Armor blocked user prompt (reason={decision.blocked_reason})."
            )
        return decision
    except Exception as e:
        logger.error(f"Model Armor prompt screening failed: {e}")
        return ScreeningDecision(
            allowed=False,
            text=INTERNAL_ERROR_MESSAGE,
            blocked_reason="model_armor_error",
        )


def sanitize_model_output(
    output_text: str,
    user_prompt: Optional[str] = None,
) -> ScreeningDecision:
    """Screens and redacts PII/secrets in LLM output via Model Armor + Cloud DLP (`sanitize_model_response`)."""
    if not output_text or not output_text.strip():
        return ScreeningDecision(allowed=True, text=output_text)

    target = _resolve_model_armor_target()
    if target is None:
        if _is_model_armor_fail_closed():
            logger.error("MODEL_ARMOR_TEMPLATE_ID is unset with MODEL_ARMOR_FAIL_CLOSED=true; failing closed.")
            return ScreeningDecision(
                allowed=False,
                text=SAFETY_REFUSAL_MESSAGE,
                blocked_reason="missing_model_armor_config",
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
        decision = _inspect_sanitization_result(
            sanitization_result=response.sanitization_result,
            original_text=output_text,
            block_message_for_pi=SAFETY_REFUSAL_MESSAGE,
        )
        if not decision.allowed:
            logger.warning(
                f"Model Armor blocked model response (reason={decision.blocked_reason})."
            )
        elif decision.redacted:
            logger.info("Model Armor + Cloud DLP redacted sensitive data in model response.")
        return decision
    except Exception as e:
        logger.error(f"Model Armor output sanitization failed: {e}")
        return ScreeningDecision(
            allowed=False,
            text=INTERNAL_ERROR_MESSAGE,
            blocked_reason="model_armor_error",
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

    target = _resolve_model_armor_target()
    if target is None:
        if _is_model_armor_fail_closed():
            logger.error("MODEL_ARMOR_TEMPLATE_ID is unset with MODEL_ARMOR_FAIL_CLOSED=true; failing closed.")
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
        redacted_text = _extract_sdp_deidentified_text(
            sanitization_result=response.sanitization_result,
            original_text=chunk_text,
        )
        if redacted_text != chunk_text:
            logger.info("Model Armor + Cloud DLP masked sensitive infoTypes in retrieved document chunk.")
        return redacted_text
    except Exception as e:
        logger.error(f"Model Armor chunk sanitization failed: {e}")
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
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            sanitized_texts = list(
                executor.map(sanitize_chunk_text, [doc.page_content or "" for doc in docs])
            )

    return [
        Document(page_content=text, metadata=dict(doc.metadata or {}))
        for doc, text in zip(docs, sanitized_texts)
    ]

