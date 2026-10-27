"""AI Security package (Pillar 3) for InteractiveRAG.

Divides AI-specific runtime protections into three focused modules:
- `armor`: Regional Google Cloud Model Armor + Cloud DLP prompt, chunk, and output screening.
- `safety`: Vertex AI Gemini `HarmBlockThreshold` settings, safety-block inspection, and refusal messages.
- `guardrails`: Approved LLM/embedding model catalogs, input/ingestion ceilings, and recursion limits.
"""

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.core.ai_security.armor import (
        ScreeningDecision,
        sanitize_chunk_text,
        sanitize_model_output,
        sanitize_retrieved_documents,
        screen_user_prompt,
    )
    from src.core.ai_security.guardrails import (
        get_bounded_recursion_limit,
        validate_chunk_count,
        validate_collection_name,
        validate_embedding_model,
        validate_file_count,
        validate_llm_model,
        validate_query_text,
    )
    from src.core.ai_security.safety import (
        INTERNAL_ERROR_MESSAGE,
        PROMPT_BLOCKED_MESSAGE,
        RECURSION_LIMIT_MESSAGE,
        SAFETY_REFUSAL_MESSAGE,
        get_gemini_safety_settings,
        is_safety_blocked,
    )

_EXPORT_MAP: dict[str, str] = {
    "ScreeningDecision": "src.core.ai_security.armor",
    "sanitize_chunk_text": "src.core.ai_security.armor",
    "sanitize_model_output": "src.core.ai_security.armor",
    "sanitize_retrieved_documents": "src.core.ai_security.armor",
    "screen_user_prompt": "src.core.ai_security.armor",
    "get_bounded_recursion_limit": "src.core.ai_security.guardrails",
    "validate_chunk_count": "src.core.ai_security.guardrails",
    "validate_collection_name": "src.core.ai_security.guardrails",
    "validate_embedding_model": "src.core.ai_security.guardrails",
    "validate_file_count": "src.core.ai_security.guardrails",
    "validate_llm_model": "src.core.ai_security.guardrails",
    "validate_query_text": "src.core.ai_security.guardrails",
    "INTERNAL_ERROR_MESSAGE": "src.core.ai_security.safety",
    "PROMPT_BLOCKED_MESSAGE": "src.core.ai_security.safety",
    "RECURSION_LIMIT_MESSAGE": "src.core.ai_security.safety",
    "SAFETY_REFUSAL_MESSAGE": "src.core.ai_security.safety",
    "get_gemini_safety_settings": "src.core.ai_security.safety",
    "is_safety_blocked": "src.core.ai_security.safety",
}

__all__ = sorted(_EXPORT_MAP.keys())


def __getattr__(name: str) -> Any:
    """Lazily resolves public package exports without eager cross-package import cycles.

    Args:
        name: Exported attribute name requested on `src.core.ai_security`.

    Returns:
        The resolved attribute from its defining submodule.

    Raises:
        AttributeError: If `name` is not part of the package public API.
    """
    module_path = _EXPORT_MAP.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_path)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Lists available attributes on `src.core.ai_security` for interactive introspection."""
    return sorted(set(globals().keys()) | set(__all__))
