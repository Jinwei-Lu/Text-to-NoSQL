"""Process-wide locks for concurrent LLM / logging / cache I/O."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

JSONL_APPEND_LOCK = threading.Lock()

_CACHE_KEY_LOCKS: dict[str, threading.Lock] = {}
_CACHE_META_LOCK = threading.Lock()


def cache_lock_for(key: str) -> threading.Lock:
    with _CACHE_META_LOCK:
        lock = _CACHE_KEY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _CACHE_KEY_LOCKS[key] = lock
        return lock


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Thread-safe single-line JSONL append (one JSON object per line)."""
    line = json.dumps(record, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with JSONL_APPEND_LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
