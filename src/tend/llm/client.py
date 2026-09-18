"""OpenAI-compatible async LLM client with transcripts, retries, and anomaly typing.

Responsibilities (and *only* these — agent semantics live in tend/agents):
  1. Send chat completions to the configured provider (DeepSeek by default).
  2. Persist full structured call diagnostics (every attempt: messages, raw response,
     usage, timing) as session-local JSON sidecars when an agent session is bound,
     and otherwise to stage-local ``<stage>/llm/<agent>_<call_id>.diagnostics.json``
     sidecars. DynaDB-style markdown call logs are the default human-readable view;
     diagnostics JSON remains the machine-readable sidecar.
  3. Classify every failure into a typed LLMError with an :class:`Anomaly` kind.
  4. Retry transient transport faults forever by default (at a fixed interval) and run a
     bounded JSON/schema *repair* loop.

Stub mode (``settings.stub``): no network; a registered ``stub_fn`` returns canned output
so the whole pipeline is exercisable offline and deterministically in tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import time
import traceback
from contextlib import asynccontextmanager, nullcontext
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse
from uuid import uuid4

import json5
from jsonschema import Draft202012Validator

from ..config import Settings
from ..errors import (
    Anomaly,
    CampaignPauseError,
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
from .types import Message

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
_ALLOWED_ROLES = {"system", "user", "assistant", "developer"}
_ALLOWED_MESSAGE_KEYS = {"role", "content", "name", "reasoning_content"}
_COMPACTED_ATTEMPTS_KIND = "compacted_attempts"


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


def _retry_after_seconds(exc: Exception) -> tuple[float | None, str | None]:
    """Extract Retry-After seconds (delta or HTTP date) from a provider exception."""

    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or getattr(exc, "headers", None)
    raw: Any = None
    if headers is not None:
        getter = getattr(headers, "get", None)
        if callable(getter):
            raw = getter("retry-after") or getter("Retry-After")
    if raw is None:
        return None, None
    text = str(raw).strip()
    try:
        seconds = float(text)
        return (max(0.0, seconds), text) if math.isfinite(seconds) else (None, text)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds()), text
    except (TypeError, ValueError, OverflowError):
        return None, text


# One HTTP/2 connection admits at most 100 concurrent streams. Stay under that
# so the 101st call opens another client instead of raising LocalProtocolError.
HTTP2_MAX_STREAMS_PER_CLIENT = 80


@dataclass
class _Http2Shard:
    client: Any
    in_flight: int = 0
    retired: bool = False


class LLMClient:
    """Async transcripting LLM client shared across all agents.

    The single provider call is concurrency-limited by a semaphore gate
    (configurable via ``settings.llm.max_concurrency``; ``<= 0`` — the default —
    runs unbounded), making this the one canonical chokepoint for live LLM
    throughput. Transient provider/transport faults are retried at a fixed interval,
    forever when ``max_retries < 0`` (the default), so provider capacity errors
    reroute rather than become benchmark failures.
    """

    def __init__(self, settings: Settings, logger: RunLogger) -> None:
        self._s = settings
        self._log = logger
        self._stub_fn: StubFn | None = None
        self._client: Any = None
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
        self._transport_reset_lock = asyncio.Lock()
        self._transport_epoch = 0
        self._last_transport_reset_monotonic = 0.0
        self._retired_provider_clients: list[Any] = []
        self._shards: list[_Http2Shard] = []
        self._unhealthy_shards: list[_Http2Shard] = []
        self._shard_lock = asyncio.Lock()
        if not settings.stub:
            self._open_live_provider_client()

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close every owned HTTP/2 shard and any retired transports."""
        clients = list(self._retired_provider_clients)
        self._retired_provider_clients = []
        for shard in self._shards:
            if shard.client is not None and shard.client not in clients:
                clients.append(shard.client)
        self._shards = []
        current = self._client
        self._client = None
        if current is not None and current not in clients:
            clients.append(current)
        for client in clients:
            close = getattr(client, "close", None)
            aclose = getattr(client, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:
                    pass
            elif callable(close):
                result = close()
                if inspect.isawaitable(result):
                    try:
                        await result
                    except Exception:
                        pass
            http_client = getattr(client, "_client", None) or getattr(client, "http_client", None)
            if http_client is not client:
                http_aclose = getattr(http_client, "aclose", None)
                if callable(http_aclose):
                    try:
                        await http_aclose()
                    except Exception:
                        pass

    def _open_live_provider_client(self) -> None:
        """Open one HTTP/2 client bounded to ~80 multiplexed streams."""
        import httpx
        from openai import AsyncOpenAI

        settings = self._s
        # One httpx HTTP/2 connection admits at most 100 concurrent streams; the
        # 101st raises LocalProtocolError. Each shard is therefore one connection
        # (max_connections=1) capped at HTTP2_MAX_STREAMS_PER_CLIENT. Extra
        # shards are opened on demand instead of stuffing more streams onto one
        # connection. Connect stays bounded by the first-token window so a hung
        # handshake fails into the same retry loop.
        ft = settings.llm.first_token_timeout_s
        connect_timeout_s = ft if ft and ft > 0 else settings.llm.timeout_s
        request_timeout = httpx.Timeout(settings.llm.timeout_s, connect=connect_timeout_s)
        http_client = httpx.AsyncClient(
            http2=True,
            limits=httpx.Limits(
                max_connections=1,
                max_keepalive_connections=1,
            ),
            timeout=request_timeout,
        )
        client = AsyncOpenAI(
            base_url=settings.llm.base_url,
            api_key=settings.llm.api_key,
            timeout=request_timeout,
            max_retries=0,
            http_client=http_client,
        )
        self._shards.append(_Http2Shard(client=client))
        self._client = client
        self._transport_epoch += 1

    def _mark_shard_unhealthy(self, shard: _Http2Shard | None) -> None:
        if shard is None or shard in self._unhealthy_shards:
            return
        shard.retired = True
        self._unhealthy_shards.append(shard)

    def _shard_admits_new_streams(self, item: _Http2Shard) -> bool:
        return (
            not item.retired
            and item not in self._unhealthy_shards
            and item.in_flight < HTTP2_MAX_STREAMS_PER_CLIENT
        )

    @asynccontextmanager
    async def _borrow_live_provider(self):
        """Hold one HTTP/2 stream slot for create() plus stream collection."""
        shard: _Http2Shard | None = None
        injected = self._client
        if injected is not None and all(
            item.client is not injected for item in self._shards
        ):
            # Tests replace ``_client`` with a fake transport after a live
            # ``__init__`` that already opened a real shard. Use the replacement
            # and do not send those calls through the leftover HTTP/2 pool.
            yield injected, None
            return
        if self._shards:
            async with self._shard_lock:
                chosen: _Http2Shard | None = None
                for item in self._shards:
                    if self._shard_admits_new_streams(item):
                        chosen = item
                        break
                if chosen is None:
                    self._open_live_provider_client()
                    chosen = self._shards[-1]
                chosen.in_flight += 1
                shard = chosen
            try:
                yield shard.client, shard
            finally:
                async with self._shard_lock:
                    shard.in_flight = max(0, shard.in_flight - 1)
            return
        raise LLMError(
            "provider transport is closed",
            retryable=True,
            context={"status_code": None},
        )

    @staticmethod
    def _stalled_stream_requires_new_transport(err: LLMError) -> bool:
        return str(err.context.get("timeout_phase") or "") in {
            "response_headers",
            "first_token",
            "transport_closed",
        }

    async def _reset_live_provider_transport(self) -> None:
        """Admit later retries on a new shard; do not aclose in-flight siblings.

        Only the stalled shard is retired from new admissions. Its connection
        stays open until LLMClient.aclose() so multiplexed siblings can finish.
        """
        if self._s.stub:
            return
        async with self._transport_reset_lock:
            now = time.monotonic()
            if now - self._last_transport_reset_monotonic < 1.0:
                return
            async with self._shard_lock:
                victims: list[_Http2Shard] = []
                if self._unhealthy_shards:
                    victims = list(self._unhealthy_shards)
                    self._unhealthy_shards.clear()
                elif self._shards:
                    current = self._client
                    victims = [item for item in self._shards if item.client is current]
                elif self._client is not None:
                    self._retired_provider_clients.append(self._client)
                for shard in victims:
                    shard.retired = True
                    if shard in self._shards:
                        self._shards.remove(shard)
                    if shard.client is not None:
                        self._retired_provider_clients.append(shard.client)
                self._open_live_provider_client()
                self._last_transport_reset_monotonic = now

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
        model = model or self._s.llm.model
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
        # One truncation budget covers the whole logical call, including later JSON
        # repair sends.  Keeping this state outside the repair loop prevents every
        # repair round from silently re-arming the expensive truncation allowance.
        truncation_state = {"seen": 0}
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
                    truncation_state=truncation_state,
                )
                if not expect_json:
                    if task_logger is not None:
                        await self._task_logger_settle_received_attempt(
                            task_logger,
                            agent=agent,
                            call_id=call_id,
                            model=model,
                            repair_index=repair,
                            call_status="success",
                            attempts=attempts,
                            request_config=request_config,
                        )
                        self._compact_attempts(attempts)
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
                        await self._task_logger_settle_received_attempt(
                            task_logger,
                            agent=agent,
                            call_id=call_id,
                            model=model,
                            repair_index=repair,
                            call_status="success",
                            attempts=attempts,
                            request_config=request_config,
                        )
                        self._compact_attempts(attempts)
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
                        await self._task_logger_settle_received_attempt(
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
                        self._compact_attempts(attempts)
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
            raise err
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
            if "role" not in m or "content" not in m:
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
            if not isinstance(content, str) or not content.strip():
                raise PromptAnomalyError(
                    "message content empty or non-string",
                    context={
                        "agent": agent,
                        "call_id": call_id,
                        "index": i,
                        "role": m.get("role"),
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
        truncation_state: dict[str, int],
    ) -> tuple[str, str | None, dict[str, int]]:
        attempt = 0
        while True:
            provider_attempt_index = self._next_provider_attempt_index(attempts)
            t0 = time.monotonic()
            provider_attempt: dict[str, Any] | None = None
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
                    "provider_cost_observed": _json_safe(
                        self._raw_provider_cost(provider_metadata)
                    ),
                    "raw_response": _json_safe(raw),
                }
                attempts.append(provider_attempt)
                self._check_response(text, finish, agent, provider_metadata=provider_metadata)
                return text, finish, usage
            except LLMError as err:
                attempt_latency_s = (
                    float(provider_attempt["latency_s"])
                    if provider_attempt is not None
                    else round(time.monotonic() - t0, 3)
                )
                raw_response = (
                    provider_attempt.get("raw_response") if provider_attempt is not None else None
                )
                attempt_diagnostics = {
                    "response_received": provider_attempt is not None,
                    "latency_s": attempt_latency_s,
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
                        "latency_s": attempt_latency_s,
                        "error": err.to_record(),
                        "attempt_diagnostics": attempt_diagnostics,
                        "stream": stream,
                        "first_token_timeout_s": first_token_timeout_s,
                    }
                )
                will_retry = err.retryable and not self._retries_exhausted(attempt)
                if isinstance(err, TruncatedResponseError):
                    truncation_state["seen"] = int(truncation_state.get("seen", 0)) + 1
                    if truncation_state["seen"] > max(0, self._s.llm.max_truncation_retries):
                        will_retry = False
                delay = max(0.0, self._s.llm.retry_interval_s)
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
                        latency_s=attempt_latency_s,
                    )
                else:
                    self._log_run_provider_attempt(
                        log,
                        agent=agent,
                        call_id=call_id,
                        model=model,
                        provider_attempt_index=provider_attempt_index,
                        transport_attempt=attempt + 1,
                        provider_metadata=(
                            provider_attempt.get("provider_metadata")
                            if provider_attempt is not None
                            else None
                        ),
                        response_received=provider_attempt is not None,
                        latency_s=attempt_latency_s,
                        usage=(
                            provider_attempt.get("usage")
                            if provider_attempt is not None
                            else None
                        ),
                        provider_cost_observed=(
                            provider_attempt.get("provider_cost_observed")
                            if provider_attempt is not None
                            else None
                        ),
                        error=err,
                        request_config=request_config,
                    )
                self._compact_attempts(attempts)
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
                if will_retry and self._stalled_stream_requires_new_transport(err):
                    await self._reset_live_provider_transport()
                if not will_retry:
                    raise err
                self._notify_retry_progress(err, attempt + 1, delay)
                # _raw_call owns the semaphore only around the live request/stream and
                # has returned/raised before this point.  No permit is held while a
                # reroutable provider failure waits, so fresh calls cannot starve.
                await asyncio.sleep(delay)
                attempt += 1

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
            async with self._borrow_live_provider() as (provider, shard):
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
                            provider.chat.completions.create(**kwargs),
                            timeout=remaining,
                        )
                    else:
                        resp = await provider.chat.completions.create(**kwargs)
                except asyncio.TimeoutError as exc:
                    self._mark_shard_unhealthy(shard)
                    raise LLMTimeoutError(
                        "provider response headers timeout",
                        context={
                            "timeout_phase": "response_headers",
                            "first_token_timeout_s": first_token_timeout_s,
                        },
                    ) from exc
                except LLMError:
                    raise
                except Exception as exc:  # noqa: BLE001 - mapped to typed anomalies below
                    mapped = self._map_provider_error(exc)
                    if self._stalled_stream_requires_new_transport(mapped):
                        self._mark_shard_unhealthy(shard)
                    raise mapped from exc
                if stream:
                    try:
                        return await self._collect_completion_stream(
                            resp,
                            first_token_timeout_s,
                            first_token_deadline,
                        )
                    except LLMError as err:
                        if self._stalled_stream_requires_new_transport(err):
                            self._mark_shard_unhealthy(shard)
                        raise
                    except Exception as exc:  # noqa: BLE001 - streaming iterator faults are provider faults
                        mapped = self._map_provider_error(exc)
                        if self._stalled_stream_requires_new_transport(mapped):
                            self._mark_shard_unhealthy(shard)
                        raise mapped from exc
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
        elif self._uses_openrouter_endpoint():
            extra_body["provider"] = {
                "allow_fallbacks": True,
                "require_parameters": False,
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
    async def _close_stream(stream_resp: Any) -> None:
        """Release an SSE stream (finished OR abandoned).

        Without an explicit close, a timed-out/abandoned stream keeps its pooled HTTP
        connection checked out and the provider keeps GENERATING (and billing) into a
        socket nobody reads — under retry-until-success that compounds into
        self-inflicted provider load. Closing a dead stream must never mask the
        original error, so failures here are swallowed.
        """
        for name in ("aclose", "close"):
            closer = getattr(stream_resp, name, None)
            if not callable(closer):
                continue
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - best-effort release, never masks the real error
                pass
            return

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

    @staticmethod
    def _map_provider_error(exc: Exception) -> LLMError:
        name = type(exc).__name__
        msg = str(exc)[:400]
        raw_status = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        if raw_status is None:
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
        retry_after_s, retry_after_raw = _retry_after_seconds(exc)
        error_context: dict[str, Any] = {"status_code": status}
        if retry_after_s is not None:
            error_context["retry_after_s"] = retry_after_s
        if retry_after_raw is not None:
            error_context["retry_after_raw"] = retry_after_raw
        if status == 402:
            return CampaignPauseError(
                "OpenRouter balance is insufficient; campaign must pause",
                context={**error_context, "pause_reason": "provider_balance_insufficient"},
            )
        if status == 429 or "RateLimit" in name:
            return RateLimitError(f"provider rate limit: {msg}", context=error_context)
        msg_lower = msg.lower()
        if (
            "Timeout" in name
            or "APIConnection" in name
            or "upstream idle timeout" in msg_lower
            or "gateway timeout" in msg_lower
        ):
            return LLMTimeoutError(
                f"provider timeout/connection: {msg}",
                context=error_context,
                retryable=True,
            )
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
            or "LocalProtocol" in name
            or (
                "ProtocolError" in name
                and (
                    "closed" in msg_lower
                    or "recv_data" in msg_lower
                    or "connectionstate" in msg_lower
                )
            )
            # httpx transport read/write faults mid-request: same transient class.
            or "ReadError" in name
            or "WriteError" in name
            or "ConnectError" in name
            or "incomplete chunked read" in msg
            or "peer closed connection" in msg
            or "max outbound streams" in msg_lower
        ):
            return LLMTimeoutError(
                f"provider stream dropped: [{name}] {msg}",
                context={**error_context, "timeout_phase": "transport_closed"},
                retryable=True,
            )
        if "BadRequest" in name and ("context" in msg.lower() or "maximum" in msg.lower()):
            return ContextOverflowError(f"context length exceeded: {msg}", context=error_context)
        # OpenRouter can surface upstream failures as standard/non-standard 5xx statuses.
        # Authentication/authorization and all other permanent 4xx errors fail fast.
        retryable = status in (408, 425) or (status is not None and 500 <= status <= 599)
        return LLMError(
            f"provider error [{name}]: {msg}", context=error_context, retryable=retryable
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
    def _next_provider_attempt_index(attempts: list[dict[str, Any]]) -> int:
        indices: list[int] = []
        for item in attempts:
            if item.get("kind") == _COMPACTED_ATTEMPTS_KIND:
                raw_index = item.get("max_provider_attempt_index")
            else:
                raw_index = item.get("provider_attempt_index")
            try:
                if raw_index is not None:
                    indices.append(int(raw_index))
            except (TypeError, ValueError):
                continue
        return max(indices, default=0) + 1

    @staticmethod
    def _provider_attempt_count(attempts: list[dict[str, Any]]) -> int:
        compacted_count = 0
        compacted_max = 0
        indices: set[int] = set()
        for item in attempts:
            if item.get("kind") == _COMPACTED_ATTEMPTS_KIND:
                compacted_count += int(item.get("provider_attempt_count", 0) or 0)
                compacted_max = max(
                    compacted_max,
                    int(item.get("max_provider_attempt_index", 0) or 0),
                )
                continue
            raw_index = item.get("provider_attempt_index")
            try:
                if raw_index is not None:
                    indices.add(int(raw_index))
            except (TypeError, ValueError):
                continue
        if indices or compacted_count:
            return compacted_count + len({index for index in indices if index > compacted_max})
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
    def _log_run_provider_attempt(
        log: RunLogger,
        *,
        agent: str,
        call_id: str,
        model: str,
        provider_attempt_index: int,
        transport_attempt: int,
        provider_metadata: dict[str, Any] | None,
        response_received: bool,
        latency_s: float,
        usage: dict[str, Any] | None,
        provider_cost_observed: Any,
        error: LLMError | None,
        request_config: dict[str, Any],
        call_status: str | None = None,
    ) -> None:
        """Record one provider attempt in the run's cost log when no TaskLogger is bound."""

        record_cost = getattr(log, "record_llm_cost", None)
        if not callable(record_cost):
            return
        resolved_latency_s = float(latency_s)
        if not math.isfinite(resolved_latency_s) or resolved_latency_s < 0:
            raise ValueError(
                "provider-attempt latency_s must be finite and non-negative"
            )
        provider_cost = LLMClient._provider_cost_usd(provider_metadata)
        known_rejection_cost = (
            LLMClient._known_rejection_cost(error) if error is not None else None
        )
        if provider_cost is None and known_rejection_cost is not None:
            provider_cost = known_rejection_cost
        resolved_status = call_status or (
            "retry" if error is not None and error.retryable else "error"
        )
        billed_provider_cost = LLMClient._provider_cost_usd(provider_metadata)
        record_cost(
            agent=agent,
            call_id=call_id,
            model=model,
            usage=usage,
            cost_usd=provider_cost,
            cost_source=(
                "known_pre_inference_rejection"
                if known_rejection_cost is not None and billed_provider_cost is None
                else "provider_usage"
                if error is None and provider_cost is not None
                else "provider_usage_retry"
                if provider_cost is not None and resolved_status == "retry"
                else "provider_usage_error"
                if provider_cost is not None
                else "unknown"
            ),
            record_type="provider_attempt",
            provider_attempt_index=provider_attempt_index,
            transport_attempt=transport_attempt,
            call_status=resolved_status,
            status=resolved_status,
            response_received=response_received,
            latency_s=resolved_latency_s,
            anomaly=(error.anomaly.value if error is not None and error.anomaly else None),
            error=error.to_record() if error is not None else None,
            provider_metadata=provider_metadata,
            provider=(provider_metadata or {}).get("provider"),
            openrouter_metadata=(provider_metadata or {}).get("openrouter_metadata"),
            provider_cost_observed=provider_cost_observed,
            request_config=request_config,
        )

    async def _task_logger_settle_received_attempt(
        self,
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
        this one cost-log append lets rejected parse/schema responses carry their
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
        self._task_logger_log_attempt(
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
        latency_s: float | None = None,
    ) -> None:
        """Write exactly one billing/routing row for a provider request."""

        response_received = provider_attempt is not None
        if response_received:
            latency_s = provider_attempt.get("latency_s")
        try:
            resolved_latency_s = float(latency_s) if latency_s is not None else math.nan
        except (TypeError, ValueError):
            resolved_latency_s = math.nan
        if not math.isfinite(resolved_latency_s) or resolved_latency_s < 0:
            raise ValueError(
                "provider-attempt latency_s must be finite and non-negative"
            )
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
        known_rejection_cost = (
            LLMClient._known_rejection_cost(error) if error is not None else None
        )
        if provider_cost is None and known_rejection_cost is not None:
            provider_cost = known_rejection_cost
        if known_rejection_cost is not None and LLMClient._provider_cost_usd(
            provider_metadata
        ) is None:
            cost_source = "known_pre_inference_rejection"
        elif provider_cost is None:
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
            latency_s=resolved_latency_s,
            usage=usage,
            finish_reason=(provider_attempt.get("finish_reason") if response_received else None),
            cost_usd=provider_cost,
            cost_source=cost_source,
            provider_cost_observed=(
                provider_attempt.get("provider_cost_observed")
                if response_received
                else None
            ),
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
        """Return only a finite, non-negative provider-reported cost for logging."""

        raw_cost = LLMClient._raw_provider_cost(provider_metadata)
        if raw_cost is None:
            return None
        try:
            cost = float(raw_cost)
        except (TypeError, ValueError):
            return None
        return cost if math.isfinite(cost) and cost >= 0 else None

    @staticmethod
    def _raw_provider_cost(provider_metadata: Any) -> Any:
        """Return the provider-reported cost exactly as received."""

        if not isinstance(provider_metadata, dict):
            return None
        provider_usage = provider_metadata.get("provider_usage")
        if not isinstance(provider_usage, dict):
            return None
        return provider_usage.get("cost")

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

    @staticmethod
    def _known_rejection_cost(err: LLMError) -> float | None:
        """Return zero when there is no evidence the provider billed a completion.

        OpenRouter 401/402/403/429 rejections are known pre-inference responses, and
        connection failures, first-token timeouts, and retryable 5xx/408 statuses end
        before a completion is produced. Content-bearing faults (truncated/empty/parse)
        return None so their cost stays unknown instead of pretending the call was free.
        """

        if isinstance(
            err,
            (
                TruncatedResponseError,
                EmptyResponseError,
                PromptAnomalyError,
                ResponseParseError,
                SchemaValidationError,
                RefusalError,
                ContextOverflowError,
            ),
        ):
            return None
        try:
            status = int(err.context.get("status_code"))
        except (TypeError, ValueError):
            status = None
        if status in {401, 402, 403, 408, 425, 429}:
            return 0.0
        if status is not None and 500 <= status <= 599:
            return 0.0
        if isinstance(err, (LLMTimeoutError, RateLimitError)):
            return 0.0
        if getattr(err, "retryable", False):
            return 0.0
        return None

    def _compact_attempts(self, attempts: list[dict[str, Any]]) -> None:
        """Bound TaskLogger-path RAM after each attempt is appended to the cost log.

        The compact summary preserves exact logical-call counts and the next contiguous
        provider index.  ``cost_summary.jsonl`` remains the full per-attempt source of
        truth; only the in-memory diagnostic window is truncated.
        """

        limit = max(1, int(self._s.llm.attempt_memory_limit))
        summary = next(
            (item for item in attempts if item.get("kind") == _COMPACTED_ATTEMPTS_KIND),
            None,
        )
        entries = [item for item in attempts if item.get("kind") != _COMPACTED_ATTEMPTS_KIND]
        indices = sorted(
            {
                int(item["provider_attempt_index"])
                for item in entries
                if item.get("provider_attempt_index") is not None
            }
        )
        if len(indices) <= limit:
            return
        drop_indices = set(indices[:-limit])
        kept = [
            item
            for item in entries
            if item.get("provider_attempt_index") is None
            or int(item["provider_attempt_index"]) not in drop_indices
        ]
        old_count = int((summary or {}).get("provider_attempt_count", 0) or 0)
        old_dropped = int((summary or {}).get("dropped_entries", 0) or 0)
        old_max = int((summary or {}).get("max_provider_attempt_index", 0) or 0)
        new_summary = {
            "kind": _COMPACTED_ATTEMPTS_KIND,
            "provider_attempt_count": old_count + len(drop_indices),
            "max_provider_attempt_index": max(old_max, max(drop_indices, default=old_max)),
            "dropped_entries": old_dropped + len(entries) - len(kept),
            "durable_ledger": "cost_summary.jsonl",
        }
        attempts[:] = [new_summary, *kept]

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

