"""Content safety controls and standardized user-facing refusal messages (Pillar 3: AI Security).

Centralizes Vertex AI Gemini `HarmCategory` / `HarmBlockThreshold` configuration,
post-generation safety block detection (`finish_reason` and `prompt_feedback`), and
sanitized, leak-free fallback messages shared across REST and SSE responses.
"""

from typing import Any, Dict

from langchain_google_genai import HarmBlockThreshold, HarmCategory

from src.core.config import get_gemini_safety_threshold
from src.core.logger import setup_logging

logger = setup_logging(name="ai_security.safety")

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


def get_gemini_safety_settings() -> Dict[HarmCategory, HarmBlockThreshold]:
    """Builds explicit Vertex AI Gemini `HarmCategory -> HarmBlockThreshold` settings.

    Reads `GEMINI_SAFETY_THRESHOLD` from runtime configuration and falls back to
    `BLOCK_LOW_AND_ABOVE` if an unrecognized value is supplied.

    Returns:
        Dictionary mapping hate speech, harassment, sexually explicit, and dangerous
        content harm categories to the configured `HarmBlockThreshold`.
    """
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
    """Checks whether a Gemini `AIMessage` indicates generation was halted by safety filters.

    Inspects both `response_metadata["finish_reason"]` and `response_metadata["prompt_feedback"]`
    so blocked turns can be replaced with `SAFETY_REFUSAL_MESSAGE` and logged cleanly.

    Args:
        response: LangChain `AIMessage` (or compatible object) returned by `ChatGoogleGenerativeAI`.

    Returns:
        True if Vertex AI safety, blocklist, prohibited content, or SPII filters blocked output.
    """
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


__all__ = [
    "INTERNAL_ERROR_MESSAGE",
    "PROMPT_BLOCKED_MESSAGE",
    "RECURSION_LIMIT_MESSAGE",
    "SAFETY_REFUSAL_MESSAGE",
    "get_gemini_safety_settings",
    "is_safety_blocked",
]
