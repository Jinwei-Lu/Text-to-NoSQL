"""Per-stage + per-task structured logging with isolated log files for parallel units.

Core invariant: no two concurrent tasks write to the same log file.
Parent logs record only lifecycle events; each parallel unit gets its own file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import structlog

from tend.utils.paths import safe_dirname

_configured = False
_STRUCTLOG_RESERVED_KEYS = {"event", "level", "logger", "message", "timestamp"}
_COST_APPEND_RETRIES = 5
_COST_APPEND_RETRY_BASE_SECONDS = 0.05
_LEGACY_ITER_LABEL_RE = re.compile(
    r"^(?P<step>[A-Za-z][\w-]*)_i(?P<iter>\d+)(?P<tail>.*)$"
)
_ITER_LABEL_RE = re.compile(r"^iter_(?P<iter>\d+)(?:_(?P<step>.*))?$")


class _RunContextFilter(logging.Filter):
    """Attach run metadata to stdlib records captured by the root handler."""

    def __init__(self, *, run_id: str, command: str) -> None:
        super().__init__()
        self._run_id = run_id
        self._command = command

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self._run_id
        if self._command:
            record.command = self._command
        return True


class _NamePrefixFilter(logging.Filter):
    """Admit only records from loggers whose name starts with a prefix.

    Used by the run-wide ``milestones.jsonl`` handler so it captures the
    first-party ``tend.*`` module-logger INFO milestones (seed/loop/D2)
    that propagate to the root logger, while excluding noisy third-party
    INFO (httpx, openai, asyncpg, sqlglot, ...).
    """

    def __init__(self, prefix: str) -> None:
        super().__init__()
        self._prefix = prefix

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(self._prefix)


def _sanitize_log_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Avoid collisions with structlog processor-managed fields."""

    sanitized: dict[str, Any] = {}
    for key, value in kwargs.items():
        out_key = f"payload_{key}" if key in _STRUCTLOG_RESERVED_KEYS else key
        sanitized[out_key] = value
    return sanitized


def _ensure_structlog_configured() -> None:
    global _configured
    if _configured:
        return

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    _configured = True


_json_formatter: structlog.stdlib.ProcessorFormatter | None = None


def _get_json_formatter() -> structlog.stdlib.ProcessorFormatter:
    global _json_formatter
    if _json_formatter is None:
        _json_formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.stdlib.ExtraAdder(),
            ],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
    return _json_formatter

__all__ = [
    '_configured',
    '_STRUCTLOG_RESERVED_KEYS',
    '_COST_APPEND_RETRIES',
    '_COST_APPEND_RETRY_BASE_SECONDS',
    '_LEGACY_ITER_LABEL_RE',
    '_ITER_LABEL_RE',
    '_RunContextFilter',
    '_NamePrefixFilter',
    '_sanitize_log_kwargs',
    '_ensure_structlog_configured',
    '_json_formatter',
    '_get_json_formatter',
    'json',
    'logging',
    'os',
    're',
    'threading',
    'time',
    'datetime',
    'timezone',
    'Path',
    'Any',
    'Literal',
    'structlog',
    'safe_dirname',
]
