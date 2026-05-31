"""Structured, file-first logging with first-class anomaly capture.

Why file-first JSONL: the primary consumer of these logs is an operator or Claude Code
triaging a failed run. They need to ``grep`` one stream and get every failure with full
structured context — including, for LLM faults, a pointer to the exact prompt that
caused it. So:

    runs/<run_id>/
        events.jsonl              every structured event (one JSON object per line)
        anomalies.jsonl           the anomaly subset — triage starts here
        llm/<agent>/<call_id>.md   readable prompt+response transcript per LLM call
        llm/<agent>/<call_id>.diagnostics.json
                                  full structured transcript and diagnostics

Anomalies also fire subscriber callbacks (the progress UI registers one) so failures
surface on the terminal the instant they happen, not at the end of the run.

Usage::

    log = setup_logging(run_dir, console=False)
    alog = log.bind(phase="A", db_id="financial", agent="wp")
    alog.info("agent_start")
    ref = alog.save_transcript("wp", call_id, transcript_dict)
    alog.anomaly(err, transcript_ref=ref)          # -> events.jsonl + anomalies.jsonl + UI
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import structlog

from ..errors import Anomaly, TendError

AnomalyCallback = Callable[[dict[str, Any]], None]


def new_run_id(prefix: str = "run") -> str:
    """Timestamped, collision-resistant run id, e.g. ``run-20260601-013355-a1b2``."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{ts}-{uuid4().hex[:4]}"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _diagnostics_ref_from_transcript(transcript_ref: str) -> str:
    if transcript_ref.endswith(".md"):
        return f"{transcript_ref[:-3]}.diagnostics.json"
    return transcript_ref


def _json_dumps(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent, default=str)


