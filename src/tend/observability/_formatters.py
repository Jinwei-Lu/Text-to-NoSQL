"""Markdown and JSON formatting helpers for run observability artifacts."""
from __future__ import annotations

import json
from typing import Any


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


def _append_json_section(lines: list[str], title: str, value: Any) -> None:
    if value is None:
        return
    lines += [title, "", "```json", _json_dumps(value, indent=2), "```", ""]


def _tool_call_name(call: dict[str, Any]) -> str:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    return str(function.get("name") or call.get("name") or call.get("type") or "unknown")


def _tool_call_arguments(call: dict[str, Any]) -> Any:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    if "arguments" in function:
        return function.get("arguments")
    return call.get("arguments")


def _append_tool_call_arguments(lines: list[str], arguments: Any) -> None:
    if arguments in (None, ""):
        lines += ["> (no arguments)", ""]
        return
    if isinstance(arguments, str):
        pretty = _try_pretty_json_text(arguments)
        if pretty is not None:
            lines += ["```json", pretty, "```", ""]
        else:
            _append_blockquote(lines, arguments)
        return
    if isinstance(arguments, (dict, list)):
        lines += ["```json", _json_dumps(arguments, indent=2), "```", ""]
        return
    _append_blockquote(lines, str(arguments))


def _append_tool_calls_section(
    lines: list[str],
    title: str,
    tool_calls: Any,
    *,
    heading_prefix: str = "###",
) -> None:
    if not isinstance(tool_calls, list) or not tool_calls:
        return
    lines += [title, ""]
    for raw_call in tool_calls:
        call = raw_call if isinstance(raw_call, dict) else {"arguments": raw_call}
        call_id = str(call.get("id") or "")
        suffix = f" (`{call_id}`)" if call_id else ""
        lines += [f"{heading_prefix} {_tool_call_name(call)}{suffix}", ""]
        _append_tool_call_arguments(lines, _tool_call_arguments(call))


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


def render_llm_transcript_markdown(payload: dict[str, Any]) -> str:
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
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else None

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
            ("Agent Session", payload.get("agent_session_ref")),
            ("Diagnostics", diagnostics_ref),
        ],
    )

    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    _append_messages(lines, messages)

    request_rows: list[tuple[str, Any]] = [
        ("Temperature", payload.get("temperature")),
        ("Max Tokens", payload.get("max_tokens")),
        ("Expect JSON", payload.get("expect_json")),
        ("JSON Repair Retries", payload.get("json_repair_retries")),
        ("Tool Count", len(tools) if tools is not None else None),
        ("Tool Choice Fallback", payload.get("tool_choice_fallback")),
        ("Stream", payload.get("stream")),
        ("First Token Timeout (s)", payload.get("first_token_timeout_s")),
        ("Cost Source", payload.get("cost_source")),
    ]
    if any(value is not None for _key, value in request_rows):
        lines += ["## Request Configuration", ""]
        _append_table(lines, request_rows)

    _append_json_section(lines, "## Response Schema", payload.get("schema"))
    _append_json_section(lines, "## Tools", tools)
    _append_json_section(lines, "## Tool Choice", payload.get("tool_choice"))

    if payload.get("prompt_build_failed"):
        lines += ["## Prompt Build Context", ""]
        _append_table(
            lines,
            [
                ("Prompt File", payload.get("prompt_file")),
                ("Input Keys", payload.get("input_keys")),
            ],
        )
        _append_content(
            lines,
            "### Input Preview",
            payload.get("input_preview"),
            prefer_json=True,
        )

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

    reasoning = usage.get("reasoning_content") or usage.get("reasoning_preview")
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

    _append_tool_calls_section(lines, "## Tool Calls", payload.get("tool_calls"))

    if attempts:
        lines += ["## Attempt Summary", ""]
        lines += _attempt_rows(attempts)
        lines += ["## Attempt Details", ""]
        for item in attempts:
            label = item.get("attempt", "?")
            kind = item.get("kind", "")
            lines += [f"### Attempt {label} {kind}", ""]
            response = item.get("response")
            if response is not None:
                _append_content(lines, "#### Response", response, prefer_json=True)
            preview = item.get("response_preview")
            if response is None and preview:
                _append_content(lines, "#### Response Preview", preview, prefer_json=True)
            _append_tool_calls_section(
                lines,
                "#### Tool Calls",
                item.get("tool_calls"),
                heading_prefix="#####",
            )
            error = item.get("error") or item.get("validation_error")
            if error:
                lines += ["#### Error", "", "```json", _json_dumps(error, indent=2), "```", ""]

    lines += [
        "## Diagnostics",
        "",
        f"Full structured payload: `{diagnostics_ref}`",
        "",
    ]
    return "\n".join(lines)
