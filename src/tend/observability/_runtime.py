"""Runtime state and public logger implementation for run observability."""
from __future__ import annotations

import threading
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import structlog

from ..errors import Anomaly, TendError
from ..run_ids import new_run_id
from ._formatters import _json_dumps, render_llm_transcript_markdown

AnomalyCallback = Callable[[dict[str, Any]], None]
EventCallback = Callable[[dict[str, Any]], None]


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
    ) -> None:
        self.run_dir = run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        self.events = _JsonlSink(run_dir / "events.jsonl")
        self.anomalies = _JsonlSink(run_dir / "anomalies.jsonl")
        self.llm_dir = run_dir / "llm"
        self.write_llm_markdown_transcripts = write_llm_markdown_transcripts
        self.subscribers: list[AnomalyCallback] = []
        self.event_subscribers: list[EventCallback] = []
        self.counts: dict[str, int] = {}
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
        self._run.events.write(record)
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
        """Record an anomaly to events.jsonl and anomalies.jsonl."""
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
        if transcript_ref and not fields.get("diagnostics_ref") and not base.get("diagnostics_ref"):
            diagnostics_ref = _diagnostics_ref_from_transcript(str(transcript_ref))
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
        self._run.events.write(record)
        self._run.anomalies.write(record)
        with self._run._lock:
            self._run.counts[record["anomaly"]] = self._run.counts.get(record["anomaly"], 0) + 1
        self._run.emit_console("error", f"anomaly:{record['anomaly']}", record)
        for callback in list(self._run.subscribers):
            try:
                callback(record)
            except Exception:
                pass
        return record

    def save_transcript(self, agent: str, call_id: str, transcript: dict[str, Any]) -> str:
        """Persist an LLM call and return the primary call artifact path."""
        out_dir = self._run.llm_dir / agent
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path = out_dir / f"{call_id}.md"
        diagnostics_path = out_dir / f"{call_id}.diagnostics.json"
        diagnostics_ref = str(diagnostics_path.relative_to(self._run.run_dir))
        markdown_ref = str(md_path.relative_to(self._run.run_dir))
        transcript_ref = (
            markdown_ref if self._run.write_llm_markdown_transcripts else diagnostics_ref
        )
        payload = {
            "ts": _utcnow(),
            "agent": agent,
            "call_id": call_id,
            **self._ctx,
            **transcript,
            "transcript_ref": transcript_ref,
            "diagnostics_ref": diagnostics_ref,
            "markdown_transcript_ref": (
                markdown_ref if self._run.write_llm_markdown_transcripts else None
            ),
            "markdown_transcript_enabled": self._run.write_llm_markdown_transcripts,
        }
        diagnostics_path.write_text(_json_dumps(payload, indent=2), encoding="utf-8")
        if self._run.write_llm_markdown_transcripts:
            md_path.write_text(render_llm_transcript_markdown(payload), encoding="utf-8")
        return transcript_ref

    def subscribe_anomaly(self, callback: AnomalyCallback) -> None:
        self._run.subscribers.append(callback)

    def subscribe_event(self, callback: EventCallback) -> None:
        self._run.event_subscribers.append(callback)

    def anomaly_counts(self) -> dict[str, int]:
        with self._run._lock:
            return dict(self._run.counts)

    def close(self) -> None:
        self._run.events.close()
        self._run.anomalies.close()


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
        _env_bool("TEND_LLM_TRANSCRIPT_MD", False)
        if write_llm_markdown_transcripts is None
        else write_llm_markdown_transcripts
    )
    return RunLogger(
        _Run(
            run_dir,
            console=console,
            write_llm_markdown_transcripts=write_md,
        )
    )
