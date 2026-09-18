"""Unified failure records and exception summarisation."""

from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

ErrorTier = Literal["system", "cancelled"]


@dataclass(slots=True)
class FailureRecord:
    """Serializable failure envelope used by logs, progress, and panels."""

    error_type: str
    error_message: str
    tier: ErrorTier = "system"
    traceback: str = ""
    stage: str | None = None
    task_id: str | None = None
    hint: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def model_dump(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error_type": self.error_type,
            "error_message": self.error_message,
            "tier": self.tier,
            "traceback": self.traceback,
            "stage": self.stage,
            "task_id": self.task_id,
            "hint": self.hint,
            "timestamp": self.timestamp,
        }
        payload.update(self.extra)
        return payload


def _safe_str(value: Any, *, max_len: int = 4000) -> str:
    try:
        text = str(value)
    except Exception as inner:  # pragma: no cover - defensive crash path
        text = f"<str failed: {type(inner).__name__}: {inner!r}>"
    if len(text) > max_len:
        return text[:max_len] + "... [truncated]"
    return text


def classify_failure_exception(exc: BaseException) -> ErrorTier:
    """Classify an exception as a cancellation or a system failure."""

    if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
        return "cancelled"
    return "system"


def exception_summary(
    exc: BaseException,
    *,
    stage: str | None = None,
    task_id: str | None = None,
    include_traceback: bool = True,
) -> dict[str, Any]:
    """Return a JSON-safe exception summary."""

    tb = (
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if include_traceback
        else ""
    )
    record = FailureRecord(
        error_type=type(exc).__name__,
        error_message=_safe_str(exc, max_len=4000),
        tier=classify_failure_exception(exc),
        traceback=tb,
        stage=stage,
        task_id=task_id,
    )
    return record.model_dump()
