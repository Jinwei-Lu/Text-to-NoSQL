"""Public facade for DynaDB structured logging."""

# The facade intentionally configures structlog before importing public helpers.
# ruff: noqa: E402

from tend.utils.logging._config import _ensure_structlog_configured, time

_ensure_structlog_configured()

from tend.utils.logging._emit import emit_to_both
from tend.utils.logging._factory import get_logger
from tend.utils.logging._formatters import (
    _format_seed_outcome_section,
    _format_tool_calls_md,
    _format_tool_results_md,
)
from tend.utils.logging._log_manager import LogManager
from tend.utils.logging._paths import _generate_call_id, _normalize_iter_label
from tend.utils.logging._run_facade import RunLoggerFacade
from tend.utils.logging._stage_logger import StageLogger
from tend.utils.logging._task_logger import AgentTurnLogPayload, TaskLogger

__all__ = [
    "get_logger",
    "AgentTurnLogPayload",
    "LogManager",
    "RunLoggerFacade",
    "StageLogger",
    "TaskLogger",
    "emit_to_both",
    "_format_seed_outcome_section",
    "_format_tool_calls_md",
    "_format_tool_results_md",
    "_generate_call_id",
    "_normalize_iter_label",
    "time",
]
