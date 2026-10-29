# Copyright (c) 2024 Maggie Mhanna
# All rights reserved.

"""Centralized application logging configuration and safe dictionary formatting utilities.

Installs consistent colored console handlers on stdout across local development and
Cloud Run containers, and provides recursive truncation helpers for diagnostic state dumps.
"""

import json
import logging
import sys
from copy import deepcopy
from typing import Any, Optional

import coloredlogs

from src.core.config import is_structured_logging_enabled
from src.core.observability.logging import GCPJsonFormatter, RequestContextLogFilter

# Define the constants for the log format
# We use the same format string as before, coloredlogs handles the coloring automatically.
LOG_FORMAT = (
    "%(levelname)s "             # Log level (will be colored)
    "[%(asctime)s] "             # Timestamp
    "{%(filename)s:%(lineno)d} " # File name and line number
    "%(funcName)s "              # Function name
    "- %(message)s"              # The actual log message
)

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _ensure_context_filter(target: Any) -> None:
    """Attaches a `RequestContextLogFilter` to a logger or handler if not already present."""
    if not any(isinstance(f, RequestContextLogFilter) for f in getattr(target, "filters", [])):
        target.addFilter(RequestContextLogFilter())


def setup_logging(
    level: int = logging.INFO,
    name: Optional[str] = None,
    force: bool = False,
) -> logging.Logger:
    """Configures the root logger (`GCPJsonFormatter` on Cloud Run, `coloredlogs` locally) and returns a named logger.

    Args:
        level: Minimum Python logging level to emit (defaults to `logging.INFO`).
        name: Optional logger namespace (e.g., `'ingestion'`, `'agent'`, `'audit'`).
        force: When `True`, rebuilds root logger handlers even if already configured.

    Returns:
        Configured `logging.Logger` instance at the requested level.
    """
    structured = is_structured_logging_enabled()
    root_logger = logging.getLogger()

    has_json_handler = any(
        isinstance(getattr(h, "formatter", None), GCPJsonFormatter)
        for h in root_logger.handlers
    )
    mode_mismatch = bool(root_logger.handlers) and (structured != has_json_handler)

    if force or not root_logger.handlers or mode_mismatch:
        for existing in list(root_logger.handlers):
            root_logger.removeHandler(existing)

        if structured:
            json_handler = logging.StreamHandler(sys.stdout)
            json_handler.setLevel(level)
            json_handler.setFormatter(GCPJsonFormatter())
            _ensure_context_filter(json_handler)
            root_logger.addHandler(json_handler)
            root_logger.setLevel(level)
        else:
            # Define custom styles for standard levels
            custom_level_styles = deepcopy(coloredlogs.DEFAULT_LEVEL_STYLES)
            custom_level_styles["error"] = {"color": "red"}
            custom_level_styles["info"] = {"color": "green", "bold": True}
            custom_level_styles["warning"] = {"color": "yellow"}

            # Define custom styles for fields
            custom_field_styles = deepcopy(coloredlogs.DEFAULT_FIELD_STYLES)
            custom_field_styles["filename"] = {"color": "cyan", "bold": True}
            custom_field_styles["funcName"] = {"color": "white", "bold": True}
            custom_field_styles["lineno"] = {"color": "cyan", "bold": True}  # e.g., make lineno cyan

            coloredlogs.install(
                level=level,
                fmt=LOG_FORMAT,
                datefmt=DATE_FORMAT,
                logger=root_logger,  # Ensure it configures the root logger,
                level_styles=custom_level_styles,
                field_styles=custom_field_styles,
                stream=sys.stdout,
            )

    for handler in root_logger.handlers:
        _ensure_context_filter(handler)
    _ensure_context_filter(root_logger)

    # 2. Retrieve the specific logger instance
    logger = logging.getLogger(name)
    logger.setLevel(level)  # Ensure the retrieved logger uses the specified level
    _ensure_context_filter(logger)

    return logger


def format_dict_for_logs(state_dict: Any, max_len: int = 100) -> str:
    """Formats a nested dictionary or list into a pretty-printed, length-bounded JSON string.

    Deep-copies `state_dict` so caller state is never mutated and truncates any string
    values exceeding `max_len` characters.

    Args:
        state_dict: Arbitrary JSON-serializable dictionary, list, or primitive structure.
        max_len: Maximum character length allowed per string value before truncation.

    Returns:
        Indented JSON string safe for diagnostic log output.
    """
    # 1. Use deepcopy to avoid modifying the original session state
    truncated_dict = deepcopy(state_dict)

    # 2. Define a recursive function to walk through the dictionary/list structure
    def truncate_recursive(data: Any) -> Any:
        """Recursively truncates string leaves inside nested dicts and lists."""
        if isinstance(data, dict):
            return {k: truncate_recursive(v) for k, v in data.items()}

        elif isinstance(data, list):
            return [truncate_recursive(item) for item in data]

        elif isinstance(data, str) and len(data) > max_len:
            # 3. Perform truncation
            return data[:max_len] + f" ... (TRUNCATED, length {len(data)})"

        else:
            # 4. Return other types (int, float, bool, None) as is
            return data

    # Apply truncation
    truncated_dict = truncate_recursive(truncated_dict)

    # 5. Pretty print the resulting dictionary as JSON
    return json.dumps(truncated_dict, indent=4)
