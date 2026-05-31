"""Structured logging with structlog + Rich console."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import structlog

from tend.config import RUN_DIR
from tend.core.sync import append_jsonl

_RUN_DIR: Path | None = None
_LOG_PATH: Path | None = None


def get_run_dir() -> Path:
    if _RUN_DIR is None:
        init_run_dir()
    assert _RUN_DIR is not None
    return _RUN_DIR


def init_run_dir(run_dir: Path | None = None) -> Path:
    global _RUN_DIR, _LOG_PATH
    _RUN_DIR = run_dir or RUN_DIR
    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_PATH = _RUN_DIR / "log.jsonl"
    (_RUN_DIR / "errors.jsonl").touch(exist_ok=True)
    (_RUN_DIR / "console.log").touch(exist_ok=True)
    from tend.core import llm_transcript as llm_transcript_module

    llm_transcript_module.init(_RUN_DIR)
    return _RUN_DIR


def _write_jsonl(path: Path, payload: dict[str, Any]) -> None:
    append_jsonl(path, payload)


def configure_logging(*, quiet: bool = False) -> structlog.BoundLogger:
    init_run_dir()
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.JSONRenderer(),
    ]
    structlog.configure(processors=processors, wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    logger = structlog.get_logger("tend")

    if not quiet and sys.stdout.isatty():
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger()
        root.handlers = [handler]
        root.setLevel(logging.INFO)

    return logger


def bind(**kwargs: Any) -> None:
    structlog.contextvars.bind_contextvars(**kwargs)


def emit(event: str, level: str = "INFO", **payload: Any) -> None:
    logger = structlog.get_logger("tend")
    log_fn = getattr(logger, level.lower(), logger.info)
    record = {"event": event, **payload}
    log_fn(event, **payload)
    if _LOG_PATH:
        _write_jsonl(_LOG_PATH, {"level": level, "event": event, **payload})
