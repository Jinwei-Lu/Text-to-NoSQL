"""Runtime state and public logger implementation for run observability."""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import structlog

from ..errors import Anomaly, TendError
from ..run_ids import new_run_id as new_run_id
from ._formatters import _json_dumps, render_llm_transcript_markdown

AnomalyCallback = Callable[[dict[str, Any]], None]
EventCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class RunOutcome:
    """Typed final run summary payload written before run sinks close."""

    status: str
    close_reason: str
    counts: dict[str, Any] = field(default_factory=dict)
    artifact_refs: dict[str, Any] = field(default_factory=dict)
    evaluation_status: str | None = None
    anomaly_count: int | None = None
    fields: dict[str, Any] = field(default_factory=dict)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _diagnostics_ref_from_transcript(transcript_ref: str) -> str:
    if transcript_ref.endswith(".md"):
        return f"{transcript_ref[:-3]}.diagnostics.json"
    return transcript_ref


def _llm_artifact_ref(
    agent: str,
    call_id: str,
    *,
    agent_session_ref: str | None = None,
    stage: str | None = None,
    suffix: str = ".diagnostics.json",
) -> str:
    if agent_session_ref:
        session_ref = agent_session_ref.split("#", 1)[0]
        session_parent = Path(session_ref).parent
        filename = f"{_safe_path_part(agent)}_{_safe_path_part(call_id)}{suffix}"
        return (session_parent / filename).as_posix()
    safe_stage = _safe_path_part(stage or agent)
    filename = f"{_safe_path_part(agent)}_{_safe_path_part(call_id)}{suffix}"
    return f"{safe_stage}/llm/{filename}"


def _llm_diagnostics_ref(
    agent: str,
    call_id: str,
    *,
    agent_session_ref: str | None = None,
    stage: str | None = None,
) -> str:
    return _llm_artifact_ref(
        agent,
        call_id,
        agent_session_ref=agent_session_ref,
        stage=stage,
        suffix=".diagnostics.json",
    )


def _safe_path_part(value: Any, *, fallback: str = "unknown") -> str:
    text = str(value if value is not None else fallback).strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "_", text).strip("._-")
    return text or fallback


def _is_final_transcript_payload(payload: dict[str, Any]) -> bool:
    return not payload.get("started") and (
        payload.get("failed")
        or payload.get("response_text") is not None
        or payload.get("response") is not None
        or payload.get("tool_calls") is not None
        or payload.get("usage") is not None
    )


def _cost_source_from_usage(usage: dict[str, Any], explicit: Any = None) -> str:
    if explicit:
        return str(explicit)
    token_keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    if any(int(usage.get(key, 0) or 0) > 0 for key in token_keys):
        return "token_usage_only"
    return "unavailable"


def _normalize_anomaly_kind(kind: Anomaly | str) -> tuple[str, str | None]:
    if isinstance(kind, Anomaly):
        return kind.value, None
    raw = str(kind)
    for candidate in (raw, raw.lower()):
        try:
            return Anomaly(candidate).value, None
        except ValueError:
            pass
    return Anomaly.INTERNAL.value, raw