def _table_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = _json_dumps(value)
    else:
        text = str(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def _append_table(lines: list[str], rows: list[tuple[str, Any]]) -> None:
    lines += ["| Field | Value |", "|-------|-------|"]
    for key, value in rows:
        if value is not None:
            lines.append(f"| {key} | {_table_value(value)} |")
    lines.append("")


def _stringify_payload(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return _json_dumps(value, indent=2)


def _append_blockquote(lines: list[str], text: str) -> None:
    if text == "":
        lines.append("> (empty)")
    else:
        for line in text.splitlines():
            lines.append(f"> {line}" if line else ">")
    lines.append("")


def _try_pretty_json_text(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except (TypeError, json.JSONDecodeError):
        return None
    return _json_dumps(parsed, indent=2)


def _append_content(
    lines: list[str],
    title: str,
    value: Any,
    *,
    prefer_json: bool = False,
) -> None:
    text = _stringify_payload(value)
    if text == "":
        return
    lines += [title, ""]
    pretty = (
        _try_pretty_json_text(text)
        if prefer_json or text.lstrip().startswith(("{", "["))
        else None
    )
    if pretty is not None:
        lines += ["```json", pretty, "```", ""]
    else:
        _append_blockquote(lines, text)


def _role_title(role: Any) -> str:
    raw = str(role or "unknown").strip() or "unknown"
    return raw[:1].upper() + raw[1:]


def _append_messages(lines: list[str], messages: list[dict[str, Any]]) -> None:
    lines += ["## Messages", ""]
    if not messages:
        lines += ["> (no messages recorded)", ""]
        return

    seen: dict[str, int] = {}
    for message in messages:
        role = str(message.get("role") or "unknown").lower()
        seen[role] = seen.get(role, 0) + 1
        suffix = f" ({seen[role]})" if seen[role] > 1 else ""
        lines += [f"### {_role_title(role)}{suffix}", ""]

        if message.get("name"):
            _append_table(lines, [("Name", message.get("name"))])

        reasoning = (
            message.get("reasoning")
            or message.get("reasoning_content")
            or message.get("reasoning_raw")
        )
        if reasoning:
            _append_content(lines, "#### Reasoning", reasoning)
        _append_blockquote(lines, _stringify_payload(message.get("content", "")))

        tool_calls = message.get("tool_calls")
        if tool_calls:
            lines += [
                "#### Tool Calls",
                "",
                "```json",
                _json_dumps(tool_calls, indent=2),
                "```",
                "",
            ]


def _attempt_rows(attempts: list[dict[str, Any]]) -> list[str]:
    rows = [
        "| Attempt | Kind | Finish | Latency (s) | Anomaly |",
        "|---------|------|--------|-------------|---------|",
    ]
    for item in attempts:
        error = item.get("error") or item.get("validation_error") or {}
        anomaly = error.get("anomaly") or ""
        rows.append(
            "| "
            f"{_table_value(item.get('attempt', ''))} | "
            f"{_table_value(item.get('kind', ''))} | "
            f"{_table_value(item.get('finish_reason', ''))} | "
            f"{_table_value(item.get('latency_s', ''))} | "
            f"{_table_value(anomaly)} |"
        )
    rows.append("")
    return rows


def _render_llm_transcript_markdown(payload: dict[str, Any]) -> str:
    call_id = payload.get("call_id", "unknown")
    status = (
        "failed"
        if payload.get("failed")
        else "started"
        if payload.get("started")
        else "completed"
    )
    diagnostics_ref = payload.get("diagnostics_ref")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    attempts = payload.get("attempts") if isinstance(payload.get("attempts"), list) else []
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    unexpected = (
        payload.get("unexpected_exception")
        if isinstance(payload.get("unexpected_exception"), dict)
        else {}
    )

    lines: list[str] = [f"# LLM Call: {call_id}", ""]
    _append_table(
        lines,
        [
            ("Agent", payload.get("agent")),
            ("Status", status),
            ("Model", payload.get("model")),
            ("Timestamp", payload.get("ts")),
            ("Phase", payload.get("phase") or payload.get("stage")),
            ("Database", payload.get("db_id")),
            ("Record", payload.get("record_id")),
            ("Diagnostics", diagnostics_ref),
        ],
    )

    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    _append_messages(lines, messages)

    lines += ["## Response", ""]
    _append_table(
        lines,
        [
            ("Finish Reason", payload.get("finish_reason")),
            ("Latency (s)", payload.get("latency_s")),
            ("Parsed OK", payload.get("parsed_ok")),
            ("Prompt Tokens", usage.get("prompt_tokens")),
            ("Completion Tokens", usage.get("completion_tokens")),
            ("Total Tokens", usage.get("total_tokens")),
            ("Error Type", error.get("error_type") or unexpected.get("exception_type")),
            ("Anomaly", error.get("anomaly")),
            ("Error Message", error.get("message") or unexpected.get("exception_message")),
        ],
    )

    reasoning = usage.get("reasoning_preview")
    if reasoning:
        _append_content(lines, "### Reasoning", reasoning)

    response_text = payload.get("response_text", payload.get("response"))
    if response_text is not None:
        _append_content(
            lines,
            "### Content",
            response_text,
            prefer_json=payload.get("parsed_ok") is True,
        )
    elif payload.get("failed"):
        lines += ["> (no model content recorded before failure)", ""]
    elif payload.get("started"):
        lines += ["> (response not recorded yet)", ""]

    if attempts:
        lines += ["## Attempt Summary", ""]
        lines += _attempt_rows(attempts)

    lines += [
        "## Diagnostics",
        "",
        f"Full structured payload: `{diagnostics_ref}`",
        "",
    ]
    return "\n".join(lines)


class _JsonlSink:
    """Thread-safe append-only JSONL writer (line-buffered)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self.path = path

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self._fp.write(line + "\n")

    def close(self) -> None:
        with self._lock:
            if not self._fp.closed:
                self._fp.close()


class _Run:
    """Shared run state (sinks, subscribers, transcript dir) referenced by all loggers."""

    def __init__(self, run_dir: Path, console: bool) -> None:
        self.run_dir = run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        self.events = _JsonlSink(run_dir / "events.jsonl")
        self.anomalies = _JsonlSink(run_dir / "anomalies.jsonl")
        self.llm_dir = run_dir / "llm"
        self.subscribers: list[AnomalyCallback] = []
        self.counts: dict[str, int] = {}          # anomaly kind -> count (for summaries)
        self._console = structlog.get_logger("tend") if console else None
        self._lock = threading.Lock()

    def emit_console(self, level: str, event: str, fields: dict[str, Any]) -> None:
        if self._console is not None:
            getattr(self._console, level, self._console.info)(event, **fields)


class RunLogger:
    """A context-bound logger. ``bind()`` returns a child carrying extra context.

    Cheap to bind (shares the underlying run state); bind freely per agent/db/record.
    """

    def __init__(self, run: _Run, context: dict[str, Any] | None = None) -> None:
        self._run = run
        self._ctx: dict[str, Any] = dict(context or {})

    # -- context ---------------------------------------------------------- #
    def bind(self, **fields: Any) -> "RunLogger":
        return RunLogger(self._run, {**self._ctx, **fields})

    @property
    def run_dir(self) -> Path:
        return self._run.run_dir

    @property
    def context(self) -> dict[str, Any]:
        return dict(self._ctx)

    # -- generic events --------------------------------------------------- #
    def _emit(self, level: str, event: str, fields: dict[str, Any]) -> dict[str, Any]:
        record = {"ts": _utcnow(), "level": level, "event": event, **self._ctx, **fields}
        self._run.events.write(record)
        self._run.emit_console(level, event, {**self._ctx, **fields})
        return record

    def debug(self, event: str, **fields: Any) -> None:
        self._emit("debug", event, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit("info", event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit("warning", event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit("error", event, fields)

    # -- anomalies (the triage stream) ----------------------------------- #
    def anomaly(
        self,
        err: TendError | None = None,
        *,
        kind: Anomaly | str | None = None,
        message: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Record an anomaly to events.jsonl AND anomalies.jsonl, and fire subscribers.

        Pass a :class:`TendError` (preferred — carries classification + context) or an
        explicit ``kind``/``message``. ``fields`` may include ``transcript_ref`` so the
        anomaly points at the exact prompt/response that triggered it.
        """
        base: dict[str, Any] = {}
        if err is not None:
            err.logged = True  # mark so agent/workflow wrappers don't re-log it
            err_record = err.to_record()
            base.update(err_record)
            # Keep the nested context object for structured tooling, while also
            # surfacing context fields at the top level for existing greps/tests.
            base.update(err_record.get("context", {}))
        if kind is not None:
            base["anomaly"] = kind.value if isinstance(kind, Anomaly) else str(kind)
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
        for cb in list(self._run.subscribers):
            try:
                cb(record)
            except Exception:  # a broken subscriber must never break logging
                pass
        return record

    # -- LLM transcripts -------------------------------------------------- #
    def save_transcript(self, agent: str, call_id: str, transcript: dict[str, Any]) -> str:
        """Persist an LLM call and return the human-readable transcript rel path.

        The returned path (relative to the run dir, e.g. ``llm/wp/<call_id>.md``) is the
        canonical ``transcript_ref`` to attach to events/anomalies. The Markdown file is
        optimized for human/agent triage; the sidecar ``.diagnostics.json`` preserves
        the complete structured payload for tooling.
        """
        out_dir = self._run.llm_dir / agent
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path = out_dir / f"{call_id}.md"
        diagnostics_path = out_dir / f"{call_id}.diagnostics.json"
        diagnostics_ref = str(diagnostics_path.relative_to(self._run.run_dir))
        payload = {
            "ts": _utcnow(),
            "agent": agent,
            "call_id": call_id,
            **self._ctx,
            **transcript,
            "transcript_ref": str(md_path.relative_to(self._run.run_dir)),
            "diagnostics_ref": diagnostics_ref,
        }
        diagnostics_path.write_text(_json_dumps(payload, indent=2), encoding="utf-8")
        md_path.write_text(_render_llm_transcript_markdown(payload), encoding="utf-8")
        return str(md_path.relative_to(self._run.run_dir))

    # -- subscriptions / lifecycle --------------------------------------- #
    def subscribe_anomaly(self, callback: AnomalyCallback) -> None:
        self._run.subscribers.append(callback)

    def anomaly_counts(self) -> dict[str, int]:
        with self._run._lock:
            return dict(self._run.counts)

    def close(self) -> None:
        self._run.events.close()
        self._run.anomalies.close()


def setup_logging(run_dir: Path, *, console: bool = False, level: str = "info") -> RunLogger:
    """Configure structlog (for the optional console stream) and open the run sinks."""
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
    return RunLogger(_Run(run_dir, console=console))
