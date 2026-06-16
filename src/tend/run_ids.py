"""Run id generation shared by configuration and observability."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4


_UNSAFE_TAG_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


def new_run_id(prefix: str = "run") -> str:
    """Return a filesystem-safe timestamped run id."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S-%fZ")
    suffix = uuid4().hex[:16]
    if prefix:
        return f"{prefix}-{timestamp}-{suffix}"
    return f"{timestamp}-{suffix}"


def run_id_with_tag(tag: str | None, *, prefix: str = "run") -> str:
    """Return a timestamped run id that includes a user-provided tag."""
    safe_tag = _safe_run_id_tag(tag)
    if not safe_tag:
        return new_run_id(prefix=prefix)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S-%fZ")
    suffix = uuid4().hex[:16]
    if prefix:
        return f"{prefix}-{timestamp}-{safe_tag}-{suffix}"
    return f"{timestamp}-{safe_tag}-{suffix}"


def _safe_run_id_tag(tag: str | None) -> str:
    raw = (tag or "").strip()
    if not raw:
        return ""
    safe = _UNSAFE_TAG_CHARS.sub("-", raw)
    safe = safe.strip("-._")
    safe = re.sub(r"-{2,}", "-", safe)
    return safe[:80]
