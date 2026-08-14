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
import hashlib
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
    "i cannot help",
    "i can't help",
    "i cannot assist",
    "i'm unable to",
    "i am unable to",
    "i won't",
    "i will not",
    "as an ai",
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
    parsed: Any | None  # parsed JSON when expect_json/schema given
    finish_reason: str | None
    usage: dict[str, int]
    latency_s: float
    attempts: int
    transcript_ref: str  # primary call artifact ref under run dir
    diagnostics_ref: str = ""  # structured sidecar ref under run dir

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
            s = s[:-3]
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
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


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
        # OpenRouter emits its opt-in routing receipt as a nested object.  For
        # streaming chat completions it appears only on the terminal chunk, which
        # ``_collect_completion_stream`` retains inside ``stream_chunk_samples``.
        # Preserve it explicitly: the generic scalar-only metadata filter above is
        # intentionally too conservative to retain arbitrary nested response data.
        router_metadata = _openrouter_metadata_from_response(safe)
        if router_metadata is not None:
            metadata["openrouter_metadata"] = router_metadata
        provider_usage = _provider_usage_from_response(safe)
        if provider_usage is not None:
            metadata["provider_usage"] = provider_usage
        for key, value in _stream_response_scalars(safe).items():
            metadata.setdefault(key, value)
    return metadata


def _openrouter_metadata_from_response(safe: dict[str, Any]) -> dict[str, Any] | None:
    direct = safe.get("openrouter_metadata")
    if isinstance(direct, dict):
        return direct
    samples = safe.get("stream_chunk_samples")
    if not isinstance(samples, list):
        return None
    for sample in reversed(samples):
        if isinstance(sample, dict) and isinstance(sample.get("openrouter_metadata"), dict):
            return sample["openrouter_metadata"]
    return None


def _provider_usage_from_response(safe: dict[str, Any]) -> dict[str, Any] | None:
    direct = safe.get("provider_usage") or safe.get("usage")
    if isinstance(direct, dict):
        return direct
    samples = safe.get("stream_chunk_samples")
    if not isinstance(samples, list):
        return None
    for sample in reversed(samples):
        if isinstance(sample, dict) and isinstance(sample.get("usage"), dict):
            return sample["usage"]
    return None


