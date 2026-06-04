"""SMART-EG file artifacts."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

MAX_INLINE_JSON_CHARS = 12_000
MAX_INLINE_TEXT_CHARS = 12_000


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _slug(value: Any, *, fallback: str = "unknown", max_len: int = 80) -> str:
    text = str(value if value is not None else fallback).strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    if not text:
        text = fallback
    return text[:max_len].strip("_") or fallback


def build_session_id(
    *,
    stage: str,
    task: str,
    db_id: str | None = None,
    record_id: str | int | None = None,
) -> str:
    """Build a readable SMART-EG agent artifact name."""

    parts = [_slug(stage), _slug(task)]
    if db_id is not None:
        parts.append(_slug(db_id))
    if record_id is not None:
        parts.append(f"record_{_slug(record_id)}")
    parts.extend([_timestamp_slug(), uuid4().hex[:8]])
    return "_".join(parts)


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _json_block(value: Any) -> list[str]:
    return [
        "```json",
        json.dumps(value, indent=2, ensure_ascii=False, default=str),
        "```",
        "",
    ]


def _append_table(lines: list[str], rows: list[tuple[str, Any]]) -> None:
    visible = [(key, value) for key, value in rows if value is not None]
    if not visible:
        return
    lines += ["| Field | Value |", "|-------|-------|"]
    for key, value in visible:
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, default=str)
        else:
            rendered = str(value)
        escaped = rendered.replace("|", "\\|")
        lines.append(f"| {key} | {escaped} |")
    lines.append("")


def _append_metric_table(lines: list[str], rows: list[tuple[str, Any]]) -> None:
    visible = [(key, value) for key, value in rows if value is not None]
    if not visible:
        return
    lines += ["| Metric | Value |", "|--------|-------|"]
    for key, value in visible:
        rendered = _inline_value(value)
        escaped = rendered.replace("|", "\\|")
        lines.append(f"| {key} | {escaped} |")
    lines.append("")


def _append_quote(lines: list[str], text: str) -> None:
    if not text:
        lines += ["> (empty)", ""]
        return
    text = _truncate_text(text, MAX_INLINE_TEXT_CHARS)
    for raw_line in text.splitlines():
        lines.append(f"> {raw_line}" if raw_line else ">")
    lines.append("")


def _append_json_complete(lines: list[str], value: Any) -> None:
    lines += ["```json", json.dumps(value, indent=2, ensure_ascii=False, default=str), "```", ""]


def _tool_name(event: dict[str, Any]) -> str:
    return str(event.get("tool") or event.get("name") or "unknown_tool")


def _tool_arguments(event: dict[str, Any]) -> Any:
    if "arguments" in event:
        return event["arguments"]
    if "args" in event:
        return event["args"]
    return {}


def _inline_value(value: Any, *, max_chars: int = 180) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) > max_chars:
        return f"{text[: max_chars - 1]}..."
    return text


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}\n... truncated {omitted} chars; see sidecar/raw event for full payload ..."


def _format_cost_value(value: Any) -> str | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == 0:
        return "unknown"
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _assistant_message(response: dict[str, Any] | None) -> dict[str, Any]:
    if not response:
        return {}
    message = response.get("assistant_message")
    return message if isinstance(message, dict) else {}


def _assistant_reasoning(response: dict[str, Any] | None) -> str:
    message = _assistant_message(response)
    reasoning = message.get("reasoning") or message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    details = message.get("reasoning_details")
    if isinstance(details, list):
        chunks = [
            str(item.get("text"))
            for item in details
            if isinstance(item, dict) and item.get("text")
        ]
        if chunks:
            return "\n".join(chunks)
    return ""


def _assistant_content(response: dict[str, Any] | None) -> str:
    message = _assistant_message(response)
    content = message.get("content") if message else None
    if not content and response:
        content = response.get("content") or response.get("response_text")
    return str(content) if isinstance(content, str) and content.strip() else ""


def _append_tool_result_payload(lines: list[str], tool_name: str, content: Any) -> None:
    lines += ["", f"> ### Tool Result: `{tool_name}`", ">"]
    if isinstance(content, dict):
        scalar_rows: list[tuple[str, Any]] = []
        for key, value in content.items():
            if isinstance(value, (dict, list)):
                continue
            scalar_rows.append((str(key), value))
            if len(scalar_rows) >= 8:
                break
        if scalar_rows:
            table_lines: list[str] = []
            _append_table(table_lines, scalar_rows)
            for line in table_lines:
                lines.append(f"> {line}" if line else ">")
    lines += [">", "> **Payload**", ">"]
    rendered = json.dumps(content, indent=2, ensure_ascii=False, default=str)
    rendered = _truncate_text(rendered, MAX_INLINE_JSON_CHARS)
    lines.append("> ```json")
    for raw_line in rendered.splitlines():
        lines.append(f"> {raw_line}" if raw_line else ">")
    lines += ["> ```", ""]


class SmartEGObserver:
    """Append-only artifact writer for one SMART-EG agent session."""

    def __init__(self, run_dir: Path, *, session_id: str | None = None) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or build_session_id(stage="solve", task="smart_eg")
        self.session_dir = self.run_dir / "solve" / "sessions" / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.agent_jsonl = self.session_dir / "agent.jsonl"
        self.agent_md = self.session_dir / "agent.md"
        self.tools_json = self.session_dir / "tools.json"
        self.evidence_path = self.session_dir / "evidence_ledger.jsonl"
        self.submit_gates_path = self.session_dir / "submit_gates.jsonl"
        self.cost_path = self.session_dir / "cost_summary.jsonl"
        self.errors_path = self.session_dir / "errors.jsonl"
        self.execution_trace_path = self.session_dir / "execution_trace.jsonl"
        self.progress_path = self.session_dir / "progress.jsonl"
        self._agent_events: list[dict[str, Any]] = []
        self._line_counts: dict[Path, int] = {}
        self._session_meta: dict[str, Any] = {}
        self._final_status: str | None = None
        self._final_state_summary: dict[str, Any] | None = None
        self._current_turn_index: int | None = None
        self._pending_error_refs: list[str] = []
        self._llm_diagnostics_refs: list[str] = []
        self._error_refs: list[str] = []

    def start_session(self, **metadata: Any) -> None:
        metadata.setdefault("started", _utcnow())
        self._session_meta.update(
            {key: value for key, value in metadata.items() if value is not None}
        )
        tools = self._session_meta.get("tools")
        if tools is not None:
            self.tools_json.write_text(
                json.dumps(tools, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        self._write_markdown()

    def set_current_turn(self, turn_index: int | None) -> None:
        self._current_turn_index = turn_index

    def agent_ref(self) -> str:
        return self._ref(self.agent_md)

    def agent_jsonl_ref(self, line_no: int | None = None) -> str:
        return self._ref(self.agent_jsonl, line_no)

    def tools_ref(self) -> str:
        return self._ref(self.tools_json)

    def transcript_refs(self) -> list[str]:
        return [self.agent_ref()]

    def diagnostics_refs(self) -> list[str]:
        return list(self._llm_diagnostics_refs)

    def error_refs(self) -> list[str]:
        return list(self._error_refs)

    def evidence_ref(self) -> str:
        return self._ref(self.evidence_path)

    def execution_trace_ref(self) -> str:
        return self._ref(self.execution_trace_path)

    def agent_event(self, event: str, payload: dict[str, Any] | None = None) -> str:
        fields = dict(payload or {})
        if self._current_turn_index is not None and "turn_index" not in fields:
            fields["turn_index"] = self._current_turn_index
        record = {"ts": _utcnow(), "event": event, **fields}
        line_no = self._append_jsonl(self.agent_jsonl, record)
        self._agent_events.append(record)
        if event == "llm_response":
            diagnostics_ref = fields.get("diagnostics_ref")
            if isinstance(diagnostics_ref, str) and diagnostics_ref:
                self._remember_unique(self._llm_diagnostics_refs, diagnostics_ref)
        self._write_markdown()
        return self.agent_jsonl_ref(line_no)

    def record_evidence(self, payload: dict[str, Any]) -> str:
        record = {"ts": _utcnow(), "event": "evidence_recorded", **dict(payload)}
        line_no = self._append_jsonl(self.evidence_path, record)
        self.agent_event("evidence_added", {"evidence_id": record.get("evidence_id")})
        return self._ref(self.evidence_path, line_no)

    def record_submit_gate(self, payload: dict[str, Any]) -> str:
        record = {"ts": _utcnow(), **dict(payload)}
        line_no = self._append_jsonl(self.submit_gates_path, record)
        ref = self._ref(self.submit_gates_path, line_no)
        self.agent_event(
            "submit_gate_checked",
            {
                "submit_tool": record.get("submit_tool"),
                "accepted": record.get("accepted"),
                "gate_ref": ref,
            },
        )
        return ref

    def record_cost(self, payload: dict[str, Any]) -> str:
        record = {
            "ts": _utcnow(),
            "cost_source": "unavailable",
            **dict(payload),
        }
        line_no = self._append_jsonl(self.cost_path, record)
        return self._ref(self.cost_path, line_no)

    def record_error(self, payload: dict[str, Any]) -> str:
        record = {"ts": _utcnow(), **dict(payload)}
        line_no = self._append_jsonl(self.errors_path, record)
        ref = self._ref(self.errors_path, line_no)
        self._pending_error_refs.append(ref)
        self._remember_unique(self._error_refs, ref)
        self.agent_event(
            "error_recorded",
            {
                "error_ref": ref,
                "error_code": record.get("error_code"),
                "tool": record.get("tool"),
                "message": record.get("message"),
                "error_type": record.get("error_type"),
            },
        )
        return ref

    def consume_error_refs(self) -> list[str]:
        refs = list(self._pending_error_refs)
        self._pending_error_refs.clear()
        return refs

    def record_execution_trace(self, payload: dict[str, Any]) -> str:
        record = {"ts": _utcnow(), **dict(payload)}
        line_no = self._append_jsonl(self.execution_trace_path, record)
        return self._ref(self.execution_trace_path, line_no)

    def record_progress(self, payload: dict[str, Any]) -> str:
        record = {"ts": _utcnow(), "solver_id": "smart-eg", **dict(payload)}
        line_no = self._append_jsonl(self.progress_path, record)
        return self._ref(self.progress_path, line_no)

    def finalize_markdown(self, *, final_status: str, state_summary: dict[str, Any]) -> None:
        self._final_status = final_status
        self._final_state_summary = dict(state_summary)
        self.evidence_path.touch(exist_ok=True)
        self._write_markdown()

    def _write_markdown(self) -> None:
        lines = [
            f"# Agent Session: {self.session_id}",
            "",
        ]
        _append_table(
            lines,
            [
                ("Stage", self._session_meta.get("stage")),
                ("Task", self._session_meta.get("task")),
                ("Model", self._session_meta.get("model")),
                ("Started", self._session_meta.get("started")),
            ],
        )
        system_prompt = self._session_meta.get("system_prompt")
        if isinstance(system_prompt, str):
            lines += ["## System Prompt", ""]
            _append_quote(lines, system_prompt)
        user_message = self._session_meta.get("user_message")
        if isinstance(user_message, str):
            lines += ["## User Message", ""]
            _append_quote(lines, user_message)
        tools = self._session_meta.get("tools")
        if tools is not None:
            lines += ["## Tools", ""]
            _append_json_complete(lines, tools)

        lines += ["---", ""]

        session_events: list[dict[str, Any]] = []
        turns: dict[int, list[dict[str, Any]]] = {}
        for event in self._agent_events:
            turn_index = event.get("turn_index")
            if isinstance(turn_index, int):
                turns.setdefault(turn_index, []).append(event)
            elif isinstance(turn_index, str) and turn_index.isdigit():
                turns.setdefault(int(turn_index), []).append(event)
            else:
                session_events.append(event)

        session_events = [
            event for event in session_events if event.get("event") != "final_outcome"
        ]

        if session_events:
            for event in session_events:
                self._append_event(lines, event)

        for turn_index in sorted(turns):
            self._append_turn(lines, turn_index, turns[turn_index])

        if self._final_status:
            lines += ["## Session Complete", ""]
            counters = (
                self._final_state_summary.get("counters", {})
                if isinstance(self._final_state_summary, dict)
                else {}
            )
            _append_table(
                lines,
                [
                    ("Finished", _utcnow()),
                    ("Outcome", self._final_status),
                    ("Turns", counters.get("llm_turns") or len(turns)),
                    ("Tool Calls", _count_events(self._agent_events, "tool_call")),
                    ("Total Tokens", counters.get("tokens")),
                    ("Total Cost (USD)", _format_cost_value(counters.get("cost_usd"))),
                ],
            )
        lines.append("")
        self.agent_md.write_text("\n".join(lines), encoding="utf-8")

    def _append_turn(
        self,
        lines: list[str],
        turn_index: int,
        events: list[dict[str, Any]],
    ) -> None:
        max_turns = self._session_meta.get("max_turns")
        displayed_max_turns = _display_max_turns(max_turns, turn_index)
        title = (
            f"## Turn {turn_index}/{displayed_max_turns}"
            if displayed_max_turns
            else f"## Turn {turn_index}"
        )
        lines += [title, ""]

        response = _first_event(events, "llm_response")
        tool_calls = [event for event in events if event.get("event") == "tool_call"]
        tool_results = [
            event for event in events if event.get("event") == "tool_observation"
        ]
        handled = {
            "turn_start",
            "llm_request",
            "llm_response",
            "tool_call",
            "tool_observation",
            "evidence_added",
            "submit_gate_checked",
            "submit_attempt",
            "history_compacted",
        }
        other_events = [event for event in events if event.get("event") not in handled]

        reasoning = _assistant_reasoning(response)
        if reasoning:
            lines += ["### Reasoning", ""]
            _append_quote(lines, reasoning)
        content = _assistant_content(response)
        if content:
            lines += ["### Content", ""]
            _append_quote(lines, content)
        self._append_tool_calls(lines, tool_calls)
        self._append_tool_results(lines, tool_results, tool_calls)
        self._append_unhandled_turn_events(lines, other_events)
        self._append_turn_metrics(lines, response)
        lines += ["---", ""]

    def _append_tool_calls(
        self,
        lines: list[str],
        tool_calls: list[dict[str, Any]],
    ) -> None:
        if not tool_calls:
            return
        lines += ["### Tool Calls", ""]
        for event in tool_calls:
            name = _tool_name(event)
            call_id = str(event.get("tool_call_id") or "unknown_call")
            lines += [f"#### {name} (`{call_id}`)", ""]
            _append_json_complete(lines, _tool_arguments(event))

    def _append_tool_results(
        self,
        lines: list[str],
        tool_results: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
    ) -> None:
        if not tool_results:
            return
        lines += ["### Tool Results", ""]
        args_by_call = {
            str(event.get("tool_call_id") or ""): _tool_arguments(event)
            for event in tool_calls
        }
        for event in tool_results:
            name = _tool_name(event)
            call_id = str(event.get("tool_call_id") or "unknown_call")
            signature = _tool_signature(name, args_by_call.get(call_id, {}))
            content = event.get("content")
            evidence_ids = _evidence_ids(content)
            error_refs = _error_refs(event, content)
            lines += [f"#### {signature} (`{call_id}`)", ""]
            _append_table(
                lines,
                [
                    ("Time", event.get("ts")),
                    ("OK", event.get("ok")),
                    ("Gate", event.get("gate_ref")),
                    ("Evidence", ", ".join(evidence_ids) if evidence_ids else None),
                    ("Error Refs", ", ".join(error_refs) if error_refs else None),
                ],
            )
            if content is None:
                content = {
                    key: value
                    for key, value in event.items()
                    if key not in {"ts", "event", "turn_index"}
                }
            _append_tool_result_payload(lines, name, content)

    def _append_unhandled_turn_events(
        self,
        lines: list[str],
        other_events: list[dict[str, Any]],
    ) -> None:
        if not other_events:
            return
        lines += ["### Context", ""]
        for event in other_events:
            lines.append(
                f"- {event.get('ts')} `{event.get('event')}` "
                f"{json.dumps(event, ensure_ascii=False, default=str)}"
            )
        lines.append("")

    def _append_turn_metrics(
        self,
        lines: list[str],
        response: dict[str, Any] | None,
    ) -> None:
        usage = response.get("usage") if response and isinstance(response.get("usage"), dict) else {}
        cost = response.get("cost") if response and isinstance(response.get("cost"), dict) else {}
        rows = [
            ("Prompt Tokens", usage.get("prompt_tokens")),
            ("Completion Tokens", usage.get("completion_tokens")),
            (
                "Cost (USD)",
                _format_cost_value(
                    cost.get("cost_usd")
                    or (response.get("cost_usd") if response else None)
                ),
            ),
        ]
        if not any(value is not None for _key, value in rows):
            return
        lines += ["### Metrics", ""]
        _append_metric_table(lines, rows)

    def _append_event(self, lines: list[str], event: dict[str, Any]) -> None:
        name = str(event.get("event") or "event")
        if name == "turn_start":
            lines += ["### Turn Start", ""]
            _append_table(
                lines,
                [
                    ("Time", event.get("ts")),
                    ("Mode", event.get("mode")),
                    ("Terminal Only", event.get("terminal_only")),
                    ("Tool Turn", event.get("tool_turn")),
                    ("Debt Count", event.get("debt_count")),
                ],
            )
            return
        if name == "tool_call":
            lines += [f"### Tool Call: {_tool_name(event)}", ""]
            _append_table(
                lines,
                [
                    ("Time", event.get("ts")),
                    ("Tool Call ID", event.get("tool_call_id")),
                ],
            )
            lines += _json_block(_tool_arguments(event))
            return
        if name == "tool_observation":
            lines += [f"### Tool Result: {_tool_name(event)}", ""]
            content = event.get("content")
            error_refs = _error_refs(event, content)
            _append_table(
                lines,
                [
                    ("Time", event.get("ts")),
                    ("Tool Call ID", event.get("tool_call_id")),
                    ("OK", event.get("ok")),
                    ("Gate", event.get("gate_ref")),
                    ("Error Refs", ", ".join(error_refs) if error_refs else None),
                ],
            )
            if content is None:
                content = {
                    key: value
                    for key, value in event.items()
                    if key not in {"ts", "event"}
                }
            lines += _json_block(content)
            return
        if name == "error_recorded":
            lines += ["### Error Recorded", ""]
            _append_table(
                lines,
                [
                    ("Time", event.get("ts")),
                    ("Code", event.get("error_code")),
                    ("Tool", event.get("tool")),
                    ("Error Type", event.get("error_type")),
                    ("Error Ref", event.get("error_ref")),
                    ("Message", event.get("message")),
                ],
            )
            return
        lines.append(
            f"- {event.get('ts')} `{name}` "
            f"{json.dumps(event, ensure_ascii=False, default=str)}"
        )
        lines.append("")

    def close(self) -> None:
        return None

    def _append_jsonl(self, path: Path, record: dict[str, Any]) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        safe = {key: _json_safe(value) for key, value in record.items()}
        previous = self._line_counts.get(path)
        if previous is None:
            previous = self._count_existing_lines(path)
        line_no = previous + 1
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(safe, ensure_ascii=False, default=str) + "\n")
        self._line_counts[path] = line_no
        return line_no

    def _ref(self, path: Path, line_no: int | None = None) -> str:
        ref = path.relative_to(self.run_dir).as_posix()
        return f"{ref}#{line_no}" if line_no is not None else ref

    def _existing_ref(self, path: Path) -> str | None:
        return self._ref(path) if path.exists() else None

    @staticmethod
    def _remember_unique(items: list[str], value: str) -> None:
        if value not in items:
            items.append(value)

    @staticmethod
    def _count_existing_lines(path: Path) -> int:
        if not path.exists():
            return 0
        return sum(1 for _line in path.read_text(encoding="utf-8").splitlines())


def _first_event(events: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for event in events:
        if event.get("event") == name:
            return event
    return None


def _count_events(events: list[dict[str, Any]], name: str) -> int:
    return sum(1 for event in events if event.get("event") == name)


def _display_max_turns(max_turns: Any, turn_index: int) -> int | None:
    if max_turns is None:
        return None
    try:
        value = int(max_turns)
    except (TypeError, ValueError):
        return None
    return max(value, turn_index)


def _tool_signature(name: str, args: Any) -> str:
    if not isinstance(args, dict) or not args:
        return f"{name}()"
    parts: list[str] = []
    for key, value in list(args.items())[:3]:
        parts.append(f"{key}={_argument_preview(value)}")
    suffix = ", ..." if len(args) > 3 else ""
    return f"{name}({', '.join(parts)}{suffix})"


def _argument_preview(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    if len(text) > 90:
        text = f"{text[:89]}..."
    if isinstance(value, str):
        return json.dumps(text, ensure_ascii=False)
    return text


def _evidence_ids(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    ids: list[str] = []
    evidence_id = value.get("evidence_id")
    if evidence_id:
        ids.append(str(evidence_id))
    evidence_ids = value.get("evidence_ids")
    if isinstance(evidence_ids, list):
        ids.extend(str(item) for item in evidence_ids if item)
    observation = value.get("observation")
    if isinstance(observation, dict):
        nested = observation.get("evidence_id")
        if nested:
            ids.append(str(nested))
    return list(dict.fromkeys(ids))


def _error_refs(event: dict[str, Any], content: Any) -> list[str]:
    refs: list[str] = []
    event_refs = event.get("error_refs")
    if isinstance(event_refs, list):
        refs.extend(str(item) for item in event_refs if item)
    elif event_refs:
        refs.append(str(event_refs))
    if isinstance(content, dict):
        content_refs = content.get("error_refs")
        if isinstance(content_refs, list):
            refs.extend(str(item) for item in content_refs if item)
        elif content_refs:
            refs.append(str(content_refs))
    return list(dict.fromkeys(refs))


class SmartEGRecorder(SmartEGObserver):
    """Runtime-facing alias around :class:`SmartEGObserver`.

    The recorder accepts a RunLogger so callers do not have to reach into logger internals
    for the run directory.
    """

    def __init__(self, logger: Any, *, session_id: str | None = None) -> None:
        run_dir = getattr(logger, "run_dir", logger)
        super().__init__(Path(run_dir), session_id=session_id)

    def agent_event(
        self,
        event: str,
        payload: dict[str, Any] | None = None,
        **fields: Any,
    ) -> str:  # type: ignore[override]
        return super().agent_event(event, {**dict(payload or {}), **fields})

    def write_evidence(self, payload: dict[str, Any]) -> str:
        self.record_evidence(payload)
        return self.evidence_ref()

    def write_submit_gate(self, payload: dict[str, Any]) -> str:
        self.record_submit_gate(payload)
        return self._ref(self.submit_gates_path)

    def write_cost_summary(self, payload: dict[str, Any]) -> str:
        self.record_cost(payload)
        return self._ref(self.cost_path)

    def write_error(self, payload: dict[str, Any]) -> str:
        self.record_error(payload)
        return self._ref(self.errors_path)

    def final_markdown(self, text: str) -> None:
        self.finalize_markdown(final_status=text, state_summary={})
