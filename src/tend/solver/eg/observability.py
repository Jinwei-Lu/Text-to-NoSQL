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
TOOL_NAME_PREVIEW_LIMIT = 14


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


def _append_json_preview(lines: list[str], value: Any, *, max_chars: int = MAX_INLINE_JSON_CHARS) -> None:
    rendered = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    truncated = _truncate_text(rendered, max_chars)
    lines += ["```json", truncated, "```", ""]


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


def _tool_schema_names(tools: Any) -> list[str]:
    if not isinstance(tools, list):
        return []
    names: list[str] = []
    for item in tools:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if isinstance(function, dict) and function.get("name"):
            names.append(str(function["name"]))
    return names


def _format_tool_names(names: list[str]) -> str:
    if len(names) <= TOOL_NAME_PREVIEW_LIMIT:
        return ", ".join(names)
    head = ", ".join(names[:TOOL_NAME_PREVIEW_LIMIT])
    return f"{head}, ... +{len(names) - TOOL_NAME_PREVIEW_LIMIT} more"


class SmartEGObserver:
    """Append-only artifact writer for one SMART-EG agent session."""

    def __init__(self, run_dir: Path, *, session_id: str | None = None) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or build_session_id(stage="solve", task="smart_eg")
        self.agent_dir = self.run_dir / "agent"
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        self.agent_jsonl = self.agent_dir / f"{self.session_id}.jsonl"
        self.agent_md = self.agent_dir / f"{self.session_id}.md"
        self.tools_json = self.agent_dir / f"{self.session_id}.tools.json"
        self.evidence_path = self.run_dir / "evidence_ledger.jsonl"
        self.submit_gates_path = self.run_dir / "submit_gates.jsonl"
        self.cost_path = self.run_dir / "cost_summary.jsonl"
        self.errors_path = self.run_dir / "errors.jsonl"
        self.execution_trace_path = self.run_dir / "execution_trace.jsonl"
        self.progress_path = self.run_dir / "progress.jsonl"
        self._agent_events: list[dict[str, Any]] = []
        self._line_counts: dict[Path, int] = {}
        self._session_meta: dict[str, Any] = {}
        self._final_status: str | None = None
        self._final_state_summary: dict[str, Any] | None = None
        self._current_turn_index: int | None = None

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
        return f"agent/{self.session_id}.md"

    def agent_jsonl_ref(self, line_no: int | None = None) -> str:
        ref = f"agent/{self.session_id}.jsonl"
        return f"{ref}#{line_no}" if line_no is not None else ref

    def tools_ref(self) -> str:
        return f"agent/{self.session_id}.tools.json"

    def evidence_ref(self) -> str:
        return "evidence_ledger.jsonl"

    def execution_trace_ref(self) -> str:
        return "execution_trace.jsonl"

    def agent_event(self, event: str, payload: dict[str, Any] | None = None) -> str:
        fields = dict(payload or {})
        if self._current_turn_index is not None and "turn_index" not in fields:
            fields["turn_index"] = self._current_turn_index
        record = {"ts": _utcnow(), "event": event, **fields}
        line_no = self._append_jsonl(self.agent_jsonl, record)
        self._agent_events.append(record)
        self._write_markdown()
        return self.agent_jsonl_ref(line_no)

    def record_evidence(self, payload: dict[str, Any]) -> str:
        record = {"ts": _utcnow(), "event": "evidence_recorded", **dict(payload)}
        line_no = self._append_jsonl(self.evidence_path, record)
        self.agent_event("evidence_added", {"evidence_id": record.get("evidence_id")})
        return f"evidence_ledger.jsonl#{line_no}"

    def record_submit_gate(self, payload: dict[str, Any]) -> str:
        record = {"ts": _utcnow(), **dict(payload)}
        line_no = self._append_jsonl(self.submit_gates_path, record)
        ref = f"submit_gates.jsonl#{line_no}"
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
        return f"cost_summary.jsonl#{line_no}"

    def record_error(self, payload: dict[str, Any]) -> str:
        record = {"ts": _utcnow(), **dict(payload)}
        line_no = self._append_jsonl(self.errors_path, record)
        return f"errors.jsonl#{line_no}"

    def record_execution_trace(self, payload: dict[str, Any]) -> str:
        record = {"ts": _utcnow(), **dict(payload)}
        line_no = self._append_jsonl(self.execution_trace_path, record)
        return f"execution_trace.jsonl#{line_no}"

    def record_progress(self, payload: dict[str, Any]) -> str:
        record = {"ts": _utcnow(), "solver_id": "smart-eg", **dict(payload)}
        line_no = self._append_jsonl(self.progress_path, record)
        return f"progress.jsonl#{line_no}"

    def finalize_markdown(self, *, final_status: str, state_summary: dict[str, Any]) -> None:
        self._final_status = final_status
        self._final_state_summary = dict(state_summary)
        self._write_markdown()

    def _write_markdown(self) -> None:
        status = self._final_status or "running"
        lines = [
            f"# Agent Session: {self.session_id}",
            "",
            f"- Status: {status}",
            f"- Updated: {_utcnow()}",
            "",
        ]
        _append_table(
            lines,
            [
                ("Stage", self._session_meta.get("stage")),
                ("Task", self._session_meta.get("task")),
                ("Model", self._session_meta.get("model")),
                ("Started", self._session_meta.get("started")),
                ("DB", self._session_meta.get("db_id")),
                ("Record", self._session_meta.get("record_id")),
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
            names = _tool_schema_names(tools)
            _append_table(
                lines,
                [
                    ("Count", len(names)),
                    ("Names", _format_tool_names(names)),
                    ("Full Schemas", self.tools_ref()),
                ],
            )

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

        final_events = [event for event in session_events if event.get("event") == "final_outcome"]
        session_events = [
            event for event in session_events if event.get("event") != "final_outcome"
        ]

        if session_events:
            lines += ["## Session Events", ""]
            for event in session_events:
                self._append_event(lines, event)

        for turn_index in sorted(turns):
            self._append_turn(lines, turn_index, turns[turn_index])

        if final_events:
            lines += ["## Session Events", ""]
            for event in final_events:
                self._append_event(lines, event)

        if self._final_state_summary is not None:
            lines += ["## Final State Summary", ""]
            lines += _json_block(self._final_state_summary)

        if self._final_status:
            lines += ["## Session Complete", ""]
            counters = (
                self._final_state_summary.get("counters", {})
                if isinstance(self._final_state_summary, dict)
                else {}
            )
            evidence = (
                self._final_state_summary.get("evidence", {})
                if isinstance(self._final_state_summary, dict)
                else {}
            )
            _append_table(
                lines,
                [
                    ("Finished", _utcnow()),
                    ("Outcome", self._final_status),
                    (
                        "Terminal Reason",
                        self._final_state_summary.get("terminal_reason")
                        if isinstance(self._final_state_summary, dict)
                        else None,
                    ),
                    ("Turns", counters.get("llm_turns") or len(turns)),
                    ("Tool Calls", _count_events(self._agent_events, "tool_call")),
                    ("Evidence Records", evidence.get("evidence_records")),
                    ("Submit Rejections", counters.get("submit_rejections")),
                    ("Total Tokens", counters.get("tokens")),
                    ("Total Cost (USD)", counters.get("cost_usd")),
                    ("Agent JSONL", self.agent_jsonl_ref()),
                    ("Evidence Ledger", self.evidence_ref()),
                    ("Submit Gates", "submit_gates.jsonl"),
                    ("Execution Trace", self.execution_trace_ref()),
                    ("Cost Summary", "cost_summary.jsonl"),
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
        mode = _turn_mode(events)
        suffix = f" - {mode}" if mode else ""
        title = (
            f"## Turn {turn_index}/{max_turns}{suffix}"
            if max_turns
            else f"## Turn {turn_index}{suffix}"
        )
        lines += [title, ""]

        turn_start = _first_event(events, "turn_start")
        request = _first_event(events, "llm_request")
        response = _first_event(events, "llm_response")
        tool_calls = [event for event in events if event.get("event") == "tool_call"]
        tool_results = [
            event for event in events if event.get("event") == "tool_observation"
        ]
        deltas = [
            event
            for event in events
            if event.get("event")
            in {
                "evidence_added",
                "submit_gate_checked",
                "submit_attempt",
                "history_compacted",
            }
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

        lines += ["### Reasoning", ""]
        content = response.get("content") if response else None
        if not content and response:
            content = response.get("response_text")
        _append_quote(lines, str(content) if content else "(tool-only response)")

        self._append_llm_call(lines, turn_start, request, response)
        self._append_tool_calls(lines, tool_calls)
        self._append_tool_results(lines, tool_results, tool_calls)
        self._append_turn_deltas(lines, deltas, other_events)
        self._append_turn_metrics(lines, response)
        lines += ["---", ""]

    def _append_llm_call(
        self,
        lines: list[str],
        turn_start: dict[str, Any] | None,
        request: dict[str, Any] | None,
        response: dict[str, Any] | None,
    ) -> None:
        lines += ["### LLM Call", ""]
        exposed_tools = request.get("tools") if request else None
        tool_count = len(exposed_tools) if isinstance(exposed_tools, list) else None
        _append_table(
            lines,
            [
                ("Turn Start", turn_start.get("ts") if turn_start else None),
                ("Mode", turn_start.get("mode") if turn_start else None),
                (
                    "Terminal Only",
                    turn_start.get("terminal_only") if turn_start else None,
                ),
                ("Tool Turn", turn_start.get("tool_turn") if turn_start else None),
                ("Debt Count", turn_start.get("debt_count") if turn_start else None),
                ("Request Time", request.get("ts") if request else None),
                ("Response Time", response.get("ts") if response else None),
                ("Call ID", response.get("call_id") if response else None),
                ("Model", response.get("model") if response else None),
                (
                    "Markdown Transcript",
                    _markdown_transcript_ref(response) if response else None,
                ),
                ("Diagnostics", response.get("diagnostics_ref") if response else None),
                (
                    "Finish Reason",
                    response.get("finish_reason") if response else None,
                ),
                ("Latency (s)", response.get("latency_s") if response else None),
                ("Tool Choice", request.get("tool_choice") if request else None),
                ("Exposed Tool Count", tool_count),
                (
                    "Exposed Tools",
                    _format_tool_names([str(item) for item in exposed_tools])
                    if isinstance(exposed_tools, list)
                    else None,
                ),
            ],
        )
        if request and request.get("messages") is not None:
            lines += ["#### Provider Request Messages", ""]
            _append_json_complete(lines, request.get("messages"))
        if request and request.get("tool_schemas") is not None:
            lines += ["#### Provider Tool Schemas", ""]
            _append_json_complete(lines, request.get("tool_schemas"))
        if response and response.get("assistant_message") is not None:
            lines += ["#### Provider Assistant Message", ""]
            _append_json_complete(lines, response.get("assistant_message"))
        elif response and response.get("tool_calls") is not None:
            lines += ["#### Normalized Assistant Tool Calls", ""]
            _append_json_complete(lines, response.get("tool_calls"))

    def _append_tool_calls(
        self,
        lines: list[str],
        tool_calls: list[dict[str, Any]],
    ) -> None:
        lines += ["### Tool Calls", ""]
        if not tool_calls:
            _append_quote(lines, "(none)")
            return
        for event in tool_calls:
            name = _tool_name(event)
            call_id = str(event.get("tool_call_id") or "unknown_call")
            lines += [f"#### {name} (`{call_id}`)", ""]
            if event.get("raw_tool_call") is not None:
                lines += ["##### Provider Tool Call", ""]
                _append_json_complete(lines, event.get("raw_tool_call"))
                lines += ["##### Parsed Arguments", ""]
            _append_json_complete(lines, _tool_arguments(event))

    def _append_tool_results(
        self,
        lines: list[str],
        tool_results: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
    ) -> None:
        lines += ["### Tool Results", ""]
        if not tool_results:
            _append_quote(lines, "(none)")
            return
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
            lines += [f"#### {signature} (`{call_id}`)", ""]
            _append_table(
                lines,
                [
                    ("Time", event.get("ts")),
                    ("OK", event.get("ok")),
                    ("Gate", event.get("gate_ref")),
                    ("Evidence", ", ".join(evidence_ids) if evidence_ids else None),
                ],
            )
            if content is None:
                content = {
                    key: value
                    for key, value in event.items()
                    if key not in {"ts", "event", "turn_index"}
                }
            _append_json_complete(lines, content)

    def _append_turn_deltas(
        self,
        lines: list[str],
        deltas: list[dict[str, Any]],
        other_events: list[dict[str, Any]],
    ) -> None:
        if not deltas and not other_events:
            return
        lines += ["### Evidence / Gate Delta", ""]
        evidence_ids = [
            str(event.get("evidence_id"))
            for event in deltas
            if event.get("event") == "evidence_added" and event.get("evidence_id")
        ]
        if evidence_ids:
            lines.append(f"- Evidence added: {', '.join(evidence_ids)}")
        for event in deltas:
            name = str(event.get("event"))
            if name == "submit_gate_checked":
                lines.append(
                    "- Submit gate: "
                    f"{event.get('submit_tool')} accepted={event.get('accepted')} "
                    f"ref={event.get('gate_ref')}"
                )
            elif name == "submit_attempt":
                lines.append(
                    "- Submit attempt: "
                    f"{event.get('submit_tool')} accepted={event.get('accepted')}"
                )
            elif name == "history_compacted":
                lines.append(
                    "- History compacted: "
                    f"reason={event.get('reason')} "
                    f"required_next_tool={event.get('required_next_tool')} "
                    f"message_count={event.get('message_count')}"
                )
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
        lines += ["### Metrics", ""]
        _append_metric_table(
            lines,
            [
                ("Prompt Tokens", usage.get("prompt_tokens")),
                ("Completion Tokens", usage.get("completion_tokens")),
                ("Total Tokens", usage.get("total_tokens")),
                (
                    "Cost (USD)",
                    cost.get("cost_usd")
                    or (response.get("cost_usd") if response else None),
                ),
                (
                    "Cost Source",
                    cost.get("cost_source")
                    or cost.get("source")
                    or (response.get("cost_source") if response else None),
                ),
                ("Latency (s)", response.get("latency_s") if response else None),
                ("Tool Calls", response.get("tool_call_count") if response else None),
                (
                    "Session Tokens",
                    response.get("cumulative_tokens") if response else None,
                ),
                (
                    "Session Cost (USD)",
                    response.get("cumulative_cost_usd") if response else None,
                ),
            ],
        )

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
        if name == "llm_request":
            lines += ["### LLM Request", ""]
            _append_table(
                lines,
                [
                    ("Time", event.get("ts")),
                    ("Mode", event.get("mode")),
                    ("Tools", event.get("tools")),
                    ("Tool Choice", event.get("tool_choice")),
                ],
            )
            if event.get("messages") is not None:
                lines += ["#### Provider Request Messages", ""]
                _append_json_preview(lines, event.get("messages"))
            if event.get("tool_schemas") is not None:
                lines += ["#### Provider Tool Schemas", ""]
                _append_json_preview(lines, event.get("tool_schemas"))
            return
        if name == "llm_response":
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            cost = event.get("cost") if isinstance(event.get("cost"), dict) else {}
            lines += ["### LLM Response", ""]
            _append_table(
                lines,
                [
                    ("Time", event.get("ts")),
                    ("Call ID", event.get("call_id")),
                    ("Markdown Transcript", _markdown_transcript_ref(event)),
                    ("Diagnostics", event.get("diagnostics_ref")),
                    ("Finish Reason", event.get("finish_reason")),
                    ("Latency (s)", event.get("latency_s")),
                    ("Prompt Tokens", usage.get("prompt_tokens")),
                    ("Completion Tokens", usage.get("completion_tokens")),
                    ("Total Tokens", usage.get("total_tokens")),
                    ("Cost (USD)", cost.get("cost_usd") or event.get("cost_usd")),
                    ("Cost Source", cost.get("cost_source") or cost.get("source")),
                    ("Tool Calls", event.get("tool_call_count")),
                ],
            )
            content = event.get("content") or event.get("response_text")
            if content:
                lines += ["#### Content", ""]
                _append_quote(lines, str(content))
            tool_calls = event.get("tool_calls")
            if tool_calls:
                lines += ["#### Tool Calls", ""]
                lines += _json_block(tool_calls)
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
            _append_table(
                lines,
                [
                    ("Time", event.get("ts")),
                    ("Tool Call ID", event.get("tool_call_id")),
                    ("OK", event.get("ok")),
                    ("Gate", event.get("gate_ref")),
                ],
            )
            content = event.get("content")
            if content is None:
                content = {
                    key: value
                    for key, value in event.items()
                    if key not in {"ts", "event"}
                }
            lines += _json_block(content)
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

    @staticmethod
    def _count_existing_lines(path: Path) -> int:
        if not path.exists():
            return 0
        return sum(1 for _line in path.read_text(encoding="utf-8").splitlines())


def _turn_mode(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        mode = event.get("mode")
        if mode:
            return str(mode)
    return None


def _first_event(events: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for event in events:
        if event.get("event") == name:
            return event
    return None


def _count_events(events: list[dict[str, Any]], name: str) -> int:
    return sum(1 for event in events if event.get("event") == name)


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


def _markdown_transcript_ref(event: dict[str, Any] | None) -> str | None:
    if not event:
        return None
    explicit = event.get("markdown_transcript_ref")
    if explicit:
        return str(explicit)
    transcript_ref = event.get("transcript_ref")
    if isinstance(transcript_ref, str) and transcript_ref.endswith(".md"):
        return transcript_ref
    return None


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
        return "submit_gates.jsonl"

    def write_cost_summary(self, payload: dict[str, Any]) -> str:
        self.record_cost(payload)
        return "cost_summary.jsonl"

    def write_error(self, payload: dict[str, Any]) -> str:
        self.record_error(payload)
        return "errors.jsonl"

    def final_markdown(self, text: str) -> None:
        self.finalize_markdown(final_status=text, state_summary={})