def _stream_response_scalars(safe: dict[str, Any]) -> dict[str, Any]:
    samples = safe.get("stream_chunk_samples")
    if not isinstance(samples, list):
        return {}
    out: dict[str, Any] = {}
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        for key in ("id", "model", "created", "provider"):
            value = sample.get(key)
            if _is_metadata_scalar(value) and value is not None:
                out[key] = value
    return out


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
            request_timeout = httpx.Timeout(settings.llm.timeout_s, connect=connect_timeout_s)
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
        temperature = (
            None
            if self._s.llm.omit_temperature
            else (self._s.llm.temperature if temperature is None else temperature)
        )
        omit_effective_max_tokens = omit_max_tokens or self._s.llm.omit_max_tokens
        max_tokens = (
            max_tokens or self._s.llm.max_tokens
            if self._s.llm.force_max_tokens or not omit_effective_max_tokens
            else None
        )
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
            "provider_base_url": self._s.llm.base_url.rstrip("/"),
            "provider_base_url_sha256": hashlib.sha256(
                self._s.llm.base_url.rstrip("/").encode("utf-8")
            ).hexdigest(),
            "temperature": temperature,
            "expect_json": expect_json,
            "schema": schema,
            "json_repair_retries": json_repair_retries,
            "provider_kwargs": provider_kwargs,
            "stream": stream,
            "first_token_timeout_s": first_token_timeout_s,
        }
        if max_tokens is not None:
            request_config["max_tokens"] = max_tokens
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
                    len(str(m.get("content", ""))) if isinstance(m, dict) else 0 for m in convo
                )
                start_ref = log.save_transcript(
                    agent,
                    call_id,
                    {
                        "model": model,
                        **request_config,
                        "messages": convo,
                        "attempts": attempts,
                        "started": True,
                    },
                )
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
                    transcript_ref=start_ref,
                    diagnostics_ref=start_diagnostics_ref,
                )
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
                    request_config=request_config,
                    repair_index=repair,
                )
                if not expect_json:
                    if task_logger is not None:
                        self._task_logger_settle_received_attempt(
                            task_logger,
                            agent=agent,
                            call_id=call_id,
                            model=model,
                            repair_index=repair,
                            call_status="success",
                            attempts=attempts,
                            request_config=request_config,
                        )
                    return self._finish(
                        agent,
                        call_id,
                        model,
                        text,
                        None,
                        finish,
                        usage,
                        t0,
                        attempts,
                        log,
                        messages=convo,
                        request_config=request_config,
                        task_logger=task_logger,
                    )
                try:
                    parsed = _extract_json(text)
                    if schema is not None:
                        errs = _schema_errors(parsed, schema)
                        if errs:
                            raise SchemaValidationError(
                                "output failed schema validation",
                                context={"violations": errs},
                            )
                    if task_logger is not None:
                        self._task_logger_settle_received_attempt(
                            task_logger,
                            agent=agent,
                            call_id=call_id,
                            model=model,
                            repair_index=repair,
                            call_status="success",
                            attempts=attempts,
                            request_config=request_config,
                        )
                    return self._finish(
                        agent,
                        call_id,
                        model,
                        text,
                        parsed,
                        finish,
                        usage,
                        t0,
                        attempts,
                        log,
                        messages=convo,
                        request_config=request_config,
                        task_logger=task_logger,
                    )
                except (ResponseParseError, SchemaValidationError) as verr:
                    attempts[-1]["validation_error"] = verr.to_record()
                    will_repair = repair < json_repair_retries
                    if task_logger is not None:
                        self._task_logger_settle_received_attempt(
                            task_logger,
                            agent=agent,
                            call_id=call_id,
                            model=model,
                            repair_index=repair,
                            call_status="retry" if will_repair else "error",
                            attempts=attempts,
                            request_config=request_config,
                            retry_kind="json_repair" if will_repair else None,
                            error=verr,
                            failure_phase="structured_output_validation",
                        )
                    if not will_repair:
                        raise
                    convo = convo + [
                        {"role": "assistant", "content": text},
                        {"role": "user", "content": self._repair_prompt(verr, schema)},
                    ]
                    if task_logger is not None:
                        raw_response = attempts[-1].get("raw_response")
                        task_logger.warning(
                            "llm_repair_retry",
                            agent=agent,
                            call_id=call_id,
                            attempt=repair + 1,
                            reason=verr.anomaly.value,
                            validation_error=verr.to_record(),
                            attempt_diagnostics={
                                "finish_reason": attempts[-1].get("finish_reason"),
                                "usage": attempts[-1].get("usage"),
                                "latency_s": attempts[-1].get("latency_s"),
                                "response_char_count": len(text),
                                "provider_metadata": attempts[-1].get("provider_metadata"),
                                "stream_chunk_count": (
                                    raw_response.get("stream_chunk_count")
                                    if isinstance(raw_response, dict)
                                    else None
                                ),
                                "reasoning_char_count": (
                                    raw_response.get("reasoning_char_count")
                                    if isinstance(raw_response, dict)
                                    else None
                                ),
                                "content_char_count": (
                                    raw_response.get("content_char_count")
                                    if isinstance(raw_response, dict)
                                    else None
                                ),
                            },
                            request_max_tokens=max_tokens,
                        )
                    else:
                        log.warning(
                            "llm_repair_retry",
                            agent=agent,
                            call_id=call_id,
                            attempt=repair + 1,
                            reason=verr.anomaly.value,
                            transcript_ref=start_ref,
                            diagnostics_ref=start_diagnostics_ref,
                        )
            raise LLMError("exhausted repair retries", context={"agent": agent})  # unreachable
        except LLMError as err:
            if task_logger is not None:
                ref = self._task_logger_log_error(
                    task_logger,
                    call_id,
                    model,
                    attempts=attempts,
                    request_config=request_config,
                    error=err,
                )
                diagnostics_ref = ref
            else:
                ref = log.save_transcript(
                    agent,
                    call_id,
                    {
                        "model": model,
                        **request_config,
                        "messages": convo,
                        "attempts": attempts,
                        "failed": True,
                        "error": err.to_record(),
                    },
                )
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
                task_id=(
                    getattr(task_logger, "task_id", None) if task_logger is not None else None
                ),
                stage=(getattr(task_logger, "stage", None) if task_logger is not None else None),
            )
            raise
        except Exception as exc:  # noqa: BLE001 - preserve prompt context for LLM-layer bugs
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            if task_logger is not None:
                ref = self._task_logger_log_error(
                    task_logger,
                    call_id,
                    model,
                    attempts=attempts,
                    request_config=request_config,
                    error=exc,
                )
            else:
                ref = log.save_transcript(
                    agent,
                    call_id,
                    {
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
                    },
                )
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
                task_id=(
                    getattr(task_logger, "task_id", None) if task_logger is not None else None
                ),
                stage=(getattr(task_logger, "stage", None) if task_logger is not None else None),
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
        temperature = (
            None
            if self._s.llm.omit_temperature
            else (self._s.llm.temperature if temperature is None else temperature)
        )
        max_tokens = (
            max_tokens or self._s.llm.max_tokens
            if self._s.llm.force_max_tokens or not self._s.llm.omit_max_tokens
            else None
        )
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
            "provider_base_url": self._s.llm.base_url.rstrip("/"),
            "provider_base_url_sha256": hashlib.sha256(
                self._s.llm.base_url.rstrip("/").encode("utf-8")
            ).hexdigest(),
            "temperature": temperature,
            "tools": tools,
            "tool_choice": tool_choice,
            "requested_tool_choice": requested_tool_choice,
            "tool_choice_disabled_for_model": tool_choice_disabled_for_model,
            "tool_choice_disabled_reason": tool_choice_disabled_reason,
            "provider_kwargs": provider_kwargs,
            "stream": stream,
            "first_token_timeout_s": first_token_timeout_s,
        }
        if max_tokens is not None:
            request_config["max_tokens"] = max_tokens
        try:
            prompt_chars = sum(
                len(str(m.get("content", ""))) if isinstance(m, dict) else 0 for m in convo
            )
            start_ref = log.save_transcript(
                agent,
                call_id,
                {
                    "model": model,
                    **request_config,
                    "messages": convo,
                    "attempts": attempts,
                    "started": True,
                },
            )
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
            ref = log.save_transcript(
                agent,
                call_id,
                {
                    "model": model,
                    **request_config,
                    "messages": convo,
                    "attempts": attempts,
                    "failed": True,
                    "error": err.to_record(),
                },
            )
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
            ref = log.save_transcript(
                agent,
                call_id,
                {
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
                },
            )
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
                    context={
                        "agent": agent,
                        "call_id": call_id,
                        "index": i,
                        "message_type": type(m).__name__,
                    },
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
                not isinstance(content, str) or not content.strip()
            ) and not allows_empty_assistant_content:
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
        self,
        agent: str,
        call_id: str,
        model: str,
        convo: list[Message],
        temperature: float | None,
        max_tokens: int | None,
        provider_kwargs: dict[str, Any],
        stream: bool,
        first_token_timeout_s: float,
        attempts: list[dict[str, Any]],
        log: RunLogger,
        *,
        transcript_ref: str,
        diagnostics_ref: str,
        task_logger: "TaskLogger | None" = None,
        request_config: dict[str, Any],
        repair_index: int,
    ) -> tuple[str, str | None, dict[str, int]]:
        attempt = 0
        # Provider-native truncation gets its own budget, separate from the transport
        # budget: it is not a transient fault and each occurrence has already spent the
        # full completion budget. Counted here so the enclosing json-repair loop cannot
        # re-arm it (a repair round starts a new _send_with_transport_retries call, which
        # is exactly the reset we must not allow to be unbounded).
        truncations_seen = 0
        while True:
            t0 = time.monotonic()
            provider_attempt: dict[str, Any] | None = None
            provider_attempt_index = self._next_provider_attempt_index(attempts)
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
                provider_attempt = {
                    "attempt": attempt,
                    "provider_attempt_index": provider_attempt_index,
                    "repair_index": repair_index,
                    "kind": "send",
                    "finish_reason": finish,
                    "usage": usage,
                    "latency_s": round(time.monotonic() - t0, 3),
                    "response": text,
                    "response_preview": text[:500],
                    "provider_kwargs": provider_kwargs,
                    "stream": stream,
                    "first_token_timeout_s": first_token_timeout_s,
                    "provider_metadata": provider_metadata,
                    "raw_response": _json_safe(raw),
                }
                attempts.append(provider_attempt)
                self._check_response(text, finish, agent, provider_metadata=provider_metadata)
                return text, finish, usage
            except LLMError as err:
                raw_response = (
                    provider_attempt.get("raw_response") if provider_attempt is not None else None
                )
                attempt_diagnostics = {
                    "response_received": provider_attempt is not None,
                    "latency_s": round(time.monotonic() - t0, 3),
                    "finish_reason": (
                        provider_attempt.get("finish_reason")
                        if provider_attempt is not None
                        else None
                    ),
                    "usage": (
                        provider_attempt.get("usage") if provider_attempt is not None else None
                    ),
                    "response_char_count": (
                        len(str(provider_attempt.get("response") or ""))
                        if provider_attempt is not None
                        else 0
                    ),
                    "provider_metadata": (
                        provider_attempt.get("provider_metadata")
                        if provider_attempt is not None
                        else None
                    ),
                    "stream_chunk_count": (
                        raw_response.get("stream_chunk_count")
                        if isinstance(raw_response, dict)
                        else None
                    ),
                    "reasoning_char_count": (
                        raw_response.get("reasoning_char_count")
                        if isinstance(raw_response, dict)
                        else None
                    ),
                    "content_char_count": (
                        raw_response.get("content_char_count")
                        if isinstance(raw_response, dict)
                        else None
                    ),
                    "stream_ended_before_first_token": (
                        raw_response.get("stream_ended_before_first_token")
                        if isinstance(raw_response, dict)
                        else None
                    ),
                    "stream_chunk_samples": (
                        raw_response.get("stream_chunk_samples")
                        if isinstance(raw_response, dict)
                        else None
                    ),
                }
                attempts.append(
                    {
                        "attempt": attempt,
                        "provider_attempt_index": provider_attempt_index,
                        "repair_index": repair_index,
                        "kind": "send_error",
                        "latency_s": round(time.monotonic() - t0, 3),
                        "error": err.to_record(),
                        "attempt_diagnostics": attempt_diagnostics,
                        "stream": stream,
                        "first_token_timeout_s": first_token_timeout_s,
                    }
                )
                will_retry = err.retryable and not self._retries_exhausted(attempt)
                if isinstance(err, TruncatedResponseError):
                    truncations_seen += 1
                    if truncations_seen > max(0, self._s.llm.max_truncation_retries):
                        will_retry = False
                delay = self._provider_retry_delay()
                if task_logger is not None:
                    self._task_logger_log_attempt(
                        task_logger,
                        agent=agent,
                        call_id=call_id,
                        model=model,
                        provider_attempt_index=provider_attempt_index,
                        transport_attempt=attempt + 1,
                        repair_index=repair_index,
                        call_status="retry" if will_retry else "error",
                        provider_attempt=provider_attempt,
                        request_config=request_config,
                        retry_kind="transport" if will_retry else None,
                        error=err,
                        failure_phase="transport_validation",
                    )
                event = "llm_transport_retry" if will_retry else "llm_transport_terminal_failure"
                if task_logger is not None:
                    task_logger.warning(
                        event,
                        agent=agent,
                        call_id=call_id,
                        attempt=attempt,
                        anomaly=err.anomaly.value if err.anomaly else None,
                        error=err.to_record(),
                        attempt_diagnostics=attempt_diagnostics,
                        request_max_tokens=max_tokens,
                        delay_s=round(delay, 2) if will_retry else 0.0,
                    )
                else:
                    log.warning(
                        event,
                        agent=agent,
                        call_id=call_id,
                        attempt=attempt,
                        anomaly=err.anomaly.value if err.anomaly else None,
                        error=err.to_record(),
                        attempt_diagnostics=attempt_diagnostics,
                        request_max_tokens=max_tokens,
                        delay_s=round(delay, 2) if will_retry else 0.0,
                        transcript_ref=transcript_ref,
                        diagnostics_ref=diagnostics_ref,
                    )
                if not will_retry:
                    raise
                self._notify_retry_progress(err, attempt + 1, delay)
                await asyncio.sleep(delay)
                attempt += 1

    async def _send_tools_with_retries(
        self,
        agent: str,
        call_id: str,
        model: str,
        convo: list[Message],
        temperature: float | None,
        max_tokens: int | None,
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
        # Same separate truncation budget as the non-tool path; ReAct drives long
        # multi-step traces through here, so an unbounded truncation retry is the most
        # expensive failure mode in the agentic arm.
        truncations_seen = 0
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
                attempts.append(
                    {
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
                    }
                )
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
                attempts.append(
                    {
                        "attempt": send_index,
                        "kind": "tool_send_error",
                        "latency_s": round(time.monotonic() - t0, 3),
                        "error": err.to_record(),
                        "stream": stream,
                        "first_token_timeout_s": first_token_timeout_s,
                        "tool_choice": active_tool_choice,
                    }
                )
                if isinstance(err, LLMTimeoutError):
                    timeout_phase = str(err.context.get("timeout_phase") or "unknown")
                    timeout_event = {
                        "response_headers": "llm_stream_response_headers_timeout",
                        "first_token": "llm_stream_first_token_timeout",
                        "inter_token": "llm_stream_inter_token_timeout",
                    }.get(timeout_phase, "llm_transport_timeout")
                    log.warning(
                        timeout_event,
                        agent=agent,
                        call_id=call_id,
                        model=model,
                        attempt=send_index,
                        timeout_phase=timeout_phase,
                        first_token_timeout_s=first_token_timeout_s,
                        inter_token_timeout_s=self._s.llm.timeout_s,
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
                if isinstance(err, TruncatedResponseError):
                    truncations_seen += 1
                    if truncations_seen > max(0, self._s.llm.max_truncation_retries):
                        raise
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
        self,
        agent: str,
        model: str,
        convo: list[Message],
        temperature: float | None,
        max_tokens: int | None,
        provider_kwargs: dict[str, Any],
        stream: bool,
        first_token_timeout_s: float,
    ) -> tuple[str, str | None, dict[str, int], Any]:
        if self._s.stub:
            return self._stub_call(agent, convo)
        kwargs: dict[str, Any] = {"model": model, "messages": convo, **provider_kwargs}
        if temperature is not None:
            kwargs["temperature"] = temperature
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
        async with self._sem or nullcontext():
            first_token_deadline = (
                time.monotonic() + first_token_timeout_s
                if stream and first_token_timeout_s > 0
                else None
            )
            try:
                if first_token_deadline is not None:
                    # With stream=True the provider sends response headers on
                    # admission, so create() returning is part of the first-token
                    # contract. Under load the provider also throttles by ACCEPTING
                    # the connection and never answering (observed 2026-06-12:
                    # established conns, zero completions for ~1h, retries cycling
                    # at the 1800s httpx read timeout) — bound the header wait by
                    # the first-token window. Non-stream calls legitimately block
                    # here for the whole generation and stay unbounded.
                    remaining = first_token_deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    resp = await asyncio.wait_for(
                        self._client.chat.completions.create(**kwargs),
                        timeout=remaining,
                    )
                else:
                    resp = await self._client.chat.completions.create(**kwargs)
            except asyncio.TimeoutError as exc:
                raise LLMTimeoutError(
                    "provider response headers timeout",
                    context={
                        "timeout_phase": "response_headers",
                        "first_token_timeout_s": first_token_timeout_s,
                    },
                ) from exc
            except Exception as exc:  # noqa: BLE001 - mapped to typed anomalies below
                raise self._map_provider_error(exc) from exc
            if stream:
                try:
                    return await self._collect_completion_stream(
                        resp,
                        first_token_timeout_s,
                        first_token_deadline,
                    )
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
        usage = (
            {
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(resp.usage, "completion_tokens", 0),
                "total_tokens": getattr(resp.usage, "total_tokens", 0),
            }
            if resp.usage
            else {}
        )
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
        first_token_deadline: float | None,
    ) -> tuple[str, str | None, dict[str, int], Any]:
        iterator = stream_resp.__aiter__()
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        chunk_count = 0
        chunk_samples: list[Any] = []
        finish: str | None = None
        usage: dict[str, int] = {}
        provider_usage: dict[str, Any] | None = None
        first_token_seen = False
        inter_token_timeout_s = self._s.llm.timeout_s

        while True:
            try:
                if not first_token_seen and first_token_deadline is not None:
                    remaining = first_token_deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    chunk = await asyncio.wait_for(anext(iterator), timeout=remaining)
                elif first_token_seen and inter_token_timeout_s > 0:
                    # Once a real reasoning/content delta has arrived, the short
                    # first-token health deadline is spent. A subsequent stream stall
                    # uses the configured request timeout instead.
                    chunk = await asyncio.wait_for(anext(iterator), timeout=inter_token_timeout_s)
                else:
                    chunk = await anext(iterator)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as exc:
                timeout_phase = "first_token" if not first_token_seen else "inter_token"
                raise LLMTimeoutError(
                    (
                        "provider stream first token timeout"
                        if not first_token_seen
                        else "provider stream inter-token timeout"
                    ),
                    context={
                        "timeout_phase": timeout_phase,
                        "first_token_timeout_s": first_token_timeout_s,
                        "inter_token_timeout_s": inter_token_timeout_s,
                    },
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
            if isinstance(safe_chunk, dict) and isinstance(safe_chunk.get("usage"), dict):
                provider_usage = safe_chunk["usage"]
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

        reasoning_text = "".join(reasoning_parts)
        content_text = "".join(text_parts)
        if reasoning_parts:
            usage["reasoning_preview"] = reasoning_text[:1200]
        return (
            content_text,
            finish,
            usage,
            {
                "stream_chunk_count": chunk_count,
                "stream_chunk_samples": chunk_samples,
                "stream_ended_before_first_token": not first_token_seen,
                "reasoning_char_count": len(reasoning_text),
                "content_char_count": len(content_text),
                "provider_usage": provider_usage,
            },
        )

    def _provider_request_options(
        self,
        *,
        response_format: dict[str, Any] | None,
        reasoning_effort: str | None,
        thinking: str | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if response_format is not None:
            kwargs["response_format"] = response_format
        extra_body: dict[str, Any] = {}
        normalized_effort = (
            str(reasoning_effort).strip().lower() if reasoning_effort is not None else ""
        )
        if normalized_effort == "none" and self._uses_openrouter_endpoint():
            # OpenRouter's explicit disable contract is the reasoning object.  Sending
            # only reasoning_effort="none" is not sufficient evidence that upstream
            # reasoning was disabled (and some endpoints simply ignore that value).
            extra_body["reasoning"] = {"enabled": False}
        elif reasoning_effort:
            kwargs["reasoning_effort"] = str(reasoning_effort)
        if thinking:
            extra_body["thinking"] = {"type": str(thinking)}
        provider_only = self._s.llm.openrouter_provider_only
        if provider_only:
            extra_body["provider"] = {
                "only": list(provider_only),
                "allow_fallbacks": self._s.llm.openrouter_allow_fallbacks,
                "require_parameters": self._s.llm.openrouter_require_parameters,
            }
        if extra_body:
            kwargs["extra_body"] = extra_body
        if self._s.llm.openrouter_metadata:
            kwargs["extra_headers"] = {"X-OpenRouter-Metadata": "enabled"}
        return kwargs

    def _uses_openrouter_endpoint(self) -> bool:
        base_url = self._s.llm.base_url.strip()
        parsed = urlparse(base_url if "://" in base_url else f"https://{base_url}")
        host = (parsed.hostname or "").lower()
        return host == "openrouter.ai" or host.endswith(".openrouter.ai")

    async def _raw_tool_call(
        self,
        agent: str,
        model: str,
        convo: list[Message],
        temperature: float | None,
        max_tokens: int | None,
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
            "tools": tools,
            "stream": stream,
            **provider_kwargs,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        if stream:
            kwargs["stream_options"] = {"include_usage": True}
        # Same contract as _raw_call: the semaphore bounds the WHOLE in-flight call
        # (stream collection included), an abandoned stream is always closed, and a
        # streaming create() must produce response headers within the first-token
        # window (accept-then-stall throttling otherwise hangs until the httpx read
        # timeout).
        async with self._sem or nullcontext():
            first_token_deadline = (
                time.monotonic() + first_token_timeout_s
                if stream and first_token_timeout_s > 0
                else None
            )
            try:
                if first_token_deadline is not None:
                    remaining = first_token_deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    resp = await asyncio.wait_for(
                        self._client.chat.completions.create(**kwargs),
                        timeout=remaining,
                    )
                else:
                    resp = await self._client.chat.completions.create(**kwargs)
            except asyncio.TimeoutError as exc:
                raise LLMTimeoutError(
                    "provider response headers timeout",
                    context={
                        "timeout_phase": "response_headers",
                        "first_token_timeout_s": first_token_timeout_s,
                    },
                ) from exc
            except Exception as exc:  # noqa: BLE001 - mapped to typed anomalies below
                raise self._map_provider_error(exc) from exc
            if stream:
                try:
                    return await self._collect_tool_stream(
                        resp,
                        first_token_timeout_s,
                        first_token_deadline,
                    )
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
        first_token_deadline: float | None,
    ) -> tuple[str, str | None, dict[str, int], Any, list[dict[str, Any]]]:
        iterator = stream_resp.__aiter__()
        chunks: list[Any] = []
        first_token_seen = False
        inter_token_timeout_s = self._s.llm.timeout_s
        while True:
            try:
                if not first_token_seen and first_token_deadline is not None:
                    remaining = first_token_deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    chunk = await asyncio.wait_for(anext(iterator), timeout=remaining)
                elif first_token_seen and inter_token_timeout_s > 0:
                    chunk = await asyncio.wait_for(anext(iterator), timeout=inter_token_timeout_s)
                else:
                    chunk = await anext(iterator)
            except StopAsyncIteration as exc:
                if not first_token_seen:
                    raise EmptyResponseError("provider stream ended before first token") from exc
                break
            except asyncio.TimeoutError as exc:
                timeout_phase = "first_token" if not first_token_seen else "inter_token"
                raise LLMTimeoutError(
                    (
                        "provider stream first token timeout"
                        if not first_token_seen
                        else "provider stream inter-token timeout"
                    ),
                    context={
                        "timeout_phase": timeout_phase,
                        "first_token_timeout_s": first_token_timeout_s,
                        "inter_token_timeout_s": inter_token_timeout_s,
                    },
                ) from exc
            chunks.append(chunk)
            if self._tool_stream_chunk_has_token(chunk):
                first_token_seen = True
        return self._assemble_tool_stream_chunks(chunks)

    def _tool_stream_chunk_has_token(self, chunk: Any) -> bool:
        """Return whether a chunk carries a real reasoning/content/tool delta."""
        for choice in self._get(chunk, "choices", []) or []:
            delta = self._get(choice, "delta", {}) or {}
            if self._get(delta, "reasoning_content") or self._get(delta, "reasoning"):
                return True
            if self._get(delta, "content"):
                return True
            for call_delta in self._get(delta, "tool_calls", []) or []:
                if self._get(call_delta, "id") or self._get(call_delta, "type"):
                    return True
                function = self._get(call_delta, "function")
                if function is not None and (
                    self._get(function, "name") or self._get(function, "arguments")
                ):
                    return True
        return False

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
            return (
                text,
                finish,
                {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
                payload,
                tool_calls,
            )
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return (
            text,
            "stop",
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            payload,
            [],
        )

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
        prompt_details = cls._get(usage, "prompt_tokens_details", {}) or {}
        cached_tokens = int(cls._get(prompt_details, "cached_tokens", 0) or 0)
        out["cache_hit_tokens"] = cached_tokens
        out["cache_miss_tokens"] = max(0, out["prompt_tokens"] - cached_tokens)
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
        msg_lower = msg.lower()
        if (
            "Timeout" in name
            or "APIConnection" in name
            or "upstream idle timeout" in msg_lower
            or "gateway timeout" in msg_lower
        ):
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
        raw_status = getattr(exc, "status_code", None)
        if raw_status is None:
            response = getattr(exc, "response", None)
            raw_status = getattr(response, "status_code", None)
        if raw_status is None:
            body = getattr(exc, "body", None)
            if isinstance(body, dict):
                raw_status = body.get("code")
                body_error = body.get("error")
                if raw_status is None and isinstance(body_error, dict):
                    raw_status = body_error.get("code")
        try:
            status = int(raw_status) if raw_status is not None else None
        except (TypeError, ValueError):
            status = None
        if status == 429:
            return RateLimitError(f"provider rate limit: {msg}", context={"status_code": status})
        # 402 = insufficient balance. Under the retry-until-success policy (max_retries<0)
        # a mid-run balance lapse should PAUSE-and-resume (retry until the account is topped
        # up), not terminally fail records and silently corrupt results. So treat it as a
        # transient fault alongside provider/CDN 5xx errors. OpenRouter can surface
        # upstream failures as non-standard 520/522/524/529 statuses; these are transport
        # faults too and must not become model-quality failures after a single request.
        retryable = status in (402, 408, 425, 500, 502, 503, 504, 520, 522, 524, 529)
        return LLMError(
            f"provider error [{name}]: {msg}", context={"status_code": status}, retryable=retryable
        )

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
        if finish == "length":
            raise TruncatedResponseError(
                "response truncated (finish_reason=length)",
                context={
                    "agent": agent,
                    "finish_reason": finish,
                    "truncation": (provider_metadata or {}).get("truncation"),
                    "incomplete_details": (provider_metadata or {}).get("incomplete_details"),
                },
            )
        if not text.strip():
            metadata = provider_metadata or {}
            if metadata.get("stream_ended_before_first_token"):
                message = "provider stream ended before first token"
            elif int(metadata.get("reasoning_char_count") or 0) > 0:
                message = "model returned reasoning without answer content"
            else:
                message = "model returned empty content"
            raise EmptyResponseError(
                message,
                context={
                    "agent": agent,
                    "finish_reason": finish,
                    "stream_chunk_count": metadata.get("stream_chunk_count"),
                    "reasoning_char_count": metadata.get("reasoning_char_count"),
                    "content_char_count": metadata.get("content_char_count"),
                },
            )
        low = text.strip().lower()
        if len(low) < 120 and any(low.startswith(m) for m in _REFUSAL_MARKERS):
            raise RefusalError(
                "response looks like a refusal", context={"agent": agent, "preview": text[:200]}
            )

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
            raise TruncatedResponseError(
                "response truncated (finish_reason=length)",
                context={
                    "agent": agent,
                    "finish_reason": finish,
                    "truncation": (provider_metadata or {}).get("truncation"),
                    "incomplete_details": (provider_metadata or {}).get("incomplete_details"),
                },
            )
        if not text.strip() and not tool_calls:
            raise EmptyResponseError(
                "model returned empty content and no tool calls",
                context={"agent": agent, "finish_reason": finish},
            )

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
    def _next_provider_attempt_index(attempts: list[dict[str, Any]]) -> int:
        indices: list[int] = []
        for item in attempts:
            raw_index = item.get("provider_attempt_index")
            try:
                if raw_index is not None:
                    indices.append(int(raw_index))
            except (TypeError, ValueError):
                continue
        return max(indices, default=0) + 1

    @staticmethod
    def _provider_attempt_count(attempts: list[dict[str, Any]]) -> int:
        indices: set[int] = set()
        for item in attempts:
            raw_index = item.get("provider_attempt_index")
            try:
                if raw_index is not None:
                    indices.add(int(raw_index))
            except (TypeError, ValueError):
                continue
        if indices:
            return len(indices)
        # Backward-compatible fallback for diagnostics created before provider
        # attempts carried a stable index.  A send followed by send_error is one
        # provider request, not two.
        sends = sum(1 for item in attempts if item.get("kind") == "send")
        errors_without_response = sum(
            1
            for item in attempts
            if item.get("kind") == "send_error"
            and not (item.get("attempt_diagnostics") or {}).get("response_received")
        )
        return sends + errors_without_response

    @staticmethod
    def _task_logger_settle_received_attempt(
        task_logger: "TaskLogger",
        *,
        agent: str,
        call_id: str,
        model: str,
        repair_index: int,
        call_status: str,
        attempts: list[dict[str, Any]],
        request_config: dict[str, Any],
        retry_kind: str | None = None,
        error: LLMError | None = None,
        failure_phase: str | None = None,
    ) -> None:
        """Settle the latest received response after output validation.

        A transport-successful response is not a successful provider attempt
        until any requested JSON/schema validation also accepts it.  Deferring
        this one ledger append lets rejected parse/schema responses carry their
        anomaly evidence on the original provider-attempt row.
        """

        provider_attempt = next(
            (
                item
                for item in reversed(attempts)
                if item.get("kind") == "send"
                and item.get("repair_index") == repair_index
                and not item.get("provider_attempt_ledger_settled")
            ),
            None,
        )
        if provider_attempt is None:
            raise RuntimeError(
                f"no unsettled provider response for call={call_id} repair={repair_index}"
            )
        provider_attempt_index = int(provider_attempt["provider_attempt_index"])
        transport_attempt = int(provider_attempt.get("attempt", 0)) + 1
        LLMClient._task_logger_log_attempt(
            task_logger,
            agent=agent,
            call_id=call_id,
            model=model,
            provider_attempt_index=provider_attempt_index,
            transport_attempt=transport_attempt,
            repair_index=repair_index,
            call_status=call_status,
            provider_attempt=provider_attempt,
            request_config=request_config,
            retry_kind=retry_kind,
            error=error,
            failure_phase=failure_phase,
        )
        provider_attempt["provider_attempt_ledger_settled"] = True

    @staticmethod
    def _task_logger_log_attempt(
        task_logger: "TaskLogger",
        *,
        agent: str,
        call_id: str,
        model: str,
        provider_attempt_index: int,
        transport_attempt: int,
        repair_index: int,
        call_status: str,
        provider_attempt: dict[str, Any] | None,
        request_config: dict[str, Any],
        retry_kind: str | None = None,
        error: LLMError | None = None,
        failure_phase: str | None = None,
    ) -> None:
        """Flush exactly one durable billing/routing row for a provider request."""

        response_received = provider_attempt is not None
        usage = provider_attempt.get("usage") if response_received else None
        if not isinstance(usage, dict):
            usage = None
        provider_metadata = provider_attempt.get("provider_metadata") if response_received else None
        if not isinstance(provider_metadata, dict):
            provider_metadata = None
        response_anomaly_evidence: dict[str, Any] | None = None
        if response_received and call_status != "success":
            existing_evidence = provider_attempt.get("response_anomaly_evidence")
            if isinstance(existing_evidence, dict):
                response_anomaly_evidence = existing_evidence
            else:
                response_text = str(provider_attempt.get("response") or "")
                try:
                    response_anomaly_evidence = task_logger.write_llm_response_anomaly_evidence(
                        call_id,
                        provider_attempt_index=provider_attempt_index,
                        transport_attempt=transport_attempt,
                        repair_index=repair_index,
                        response_text=response_text,
                        call_status=call_status,
                        failure_phase=failure_phase or "provider_response_validation",
                        anomaly=(
                            error.anomaly.value if error is not None and error.anomaly else None
                        ),
                        error_type=(type(error).__name__ if error is not None else None),
                        finish_reason=provider_attempt.get("finish_reason"),
                    )
                    response_anomaly_evidence["write_status"] = "written"
                except Exception as evidence_error:  # noqa: BLE001 - preserve billing row
                    response_anomaly_evidence = {
                        "schema": "tend.provider_response_anomaly.v1",
                        "write_status": "error",
                        "error_type": type(evidence_error).__name__,
                        "error_message": str(evidence_error),
                    }
                    task_logger.warning(
                        "llm_response_anomaly_evidence_write_failed",
                        call_id=call_id,
                        provider_attempt_index=provider_attempt_index,
                        repair_index=repair_index,
                        error_type=type(evidence_error).__name__,
                        error_message=str(evidence_error),
                    )
                provider_attempt["response_anomaly_evidence"] = response_anomaly_evidence
        provider_cost = LLMClient._provider_cost_usd(provider_metadata)
        if provider_cost is None:
            cost_source = "unknown"
        elif call_status == "retry":
            cost_source = "provider_usage_retry"
        elif call_status == "error":
            cost_source = "provider_usage_error"
        else:
            cost_source = "provider_usage"
        task_logger.log_llm_attempt(
            call_id,
            agent=agent,
            model=model,
            provider_attempt_index=provider_attempt_index,
            transport_attempt=transport_attempt,
            repair_index=repair_index,
            call_status=call_status,
            response_received=response_received,
            usage=usage,
            finish_reason=(provider_attempt.get("finish_reason") if response_received else None),
            cost_usd=provider_cost,
            cost_source=cost_source,
            provider_metadata=provider_metadata,
            request_config=request_config,
            retry_kind=retry_kind,
            anomaly=(error.anomaly.value if error is not None and error.anomaly else None),
            error=error.to_record() if error is not None else None,
            response_anomaly_evidence=response_anomaly_evidence,
        )

    @staticmethod
    def _task_logger_log_error(
        task_logger: "TaskLogger",
        call_id: str,
        model: str,
        *,
        attempts: list[dict[str, Any]],
        request_config: dict[str, Any],
        error: BaseException,
    ) -> str:
        """Close out a failed TaskLogger-logged call, returning its ``llm/<call_id>.md`` ref."""
        provider_attempt = next(
            (item for item in reversed(attempts) if item.get("kind") == "send"),
            {},
        )
        usage = provider_attempt.get("usage")
        if not isinstance(usage, dict):
            usage = {"prompt_tokens": 0, "completion_tokens": 0}
        provider_metadata = provider_attempt.get("provider_metadata")
        provider_cost = LLMClient._provider_cost_usd(provider_metadata)
        task_logger.log_llm_response(
            call_id,
            response_raw={
                "model": model,
                "call_status": "error",
                "attempt_count": LLMClient._provider_attempt_count(attempts),
                "request_config": request_config,
                "provider_metadata": provider_metadata,
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
            },
            usage=usage,
            finish_reason="error",
            cost_usd=provider_cost or 0.0,
            cost_source="provider_usage_error" if provider_cost is not None else "error",
            append_cost_record=False,
        )
        return str(getattr(task_logger, "last_llm_call_path", None) or "")

    @staticmethod
    def _cost_source(usage: dict[str, int]) -> str:
        token_keys = ("prompt_tokens", "completion_tokens", "total_tokens")
        if any(int(usage.get(key, 0) or 0) > 0 for key in token_keys):
            return "token_usage_only"
        return "unavailable"

    @staticmethod
    def _provider_cost_usd(provider_metadata: Any) -> float | None:
        if not isinstance(provider_metadata, dict):
            return None
        provider_usage = provider_metadata.get("provider_usage")
        if not isinstance(provider_usage, dict):
            return None
        raw_cost = provider_usage.get("cost")
        try:
            return float(raw_cost) if raw_cost is not None else None
        except (TypeError, ValueError):
            return None

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
        max_attempts = None if self._s.llm.max_retries < 0 else self._s.llm.max_retries + 1
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
            tail = (
                "\nReturn ONLY a JSON object that conforms to the required schema. "
                "Do not include prose or code fences."
            )
        return f"Your previous reply was rejected:\n{lines}\nFix it and reply again.{tail}"

    def _finish(
        self,
        agent: str,
        call_id: str,
        model: str,
        text: str,
        parsed: Any,
        finish: str | None,
        usage: dict[str, int],
        t0: float,
        attempts: list[dict[str, Any]],
        log: RunLogger,
        *,
        messages: list[Message],
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
            provider_cost = self._provider_cost_usd(provider_metadata)
            cost_source = (
                "provider_usage" if provider_cost is not None else self._cost_source(usage)
            )
            message: dict[str, Any] = {"role": "assistant", "content": text}
            reasoning_content = self._reasoning_content(usage)
            if reasoning_content:
                message["reasoning_content"] = reasoning_content
            task_logger.log_llm_response(
                call_id,
                response_raw={
                    "model": model,
                    "call_status": "success",
                    "attempt_count": self._provider_attempt_count(attempts),
                    "choices": [{"message": message, "finish_reason": finish}],
                    "usage": usage,
                    "provider_metadata": provider_metadata,
                    "request_config": request_config,
                },
                usage=usage,
                finish_reason=finish or "unknown",
                cost_usd=provider_cost or 0.0,
                cost_source=cost_source,
                append_cost_record=False,
            )
            self._notify_usage_progress(
                call_id=call_id,
                usage=usage,
                cost_source=cost_source,
            )
            ref = str(getattr(task_logger, "last_llm_call_path", None) or "")
            return LLMResult(
                agent=agent,
                call_id=call_id,
                model=model,
                text=text,
                parsed=parsed,
                finish_reason=finish,
                usage=usage,
                latency_s=latency,
                attempts=self._provider_attempt_count(attempts),
                transcript_ref=ref,
                diagnostics_ref=ref,
            )
        ref = log.save_transcript(
            agent,
            call_id,
            {
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
            },
        )
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
        log.info(
            "llm_call_ok",
            agent=agent,
            call_id=call_id,
            model=model,
            attempts=len(attempts),
            latency_s=latency,
            total_tokens=usage.get("total_tokens", 0),
            transcript_ref=ref,
            diagnostics_ref=diagnostics_ref,
        )
        self._notify_usage_progress(
            call_id=call_id,
            usage=usage,
            cost_source=self._cost_source(usage),
        )
        return LLMResult(
            agent=agent,
            call_id=call_id,
            model=model,
            text=text,
            parsed=parsed,
            finish_reason=finish,
            usage=usage,
            latency_s=latency,
            attempts=self._provider_attempt_count(attempts),
            transcript_ref=ref,
            diagnostics_ref=diagnostics_ref,
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
        ref = log.save_transcript(
            agent,
            call_id,
            {
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
            },
        )
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
        log.info(
            "llm_call_ok",
            agent=agent,
            call_id=call_id,
            model=model,
            attempts=len(attempts),
            latency_s=latency,
            total_tokens=usage.get("total_tokens", 0),
            tool_calls=len(tool_calls),
            cost_source=cost_source,
            transcript_ref=ref,
            diagnostics_ref=diagnostics_ref,
        )
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
