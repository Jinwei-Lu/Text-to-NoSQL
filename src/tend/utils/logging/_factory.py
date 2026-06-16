from __future__ import annotations

# ruff: noqa: F403,F405

from tend.utils.logging._config import *

def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Simple module-level logger for code outside of stage/task context."""
    _ensure_structlog_configured()
    return structlog.get_logger(name)
