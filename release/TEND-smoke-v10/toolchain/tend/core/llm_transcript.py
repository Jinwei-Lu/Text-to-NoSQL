"""Per-call LLM transcript logging (full prompt + response)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from tend.config import RUN_DIR
from tend.core.sync import append_jsonl

_TRANSCRIPT_PATH: Path | None = None
_MAX_CHARS = int(os.getenv("TEND_LLM_LOG_MAX_CHARS", "0") or "0")


def init(run_dir: Path | None = None) -> Path:
    global _TRANSCRIPT_PATH
    run_dir = run_dir or RUN_DIR
    run_dir.mkdir(parents=True, exist_ok=True)
    _TRANSCRIPT_PATH = run_dir / "llm_transcript.jsonl"
    _TRANSCRIPT_PATH.touch(exist_ok=True)
    return run_dir


def transcript_path() -> Path | None:
    if _TRANSCRIPT_PATH is None:
        init()
    return _TRANSCRIPT_PATH


def _clip(text: str) -> str:
    if _MAX_CHARS <= 0 or len(text) <= _MAX_CHARS:
        return text
    return text[: _MAX_CHARS] + f"\n... [truncated, total {len(text)} chars]"


def _serialize_response(payload: dict | str) -> tuple[Any, str]:
    if isinstance(payload, dict):
        raw = json.dumps(payload, ensure_ascii=False)
        return payload, raw
    return payload, str(payload)


def log_call(
    *,
    pool: str,
    model: str,
    seed: int,
    prompt: str,
    response: dict | str,
    prompt_hash: str,
    cache_hit: bool,
    stub: bool,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: float = 0.0,
    cost_usd: float = 0.0,
    temperature: float = 0.0,
    error: str | None = None,
) -> None:
    """Append one LLM exchange to {RUN_DIR}/llm_transcript.jsonl."""
    path = transcript_path()
    assert path is not None

    response_obj, response_raw = _serialize_response(response)
    ctx = structlog.contextvars.get_contextvars()

    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "llm.transcript",
        "pool": pool,
        "model": model,
        "seed": seed,
        "temperature": temperature,
        "prompt_hash": prompt_hash,
        "cache_hit": cache_hit,
        "stub": stub,
        "prompt": _clip(prompt),
        "response": response_obj if isinstance(response_obj, (dict, list)) else _clip(str(response_obj)),
        "response_raw": _clip(response_raw),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "latency_ms": round(latency_ms, 3),
        "cost_usd": round(cost_usd, 6),
    }
    if error:
        record["error"] = error
    for key in ("db_id", "record_id", "agent", "stage"):
        if key in ctx and ctx[key] is not None:
            record[key] = ctx[key]

    append_jsonl(path, record)
