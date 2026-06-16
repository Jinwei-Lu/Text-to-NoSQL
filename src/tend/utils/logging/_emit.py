from __future__ import annotations

# ruff: noqa: F403,F405

from tend.utils.logging._config import *

def emit_to_both(
    stage_log: Any | None,
    task_log: Any | None,
    event: str,
    *,
    level: str = "info",
    **payload: Any,
) -> None:
    """Emit the same structured event to two structlog loggers in one call.

    Both ``stage_log`` and ``task_log`` may be ``None`` — non-None loggers
    receive ``logger.<level>(event, **payload)``. Use ``level="warning"`` /
    ``"error"`` / ``"debug"`` to override the default ``info`` severity.

    This collapses the orchestrator's paired-emit pattern (37 sites where
    the same event was fired to both a stage logger and a task logger
    with byte-identical payloads).
    """
    for logger in (stage_log, task_log):
        if logger is None:
            continue
        method = getattr(logger, level, None)
        if method is None:
            method = logger.info  # type: ignore[union-attr]
        method(event, **payload)