class _JsonlSink:
    """Thread-safe append-only JSONL writer."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self.path = path

    def write(self, record: dict[str, Any]) -> None:
        line = _json_dumps(record)
        with self._lock:
            self._fp.write(line + "\n")

    def close(self) -> None:
        with self._lock:
            if not self._fp.closed:
                self._fp.close()


class _Run:
    """Shared run state referenced by all context-bound loggers."""

    def __init__(
        self,
        run_dir: Path,
        console: bool,
        *,
        write_llm_markdown_transcripts: bool,
        write_llm_markdown_transcripts_explicit: bool,
    ) -> None:
        self.run_dir = run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        self.run_log = _JsonlSink(run_dir / "run.log")
        self.errors = _JsonlSink(run_dir / "errors.jsonl")
        self.milestones = _JsonlSink(run_dir / "milestones.jsonl")
        self.costs = _JsonlSink(run_dir / "cost_summary.jsonl")
        self.summary_path = run_dir / "run_summary.json"
        self.write_llm_markdown_transcripts = write_llm_markdown_transcripts
        self.write_llm_markdown_transcripts_explicit = (
            write_llm_markdown_transcripts_explicit
        )
        self.subscribers: list[AnomalyCallback] = []
        self.event_subscribers: list[EventCallback] = []
        self.counts: dict[str, int] = {}
        self.llm_diagnostics_by_transcript_ref: dict[str, str] = {}
        self.started_at = _utcnow()
        self.error_count = 0
        self.cost_count = 0
        self._console = structlog.get_logger("tend") if console else None
        self._lock = threading.Lock()

    def emit_console(self, level: str, event: str, fields: dict[str, Any]) -> None:
        if self._console is not None:
            getattr(self._console, level, self._console.info)(event, **fields)

    def notify_event(self, record: dict[str, Any]) -> None:
        for callback in list(self.event_subscribers):
            try:
                callback(record)
            except Exception:
                pass


class RunLogger:
    """A context-bound logger. ``bind()`` returns a child carrying extra context."""

    def __init__(self, run: _Run, context: dict[str, Any] | None = None) -> None:
        self._run = run
        self._ctx: dict[str, Any] = dict(context or {})

    def bind(self, **fields: Any) -> "RunLogger":
        return RunLogger(self._run, {**self._ctx, **fields})

    @property
    def run_dir(self) -> Path:
        return self._run.run_dir

    @property
    def context(self) -> dict[str, Any]:
        return dict(self._ctx)

    def _emit(self, level: str, event: str, fields: dict[str, Any]) -> dict[str, Any]:
        record = {"ts": _utcnow(), "level": level, "event": event, **self._ctx, **fields}
        self._run.run_log.write(record)
        if level in {"info", "warning", "error", "critical"}:
            self._run.milestones.write(record)
        if level in {"error", "critical"}:
            self._write_error_index(record)
        self._run.emit_console(level, event, {**self._ctx, **fields})
        self._run.notify_event(record)
        return record

    def debug(self, event: str, **fields: Any) -> None:
        self._emit("debug", event, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit("info", event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit("warning", event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit("error", event, fields)

    def anomaly(
        self,
        err: TendError | None = None,
        *,
        kind: Anomaly | str | None = None,
        message: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Record an anomaly to the DynaDB-style run logs and error index."""
        base: dict[str, Any] = {}
        if err is not None:
            err.logged = True
            err_record = err.to_record()
            base.update(err_record)
            base.update(err_record.get("context", {}))
        if kind is not None:
            normalized, original = _normalize_anomaly_kind(kind)
            base["anomaly"] = normalized
            if original is not None:
                base["original_anomaly_kind"] = original
                base.setdefault("message", f"unregistered anomaly kind: {original}")
        if message is not None:
            base["message"] = message
        if not base.get("anomaly"):
            base["anomaly"] = Anomaly.INTERNAL.value
        base.setdefault("message", "unspecified anomaly")
        transcript_ref = fields.get("transcript_ref") or base.get("transcript_ref")
        diagnostics_explicit = "diagnostics_ref" in fields or "diagnostics_ref" in base
        if transcript_ref and not diagnostics_explicit:
            call_id = fields.get("call_id") or base.get("call_id")
            agent = fields.get("agent") or base.get("agent") or self._ctx.get("agent")
            if agent and call_id:
                diagnostics_ref = _llm_diagnostics_ref(
                    str(agent),
                    str(call_id),
                    agent_session_ref=(
                        fields.get("agent_session_ref")
                        or base.get("agent_session_ref")
                        or self._ctx.get("agent_session_ref")
                    ),
                    stage=(
                        fields.get("stage")
                        or base.get("stage")
                        or self._ctx.get("stage")
                    ),
                )
            else:
                diagnostics_ref = self._run.llm_diagnostics_by_transcript_ref.get(
                    str(transcript_ref),
                    _diagnostics_ref_from_transcript(str(transcript_ref)),
                )
            fields["diagnostics_ref"] = diagnostics_ref
            context = base.get("context")
            if (
                isinstance(context, dict)
                and context.get("transcript_ref")
                and not context.get("diagnostics_ref")
            ):
                context["diagnostics_ref"] = diagnostics_ref

        record = {
            "ts": _utcnow(),
            "level": "error",
            "event": "anomaly",
            **self._ctx,
            **base,
            **fields,
        }
        self._run.run_log.write(record)
        self._run.milestones.write(record)
        with self._run._lock:
            self._run.counts[record["anomaly"]] = self._run.counts.get(record["anomaly"], 0) + 1
        self._write_error_index(record)
        self._run.emit_console("error", f"anomaly:{record['anomaly']}", record)
        self._run.notify_event(record)
        for callback in list(self._run.subscribers):
            try:
                callback(record)
            except Exception:
                pass
        return record

    def record_error(self, event: str = "error_recorded", **fields: Any) -> dict[str, Any]:
        """Record a non-exception solver/tool error into the central error index."""
        record = {"ts": _utcnow(), "level": "error", "event": event, **self._ctx, **fields}
        self._run.run_log.write(record)
        self._run.milestones.write(record)
        indexed = self._write_error_index(record)
        self._run.emit_console("error", event, record)
        self._run.notify_event(record)
        return indexed

    def record_llm_cost(
        self,
        *,
        agent: str,
        call_id: str,
        model: str | None,
        usage: dict[str, Any] | None = None,
        cost_usd: float | None = None,
        cost_source: str | None = None,
        transcript_ref: str | None = None,
        diagnostics_ref: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Append a DynaDB-style run-level LLM usage/cost row."""
        usage = usage or {}
        record = {
            "ts": _utcnow(),
            "event": "llm_cost",
            "agent": agent,
            "call_id": call_id,
            "model": model,
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
            "cost_usd": cost_usd,
            "cost_source": _cost_source_from_usage(usage, cost_source),
            "transcript_ref": transcript_ref,
            "diagnostics_ref": diagnostics_ref,
            **self._ctx,
            **fields,
        }
        with self._run._lock:
            self._run.cost_count += 1
            record["cost_index"] = self._run.cost_count
        self._run.costs.write(record)
        return record

    def save_transcript(self, agent: str, call_id: str, transcript: dict[str, Any]) -> str:
        """Persist an LLM call and return the primary call artifact path."""
        raw_agent_session_ref = (
            self._ctx.get("agent_session_ref") or transcript.get("agent_session_ref")
        )
        agent_session_ref = str(raw_agent_session_ref) if raw_agent_session_ref else None
        ctx_write_markdown = self._ctx.get("write_llm_markdown_transcript")
        write_markdown = (
            bool(ctx_write_markdown)
            if ctx_write_markdown is not None
            else self._run.write_llm_markdown_transcripts
            and (not agent_session_ref or self._run.write_llm_markdown_transcripts_explicit)
        )
        diagnostics_ref = _llm_diagnostics_ref(
            agent,
            call_id,
            agent_session_ref=agent_session_ref,
            stage=self._ctx.get("stage"),
        )
        markdown_suffix = ".debug.md" if agent_session_ref else ".md"
        markdown_ref = _llm_artifact_ref(
            agent,
            call_id,
            agent_session_ref=agent_session_ref,
            stage=self._ctx.get("stage"),
            suffix=markdown_suffix,
        )
        diagnostics_path = self._run.run_dir / diagnostics_ref
        diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
        md_path = self._run.run_dir / markdown_ref
        transcript_ref = agent_session_ref or (
            markdown_ref if write_markdown else diagnostics_ref
        )
        payload = {
            "ts": _utcnow(),
            "agent": agent,
            "call_id": call_id,
            **self._ctx,
            **transcript,
            "agent_session_ref": agent_session_ref,
            "transcript_ref": transcript_ref,
            "diagnostics_ref": diagnostics_ref,
            "markdown_transcript_ref": (
                markdown_ref if write_markdown else None
            ),
            "markdown_transcript_enabled": write_markdown,
        }
        diagnostics_path.write_text(_json_dumps(payload, indent=2), encoding="utf-8")
        with self._run._lock:
            self._run.llm_diagnostics_by_transcript_ref[transcript_ref] = diagnostics_ref
            if write_markdown:
                self._run.llm_diagnostics_by_transcript_ref[markdown_ref] = diagnostics_ref
        if write_markdown:
            md_path.write_text(render_llm_transcript_markdown(payload), encoding="utf-8")
        if _is_final_transcript_payload(payload):
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            cost = payload.get("cost") if isinstance(payload.get("cost"), dict) else {}
            raw_cost = payload.get("cost_usd", cost.get("cost_usd"))
            try:
                cost_usd = float(raw_cost) if raw_cost is not None else None
            except (TypeError, ValueError):
                cost_usd = None
            self.record_llm_cost(
                agent=agent,
                call_id=call_id,
                model=payload.get("model"),
                usage=usage,
                cost_usd=cost_usd,
                cost_source=(
                    payload.get("cost_source")
                    or cost.get("cost_source")
                    or cost.get("source")
                ),
                transcript_ref=transcript_ref,
                diagnostics_ref=diagnostics_ref,
            )
        return transcript_ref

    def llm_diagnostics_ref(
        self,
        agent: str,
        call_id: str,
        *,
        transcript_ref: str | None = None,
    ) -> str:
        if transcript_ref:
            with self._run._lock:
                mapped = self._run.llm_diagnostics_by_transcript_ref.get(transcript_ref)
            if mapped:
                return mapped
        return _llm_diagnostics_ref(
            agent,
            call_id,
            agent_session_ref=self._ctx.get("agent_session_ref"),
            stage=self._ctx.get("stage"),
        )

    def create_child(self, **fields: Any) -> "RunLogger":
        """Return an isolated child logger for a concurrent branch/session."""
        base_context = {
            key: value
            for key, value in self._ctx.items()
            if key not in {"agent_session_ref", "session_id"}
        }
        child_fields = {
            key: value
            for key, value in fields.items()
            if value is not None and key not in {"agent_session_ref", "session_id"}
        }
        return RunLogger(self._run, {**base_context, **child_fields})

    def subscribe_anomaly(self, callback: AnomalyCallback) -> None:
        self._run.subscribers.append(callback)

    def subscribe_event(self, callback: EventCallback) -> None:
        self._run.event_subscribers.append(callback)

    def anomaly_counts(self) -> dict[str, int]:
        with self._run._lock:
            return dict(self._run.counts)

    def finalizer(self) -> "RunFinalizer":
        return RunFinalizer(self)

    def write_run_summary(self, *, outcome: str, **fields: Any) -> None:
        payload = {
            "run_dir": str(self._run.run_dir),
            "outcome": outcome,
            "started_at": self._run.started_at,
            "finished_at": _utcnow(),
            "error_count": self._run.error_count,
            "cost_count": self._run.cost_count,
            "run_log": "run.log",
            "milestones_log": "milestones.jsonl",
            "errors_log": "errors.jsonl",
            "cost_summary": "cost_summary.jsonl",
            **self._ctx,
            **fields,
        }
        self._run.summary_path.write_text(
            _json_dumps(payload, indent=2),
            encoding="utf-8",
        )

    def close(self) -> None:
        if not self._run.summary_path.exists():
            self.write_run_summary(outcome="closed_without_outcome")
        self._run.run_log.close()
        self._run.errors.close()
        self._run.milestones.close()
        self._run.costs.close()

    def _write_error_index(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = dict(record)
        with self._run._lock:
            self._run.error_count += 1
            payload["error_index"] = self._run.error_count
        self._run.errors.write(payload)
        return payload


class RunFinalizer:
    """Write exactly one typed run outcome summary for a run logger."""

    def __init__(self, logger: RunLogger) -> None:
        self._logger = logger
        self._finished = False

    def finish(self, outcome: RunOutcome | None = None, **fields: Any) -> None:
        if self._finished or self._logger._run.summary_path.exists():
            return
        if outcome is None:
            outcome = RunOutcome(
                status=str(fields.pop("status")),
                close_reason=str(fields.pop("close_reason", "completed")),
                counts=dict(fields.pop("counts", {}) or {}),
                artifact_refs=dict(fields.pop("artifact_refs", {}) or {}),
                evaluation_status=fields.pop("evaluation_status", None),
                anomaly_count=fields.pop("anomaly_count", None),
                fields=dict(fields),
            )
        payload = {
            "status": outcome.status,
            "close_reason": outcome.close_reason,
            "counts": outcome.counts,
            "artifact_refs": outcome.artifact_refs,
            "evaluation_status": outcome.evaluation_status,
            "anomaly_count": outcome.anomaly_count,
            **outcome.fields,
        }
        self._logger.write_run_summary(outcome=outcome.status, **payload)
        self._finished = True


def setup_logging(
    run_dir: Path,
    *,
    console: bool = False,
    level: str = "info",
    write_llm_markdown_transcripts: bool | None = None,
) -> RunLogger:
    """Configure structlog and open the run sinks."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            {"debug": 10, "info": 20, "warning": 30, "error": 40}.get(level, 20)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    write_md = (
        _env_bool("TEND_LLM_TRANSCRIPT_MD", True)
        if write_llm_markdown_transcripts is None
        else write_llm_markdown_transcripts
    )
    return RunLogger(
        _Run(
            run_dir,
            console=console,
            write_llm_markdown_transcripts=write_md,
            write_llm_markdown_transcripts_explicit=(
                write_llm_markdown_transcripts is not None
            ),
        )
    )
