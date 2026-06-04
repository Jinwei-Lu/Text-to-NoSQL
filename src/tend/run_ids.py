"""Run id generation shared by configuration and observability."""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4


def new_run_id(prefix: str = "run") -> str:
    """Return a filesystem-safe timestamped run id."""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    suffix = uuid4().hex[:4]
    if prefix:
        return f"{prefix}-{timestamp}-{suffix}"
    return f"{timestamp}-{suffix}"
