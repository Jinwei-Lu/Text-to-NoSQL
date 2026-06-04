"""OpenAI-compatible async LLM client with transcripts, retries, and anomaly typing.

Responsibilities (and *only* these — agent semantics live in tend/agents):
  1. Send chat completions to the configured provider (DeepSeek by default).
  2. Persist full structured call diagnostics (every attempt: messages, raw response,
     usage, timing) as session-local JSON sidecars when an agent session is bound,
     falling back to ``llm/<agent>/<call_id>.diagnostics.json`` only for legacy
     calls without a canonical session. Optional debug markdown transcripts can be
     enabled at the logger layer.
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
from .types import Message, ToolChoice, ToolLLMResult, ToolSchema, parse_tool_calls

StubFn = Callable[[str, list[Message], dict | None], "str | dict[str, Any]"]

_REFUSAL_MARKERS = (
    "i cannot help", "i can't help", "i cannot assist", "i'm unable to",
    "i am unable to", "i won't", "i will not", "as an ai",
)
_ALLOWED_ROLES = {"system", "user", "assistant", "tool", "developer"}
_ALLOWED_MESSAGE_KEYS = {"role", "content", "name", "tool_call_id", "tool_calls"}


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
    return f"llm/{agent}/{call_id}.diagnostics.json"


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
        self._tool_choice_unsupported_models: set[str] = set()
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
        request_config = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "expect_json": expect_json,
            "schema": schema,
            "json_repair_retries": json_repair_retries,
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
            log.info("llm_call_start", agent=agent, call_id=call_id, model=model,
                     message_count=len(convo),
                     prompt_chars=prompt_chars,
                     transcript_ref=start_ref, diagnostics_ref=start_diagnostics_ref)
            if self._s.llm.prompt_warn_chars > 0 and prompt_chars >= self._s.llm.prompt_warn_chars:
                log.warning(
                    "llm_prompt_size_warning",
                    agent=agent,
                    call_id=call_id,
                    model=model,
                    prompt_chars=prompt_chars,
                    threshold_chars=self._s.llm.prompt_warn_chars,
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
                    attempts,
                    log,
                    transcript_ref=start_ref,
                    diagnostics_ref=start_diagnostics_ref,
                )
                if not expect_json:
                    return self._finish(agent, call_id, model, text, None, finish, usage,
                                        t0, attempts, log, messages=convo,
                                        request_config=request_config)
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
                                        request_config=request_config)
                except (ResponseParseError, SchemaValidationError) as verr:
                    attempts[-1]["validation_error"] = verr.to_record()
                    if repair >= json_repair_retries:
                        raise
                    convo = convo + [
                        {"role": "assistant", "content": text},
                        {"role": "user", "content": self._repair_prompt(verr, schema)},
                    ]
                    log.warning("llm_repair_retry", agent=agent, call_id=call_id,
                                attempt=repair + 1, reason=verr.anomaly.value,
                                transcript_ref=start_ref,
                                diagnostics_ref=start_diagnostics_ref)
            raise LLMError("exhausted repair retries", context={"agent": agent})  # unreachable
        except LLMError as err:
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
        stream: bool = True,
        first_token_timeout_s: float = 6.0,
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
        if tool_choice is not None and model in self._tool_choice_unsupported_models:
            log.warning(
                "llm_tool_choice_disabled_for_model",
                agent=agent,
                call_id=call_id,
                model=model,
                requested_tool_choice=tool_choice,
                reason="previous provider rejection for this model",
            )
            tool_choice = None

        convo = list(messages)
        attempts: list[dict[str, Any]] = []
        t0 = time.monotonic()
        request_config = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "tools": tools,
            "tool_choice": tool_choice,
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
            if self._s.llm.prompt_warn_chars > 0 and prompt_chars >= self._s.llm.prompt_warn_chars:
                log.warning(
                    "llm_prompt_size_warning",
                    agent=agent,
                    call_id=call_id,
                    model=model,
                    prompt_chars=prompt_chars,
                    threshold_chars=self._s.llm.prompt_warn_chars,
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
        temperature: float, max_tokens: int, attempts: list[dict[str, Any]],
        log: RunLogger,
        *,
        transcript_ref: str,
        diagnostics_ref: str,
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
                            delay_s=round(delay, 2),
                            transcript_ref=transcript_ref,
                            diagnostics_ref=diagnostics_ref)
                await asyncio.sleep(delay)
        assert last is not None
        raise last

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
                if not err.retryable or retries_used >= self._s.llm.max_retries:
                    raise
                delay = min(8.0, 0.5 * (2 ** retries_used)) + random.uniform(0, 0.3)
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
                retries_used += 1
                send_index += 1
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

    async def _raw_tool_call(
        self,
        agent: str,
        model: str,
        convo: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSchema],
        tool_choice: ToolChoice | None,
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
        }
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        if stream:
            kwargs["stream_options"] = {"include_usage": True}
        async with (self._sem or nullcontext()):
            try:
                resp = await self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - mapped to typed anomalies below
                raise self._map_provider_error(exc) from exc
        if stream:
            return await self._collect_tool_stream(resp, first_token_timeout_s)
        choice = resp.choices[0]
        message = choice.message
        text = getattr(message, "content", None) or ""
        usage = self._usage_dict(getattr(resp, "usage", None))
        reasoning = getattr(message, "reasoning_content", None)
        if reasoning:
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
                content = self._get(delta, "content")
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

    @staticmethod
    def _cost_source(usage: dict[str, int]) -> str:
        token_keys = ("prompt_tokens", "completion_tokens", "total_tokens")
        if any(int(usage.get(key, 0) or 0) > 0 for key in token_keys):
            return "api"
        return "unavailable"

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
        ref = log.save_transcript(agent, call_id, {
            "model": model,
            **request_config,
            "messages": messages,
            "tool_choice_fallback": tool_choice_fallback,
            "attempts": attempts,
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
        parsed_tool_calls = parse_tool_calls(tool_calls)
        assistant_message: Message = {"role": "assistant", "content": text}
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
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
