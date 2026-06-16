"""Emit structured events that reach the per-task log file.

Module-level loggers (``log = get_logger(__name__)``) route to a logger
named after the module path (e.g. ``tend.derive.d1.seed``). Per-task
log files like ``portfolio.log`` / ``b2/<model_id>`` filter on the task
logger name (e.g. ``tend.<runid>.d1.portfolio.<pid>``), so module-level
events get dropped and become invisible to anyone debugging via the per-
task log.

``emit_task_event`` routes through the TaskLogger when one is available
(so events land in the per-task log alongside the rest of the trace) and
falls back to a module logger otherwise (so standalone helpers and unit
tests still emit something).

Use this for any structured event a human will want to grep in
portfolio.log — rejection reasons, gate failures, quarantine decisions,
preflight skips, etc.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tend.utils.logging import get_logger

if TYPE_CHECKING:
    from tend.utils.logging import TaskLogger

_fallback_log = get_logger(__name__)


def emit_task_event(
    task_logger: "TaskLogger | None",
    event_name: str,
    **fields: Any,
) -> None:
    """Emit ``event_name`` with structured fields via TaskLogger when bound.

    Falls back to the module-level logger when ``task_logger`` is None
    or its ``info`` raises (e.g. broken pipe during teardown). The
    fallback path keeps the event in the global log so debugging is
    still possible — just not in the per-task file.
    """
    if task_logger is not None:
        try:
            task_logger.info(event_name, **fields)
            return
        except Exception:
            _fallback_log.debug(
                "emit_task_event_failed",
                event=event_name,
                exc_info=True,
            )
    _fallback_log.info(event_name, **fields)
