"""OpenAI-compatible async LLM client with transcripts, retries, and anomaly typing.

Responsibilities (and *only* these — agent semantics live in tend/agents):
  1. Send chat completions to the configured provider (DeepSeek by default).
  2. Persist a full transcript (every attempt: messages, raw response, usage, timing)
     as ``llm/<agent>/<call_id>.md`` plus ``.diagnostics.json`` so any anomaly
     points at a readable prompt/response file with full structured detail nearby.
  3. Classify every failure into a typed LLMError with an :class:`Anomaly` kind.
  4. Retry transport faults (rate-limit/timeout/empty/truncated) with backoff, and run a
     bounded JSON/schema *repair* loop (feed the validation error back to the model).

Stub mode (``settings.stub``): no network; a registered ``stub_fn`` returns canned output
so the whole pipeline is exercisable offline and deterministically in tests.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
import traceback
from contextlib import nullcontext
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Callable
from uuid import uuid4

import json5
from jsonschema import Draft202012Validator

from ..config import Settings
from ..errors import (
    Anomaly,
    ContextOverflowError,
    EmptyResponseError,
    LLMError,
    LLMTimeoutError,
    PromptAnomalyError,
    RateLimitError,
    RefusalError,
    ResponseParseError,
    SchemaValidationError,
    TruncatedResponseError,
)
from ..observability import RunLogger

Message = dict[str, str]                       # {"role": ..., "content": ...}
StubFn = Callable[[str, list[Message], dict | None], "str | dict[str, Any]"]

_REFUSAL_MARKERS = (
    "i cannot help", "i can't help", "i cannot assist", "i'm unable to",
    "i am unable to", "i won't", "i will not", "as an ai",
)


@dataclass
class LLMResult:
    """A completed, validated model call."""

    agent: str
    call_id: str
    model: str
    text: str
    parsed: Any | None                          # parsed JSON when expect_json/schema given
    finish_reason: str | None
    usage: dict[str, int]
    latency_s: float
    attempts: int
    transcript_ref: str                         # rel path under the run dir

    @property
    def diagnostics_ref(self) -> str:
        """Structured sidecar path for the transcript, relative to the run dir."""
        return _diagnostics_ref_from_transcript(self.transcript_ref)

    @property
    def data(self) -> dict[str, Any]:
        if not isinstance(self.parsed, dict):
            raise SchemaValidationError(
                "expected a JSON object result", context={"got_type": type(self.parsed).__name__}
            )
        return self.parsed


def _diagnostics_ref_from_transcript(transcript_ref: str) -> str:
    if transcript_ref.endswith(".md"):
        return f"{transcript_ref[:-3]}.diagnostics.json"
    return transcript_ref


def _strip_code_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s
        if s.endswith("```"):
            s = s[: -3]
        # drop a leading ``json`` language tag if it survived
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    return s.strip()


def _extract_json(text: str) -> Any:
    """Best-effort parse: strict JSON, then code-fence strip, then json5 (lenient)."""
    candidates = [text, _strip_code_fence(text)]
    for cand in candidates:
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
    # last resort: locate the outermost {...} or [...] and json5-parse it
    for cand in candidates:
        start = min((cand.find(c) for c in "{[" if cand.find(c) >= 0), default=-1)
        if start >= 0:
            end = max(cand.rfind("}"), cand.rfind("]"))
            if end > start:
                try:
                    return json5.loads(cand[start : end + 1])
                except Exception:  # noqa: BLE001 - json5 raises broad ValueError subclasses
                    pass
    raise ResponseParseError("response is not valid JSON", context={"preview": text[:300]})


def _schema_errors(data: Any, schema: dict) -> list[str]:
    validator = Draft202012Validator(schema)
    errs = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    out = []
    for e in errs[:8]:
        loc = "$" + "".join(f"[{p!r}]" for p in e.path)
        out.append(f"{loc}: {e.message}")
    return out


def _json_safe(value: Any, *, max_depth: int = 8) -> Any:
    if max_depth < 0:
        return _safe_repr(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): _json_safe(v, max_depth=max_depth - 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v, max_depth=max_depth - 1) for v in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value), max_depth=max_depth - 1)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe(model_dump(mode="json"), max_depth=max_depth - 1)
        except TypeError:
            return _json_safe(model_dump(), max_depth=max_depth - 1)
        except Exception:  # noqa: BLE001 - fall through to safer object handling
            pass
    if hasattr(value, "__dict__"):
        return {
            str(k): _json_safe(v, max_depth=max_depth - 1)
            for k, v in vars(value).items()
            if not str(k).startswith("_")
        }
    return _safe_repr(value)


def _safe_repr(value: Any, limit: int = 1200) -> str:
    text = repr(value)
    return text if len(text) <= limit else f"{text[:limit - 3]}..."


def _provider_metadata(raw: Any, finish: str | None) -> dict[str, Any]:
    safe = _json_safe(raw)
    metadata: dict[str, Any] = {"finish_reason": finish}
    if isinstance(safe, dict):
        for key, value in safe.items():
            if _is_metadata_scalar(value) or key in {
                "finish_reason",
                "refusal",
                "truncation",
                "incomplete_details",
                "status",
                "error",
            }:
                metadata.setdefault(key, value)
        choices = safe.get("choices")
        if isinstance(choices, list):
            metadata["choices"] = [_choice_metadata(choice) for choice in choices]
    return metadata


def _choice_metadata(choice: Any) -> dict[str, Any]:
    if not isinstance(choice, dict):
        return {"raw": choice}
    out: dict[str, Any] = {}
    for key in (
        "index",
        "finish_reason",
        "stop_reason",
        "truncation",
        "incomplete_details",
        "content_filter_results",
    ):
        if key in choice:
            out[key] = choice[key]
    message = choice.get("message")
    if isinstance(message, dict):
        msg: dict[str, Any] = {}
        for key in ("role", "refusal", "reasoning_content", "annotations"):
            if key in message:
                msg[key] = message[key]
        if "content" in message:
            msg["content_preview"] = str(message.get("content") or "")[:500]
        if msg:
            out["message"] = msg
    return out


def _is_metadata_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _metadata_refusal(provider_metadata: dict[str, Any] | None) -> Any | None:
    if not provider_metadata:
        return None
    refusal = provider_metadata.get("refusal")
    if refusal:
        return refusal
    for choice in provider_metadata.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict) and message.get("refusal"):
            return message["refusal"]
    return None


class LLMClient:
    """Async transcripting LLM client shared across all agents.

    The single provider call is concurrency-limited by a semaphore gate
    (configurable via ``settings.llm.max_concurrency``; ``<= 0`` runs unbounded),
    making this the one canonical chokepoint for live LLM throughput.
    """

    def __init__(self, settings: Settings, logger: RunLogger) -> None:
        self._s = settings
        self._log = logger
        self._stub_fn: StubFn | None = None
        self._client: Any = None
        self._sem = (
            asyncio.Semaphore(settings.llm.max_concurrency)
            if settings.llm.max_concurrency > 0
            else None
        )
        if not settings.stub:
            # imported lazily so stub/test runs need no network stack configured
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                base_url=settings.llm.base_url,
                api_key=settings.llm.api_key,
                timeout=settings.llm.timeout_s,
                max_retries=0,  # we own the retry policy (for typed anomalies + transcripts)
            )

    def set_stub(self, fn: StubFn) -> None:
        """Register the canned-response function used when ``settings.stub`` is True."""
        self._stub_fn = fn

    # ------------------------------------------------------------------ #
    async def complete(
        self,
        *,
        agent: str,
        messages: list[Message],
        logger: RunLogger | None = None,
        schema: dict | None = None,
        expect_json: bool | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_repair_retries: int = 2,
    ) -> LLMResult:
        """Run one logical completion, returning a validated :class:`LLMResult`.

        Raises a typed :class:`~tend.errors.LLMError` (already logged as an anomaly with a
        ``transcript_ref``) if the call cannot be completed within the retry budgets.
        """
        log = (logger or self._log).bind(agent=agent)
        call_id = uuid4().hex[:12]
        model = model or self._s.llm.model_for(agent)
        temperature = self._s.llm.temperature if temperature is None else temperature
        max_tokens = max_tokens or self._s.llm.max_tokens
        expect_json = (schema is not None) if expect_json is None else expect_json

        convo = list(messages)
        attempts: list[dict[str, Any]] = []
        t0 = time.monotonic()
        try:
            start_ref = log.save_transcript(agent, call_id, {
                "model": model,
                "messages": convo,
                "attempts": attempts,
                "started": True,
            })
            start_diagnostics_ref = _diagnostics_ref_from_transcript(start_ref)
            log.info("llm_call_start", agent=agent, call_id=call_id, model=model,
                     message_count=len(convo),
                     prompt_chars=sum(len(str(m.get("content", ""))) for m in convo),
                     transcript_ref=start_ref, diagnostics_ref=start_diagnostics_ref)
            # prompt validation is inside the try so prompt anomalies are captured too
            self._validate_prompt(messages, agent, call_id)
            for repair in range(json_repair_retries + 1):
                text, finish, usage = await self._send_with_transport_retries(
                    agent, call_id, model, convo, temperature, max_tokens, attempts, log
                )
                if not expect_json:
                    return self._finish(agent, call_id, model, text, None, finish, usage,
                                        t0, attempts, log, messages=convo)
                try:
                    parsed = _extract_json(text)
                    if schema is not None:
                        errs = _schema_errors(parsed, schema)
                        if errs:
                            raise SchemaValidationError(
                                "output failed schema validation",
                                context={"violations": errs},
                            )
                    return self._finish(agent, call_id, model, text, parsed, finish, usage,
                                        t0, attempts, log, messages=convo)
                except (ResponseParseError, SchemaValidationError) as verr:
                    attempts[-1]["validation_error"] = verr.to_record()
                    if repair >= json_repair_retries:
                        raise
                    convo = convo + [
                        {"role": "assistant", "content": text},
                        {"role": "user", "content": self._repair_prompt(verr, schema)},
                    ]
                    log.warning("llm_repair_retry", agent=agent, call_id=call_id,
                                attempt=repair + 1, reason=verr.anomaly.value)
            raise LLMError("exhausted repair retries", context={"agent": agent})  # unreachable
        except LLMError as err:
            ref = log.save_transcript(agent, call_id, {
                "model": model, "messages": convo, "attempts": attempts,
                "failed": True, "error": err.to_record(),
            })
            diagnostics_ref = _diagnostics_ref_from_transcript(ref)
            err.with_context(
                agent=agent,
                call_id=call_id,
                model=model,
                transcript_ref=ref,
                diagnostics_ref=diagnostics_ref,
            )
            log.anomaly(
                err,
                transcript_ref=ref,
                diagnostics_ref=diagnostics_ref,
                call_id=call_id,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - preserve prompt context for LLM-layer bugs
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            ref = log.save_transcript(agent, call_id, {
                "model": model,
                "messages": convo,
                "attempts": attempts,
                "failed": True,
                "unexpected_exception": {
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "traceback": tb,
                },
            })
            err = LLMError(
                f"unexpected LLM client error: {type(exc).__name__}: {exc}",
                anomaly=Anomaly.INTERNAL,
                context={
                    "agent": agent,
                    "call_id": call_id,
                    "model": model,
                    "transcript_ref": ref,
                    "diagnostics_ref": _diagnostics_ref_from_transcript(ref),
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "traceback": tb,
                },
            )
            log.anomaly(
                err,
                transcript_ref=ref,
                diagnostics_ref=_diagnostics_ref_from_transcript(ref),
                call_id=call_id,
            )
            raise err from exc

    # ------------------------------------------------------------------ #
    def _validate_prompt(self, messages: list[Message], agent: str, call_id: str) -> None:
        if not messages:
            raise PromptAnomalyError("empty message list", context={"agent": agent})
        for i, m in enumerate(messages):
            if "role" not in m or "content" not in m:
                raise PromptAnomalyError(
                    "message missing role/content",
                    context={"agent": agent, "index": i, "keys": list(m)},
                )
            if not isinstance(m["content"], str) or not m["content"].strip():
                raise PromptAnomalyError(
                    "message content empty or non-string",
                    context={"agent": agent, "index": i, "role": m.get("role")},
                )

    async def _send_with_transport_retries(
        self, agent: str, call_id: str, model: str, convo: list[Message],
        temperature: float, max_tokens: int, attempts: list[dict[str, Any]],
        log: RunLogger,
    ) -> tuple[str, str | None, dict[str, int]]:
        last: LLMError | None = None
        for attempt in range(self._s.llm.max_retries + 1):
            t0 = time.monotonic()
            try:
                text, finish, usage, raw = await self._raw_call(
                    agent, model, convo, temperature, max_tokens
                )
                provider_metadata = _provider_metadata(raw, finish)
                attempts.append({
                    "attempt": attempt, "kind": "send", "finish_reason": finish,
                    "usage": usage, "latency_s": round(time.monotonic() - t0, 3),
                    "response": text,
                    "response_preview": text[:500],
                    "provider_metadata": provider_metadata,
                    "raw_response": _json_safe(raw),
                })
                self._check_response(text, finish, agent, provider_metadata=provider_metadata)
                return text, finish, usage
            except LLMError as err:
                last = err
                attempts.append({
                    "attempt": attempt, "kind": "send_error",
                    "latency_s": round(time.monotonic() - t0, 3),
                    "error": err.to_record(),
                })
                if not err.retryable or attempt >= self._s.llm.max_retries:
                    raise
                delay = min(8.0, 0.5 * (2 ** attempt)) + random.uniform(0, 0.3)
                log.warning("llm_transport_retry", agent=agent, call_id=call_id,
                            attempt=attempt, anomaly=err.anomaly.value,
                            delay_s=round(delay, 2))
                await asyncio.sleep(delay)
        assert last is not None
        raise last

    async def _raw_call(
        self, agent: str, model: str, convo: list[Message],
        temperature: float, max_tokens: int,
    ) -> tuple[str, str | None, dict[str, int], Any]:
        if self._s.stub:
            return self._stub_call(agent, convo)
        async with (self._sem or nullcontext()):
            try:
                resp = await self._client.chat.completions.create(
                    model=model, messages=convo, temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception as exc:  # noqa: BLE001 - mapped to typed anomalies below
                raise self._map_provider_error(exc) from exc
        choice = resp.choices[0]
        text = choice.message.content or ""
        usage = {
            "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0),
            "completion_tokens": getattr(resp.usage, "completion_tokens", 0),
            "total_tokens": getattr(resp.usage, "total_tokens", 0),
        } if resp.usage else {}
        # reasoning models (deepseek-v4-flash) return chain-of-thought separately; capture it
        # for the transcript so anomalies can be diagnosed against the model's actual reasoning
        reasoning = getattr(choice.message, "reasoning_content", None)
        if reasoning:
            usage["reasoning_preview"] = str(reasoning)[:1200]
        return text, choice.finish_reason, usage, resp

    def _stub_call(
        self, agent: str, convo: list[Message]
    ) -> tuple[str, str | None, dict[str, int], Any]:
        if self._stub_fn is None:
            payload: str | dict = {"_stub": True, "agent": agent}
        else:
            payload = self._stub_fn(agent, convo, None)
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return text, "stop", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, None

    @staticmethod
    def _map_provider_error(exc: Exception) -> LLMError:
        name = type(exc).__name__
        msg = str(exc)[:400]
        if "RateLimit" in name:
            return RateLimitError(f"provider rate limit: {msg}")
        if "Timeout" in name or "APIConnection" in name:
            return LLMTimeoutError(f"provider timeout/connection: {msg}", retryable=True)
        if "BadRequest" in name and ("context" in msg.lower() or "maximum" in msg.lower()):
            return ContextOverflowError(f"context length exceeded: {msg}")
        status = getattr(exc, "status_code", None)
        retryable = status in (500, 502, 503, 504) if status else False
        return LLMError(f"provider error [{name}]: {msg}",
                        context={"status_code": status}, retryable=retryable)

    @staticmethod
    def _check_response(
        text: str,
        finish: str | None,
        agent: str,
        *,
        provider_metadata: dict[str, Any] | None = None,
    ) -> None:
        refusal = _metadata_refusal(provider_metadata)
        if refusal:
            raise RefusalError(
                "model returned a refusal",
                context={
                    "agent": agent,
                    "finish_reason": finish,
                    "refusal": _safe_repr(refusal, limit=500),
                },
            )
        if not text.strip():
            raise EmptyResponseError("model returned empty content",
                                     context={"agent": agent, "finish_reason": finish})
        if finish == "length":
            raise TruncatedResponseError("response truncated (finish_reason=length)",
                                         context={
                                             "agent": agent,
                                             "finish_reason": finish,
                                             "truncation": (
                                                 provider_metadata or {}
                                             ).get("truncation"),
                                             "incomplete_details": (
                                                 provider_metadata or {}
                                             ).get("incomplete_details"),
                                         })
        low = text.strip().lower()
        if len(low) < 120 and any(low.startswith(m) for m in _REFUSAL_MARKERS):
            raise RefusalError("response looks like a refusal",
                               context={"agent": agent, "preview": text[:200]})

    @staticmethod
    def _repair_prompt(err: LLMError, schema: dict | None) -> str:
        detail = err.context.get("violations") or [err.message]
        lines = "\n".join(f"  - {d}" for d in detail)
        tail = ""
        if schema is not None and err.anomaly == Anomaly.SCHEMA_INVALID:
            tail = ("\nReturn ONLY a JSON object that conforms to the required schema. "
                    "Do not include prose or code fences.")
        return (f"Your previous reply was rejected:\n{lines}\n"
                f"Fix it and reply again.{tail}")

    def _finish(
        self, agent: str, call_id: str, model: str, text: str, parsed: Any,
        finish: str | None, usage: dict[str, int], t0: float,
        attempts: list[dict[str, Any]], log: RunLogger, *, messages: list[Message],
    ) -> LLMResult:
        latency = round(time.monotonic() - t0, 3)
        provider_metadata = next(
            (
                item.get("provider_metadata")
                for item in reversed(attempts)
                if item.get("provider_metadata")
            ),
            None,
        )
        ref = log.save_transcript(agent, call_id, {
            "model": model,
            "messages": messages,
            "attempts": attempts,
            "response_text": text,
            "parsed": parsed,
            "finish_reason": finish,
            "usage": usage,
            "latency_s": latency,
            "parsed_ok": parsed is not None,
            "provider_metadata": provider_metadata,
        })
        diagnostics_ref = _diagnostics_ref_from_transcript(ref)
        log.info("llm_call_ok", agent=agent, call_id=call_id, model=model,
                 attempts=len(attempts), latency_s=latency,
                 total_tokens=usage.get("total_tokens", 0),
                 transcript_ref=ref, diagnostics_ref=diagnostics_ref)
        return LLMResult(
            agent=agent, call_id=call_id, model=model, text=text, parsed=parsed,
            finish_reason=finish, usage=usage, latency_s=latency,
            attempts=len(attempts), transcript_ref=ref,
        )
