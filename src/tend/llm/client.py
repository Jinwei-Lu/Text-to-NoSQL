"""OpenAI-compatible async LLM client with transcripts, retries, and anomaly typing.

Responsibilities (and *only* these — agent semantics live in tend/agents):
  1. Send chat completions to the configured provider (DeepSeek by default).
  2. Persist full structured call diagnostics (every attempt: messages, raw response,
     usage, timing) as session-local JSON sidecars when an agent session is bound,
     and otherwise to stage-local ``<stage>/llm/<agent>_<call_id>.diagnostics.json``
     sidecars. DynaDB-style markdown call logs are the default human-readable view;
     diagnostics JSON remains the machine-readable sidecar.
  3. Classify every failure into a typed LLMError with an :class:`Anomaly` kind.
  4. Retry transport faults (rate-limit/timeout/empty/truncated) at a fixed interval —
     forever by default, since only the provider can recover — and run a bounded
     JSON/schema *repair* loop (feed the validation error back to the model).

Stub mode (``settings.stub``): no network; a registered ``stub_fn`` returns canned output
so the whole pipeline is exercisable offline and deterministically in tests.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
import traceback
from contextlib import nullcontext
from dataclasses import asdict, dataclass, is_dataclass
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse
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
from .types import Message, ToolChoice, ToolLLMResult, ToolSchema, parse_tool_calls

if TYPE_CHECKING:
    from ..utils.logging import TaskLogger

StubFn = Callable[[str, list[Message], dict | None], "str | dict[str, Any]"]

_REFUSAL_MARKERS = (
    "i cannot help", "i can't help", "i cannot assist", "i'm unable to",
    "i am unable to", "i won't", "i will not", "as an ai",
)
_ALLOWED_ROLES = {"system", "user", "assistant", "tool", "developer"}
_ALLOWED_MESSAGE_KEYS = {
    "role",
    "content",
    "name",
    "tool_call_id",
    "tool_calls",
    "reasoning_content",
}
_DEEPSEEK_OPENAI_HOST = "api.deepseek.com"
_DEEPSEEK_TOOL_CHOICE_DISABLED_REASON = (
    "deepseek OpenAI-format thinking mode does not support tool_choice"
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
    transcript_ref: str                         # primary call artifact ref under run dir
    diagnostics_ref: str = ""                   # structured sidecar ref under run dir

    def __post_init__(self) -> None:
        if not self.diagnostics_ref:
            self.diagnostics_ref = _diagnostics_ref_from_transcript(self.transcript_ref)

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


def _llm_diagnostics_ref(
    log: RunLogger,
    agent: str,
    call_id: str,
    *,
    transcript_ref: str | None = None,
) -> str:
    ref_for_call = getattr(log, "llm_diagnostics_ref", None)
    if callable(ref_for_call):
        return str(ref_for_call(agent, call_id, transcript_ref=transcript_ref))
    return f"{agent}/llm/{agent}_{call_id}.diagnostics.json"


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
    (configurable via ``settings.llm.max_concurrency``; ``<= 0`` — the default —
    runs unbounded), making this the one canonical chokepoint for live LLM
    throughput. Provider/transport faults are retried at ``retry_interval_s``,
    forever when ``max_retries < 0`` (the default), so a flaky provider stalls
    rather than fails the run.
    """

    def __init__(self, settings: Settings, logger: RunLogger) -> None:
        self._s = settings
        self._log = logger
        self._stub_fn: StubFn | None = None
        self._client: Any = None
        self._tool_choice_unsupported_models: set[str] = set()
        self.on_usage: Callable[..., None] | None = None
        self.on_retry: Callable[..., None] | None = None
        self.on_provider_wait: Callable[..., None] | None = None
        self.on_provider_ok: Callable[[], None] | None = None
        self._progress_callback_failures_seen: set[str] = set()
        self._sem = (
            asyncio.Semaphore(settings.llm.max_concurrency)
            if settings.llm.max_concurrency > 0
            else None
        )
        if not settings.stub:
            # imported lazily so stub/test runs need no network stack configured
            import httpx
            from openai import AsyncOpenAI

            # httpx's default pool (max_connections=100) silently caps every process at
            # ~100 in-flight calls and fires spurious connection/timeout errors once the
            # pool queue outwaits the first-token watchdog — the historical "~150
            # concurrency ceiling" was THIS, not the provider. The semaphore
            # (max_concurrency) is the intended limiter, so the pool must never bind:
            # size it above the semaphore, or unlimited when concurrency is unbounded.
            max_conc = settings.llm.max_concurrency
            pool_limit = None if max_conc <= 0 else max(256, max_conc + 32)
            # The first-token watchdog only guards the stream AFTER response headers
            # arrive; TCP/TLS connect is governed solely by the httpx connect timeout.
            # Under multi-process launch bursts the provider rate-limits new
            # connections per IP (first RSTs, then silently dropped SYNs), and a
            # connect timeout equal to timeout_s turns every hung handshake into a
            # 30-minute dead cycle (observed 2026-06-11: stuck runs held 3 TCP conns
            # for 110 in-flight calls). Bound connect by the first-token window so a
            # hung handshake fails fast into the same retry loop; read/write/pool stay
            # at timeout_s (streaming stalls are the inter-token watchdog's job).
            ft = settings.llm.first_token_timeout_s
            connect_timeout_s = ft if ft and ft > 0 else settings.llm.timeout_s
            request_timeout = httpx.Timeout(
                settings.llm.timeout_s, connect=connect_timeout_s
            )
            http_client = httpx.AsyncClient(
                # HTTP/2: multiplex every stream over a handful of long-lived
                # connections. The provider LB rate-limits NEW TCP connections per
                # IP (token bucket), so HTTP/1.1's one-connection-per-call at high
                # concurrency starves itself into a self-sustaining retry storm
                # (observed 2026-06-12: 14 connects/s per proc → all timeout →
                # retry forever, while a single fresh probe call was admitted in
                # 1s). Requires the `h2` package; verified the endpoint negotiates
                # HTTP/2 for streaming chat completions.
                http2=True,
                limits=httpx.Limits(
                    max_connections=pool_limit,
                    max_keepalive_connections=min(200, pool_limit or 200),
                ),
                timeout=request_timeout,
            )
            self._client = AsyncOpenAI(
                base_url=settings.llm.base_url,
                api_key=settings.llm.api_key,
                # granular httpx.Timeout, NOT a float: the openai client forwards its
                # per-request timeout to httpx, which would override the transport's
                # connect timeout with timeout_s again.
                timeout=request_timeout,
                max_retries=0,  # we own the retry policy (for typed anomalies + transcripts)
                http_client=http_client,
            )

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the owned provider transport, if this client opened one."""
        client = self._client
        if client is None:
            return
        close = getattr(client, "close", None)
        aclose = getattr(client, "aclose", None)
        if callable(aclose):
            await aclose()
        elif callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result
        http_client = getattr(client, "_client", None) or getattr(client, "http_client", None)
        if http_client is not client:
            http_aclose = getattr(http_client, "aclose", None)
            if callable(http_aclose):
                await http_aclose()
        self._client = None

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
        task_logger: "TaskLogger | None" = None,
        schema: dict | None = None,
        expect_json: bool | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        thinking: str | None = None,
        stream: bool | None = None,
        first_token_timeout_s: float | None = None,
        omit_max_tokens: bool = False,
        json_repair_retries: int = 2,
    ) -> LLMResult:
        """Run one logical completion, returning a validated :class:`LLMResult`.

        Raises a typed :class:`~tend.errors.LLMError` (already logged as an anomaly with a
        ``transcript_ref``) if the call cannot be completed within the retry budgets.

        When ``task_logger`` is given, the call is logged DynaDB-style through the
        TaskLogger protocol (``llm/<call_id>.md`` + ``cost_summary.jsonl``) and the
        legacy RunLogger transcript path is fully bypassed.
        """
        log = (logger or self._log).bind(agent=agent)
        call_id = task_logger.new_llm_call_id() if task_logger is not None else uuid4().hex[:12]
        model = model or self._s.llm.model_for(agent)
        temperature = self._s.llm.temperature if temperature is None else temperature
        max_tokens = None if omit_max_tokens else (max_tokens or self._s.llm.max_tokens)
        reasoning_effort = reasoning_effort or self._s.llm.reasoning_effort
        thinking = thinking or self._s.llm.thinking
        stream = self._s.llm.stream if stream is None else stream
        first_token_timeout_s = (
            self._s.llm.first_token_timeout_s
            if first_token_timeout_s is None
            else first_token_timeout_s
        )
        expect_json = (schema is not None) if expect_json is None else expect_json

        convo = list(messages)
        attempts: list[dict[str, Any]] = []
        t0 = time.monotonic()
        provider_kwargs = self._provider_request_options(
            response_format=response_format,
            reasoning_effort=reasoning_effort,
            thinking=thinking,
        )
        request_config = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "expect_json": expect_json,
            "schema": schema,
            "json_repair_retries": json_repair_retries,
            "provider_kwargs": provider_kwargs,
            "stream": stream,
            "first_token_timeout_s": first_token_timeout_s,
        }
        try:
            if task_logger is not None:
                start_ref = ""
                start_diagnostics_ref = ""
                task_logger.log_llm_request(
                    call_id,
                    model=model,
                    messages=convo,
                    tools=None,
                    temperature=temperature,
                    response_format=response_format,
                    agent=agent,
                    expect_json=expect_json,
                    stream=stream,
                )
            else:
                prompt_chars = sum(
                    len(str(m.get("content", ""))) if isinstance(m, dict) else 0
                    for m in convo
                )
                start_ref = log.save_transcript(agent, call_id, {
                    "model": model,
                    **request_config,
                    "messages": convo,
                    "attempts": attempts,
                    "started": True,
                })
                start_diagnostics_ref = _llm_diagnostics_ref(
                    log, agent, call_id, transcript_ref=start_ref
                )
                log.info("llm_call_start", agent=agent, call_id=call_id, model=model,
                         message_count=len(convo),
                         prompt_chars=prompt_chars,
                         transcript_ref=start_ref, diagnostics_ref=start_diagnostics_ref)
            # prompt validation is inside the try so prompt anomalies are captured too
            self._validate_prompt(messages, agent, call_id)
            for repair in range(json_repair_retries + 1):
                text, finish, usage = await self._send_with_transport_retries(
                    agent,
                    call_id,
                    model,
                    convo,
                    temperature,
                    max_tokens,
                    provider_kwargs,
                    stream,
                    first_token_timeout_s,
                    attempts,
                    log,
                    transcript_ref=start_ref,
                    diagnostics_ref=start_diagnostics_ref,
                    task_logger=task_logger,
                )
                if not expect_json:
                    return self._finish(agent, call_id, model, text, None, finish, usage,
                                        t0, attempts, log, messages=convo,
                                        request_config=request_config,
                                        task_logger=task_logger)
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
                                        t0, attempts, log, messages=convo,
                                        request_config=request_config,
                                        task_logger=task_logger)
                except (ResponseParseError, SchemaValidationError) as verr:
                    attempts[-1]["validation_error"] = verr.to_record()
                    if repair >= json_repair_retries:
                        raise
                    convo = convo + [
                        {"role": "assistant", "content": text},
                        {"role": "user", "content": self._repair_prompt(verr, schema)},
                    ]
                    if task_logger is not None:
                        task_logger.warning("llm_repair_retry", agent=agent, call_id=call_id,
                                            attempt=repair + 1, reason=verr.anomaly.value)
                    else:
                        log.warning("llm_repair_retry", agent=agent, call_id=call_id,
                                    attempt=repair + 1, reason=verr.anomaly.value,
                                    transcript_ref=start_ref,
                                    diagnostics_ref=start_diagnostics_ref)
            raise LLMError("exhausted repair retries", context={"agent": agent})  # unreachable
        except LLMError as err:
            if task_logger is not None:
                ref = self._task_logger_log_error(task_logger, call_id, model)
                diagnostics_ref = ref
            else:
                ref = log.save_transcript(agent, call_id, {
                    "model": model, **request_config, "messages": convo, "attempts": attempts,
                    "failed": True, "error": err.to_record(),
                })
                diagnostics_ref = _llm_diagnostics_ref(log, agent, call_id, transcript_ref=ref)
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
            if task_logger is not None:
                ref = self._task_logger_log_error(task_logger, call_id, model)
            else:
                ref = log.save_transcript(agent, call_id, {
                    "model": model,
                    **request_config,
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
                    "diagnostics_ref": _llm_diagnostics_ref(
                        log, agent, call_id, transcript_ref=ref
                    ),
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "traceback": tb,
                },
            )
            log.anomaly(
                err,
                transcript_ref=ref,
                diagnostics_ref=_llm_diagnostics_ref(log, agent, call_id, transcript_ref=ref),
                call_id=call_id,
            )
            raise err from exc

    # ------------------------------------------------------------------ #
    async def complete_with_tools(
        self,
        *,
        agent: str,
        messages: list[Message],
        tools: list[ToolSchema],
        logger: RunLogger | None = None,
        tool_choice: ToolChoice | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool | None = None,
        first_token_timeout_s: float | None = None,
    ) -> ToolLLMResult:
        """Run one provider-native tool-call completion.

        This is intentionally separate from :meth:`complete`: JSON parsing/schema repair
        remains the structured-output contract, while SMART-EG can use native provider
        tool calls with the same transcript, retry, and anomaly behavior.
        """
        log = (logger or self._log).bind(agent=agent)
        call_id = uuid4().hex[:12]
        model = model or self._s.llm.model_for(agent)
        temperature = self._s.llm.temperature if temperature is None else temperature
        max_tokens = max_tokens or self._s.llm.max_tokens
        stream = self._s.llm.stream if stream is None else stream
        first_token_timeout_s = (
            self._s.llm.first_token_timeout_s
            if first_token_timeout_s is None
            else first_token_timeout_s
        )
        provider_kwargs = self._tool_provider_request_options(model)
        requested_tool_choice = tool_choice
        tool_choice_disabled_for_model = False
        tool_choice_disabled_reason = None
        if tool_choice is not None:
            tool_choice_disabled_reason = self._tool_choice_disabled_reason(model)
            if tool_choice_disabled_reason:
                tool_choice = None
                tool_choice_disabled_for_model = True

        convo = list(messages)
        attempts: list[dict[str, Any]] = []
        t0 = time.monotonic()
        request_config = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "tools": tools,
            "tool_choice": tool_choice,
            "requested_tool_choice": requested_tool_choice,
            "tool_choice_disabled_for_model": tool_choice_disabled_for_model,
            "tool_choice_disabled_reason": tool_choice_disabled_reason,
            "provider_kwargs": provider_kwargs,
            "stream": stream,
            "first_token_timeout_s": first_token_timeout_s,
        }
        try:
            prompt_chars = sum(
                len(str(m.get("content", ""))) if isinstance(m, dict) else 0
                for m in convo
            )
            start_ref = log.save_transcript(agent, call_id, {
                "model": model,
                **request_config,
                "messages": convo,
                "attempts": attempts,
                "started": True,
            })
            start_diagnostics_ref = _llm_diagnostics_ref(
                log, agent, call_id, transcript_ref=start_ref
            )
            log.info(
                "llm_call_start",
                agent=agent,
                call_id=call_id,
                model=model,
                message_count=len(convo),
                prompt_chars=prompt_chars,
                tools_count=len(tools),
                stream=stream,
                first_token_timeout_s=first_token_timeout_s,
                transcript_ref=start_ref,
                diagnostics_ref=start_diagnostics_ref,
            )
            self._validate_prompt(convo, agent, call_id)
            self._validate_tools(tools, tool_choice, agent, call_id)
            text, finish, usage, raw, tool_calls, fallback = await self._send_tools_with_retries(
                agent,
                call_id,
                model,
                convo,
                temperature,
                max_tokens,
                tools,
                tool_choice,
                provider_kwargs,
                stream,
                first_token_timeout_s,
                attempts,
                log,
                transcript_ref=start_ref,
                diagnostics_ref=start_diagnostics_ref,
            )
            return self._finish_tools(
                agent,
                call_id,
                model,
                text,
                finish,
                usage,
                raw,
                tool_calls,
                t0,
                attempts,
                log,
                messages=convo,
                request_config=request_config,
                tool_choice_fallback=fallback,
            )
        except LLMError as err:
            ref = log.save_transcript(agent, call_id, {
                "model": model,
                **request_config,
                "messages": convo,
                "attempts": attempts,
                "failed": True,
                "error": err.to_record(),
            })
            diagnostics_ref = _llm_diagnostics_ref(log, agent, call_id, transcript_ref=ref)
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
                **request_config,
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
                f"unexpected LLM tool client error: {type(exc).__name__}: {exc}",
                anomaly=Anomaly.INTERNAL,
                context={
                    "agent": agent,
                    "call_id": call_id,
                    "model": model,
                    "transcript_ref": ref,
                    "diagnostics_ref": _llm_diagnostics_ref(
                        log, agent, call_id, transcript_ref=ref
                    ),
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "traceback": tb,
                },
            )
            log.anomaly(
                err,
                transcript_ref=ref,
                diagnostics_ref=_llm_diagnostics_ref(log, agent, call_id, transcript_ref=ref),
                call_id=call_id,
            )
            raise err from exc

    # ------------------------------------------------------------------ #
    def _validate_prompt(self, messages: list[Message], agent: str, call_id: str) -> None:
        if not messages:
            raise PromptAnomalyError("empty message list", context={"agent": agent})
        for i, m in enumerate(messages):
            if not isinstance(m, dict):
                raise PromptAnomalyError(
                    "message is not an object",
                    context={"agent": agent, "call_id": call_id, "index": i,
                             "message_type": type(m).__name__},
                )
            role = m.get("role")
            allows_empty_assistant_content = role == "assistant" and bool(m.get("tool_calls"))
            if "role" not in m or ("content" not in m and not allows_empty_assistant_content):
                raise PromptAnomalyError(
                    "message missing role/content",
                    context={"agent": agent, "index": i, "keys": list(m)},
                )
            if role not in _ALLOWED_ROLES:
                raise PromptAnomalyError(
                    "message role is not supported",
                    context={
                        "agent": agent,
                        "call_id": call_id,
                        "index": i,
                        "role": role,
                        "allowed_roles": sorted(_ALLOWED_ROLES),
                    },
                )
            extra_keys = sorted(set(m) - _ALLOWED_MESSAGE_KEYS)
            if extra_keys:
                raise PromptAnomalyError(
                    "message contains unsupported fields",
                    context={
                        "agent": agent,
                        "call_id": call_id,
                        "index": i,
                        "keys": list(m),
                        "unsupported_keys": extra_keys,
                    },
                )
            content = m.get("content", "")
            if (
                (not isinstance(content, str) or not content.strip())
                and not allows_empty_assistant_content
            ):
                raise PromptAnomalyError(
                    "message content empty or non-string",
                    context={
                        "agent": agent,
                        "call_id": call_id,
                        "index": i,
                        "role": m.get("role"),
                    },
                )
        self._validate_tool_message_pairs(messages, agent, call_id)

    def _validate_tools(
        self,
        tools: list[ToolSchema],
        tool_choice: ToolChoice | None,
        agent: str,
        call_id: str,
    ) -> None:
        if not isinstance(tools, list) or not tools:
            raise PromptAnomalyError(
                "tools must be a non-empty list",
                context={"agent": agent, "call_id": call_id, "tools_type": type(tools).__name__},
            )
        names: set[str] = set()
        for i, tool in enumerate(tools):
            if not isinstance(tool, dict) or tool.get("type") != "function":
                raise PromptAnomalyError(
                    "tool schema must be an OpenAI function tool",
                    context={"agent": agent, "call_id": call_id, "index": i, "tool": tool},
                )
            function = tool.get("function")
            if not isinstance(function, dict):
                raise PromptAnomalyError(
                    "tool schema missing function object",
                    context={"agent": agent, "call_id": call_id, "index": i},
                )
            name = function.get("name")
            parameters = function.get("parameters")
            if not isinstance(name, str) or not name.strip():
                raise PromptAnomalyError(
                    "tool function missing name",
                    context={"agent": agent, "call_id": call_id, "index": i},
                )
            if parameters is not None and not isinstance(parameters, dict):
                raise PromptAnomalyError(
                    "tool function parameters must be a JSON schema object",
                    context={"agent": agent, "call_id": call_id, "index": i, "name": name},
                )
            names.add(name)
        if tool_choice is None or isinstance(tool_choice, str):
            return
        if not isinstance(tool_choice, dict):
            raise PromptAnomalyError(
                "tool_choice must be a string or OpenAI tool choice object",
                context={
                    "agent": agent,
                    "call_id": call_id,
                    "tool_choice_type": type(tool_choice).__name__,
                },
            )
        choice_function = tool_choice.get("function")
        choice_name = choice_function.get("name") if isinstance(choice_function, dict) else None
        if tool_choice.get("type") != "function" or not isinstance(choice_name, str):
            raise PromptAnomalyError(
                "tool_choice must name a function tool",
                context={"agent": agent, "call_id": call_id, "tool_choice": tool_choice},
            )
        if choice_name not in names:
            raise PromptAnomalyError(
                "tool_choice names an unknown tool",
                context={
                    "agent": agent,
                    "call_id": call_id,
                    "tool_choice": tool_choice,
                    "available_tools": sorted(names),
                },
            )

    def _validate_tool_message_pairs(
        self,
        messages: list[Message],
        agent: str,
        call_id: str,
    ) -> None:
        pending: set[str] = set()
        for i, message in enumerate(messages):
            role = message.get("role")
            if role == "assistant":
                if pending:
                    raise PromptAnomalyError(
                        "assistant tool_calls missing tool result messages",
                        context={
                            "agent": agent,
                            "call_id": call_id,
                            "index": i,
                            "missing_tool_call_ids": sorted(pending),
                        },
                    )
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    continue
                if not isinstance(tool_calls, list):
                    raise PromptAnomalyError(
                        "assistant tool_calls must be a list",
                        context={"agent": agent, "call_id": call_id, "index": i},
                    )
                pending = set()
                for j, call in enumerate(tool_calls):
                    if not isinstance(call, dict):
                        raise PromptAnomalyError(
                            "assistant tool_call must be an object",
                            context={
                                "agent": agent,
                                "call_id": call_id,
                                "index": i,
                                "tool_call_index": j,
                            },
                        )
                    call_id_value = call.get("id")
                    function = call.get("function")
                    if (
                        not isinstance(call_id_value, str)
                        or not call_id_value.strip()
                        or call.get("type") != "function"
                        or not isinstance(function, dict)
                        or not isinstance(function.get("name"), str)
                        or not isinstance(function.get("arguments"), str)
                    ):
                        raise PromptAnomalyError(
                            "assistant tool_call is not OpenAI-compatible",
                            context={
                                "agent": agent,
                                "call_id": call_id,
                                "index": i,
                                "tool_call_index": j,
                            },
                        )
                    pending.add(call_id_value)
                continue
            if role == "tool":
                tool_call_id = message.get("tool_call_id")
                if not isinstance(tool_call_id, str) or not tool_call_id.strip():
                    raise PromptAnomalyError(
                        "tool result message missing tool_call_id",
                        context={"agent": agent, "call_id": call_id, "index": i},
                    )
                if tool_call_id not in pending:
                    raise PromptAnomalyError(
                        "tool message has no matching assistant tool_call",
                        context={
                            "agent": agent,
                            "call_id": call_id,
                            "index": i,
                            "tool_call_id": tool_call_id,
                            "pending_tool_call_ids": sorted(pending),
                        },
                    )
                pending.remove(tool_call_id)
                continue
            if pending:
                raise PromptAnomalyError(
                    "assistant tool_calls missing tool result messages",
                    context={
                        "agent": agent,
                        "call_id": call_id,
                        "index": i,
                        "missing_tool_call_ids": sorted(pending),
                    },
                )
        if pending:
            raise PromptAnomalyError(
                "assistant tool_calls missing tool result messages",
                context={
                    "agent": agent,
                    "call_id": call_id,
                    "missing_tool_call_ids": sorted(pending),
                },
            )

    async def _send_with_transport_retries(
        self, agent: str, call_id: str, model: str, convo: list[Message],
        temperature: float, max_tokens: int | None, provider_kwargs: dict[str, Any],
        stream: bool, first_token_timeout_s: float,
        attempts: list[dict[str, Any]],
        log: RunLogger,
        *,
        transcript_ref: str,
        diagnostics_ref: str,
        task_logger: "TaskLogger | None" = None,
    ) -> tuple[str, str | None, dict[str, int]]:
        attempt = 0
        while True:
            t0 = time.monotonic()
            try:
                text, finish, usage, raw = await self._raw_call(
                    agent,
                    model,
                    convo,
                    temperature,
                    max_tokens,
                    provider_kwargs,
                    stream,
                    first_token_timeout_s,
                )
                provider_metadata = _provider_metadata(raw, finish)
                attempts.append({
                    "attempt": attempt, "kind": "send", "finish_reason": finish,
                    "usage": usage, "latency_s": round(time.monotonic() - t0, 3),
                    "response": text,
                    "response_preview": text[:500],
                    "provider_kwargs": provider_kwargs,
                    "stream": stream,
                    "first_token_timeout_s": first_token_timeout_s,
                    "provider_metadata": provider_metadata,
                    "raw_response": _json_safe(raw),
                })
                self._check_response(text, finish, agent, provider_metadata=provider_metadata)
                return text, finish, usage
            except LLMError as err:
                attempts.append({
                    "attempt": attempt, "kind": "send_error",
                    "latency_s": round(time.monotonic() - t0, 3),
                    "error": err.to_record(),
                    "stream": stream,
                    "first_token_timeout_s": first_token_timeout_s,
                })
                if not err.retryable or self._retries_exhausted(attempt):
                    raise
                delay = self._provider_retry_delay()
                if task_logger is not None:
                    task_logger.warning("llm_transport_retry", agent=agent, call_id=call_id,
                                        attempt=attempt, anomaly=err.anomaly.value,
                                        delay_s=round(delay, 2))
                else:
                    log.warning("llm_transport_retry", agent=agent, call_id=call_id,
                                attempt=attempt, anomaly=err.anomaly.value,
                                delay_s=round(delay, 2),
                                transcript_ref=transcript_ref,
                                diagnostics_ref=diagnostics_ref)
                self._notify_retry_progress(err, attempt + 1, delay)
                await asyncio.sleep(delay)
                attempt += 1

    async def _send_tools_with_retries(
        self,
        agent: str,
        call_id: str,
        model: str,
        convo: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSchema],
        tool_choice: ToolChoice | None,
        provider_kwargs: dict[str, Any],
        stream: bool,
        first_token_timeout_s: float,
        attempts: list[dict[str, Any]],
        log: RunLogger,
        *,
        transcript_ref: str,
        diagnostics_ref: str,
    ) -> tuple[str, str | None, dict[str, int], Any, list[dict[str, Any]], bool]:
        last: LLMError | None = None
        active_tool_choice = tool_choice
        fallback_used = False
        retries_used = 0
        send_index = 0
        while True:
            t0 = time.monotonic()
            try:
                text, finish, usage, raw, tool_calls = await self._raw_tool_call(
                    agent,
                    model,
                    convo,
                    temperature,
                    max_tokens,
                    tools,
                    active_tool_choice,
                    provider_kwargs,
                    stream,
                    first_token_timeout_s,
                )
                provider_metadata = _provider_metadata(raw, finish)
                attempts.append({
                    "attempt": send_index,
                    "kind": "tool_send",
                    "finish_reason": finish,
                    "usage": usage,
                    "latency_s": round(time.monotonic() - t0, 3),
                    "response": text,
                    "response_preview": text[:500],
                    "tool_calls": tool_calls,
                    "provider_metadata": provider_metadata,
                    "raw_response": _json_safe(raw),
                    "stream": stream,
                    "first_token_timeout_s": first_token_timeout_s,
                    "tool_choice": active_tool_choice,
                })
                self._check_tool_response(
                    text,
                    tool_calls,
                    finish,
                    agent,
                    provider_metadata=provider_metadata,
                )
                return text, finish, usage, raw, tool_calls, fallback_used
            except LLMError as err:
                last = err
                attempts.append({
                    "attempt": send_index,
                    "kind": "tool_send_error",
                    "latency_s": round(time.monotonic() - t0, 3),
                    "error": err.to_record(),
                    "stream": stream,
                    "first_token_timeout_s": first_token_timeout_s,
                    "tool_choice": active_tool_choice,
                })
                if isinstance(err, LLMTimeoutError):
                    timeout_event = (
                        "llm_stream_first_token_timeout"
                        if "first token" in err.message
                        else "llm_stream_inter_token_timeout"
                    )
                    log.warning(
                        timeout_event,
                        agent=agent,
                        call_id=call_id,
                        model=model,
                        attempt=send_index,
                        first_token_timeout_s=first_token_timeout_s,
                        transcript_ref=transcript_ref,
                        diagnostics_ref=diagnostics_ref,
                    )
                if (
                    active_tool_choice is not None
                    and not fallback_used
                    and self._should_fallback_tool_choice(err)
                ):
                    log.warning(
                        "llm_tool_choice_fallback",
                        agent=agent,
                        call_id=call_id,
                        model=model,
                        requested_tool_choice=active_tool_choice,
                        reason=err.message,
                        transcript_ref=transcript_ref,
                        diagnostics_ref=diagnostics_ref,
                    )
                    self._tool_choice_unsupported_models.add(model)
                    active_tool_choice = None
                    fallback_used = True
                    send_index += 1
                    continue
                if not err.retryable or self._retries_exhausted(retries_used):
                    raise
                delay = self._provider_retry_delay()
                log.warning(
                    "llm_transport_retry",
                    agent=agent,
                    call_id=call_id,
                    attempt=send_index,
                    anomaly=err.anomaly.value if err.anomaly else None,
                    delay_s=round(delay, 2),
                    transcript_ref=transcript_ref,
                    diagnostics_ref=diagnostics_ref,
                )
                self._notify_retry_progress(err, retries_used + 1, delay)
                retries_used += 1
                send_index += 1
                await asyncio.sleep(delay)
        assert last is not None
        raise last

    async def _raw_call(
        self, agent: str, model: str, convo: list[Message],
        temperature: float, max_tokens: int | None, provider_kwargs: dict[str, Any],
        stream: bool, first_token_timeout_s: float,
    ) -> tuple[str, str | None, dict[str, int], Any]:
        if self._s.stub:
            return self._stub_call(agent, convo)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": convo,
            "temperature": temperature,
            **provider_kwargs,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if stream:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
        # The semaphore must bound the ENTIRE in-flight call. With stream=True,
        # ``create()`` returns as soon as response HEADERS arrive (<1s), so a semaphore
        # wrapping only the create would release immediately and TEND_LLM_MAX_CONCURRENCY
        # would bound nothing — every queued work item's stream would run at once (the
        # connect-stampede / congestion failure mode observed at scale).
        async with (self._sem or nullcontext()):
            try:
                if stream and first_token_timeout_s > 0:
                    # With stream=True the provider sends response headers on
                    # admission, so create() returning is part of the first-token
                    # contract. Under load the provider also throttles by ACCEPTING
                    # the connection and never answering (observed 2026-06-12:
                    # established conns, zero completions for ~1h, retries cycling
                    # at the 1800s httpx read timeout) — bound the header wait by
                    # the first-token window. Non-stream calls legitimately block
                    # here for the whole generation and stay unbounded.
                    resp = await asyncio.wait_for(
                        self._client.chat.completions.create(**kwargs),
                        timeout=first_token_timeout_s,
                    )
                else:
                    resp = await self._client.chat.completions.create(**kwargs)
            except asyncio.TimeoutError as exc:
                raise LLMTimeoutError(
                    "provider response headers timeout",
                    context={"first_token_timeout_s": first_token_timeout_s},
                ) from exc
            except Exception as exc:  # noqa: BLE001 - mapped to typed anomalies below
                raise self._map_provider_error(exc) from exc
            if stream:
                try:
                    return await self._collect_completion_stream(resp, first_token_timeout_s)
                except LLMError:
                    raise
                except Exception as exc:  # noqa: BLE001 - streaming iterator faults are provider faults
                    raise self._map_provider_error(exc) from exc
                finally:
                    # Always release the SSE stream — an abandoned (timed-out) stream
                    # keeps its pooled connection checked out and the provider keeps
                    # GENERATING (and billing) into a socket nobody reads; under
                    # retry-until-success that snowballs into self-inflicted load.
                    await self._close_stream(resp)
        choice = resp.choices[0]
        text = choice.message.content or ""
        usage = {
            "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0),
            "completion_tokens": getattr(resp.usage, "completion_tokens", 0),
            "total_tokens": getattr(resp.usage, "total_tokens", 0),
        } if resp.usage else {}
        # reasoning models (deepseek-v4-flash) return chain-of-thought separately; capture it
        # for the transcript so anomalies can be diagnosed against the model's actual reasoning
        reasoning = getattr(choice.message, "reasoning_content", None) or getattr(
            choice.message, "reasoning", None
        )
        if reasoning:
            usage["reasoning_preview"] = str(reasoning)[:1200]
        return text, choice.finish_reason, usage, resp

    async def _collect_completion_stream(
        self,
        stream_resp: Any,
        first_token_timeout_s: float,
    ) -> tuple[str, str | None, dict[str, int], Any]:
        iterator = stream_resp.__aiter__()
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        chunk_count = 0
        chunk_samples: list[Any] = []
        finish: str | None = None
        usage: dict[str, int] = {}
        first_token_seen = False
        deadline = (
            time.monotonic() + first_token_timeout_s
            if first_token_timeout_s > 0
            else None
        )

        while True:
            try:
                if not first_token_seen and deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    chunk = await asyncio.wait_for(anext(iterator), timeout=remaining)
                elif first_token_timeout_s > 0:
                    # Inter-token stall watchdog (same contract as the tool stream): a
                    # stream that stops producing chunks mid-generation is dead — retry
                    # now instead of hanging on the 900s outer transport guard.
                    chunk = await asyncio.wait_for(
                        anext(iterator), timeout=first_token_timeout_s
                    )
                else:
                    chunk = await anext(iterator)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as exc:
                raise LLMTimeoutError(
                    (
                        "provider stream first token timeout"
                        if not first_token_seen
                        else "provider stream inter-token timeout"
                    ),
                    context={"first_token_timeout_s": first_token_timeout_s},
                ) from exc

            chunk_count += 1
            safe_chunk = _json_safe(chunk)
            if chunk_count <= 3:
                chunk_samples.append(safe_chunk)
            elif len(chunk_samples) < 6:
                chunk_samples.append(safe_chunk)
            else:
                chunk_samples[-3:] = chunk_samples[-2:] + [safe_chunk]
            chunk_usage = self._usage_dict(self._get(chunk, "usage"))
            if chunk_usage:
                usage = chunk_usage
            for choice in self._get(chunk, "choices", []) or []:
                finish = self._get(choice, "finish_reason") or finish
                delta = self._get(choice, "delta", {}) or {}
                # OpenRouter normalizes the reasoning stream to `delta.reasoning`;
                # native DeepSeek uses `delta.reasoning_content`. Accept both, or long
                # reasoning stretches look token-less to the first-token watchdog.
                reasoning = self._get(delta, "reasoning_content") or self._get(delta, "reasoning")
                content = self._get(delta, "content")
                if reasoning:
                    first_token_seen = True
                    reasoning_parts.append(str(reasoning))
                if content:
                    first_token_seen = True
                    text_parts.append(str(content))

        if not first_token_seen:
            raise EmptyResponseError(
                "provider stream ended before first token",
                context={"first_token_timeout_s": first_token_timeout_s},
            )
        if reasoning_parts:
            usage["reasoning_preview"] = "".join(reasoning_parts)[:1200]
        return "".join(text_parts), finish, usage, {
            "stream_chunk_count": chunk_count,
            "stream_chunk_samples": chunk_samples,
        }

    @staticmethod
    def _provider_request_options(
        *,
        response_format: dict[str, Any] | None,
        reasoning_effort: str | None,
        thinking: str | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if response_format is not None:
            kwargs["response_format"] = response_format
        if reasoning_effort:
            kwargs["reasoning_effort"] = str(reasoning_effort)
        if thinking:
            kwargs["extra_body"] = {"thinking": {"type": str(thinking)}}
        return kwargs

    async def _raw_tool_call(
        self,
        agent: str,
        model: str,
        convo: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSchema],
        tool_choice: ToolChoice | None,
        provider_kwargs: dict[str, Any],
        stream: bool,
        first_token_timeout_s: float,
    ) -> tuple[str, str | None, dict[str, int], Any, list[dict[str, Any]]]:
        if self._s.stub:
            return self._stub_tool_call(agent, convo)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": convo,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "tools": tools,
            "stream": stream,
            **provider_kwargs,
        }
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        if stream:
            kwargs["stream_options"] = {"include_usage": True}
        # Same contract as _raw_call: the semaphore bounds the WHOLE in-flight call
        # (stream collection included), an abandoned stream is always closed, and a
        # streaming create() must produce response headers within the first-token
        # window (accept-then-stall throttling otherwise hangs until the httpx read
        # timeout).
        async with (self._sem or nullcontext()):
            try:
                if stream and first_token_timeout_s > 0:
                    resp = await asyncio.wait_for(
                        self._client.chat.completions.create(**kwargs),
                        timeout=first_token_timeout_s,
                    )
                else:
                    resp = await self._client.chat.completions.create(**kwargs)
            except asyncio.TimeoutError as exc:
                raise LLMTimeoutError(
                    "provider response headers timeout",
                    context={"first_token_timeout_s": first_token_timeout_s},
                ) from exc
            except Exception as exc:  # noqa: BLE001 - mapped to typed anomalies below
                raise self._map_provider_error(exc) from exc
            if stream:
                try:
                    return await self._collect_tool_stream(resp, first_token_timeout_s)
                except LLMError:
                    raise
                except Exception as exc:  # noqa: BLE001 - streaming iterator faults are provider faults
                    raise self._map_provider_error(exc) from exc
                finally:
                    await self._close_stream(resp)
        choice = resp.choices[0]
        message = choice.message
        text = getattr(message, "content", None) or ""
        usage = self._usage_dict(getattr(resp, "usage", None))
        reasoning = getattr(message, "reasoning_content", None) or getattr(
            message, "reasoning", None
        )
        if reasoning:
            usage["reasoning_content"] = str(reasoning)
            usage["reasoning_preview"] = str(reasoning)[:1200]
        tool_calls = self._normalize_tool_calls(getattr(message, "tool_calls", None))
        return text, choice.finish_reason, usage, resp, tool_calls

    async def _collect_tool_stream(
        self,
        stream_resp: Any,
        first_token_timeout_s: float,
    ) -> tuple[str, str | None, dict[str, int], Any, list[dict[str, Any]]]:
        iterator = stream_resp.__aiter__()
        try:
            if first_token_timeout_s > 0:
                first_chunk = await asyncio.wait_for(
                    anext(iterator),
                    timeout=first_token_timeout_s,
                )
            else:
                first_chunk = await anext(iterator)
        except StopAsyncIteration as exc:
            raise EmptyResponseError("provider stream ended before first token") from exc
        except asyncio.TimeoutError as exc:
            raise LLMTimeoutError(
                "provider stream first token timeout",
                context={"first_token_timeout_s": first_token_timeout_s},
            ) from exc

        chunks = [first_chunk]
        while True:
            try:
                if first_token_timeout_s > 0:
                    chunk = await asyncio.wait_for(
                        anext(iterator),
                        timeout=first_token_timeout_s,
                    )
                else:
                    chunk = await anext(iterator)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as exc:
                raise LLMTimeoutError(
                    "provider stream inter-token timeout",
                    context={"first_token_timeout_s": first_token_timeout_s},
                ) from exc
            chunks.append(chunk)
        return self._assemble_tool_stream_chunks(chunks)

    def _assemble_tool_stream_chunks(
        self,
        chunks: list[Any],
    ) -> tuple[str, str | None, dict[str, int], Any, list[dict[str, Any]]]:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish: str | None = None
        usage: dict[str, int] = {}
        tool_calls_by_index: dict[int, dict[str, Any]] = {}
        raw_chunks: list[Any] = []
        for chunk in chunks:
            raw_chunks.append(_json_safe(chunk))
            chunk_usage = self._usage_dict(self._get(chunk, "usage"))
            if chunk_usage:
                usage = chunk_usage
            for choice in self._get(chunk, "choices", []) or []:
                finish = self._get(choice, "finish_reason") or finish
                delta = self._get(choice, "delta", {}) or {}
                # OpenRouter normalizes the reasoning stream to `delta.reasoning`;
                # native DeepSeek uses `delta.reasoning_content`. Accept both, or long
                # reasoning stretches look token-less to the first-token watchdog.
                reasoning = self._get(delta, "reasoning_content") or self._get(delta, "reasoning")
                content = self._get(delta, "content")
                if reasoning:
                    reasoning_parts.append(str(reasoning))
                if content:
                    text_parts.append(str(content))
                for call_delta in self._get(delta, "tool_calls", []) or []:
                    index = self._get(call_delta, "index")
                    if not isinstance(index, int):
                        index = len(tool_calls_by_index)
                    item = tool_calls_by_index.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    call_id = self._get(call_delta, "id")
                    if call_id:
                        item["id"] = str(call_id)
                    call_type = self._get(call_delta, "type")
                    if call_type:
                        item["type"] = str(call_type)
                    function = self._get(call_delta, "function")
                    if function is not None:
                        name = self._get(function, "name")
                        if name:
                            item["function"]["name"] = str(name)
                        arguments = self._get(function, "arguments")
                        if arguments:
                            item["function"]["arguments"] += str(arguments)
        tool_calls = [
            call
            for _index, call in sorted(tool_calls_by_index.items(), key=lambda item: item[0])
            if call.get("id") or call.get("function", {}).get("name")
        ]
        raw = {"stream_chunks": raw_chunks}
        if reasoning_parts:
            reasoning_content = "".join(reasoning_parts)
            usage["reasoning_content"] = reasoning_content
            usage["reasoning_preview"] = reasoning_content[:1200]
        return "".join(text_parts), finish, usage, raw, tool_calls

    def _stub_call(
        self, agent: str, convo: list[Message]
    ) -> tuple[str, str | None, dict[str, int], Any]:
        if self._stub_fn is None:
            payload: str | dict = {"_stub": True, "agent": agent}
        else:
            payload = self._stub_fn(agent, convo, None)
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return text, "stop", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, None

    def _stub_tool_call(
        self,
        agent: str,
        convo: list[Message],
    ) -> tuple[str, str | None, dict[str, int], Any, list[dict[str, Any]]]:
        if self._stub_fn is None:
            payload: str | dict = {"_stub": True, "agent": agent}
        else:
            payload = self._stub_fn(agent, convo, None)
        if isinstance(payload, dict) and "tool_calls" in payload:
            text = str(payload.get("content") or "")
            tool_calls = self._normalize_tool_calls(payload.get("tool_calls"))
            finish = "tool_calls" if tool_calls else "stop"
            return text, finish, {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }, payload, tool_calls
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return text, "stop", {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }, payload, []

    @staticmethod
    async def _close_stream(stream_resp: Any) -> None:
        """Release an SSE stream (finished OR abandoned).

        Without an explicit close, a timed-out/abandoned stream keeps its pooled HTTP
        connection checked out and the provider keeps GENERATING (and billing) into a
        socket nobody reads — under retry-until-success that compounds into
        self-inflicted provider load. Closing a dead stream must never mask the
        original error, so failures here are swallowed.
        """
        close = getattr(stream_resp, "close", None)
        if close is None:
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - best-effort release, never masks the real error
            pass

    @staticmethod
    def _get(value: Any, key: str, default: Any = None) -> Any:
        if isinstance(value, dict):
            return value.get(key, default)
        return getattr(value, key, default)

    @classmethod
    def _usage_dict(cls, usage: Any) -> dict[str, int]:
        if not usage:
            return {}
        out = {
            "prompt_tokens": int(cls._get(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(cls._get(usage, "completion_tokens", 0) or 0),
            "total_tokens": int(cls._get(usage, "total_tokens", 0) or 0),
        }
        return out

    @classmethod
    def _normalize_tool_calls(cls, tool_calls: Any) -> list[dict[str, Any]]:
        safe = _json_safe(tool_calls)
        if safe is None:
            return []
        if not isinstance(safe, list):
            safe = [safe]
        out: list[dict[str, Any]] = []
        for call in safe:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                function = {}
            arguments = function.get("arguments", "")
            if isinstance(arguments, (dict, list)):
                arguments = json.dumps(arguments, ensure_ascii=False)
            normalized = {
                "id": str(call.get("id") or ""),
                "type": str(call.get("type") or "function"),
                "function": {
                    "name": str(function.get("name") or ""),
                    "arguments": str(arguments or ""),
                },
            }
            if normalized["id"] or normalized["function"]["name"]:
                out.append(normalized)
        return out

    @staticmethod
    def _map_provider_error(exc: Exception) -> LLMError:
        name = type(exc).__name__
        msg = str(exc)[:400]
        if "RateLimit" in name:
            return RateLimitError(f"provider rate limit: {msg}")
        if "Timeout" in name or "APIConnection" in name:
            return LLMTimeoutError(f"provider timeout/connection: {msg}", retryable=True)
        # A stream the provider drops mid-body (RemoteProtocolError / incomplete chunked
        # read) is the same transient transport fault as a connect failure: the response
        # never ARRIVED, so the retry-until-arrival policy owns it. Without this, heavy-
        # concurrency stream drops terminally fail k=1 records with pure infra noise.
        if (
            "RemoteProtocol" in name
            or "ChunkedEncoding" in name
            or "IncompleteRead" in name
            # HTTP/2 stream-level faults (server reset/terminated a multiplexed
            # stream): same transient transport class as a dropped HTTP/1.1 body.
            or "EndOfStream" in name
            or "StreamReset" in name
            or "ConnectionTerminated" in name
            or "BrokenResource" in name
            # httpx transport read/write faults mid-request: same transient class.
            or "ReadError" in name
            or "WriteError" in name
            or "ConnectError" in name
            or "incomplete chunked read" in msg
            or "peer closed connection" in msg
        ):
            return LLMTimeoutError(f"provider stream dropped: [{name}] {msg}", retryable=True)
        if "BadRequest" in name and ("context" in msg.lower() or "maximum" in msg.lower()):
            return ContextOverflowError(f"context length exceeded: {msg}")
        status = getattr(exc, "status_code", None)
        # 402 = insufficient balance. Under the retry-until-success policy (max_retries<0)
        # a mid-run balance lapse should PAUSE-and-resume (retry until the account is topped
        # up), not terminally fail records and silently corrupt results. So treat it as a
        # transient fault alongside the 5xx server errors.
        retryable = status in (402, 500, 502, 503, 504) if status else False
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
    def _check_tool_response(
        text: str,
        tool_calls: list[dict[str, Any]],
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
        if not text.strip() and not tool_calls:
            raise EmptyResponseError("model returned empty content and no tool calls",
                                     context={"agent": agent, "finish_reason": finish})

    @staticmethod
    def _should_fallback_tool_choice(err: LLMError) -> bool:
        message = err.message.lower()
        field = str(err.context.get("field") or "").lower()
        return "tool_choice" in message or field == "tool_choice"

    def _tool_choice_disabled_reason(self, model: str) -> str | None:
        if model in self._tool_choice_unsupported_models:
            return "previous provider rejection for this model"
        if self._uses_deepseek_openai_endpoint() and self._deepseek_thinking_enabled(model):
            return _DEEPSEEK_TOOL_CHOICE_DISABLED_REASON
        return None

    def _tool_provider_request_options(self, model: str) -> dict[str, Any]:
        thinking = self._s.llm.thinking
        if (
            thinking is None
            and self._uses_deepseek_openai_endpoint()
            and self._deepseek_thinking_enabled(model)
        ):
            thinking = "enabled"
        return self._provider_request_options(
            response_format=None,
            reasoning_effort=self._s.llm.reasoning_effort,
            thinking=thinking,
        )

    def _uses_deepseek_openai_endpoint(self) -> bool:
        base_url = self._s.llm.base_url.strip()
        parsed = urlparse(base_url if "://" in base_url else f"https://{base_url}")
        host = parsed.hostname or ""
        path = parsed.path.rstrip("/")
        return host.lower() == _DEEPSEEK_OPENAI_HOST and not path.startswith("/anthropic")

    def _deepseek_thinking_enabled(self, model: str) -> bool:
        thinking = self._s.llm.thinking
        if thinking is not None:
            return str(thinking).strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
                "disable",
                "disabled",
                "none",
            }
        model_name = model.strip().lower().rsplit("/", 1)[-1]
        return model_name != "deepseek-chat"

    @staticmethod
    def _task_logger_log_error(task_logger: "TaskLogger", call_id: str, model: str) -> str:
        """Close out a failed TaskLogger-logged call, returning its ``llm/<call_id>.md`` ref."""
        task_logger.log_llm_response(
            call_id,
            response_raw={"model": model},
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            finish_reason="error",
            cost_usd=0.0,
            cost_source="error",
        )
        return str(getattr(task_logger, "last_llm_call_path", None) or "")

    @staticmethod
    def _cost_source(usage: dict[str, int]) -> str:
        token_keys = ("prompt_tokens", "completion_tokens", "total_tokens")
        if any(int(usage.get(key, 0) or 0) > 0 for key in token_keys):
            return "token_usage_only"
        return "unavailable"

    @staticmethod
    def _reasoning_content(usage: dict[str, Any]) -> str:
        reasoning = usage.get("reasoning_content") or usage.get("reasoning_preview")
        return str(reasoning) if reasoning else ""

    def _notify_usage_progress(
        self,
        *,
        call_id: str,
        usage: dict[str, int],
        cost_source: str,
    ) -> None:
        callback = self.on_usage
        if callback is None:
            return
        try:
            callback(
                None,
                int(usage.get("prompt_tokens", 0) or 0),
                int(usage.get("completion_tokens", 0) or 0),
                cost_source,
                call_id,
                0,
                0,
            )
        except Exception as exc:
            self._log_progress_callback_failure(
                "on_usage",
                exc,
                call_id=call_id,
            )

    def _retries_exhausted(self, attempts_done: int) -> bool:
        """Provider-fault retry budget. ``max_retries < 0`` retries forever."""
        max_retries = self._s.llm.max_retries
        return max_retries >= 0 and attempts_done >= max_retries

    def _provider_retry_delay(self) -> float:
        """Fixed wait between provider-fault retries (``retry_interval_s``)."""
        return max(0.0, self._s.llm.retry_interval_s)

    def _notify_retry_progress(
        self,
        err: LLMError,
        attempt: int,
        wait_s: float,
    ) -> None:
        callback = self.on_retry
        if callback is None:
            return
        if isinstance(err, RateLimitError):
            reason = "rate_limit"
        elif isinstance(err, EmptyResponseError):
            reason = "empty_content"
        elif isinstance(err, ResponseParseError):
            reason = "structured_parse"
        elif isinstance(err, LLMTimeoutError):
            reason = "network_transient"
        else:
            reason = "api_transient"
        max_attempts = (
            None if self._s.llm.max_retries < 0 else self._s.llm.max_retries + 1
        )
        try:
            callback(
                reason,
                attempt,
                wait_s,
                type(err).__name__,
                max_attempts,
            )
        except Exception as exc:
            self._log_progress_callback_failure(
                "on_retry",
                exc,
                attempt=attempt,
                reason=reason,
            )

    def _log_progress_callback_failure(
        self,
        callback_name: str,
        exc: Exception,
        **fields: Any,
    ) -> None:
        key = f"{callback_name}:{type(exc).__name__}"
        if key in self._progress_callback_failures_seen:
            return
        self._progress_callback_failures_seen.add(key)
        try:
            self._log.warning(
                "llm_progress_callback_failed",
                callback=callback_name,
                error_type=type(exc).__name__,
                message=str(exc),
                **fields,
            )
        except Exception:
            pass

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
        request_config: dict[str, Any],
        task_logger: "TaskLogger | None" = None,
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
        if task_logger is not None:
            cost_source = self._cost_source(usage)
            message: dict[str, Any] = {"role": "assistant", "content": text}
            reasoning_content = self._reasoning_content(usage)
            if reasoning_content:
                message["reasoning_content"] = reasoning_content
            task_logger.log_llm_response(
                call_id,
                response_raw={
                    "model": model,
                    "choices": [{"message": message, "finish_reason": finish}],
                    "usage": usage,
                    "provider_metadata": provider_metadata,
                },
                usage=usage,
                finish_reason=finish or "unknown",
                cost_usd=0.0,
                cost_source=cost_source,
            )
            self._notify_usage_progress(
                call_id=call_id,
                usage=usage,
                cost_source=cost_source,
            )
            ref = str(getattr(task_logger, "last_llm_call_path", None) or "")
            return LLMResult(
                agent=agent, call_id=call_id, model=model, text=text, parsed=parsed,
                finish_reason=finish, usage=usage, latency_s=latency,
                attempts=len(attempts), transcript_ref=ref, diagnostics_ref=ref,
            )
        ref = log.save_transcript(agent, call_id, {
            "model": model,
            **request_config,
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
        diagnostics_ref = _llm_diagnostics_ref(log, agent, call_id, transcript_ref=ref)
        if self._s.llm.slow_call_warn_s > 0 and latency >= self._s.llm.slow_call_warn_s:
            log.warning(
                "llm_slow_call",
                agent=agent,
                call_id=call_id,
                model=model,
                latency_s=latency,
                threshold_s=self._s.llm.slow_call_warn_s,
                attempts=len(attempts),
                transcript_ref=ref,
                diagnostics_ref=diagnostics_ref,
            )
        log.info("llm_call_ok", agent=agent, call_id=call_id, model=model,
                 attempts=len(attempts), latency_s=latency,
                 total_tokens=usage.get("total_tokens", 0),
                 transcript_ref=ref, diagnostics_ref=diagnostics_ref)
        self._notify_usage_progress(
            call_id=call_id,
            usage=usage,
            cost_source=self._cost_source(usage),
        )
        return LLMResult(
            agent=agent, call_id=call_id, model=model, text=text, parsed=parsed,
            finish_reason=finish, usage=usage, latency_s=latency,
            attempts=len(attempts), transcript_ref=ref, diagnostics_ref=diagnostics_ref,
        )

    def _finish_tools(
        self,
        agent: str,
        call_id: str,
        model: str,
        text: str,
        finish: str | None,
        usage: dict[str, int],
        raw: Any,
        tool_calls: list[dict[str, Any]],
        t0: float,
        attempts: list[dict[str, Any]],
        log: RunLogger,
        *,
        messages: list[Message],
        request_config: dict[str, Any],
        tool_choice_fallback: bool,
    ) -> ToolLLMResult:
        latency = round(time.monotonic() - t0, 3)
        provider_metadata = next(
            (
                item.get("provider_metadata")
                for item in reversed(attempts)
                if item.get("provider_metadata")
            ),
            None,
        )
        cost_source = self._cost_source(usage)
        assistant_message: Message = {"role": "assistant", "content": text}
        reasoning_content = self._reasoning_content(usage)
        if reasoning_content:
            assistant_message["reasoning_content"] = reasoning_content
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        ref = log.save_transcript(agent, call_id, {
            "model": model,
            **request_config,
            "messages": messages,
            "tool_choice_fallback": tool_choice_fallback,
            "attempts": attempts,
            "assistant_message": assistant_message,
            "response_text": text,
            "tool_calls": tool_calls,
            "finish_reason": finish,
            "usage": usage,
            "latency_s": latency,
            "parsed_ok": None,
            "provider_metadata": provider_metadata,
            "raw_response": _json_safe(raw),
            "cost_source": cost_source,
        })
        diagnostics_ref = _llm_diagnostics_ref(log, agent, call_id, transcript_ref=ref)
        if self._s.llm.slow_call_warn_s > 0 and latency >= self._s.llm.slow_call_warn_s:
            log.warning(
                "llm_slow_call",
                agent=agent,
                call_id=call_id,
                model=model,
                latency_s=latency,
                threshold_s=self._s.llm.slow_call_warn_s,
                attempts=len(attempts),
                transcript_ref=ref,
                diagnostics_ref=diagnostics_ref,
            )
        log.info("llm_call_ok", agent=agent, call_id=call_id, model=model,
                 attempts=len(attempts), latency_s=latency,
                 total_tokens=usage.get("total_tokens", 0),
                 tool_calls=len(tool_calls),
                 cost_source=cost_source,
                 transcript_ref=ref, diagnostics_ref=diagnostics_ref)
        self._notify_usage_progress(
            call_id=call_id,
            usage=usage,
            cost_source=cost_source,
        )
        parsed_tool_calls = parse_tool_calls(tool_calls)
        cost = {
            "source": cost_source,
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
        return ToolLLMResult(
            agent=agent,
            call_id=call_id,
            model=model,
            assistant_message=assistant_message,
            tool_calls=parsed_tool_calls,
            cost=cost,
            text=text,
            finish_reason=finish,
            usage=usage,
            latency_s=latency,
            attempts=len(attempts),
            transcript_ref=ref,
            diagnostics_ref=diagnostics_ref,
            provider_metadata=provider_metadata or {},
            tool_choice_fallback=tool_choice_fallback,
        )
