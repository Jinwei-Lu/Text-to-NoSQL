"""Agent contract: lifecycle wrapper, shared context, LLM agent base, and registry.

Two layers:

  * :class:`Agent` — the abstract unit of work. Its :meth:`Agent.__call__` is the uniform
    lifecycle: bind logging context, open a progress task, time the run, and convert any
    failure into a logged anomaly + a failed progress task. Subclass and implement
    :meth:`Agent.run` for deterministic or LLM-assisted stages.

  * :class:`LLMAgent` — adds the standard "prompt -> model -> validated JSON -> contract"
    flow: it loads the methodology prompt once, calls the model with the output schema
    (the client handles transport/JSON/schema repair), then runs :meth:`check_contract`
    with a bounded feedback-repair loop for *semantic* violations the schema can't express.

The :data:`REGISTRY` lets the workflow look agents up by id without importing each module.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar
from uuid import uuid4

from ..config import Settings
from ..errors import ContractViolationError, PromptAnomalyError, TendError, wrap_unexpected
from ..llm import LLMClient, Message
from ..observability import ProgressReporter, RunLogger


@dataclass
class AgentContext:
    """Shared services + the identifiers an agent is currently scoped to.

    Cheap to :meth:`bind` per db/record; the bound ``log`` carries those identifiers so
    every event/anomaly is attributable without the agent threading them by hand.
    """

    settings: Settings
    llm: LLMClient
    log: RunLogger
    progress: ProgressReporter | None = None
    source: Any = None                      # BirdSource (Phase A); avoids an import cycle
    mongo: Any = None                       # MongoExecutor (set once execution layer lands)
    db_id: str | None = None
    record_id: int | None = None
    phase: str = "A"
    group: str | None = None                # progress group id (defaults to db_id)
    work_item_id: str | None = None         # caller-supplied progress task discriminator
    log_mgr: Any = None                     # tend.utils.logging.LogManager (solver/baseline/ablation runs)
    extra: dict[str, Any] = field(default_factory=dict)

    def bind(self, **fields: Any) -> "AgentContext":
        log = self.log
        binders = {k: v for k, v in fields.items()
                   if k in ("db_id", "record_id", "phase") and v is not None}
        if binders:
            log = log.bind(**binders)
        return replace(self, log=log, **{k: v for k, v in fields.items()
                                         if k in {f.name for f in self.__dataclass_fields__.values()}})


REGISTRY: dict[str, type["Agent"]] = {}


def register(cls: type["Agent"]) -> type["Agent"]:
    """Class decorator: add an agent to the global registry keyed by ``cls.id``."""
    if not getattr(cls, "id", None):
        raise ValueError(f"{cls.__name__} must define a non-empty class var `id`")
    if cls.id in REGISTRY and REGISTRY[cls.id] is not cls:
        raise ValueError(f"duplicate agent id: {cls.id}")
    REGISTRY[cls.id] = cls
    return cls


def get_agent(agent_id: str) -> "Agent":
    try:
        return REGISTRY[agent_id]()
    except KeyError as exc:
        raise KeyError(f"no agent registered as {agent_id!r}; known={sorted(REGISTRY)}") from exc


class Agent(ABC):
    """Abstract unit of work with a uniform observability lifecycle."""

    id: ClassVar[str] = ""
    phase: ClassVar[str] = "A"
    title: ClassVar[str] = ""

    @abstractmethod
    async def run(self, ctx: AgentContext, inputs: dict[str, Any]) -> dict[str, Any]:
        """Do the work and return the validated output dict. Raise a TendError on failure."""

    async def __call__(self, ctx: AgentContext, inputs: dict[str, Any]) -> dict[str, Any]:
        ctx = ctx.bind(phase=self.phase)
        log = ctx.log.bind(agent=self.id)
        task_id = _agent_task_id(self.id, ctx, inputs)
        ctx = replace(ctx, log=log, extra={**ctx.extra, "_task_id": task_id})
        group = ctx.group or ctx.db_id or self.phase
        if ctx.progress:
            ctx.progress.start_task(task_id, self.title or self.id, group=group)
        log.info("agent_start", title=self.title)
        t0 = time.monotonic()
        try:
            out = await self.run(ctx, inputs)
        except TendError as err:
            err.with_context(agent=self.id, db_id=ctx.db_id, record_id=ctx.record_id)
            if not err.logged:
                log.anomaly(err)
            if ctx.progress:
                ctx.progress.finish_task(task_id, ok=False,
                                         anomaly=err.anomaly.value if err.anomaly else None)
            log.error("agent_failed", elapsed_s=round(time.monotonic() - t0, 3),
                      anomaly=err.anomaly.value if err.anomaly else None)
            raise
        except Exception as exc:  # noqa: BLE001 - coerce stray errors into typed anomalies
            err = wrap_unexpected(exc, agent=self.id, db_id=ctx.db_id, record_id=ctx.record_id)
            log.anomaly(err)
            if ctx.progress:
                ctx.progress.finish_task(task_id, ok=False, anomaly="internal")
            raise err from exc
        elapsed = round(time.monotonic() - t0, 3)
        if ctx.progress:
            ctx.progress.finish_task(task_id, ok=True)
        log.info("agent_done", elapsed_s=elapsed)
        return out


class LLMAgent(Agent):
    """Base for prompt-driven agents: prompt -> model(schema) -> contract-checked output."""

    #: filename under proposals/agent_prompts/ (e.g. "wp_workload_profiler.md")
    prompt_file: ClassVar[str] = ""
    #: JSON Schema the model output must satisfy (validated by the client)
    output_schema: ClassVar[dict[str, Any]] = {}
    #: max semantic-contract repair turns (separate from the client's JSON/schema repair)
    contract_retries: ClassVar[int] = 2
    #: sampling temperature override (None -> settings default)
    temperature: ClassVar[float | None] = None
    #: run postprocess in a worker thread when the hook performs blocking local IO/execution
    offload_postprocess: ClassVar[bool] = False

    _prompt_cache: ClassVar[dict[str, str]] = {}

    # ------------------------------------------------------------------ #
    def prompt_text(self, ctx: AgentContext) -> str:
        if self.prompt_file not in self._prompt_cache:
            path = ctx.settings.paths.agent_prompts / self.prompt_file
            if not path.exists():
                raise PromptAnomalyError(
                    "agent prompt not found",
                    context={
                        "agent": self.id,
                        "prompt_file": self.prompt_file,
                        "prompt_path": str(path),
                    },
                )
            try:
                self._prompt_cache[self.prompt_file] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise PromptAnomalyError(
                    "agent prompt could not be read",
                    context={
                        "agent": self.id,
                        "prompt_file": self.prompt_file,
                        "prompt_path": str(path),
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                    },
                ) from exc
        return self._prompt_cache[self.prompt_file]

    def build_messages(self, ctx: AgentContext, inputs: dict[str, Any]) -> list[Message]:
        """Default message construction; override to customize framing per agent."""
        system = self.prompt_text(ctx)
        if self.output_schema:
            # Static schema note lives in the (stable, cacheable) system prefix, not the
            # volatile user turn, so provider prefix caching can key on it.
            system = system + ("\n\nReturn ONLY a single JSON object conforming to this schema "
                               "(no prose, no code fences):\n"
                               + json.dumps(self.output_schema, ensure_ascii=False))
        user = self.render_inputs(ctx, inputs)
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def render_inputs(self, ctx: AgentContext, inputs: dict[str, Any]) -> str:
        """How the structured inputs are presented to the model. Override as needed."""
        head = f"# Task for agent {self.id} ({self.title})\n"
        ctx_line = ""
        if ctx.db_id:
            ctx_line += f"db_id: {ctx.db_id}\n"
        if ctx.record_id is not None:
            ctx_line += f"record_id: {ctx.record_id}\n"
        # sort_keys keeps equal inputs serializing identically run-to-run (cache-stable);
        # the volatile db_id/record_id ctx_line trails the stable skeleton.
        body = ("\n## Inputs\n```json\n"
                + json.dumps(inputs, ensure_ascii=False, indent=2, sort_keys=True) + "\n```")
        return head + body + ctx_line

    def check_contract(
        self, ctx: AgentContext, inputs: dict[str, Any], output: dict[str, Any]
    ) -> list[str]:
        """Return a list of semantic-contract violations (empty = pass). Override per agent.

        These are checks the JSON Schema cannot express (e.g. "mutations must EX-fail",
        ">=5 mutations", "every variant is dispatched"). Non-empty triggers a feedback
        repair turn up to :attr:`contract_retries`.
        """
        return []

    async def run(self, ctx: AgentContext, inputs: dict[str, Any]) -> dict[str, Any]:
        try:
            messages = self.build_messages(ctx, inputs)
        except TendError as err:
            err.with_context(
                agent=self.id,
                prompt_file=self.prompt_file,
                input_keys=sorted(str(k) for k in inputs),
            )
            _attach_presend_prompt_diagnostics(self, ctx, inputs, err)
            raise
        except Exception as exc:  # noqa: BLE001 - prompt construction must be diagnosable
            err = PromptAnomalyError(
                "agent prompt construction failed",
                context={
                    "agent": self.id,
                    "prompt_file": self.prompt_file,
                    "input_keys": sorted(str(k) for k in inputs),
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "traceback": "".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    ),
                },
            )
            _attach_presend_prompt_diagnostics(self, ctx, inputs, err)
            raise err from exc
        schema = self.output_schema or None
        for attempt in range(self.contract_retries + 1):
            result = await ctx.llm.complete(
                agent=self.id, messages=messages, logger=ctx.log,
                schema=schema, temperature=self.temperature,
            )
            output = result.data if schema else {"text": result.text}
            violations = self.check_contract(ctx, inputs, output)
            if not violations:
                ctx.log.info(
                    "agent_contract_ok",
                    agent=self.id,
                    call_id=result.call_id,
                    transcript_ref=result.transcript_ref,
                    diagnostics_ref=result.diagnostics_ref,
                )
                try:
                    if self.offload_postprocess:
                        # Offloading only makes sense for a blocking (sync) hook. An async
                        # postprocess run in a worker thread would return an un-awaited
                        # coroutine, so reject that combination explicitly.
                        if inspect.iscoroutinefunction(self.postprocess):
                            raise ValueError(
                                f"{type(self).__name__}.postprocess is a coroutine function but "
                                "offload_postprocess=True; an async hook cannot be offloaded to a "
                                "worker thread"
                            )
                        return await asyncio.to_thread(
                            self.postprocess, ctx, inputs, output, result
                        )
                    processed = self.postprocess(ctx, inputs, output, result)
                    if inspect.isawaitable(processed):
                        processed = await processed
                    return processed
                except TendError as err:
                    err.with_context(
                        agent=self.id,
                        call_id=result.call_id,
                        transcript_ref=result.transcript_ref,
                        diagnostics_ref=result.diagnostics_ref,
                    )
                    if not err.retryable or attempt >= self.contract_retries:
                        raise
                    ctx.progress and ctx.progress.retry_task(
                        _active_task_id(ctx) or _agent_task_id(self.id, ctx, inputs),
                        detail=f"postprocess retry {attempt + 1}",
                    )
                    ctx.log.warning(
                        "agent_postprocess_retry",
                        agent=self.id,
                        attempt=attempt + 1,
                        error_type=type(err).__name__,
                        anomaly=err.anomaly.value if err.anomaly else None,
                        reason=err.message,
                        context=err.context,
                        transcript_ref=result.transcript_ref,
                        diagnostics_ref=result.diagnostics_ref,
                    )
                    messages = messages + [
                        {"role": "assistant", "content": result.text},
                        {
                            "role": "user",
                            "content": (
                                "Your output passed JSON/schema validation but failed "
                                "deterministic postprocess:\n"
                                f"  - {type(err).__name__}: {err.message}\n"
                                "Return a corrected JSON object."
                            ),
                        },
                    ]
                    continue
            if attempt >= self.contract_retries:
                raise ContractViolationError(
                    "agent output failed semantic contract",
                    context={"agent": self.id, "violations": violations,
                             "transcript_ref": result.transcript_ref,
                             "diagnostics_ref": result.diagnostics_ref},
                )
            ctx.progress and ctx.progress.retry_task(
                _active_task_id(ctx) or _agent_task_id(self.id, ctx, inputs),
                detail=f"contract retry {attempt + 1}")
            ctx.log.warning("agent_contract_retry", agent=self.id, attempt=attempt + 1,
                            violations=violations,
                            call_id=result.call_id,
                            transcript_ref=result.transcript_ref,
                            diagnostics_ref=result.diagnostics_ref)
            messages = messages + [
                {"role": "assistant", "content": result.text},
                {"role": "user", "content": "Your output violated these requirements:\n"
                 + "\n".join(f"  - {v}" for v in violations)
                 + "\nReturn a corrected JSON object."},
            ]
        raise ContractViolationError("unreachable", context={"agent": self.id})

    def postprocess(
        self, ctx: AgentContext, inputs: dict[str, Any], output: dict[str, Any],
        result: Any,
    ) -> dict[str, Any]:
        """Hook to enrich/annotate the validated output before returning. Default: passthrough."""
        return output


def _attach_presend_prompt_diagnostics(
    agent: LLMAgent,
    ctx: AgentContext,
    inputs: dict[str, Any],
    err: TendError,
) -> None:
    """Persist a synthetic transcript for failures before LLMClient sees messages."""
    call_id = f"prompt-{uuid4().hex[:12]}"
    model = ctx.settings.llm.model_for(agent.id)
    try:
        ref = ctx.log.save_transcript(agent.id, call_id, {
            "model": model,
            "messages": [],
            "attempts": [],
            "failed": True,
            "prompt_build_failed": True,
            "prompt_file": agent.prompt_file,
            "input_keys": sorted(str(key) for key in inputs),
            "input_preview": _json_preview(inputs),
            "error": err.to_record(),
        })
    except Exception as exc:
        err.with_context(
            diagnostics_write_failed=True,
            diagnostics_write_error_type=type(exc).__name__,
            diagnostics_write_error=str(exc),
        )
        try:
            ctx.log.warning(
                "prompt_diagnostics_write_failed",
                agent=agent.id,
                call_id=call_id,
                error_type=type(exc).__name__,
                message=str(exc),
            )
        except Exception:
            pass
        return
    err.with_context(
        call_id=call_id,
        model=model,
        transcript_ref=ref,
        diagnostics_ref=ref[:-3] + ".diagnostics.json" if ref.endswith(".md") else ref,
    )


def _json_preview(value: Any, limit: int = 20000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        text = repr(value)
    if len(text) <= limit:
        return text
    return text[: limit - 80] + "\n... [truncated prompt-build input preview]"


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def _active_task_id(ctx: AgentContext) -> str | None:
    task_id = ctx.extra.get("_task_id")
    return str(task_id) if task_id else None


def _agent_task_id(agent_id: str, ctx: AgentContext, inputs: dict[str, Any]) -> str:
    parts = [agent_id, ctx.db_id, _s(ctx.record_id)]
    identity = _work_item_identity(ctx, inputs)
    if identity:
        parts.append(identity)
    return ":".join(_task_id_part(p) for p in parts if _s(p)) or agent_id


def _work_item_identity(ctx: AgentContext, inputs: dict[str, Any]) -> str:
    explicit = (
        ctx.work_item_id
        or ctx.extra.get("work_item_id")
        or inputs.get("work_item_id")
        or inputs.get("task_item_id")
        or inputs.get("item_id")
    )
    if explicit is not None:
        return f"item={_identity_value(explicit)}"

    fields: list[tuple[str, Any]] = []
    for key in ("collection", "stage_index", "stage_name", "stage"):
        if key in inputs and inputs[key] is not None:
            fields.append((key, inputs[key]))
    return ",".join(f"{key}={_identity_value(value)}" for key, value in fields)


def _identity_value(value: Any) -> str:
    if isinstance(value, dict):
        if len(value) == 1:
            return _task_id_part(next(iter(value)))
        keys = ",".join(sorted(str(k) for k in value)[:4])
        return _task_id_part(keys or "dict")
    if isinstance(value, (list, tuple)):
        return _task_id_part(",".join(_identity_value(v) for v in value[:4]))
    return _task_id_part(value)


def _task_id_part(value: Any, limit: int = 80) -> str:
    text = str(value)
    cleaned = "".join(ch if ch.isalnum() or ch in "._=-" else "_" for ch in text)
    cleaned = cleaned.strip("_")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit]
    return cleaned or "unknown"
