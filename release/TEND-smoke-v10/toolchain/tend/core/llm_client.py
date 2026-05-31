"""Thin OpenAI wrapper with pool routing, cache, cost logging, and transcript logging."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from openai import OpenAI

from tend.config import (
    LLM_CACHE_DIR,
    LLM_DEFAULTS,
    LLM_MAX_CONCURRENCY,
    default_llm_stub,
    load_pool_roster,
    make_client,
)
from tend.core import cost as cost_module
from tend.core import llm_transcript
from tend.core import logging as log_module
from tend.core.sync import cache_lock_for

# Models known to reject non-default temperature (legacy proxy roster).
_MODELS_REQUIRING_DEFAULT_TEMP: set[str] = set()

_API_SEMAPHORE = threading.Semaphore(LLM_MAX_CONCURRENCY)


def _model_forces_default_temperature(model: str) -> bool:
    if model in _MODELS_REQUIRING_DEFAULT_TEMP:
        return True
    lowered = model.lower()
    return lowered.startswith("gpt-5") or lowered.startswith("o1") or lowered.startswith("o3")


@dataclass
class LLMPool:
    name: str
    models: list[str]


def _flatten_panel(panel: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    for models in panel.values():
        out.extend(models)
    return out


def get_pool(name: str) -> LLMPool:
    roster = load_pool_roster()
    if name == "B_panel":
        return LLMPool(name=name, models=_flatten_panel(roster[name]))
    models = roster.get(name, [])
    return LLMPool(name=name, models=list(models))


class LLMClient:
    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        stub: bool | None = None,
        use_cache: bool | None = None,
    ):
        self.stub = default_llm_stub() if stub is None else stub
        self.client = None if self.stub else make_client()
        self.cache_dir = cache_dir or LLM_CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.use_cache = True if use_cache is None else use_cache

    def _cache_key(self, pool: str, prompt: str, seed: int, model: str) -> str:
        digest = hashlib.sha256(f"{pool}|{model}|{seed}|{prompt}".encode()).hexdigest()
        return digest

    def _read_cache(self, key: str) -> dict | str | None:
        path = self.cache_dir / f"{key}.json"
        lock = cache_lock_for(key)
        with lock:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        return None

    def _write_cache(self, key: str, payload: dict | str) -> None:
        if isinstance(payload, dict) and payload.get("stub"):
            return
        path = self.cache_dir / f"{key}.json"
        tmp = path.with_suffix(".json.tmp")
        body = json.dumps(payload, ensure_ascii=False)
        lock = cache_lock_for(key)
        with lock:
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, path)

    def _log_transcript(
        self,
        *,
        pool: str,
        model: str,
        seed: int,
        prompt: str,
        response: dict | str,
        prompt_hash: str,
        cache_hit: bool,
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: float = 0.0,
        cost_usd: float = 0.0,
        temperature: float = 0.0,
        error: str | None = None,
    ) -> None:
        llm_transcript.log_call(
            pool=pool,
            model=model,
            seed=seed,
            prompt=prompt,
            response=response,
            prompt_hash=prompt_hash,
            cache_hit=cache_hit,
            stub=self.stub,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            temperature=temperature,
            error=error,
        )

    def _create_completion(self, kwargs: dict[str, Any]) -> Any:
        assert self.client is not None
        with _API_SEMAPHORE:
            try:
                return self.client.chat.completions.create(**kwargs)
            except Exception as call_exc:  # noqa: BLE001
                msg = str(call_exc).lower()
                if "temperature" in msg and "does not support" in msg and "temperature" in kwargs:
                    kwargs = dict(kwargs)
                    kwargs.pop("temperature", None)
                    model = str(kwargs.get("model", ""))
                    _MODELS_REQUIRING_DEFAULT_TEMP.add(model)
                    return self.client.chat.completions.create(**kwargs)
                if "seed" in msg and ("unsupported" in msg or "unknown" in msg) and "seed" in kwargs:
                    kwargs = dict(kwargs)
                    kwargs.pop("seed", None)
                    return self.client.chat.completions.create(**kwargs)
                raise

    def call(
        self,
        pool: str,
        prompt: str,
        *,
        seed: int,
        temperature: float = 0.0,
        schema: dict | None = None,
        model_override: str | None = None,
        extra: dict | None = None,
    ) -> dict | str:
        llm_pool = get_pool(pool)
        model = model_override or (llm_pool.models[0] if llm_pool.models else "deepseek-v4-flash")
        cache_key = self._cache_key(pool, prompt, seed, model)
        cached = self._read_cache(cache_key) if self.use_cache else None
        if cached is not None:
            if isinstance(cached, dict) and cached.get("stub"):
                cached = None
            else:
                cost_module.record_call(
                    pool=pool,
                    model=model,
                    tokens_in=0,
                    tokens_out=0,
                    latency_ms=0,
                    cost_usd=0.0,
                    cache_hit=True,
                )
                log_module.emit(
                    "llm_call",
                    pool=pool,
                    model=model,
                    prompt_hash=cache_key[:12],
                    cache_hit=True,
                    transcript_file="llm_transcript.jsonl",
                )
                self._log_transcript(
                    pool=pool,
                    model=model,
                    seed=seed,
                    prompt=prompt,
                    response=cached,
                    prompt_hash=cache_key,
                    cache_hit=True,
                    temperature=temperature,
                )
                return cached

        start = time.time()
        if self.stub:
            response_text = json.dumps({"stub": True, "pool": pool, "prompt_len": len(prompt)})
            payload: dict | str = json.loads(response_text)
            self._log_transcript(
                pool=pool,
                model=model,
                seed=seed,
                prompt=prompt,
                response=payload,
                prompt_hash=cache_key,
                cache_hit=False,
                latency_ms=(time.time() - start) * 1000,
                temperature=temperature,
            )
            log_module.emit(
                "llm_call",
                pool=pool,
                model=model,
                prompt_hash=cache_key[:12],
                cache_hit=False,
                stub=True,
                transcript_file="llm_transcript.jsonl",
            )
            return payload

        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "seed": seed,
            }
            effective_temp = temperature if temperature else LLM_DEFAULTS["temperature"]
            if not _model_forces_default_temperature(model):
                kwargs["temperature"] = effective_temp
            if extra:
                kwargs.update(extra)
            completion = self._create_completion(kwargs)
            response_text = completion.choices[0].message.content or ""
            usage = completion.usage
            tokens_in = usage.prompt_tokens if usage else 0
            tokens_out = usage.completion_tokens if usage else 0
            cost_usd = (tokens_in * 0.000001) + (tokens_out * 0.000002)
            latency_ms = (time.time() - start) * 1000
            cost_module.record_call(
                pool=pool,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                cache_hit=False,
            )
            log_module.emit(
                "llm_call",
                pool=pool,
                model=model,
                prompt_hash=cache_key[:12],
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                cache_hit=False,
                transcript_file="llm_transcript.jsonl",
            )
            if schema:
                try:
                    payload = json.loads(response_text)
                except json.JSONDecodeError:
                    payload = {"text": response_text}
            else:
                payload = response_text
            self._log_transcript(
                pool=pool,
                model=model,
                seed=seed,
                prompt=prompt,
                response=payload,
                prompt_hash=cache_key,
                cache_hit=False,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                temperature=temperature,
            )
            self._write_cache(cache_key, payload)
            return payload
        except Exception as exc:  # noqa: BLE001
            self._log_transcript(
                pool=pool,
                model=model,
                seed=seed,
                prompt=prompt,
                response={"error": str(exc)},
                prompt_hash=cache_key,
                cache_hit=False,
                latency_ms=(time.time() - start) * 1000,
                temperature=temperature,
                error=str(exc),
            )
            raise
