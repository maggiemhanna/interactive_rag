"""Standardized user-facing refusal and error messages (Pillar 3: AI Security).

Centralizes sanitized, leak-free fallback messages shared across Model Armor screening,
bounded agent execution, and REST/SSE error responses.
"""

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
SCREENING_UNAVAILABLE_MESSAGE = (
    "AI security screening is temporarily unavailable. Please try again shortly."
)

__all__ = [
    "INTERNAL_ERROR_MESSAGE",
    "PROMPT_BLOCKED_MESSAGE",
    "RECURSION_LIMIT_MESSAGE",
    "SAFETY_REFUSAL_MESSAGE",
    "SCREENING_UNAVAILABLE_MESSAGE",
]
