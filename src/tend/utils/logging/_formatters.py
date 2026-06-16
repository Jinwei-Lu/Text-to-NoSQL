from __future__ import annotations

# ruff: noqa: F403,F405

from tend.utils.logging._config import *

from tend.utils.logging._paths import *
from tend.utils.json_diagnostics import strip_json_code_fence

def _strip_code_fence(text: str) -> str:
    """Remove an optional markdown code fence (```json ... ``` or ``` ... ```)."""
    stripped, _ = strip_json_code_fence(text)
    return stripped

def _deep_parse_json_strings(obj: Any) -> Any:
    """Recursively parse string values that contain valid JSON."""
    if isinstance(obj, str):
        trimmed = obj.strip()
        if trimmed.startswith(("{", "[")):
            try:
                parsed = json.loads(trimmed)
                return _deep_parse_json_strings(parsed)
            except (json.JSONDecodeError, TypeError):
                pass
        return obj
    if isinstance(obj, dict):
        return {k: _deep_parse_json_strings(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_parse_json_strings(item) for item in obj]
    return obj

def _is_json_key_boundary(text: str, comma_index: int) -> bool:
    """Return true when ``, "key":`` starts at *comma_index*."""

    i = comma_index + 1
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text) or text[i] != '"':
        return False

    i += 1
    escaped = False
    while i < len(text):
        ch = text[i]
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            i += 1
            break
        i += 1
    else:
        return False

    while i < len(text) and text[i].isspace():
        i += 1
    return i < len(text) and text[i] == ":"

def _find_unquoted_json_value_end(text: str, start: int) -> int:
    """Find the end of a bare string value in a JSON-like object."""

    i = start
    while i < len(text):
        ch = text[i]
        if ch in "}]":
            return i
        if ch == "," and _is_json_key_boundary(text, i):
            return i
        i += 1
    return len(text)

def _json_value_has_valid_start(text: str, index: int) -> bool:
    ch = text[index]
    if ch in '"{[-0123456789':
        return True
    for literal in ("true", "false", "null"):
        if text.startswith(literal, index):
            end = index + len(literal)
            if end == len(text) or text[end].isspace() or text[end] in ",}]":
                return True
    return False

def _next_json_string_state(
    ch: str, *, in_string: bool, escaped: bool
) -> tuple[bool, bool]:
    if not in_string:
        return ch == '"', False
    if escaped:
        return True, False
    if ch == "\\":
        return True, True
    if ch == '"':
        return False, False
    return True, False

def _append_unquoted_value_after_colon(
    text: str, index: int, out: list[str]
) -> tuple[int, bool]:
    i = index + 1
    while i < len(text) and text[i].isspace():
        out.append(text[i])
        i += 1
    if i >= len(text) or _json_value_has_valid_start(text, i):
        return i, False

    end = _find_unquoted_json_value_end(text, i)
    raw_value = text[i:end].strip()
    if not raw_value:
        return i, False
    out.append(json.dumps(raw_value, ensure_ascii=False))
    return end, True

def _repair_unquoted_json_string_values(text: str) -> str | None:
    """Best-effort display-only repair for JSON-like tool arguments.

    Some providers return structured tool arguments that are close to JSON but
    contain a long prose value without string quotes.  Logging can still make
    those calls readable by quoting that value before pretty-printing.  This is
    intentionally narrow and only used after strict JSON parsing fails.
    """

    if not text.strip().startswith(("{", "[")):
        return None

    out: list[str] = []
    i = 0
    in_string = False
    escaped = False
    changed = False

    while i < len(text):
        ch = text[i]
        out.append(ch)

        if in_string or ch == '"':
            in_string, escaped = _next_json_string_state(
                ch, in_string=in_string, escaped=escaped
            )
            i += 1
            continue

        if ch != ":":
            i += 1
            continue

        i, repaired = _append_unquoted_value_after_colon(text, i, out)
        changed = changed or repaired

    if not changed:
        return None
    return "".join(out)

def _parse_json_for_log(text: str) -> Any | None:
    """Parse strict JSON, then a narrow JSON-like fallback for readable logs."""

    stripped = _strip_code_fence(text.strip())
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return _deep_parse_json_strings(json.loads(stripped))
    except (json.JSONDecodeError, TypeError):
        repaired = _repair_unquoted_json_string_values(stripped)
        if repaired is None:
            return None
        try:
            return _deep_parse_json_strings(json.loads(repaired))
        except (json.JSONDecodeError, TypeError):
            return None

def _shorten_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "\u2026"

def _format_content_as_markdown(content: str) -> list[str]:
    """Format LLM response content: pretty-print JSON in a fenced block, otherwise blockquote."""
    parsed = _parse_json_for_log(content)
    if parsed is not None:
        return ["```json", json.dumps(parsed, indent=2, ensure_ascii=False), "```"]
    lines: list[str] = []
    for line in content.splitlines():
        lines.append(f"> {line}" if line else ">")
    return lines

def _format_tool_calls_md(tool_calls: list[dict[str, Any]]) -> list[str]:
    """Format tool calls as markdown. Shared by workflow and agent session logging."""
    lines: list[str] = []
    for tc in tool_calls:
        func = tc.get("function") or {}
        name = func.get("name") or tc.get("name") or tc.get("type") or "unknown"
        tc_id = tc.get("id", "")
        lines += [f"#### {name} (`{tc_id}`)", ""]
        raw_args = func.get("arguments") or tc.get("arguments") or ""
        if raw_args:
            if isinstance(raw_args, (dict, list)):
                parsed = _deep_parse_json_strings(raw_args)
                lines += [
                    "```json",
                    json.dumps(parsed, indent=2, ensure_ascii=False),
                    "```",
                ]
            elif isinstance(raw_args, str):
                parsed = _parse_json_for_log(raw_args)
                if parsed is not None:
                    lines += [
                        "```json",
                        json.dumps(parsed, indent=2, ensure_ascii=False),
                        "```",
                    ]
                else:
                    for arg_line in raw_args.splitlines():
                        lines.append(f"> {arg_line}" if arg_line else ">")
            else:
                for arg_line in str(raw_args).splitlines():
                    lines.append(f"> {arg_line}" if arg_line else ">")
        lines.append("")
    return lines

def _tool_call_id(tc: dict[str, Any]) -> str:
    return str(tc.get("id") or tc.get("tool_call_id") or "")

def _tool_call_name(tc: dict[str, Any]) -> str:
    func = tc.get("function") or {}
    return str(func.get("name") or tc.get("name") or tc.get("type") or "unknown")

def _tool_call_arguments(tc: dict[str, Any]) -> Any:
    func = tc.get("function") or {}
    raw_args = func.get("arguments") if "arguments" in func else tc.get("arguments")
    if raw_args in (None, ""):
        return None
    if isinstance(raw_args, (dict, list)):
        return _deep_parse_json_strings(raw_args)
    if isinstance(raw_args, str):
        parsed = _parse_json_for_log(raw_args)
        return parsed if parsed is not None else raw_args
    return raw_args

def _format_tool_signature_value(value: Any, *, limit: int = 80) -> str:
    if isinstance(value, str):
        text = json.dumps(value, ensure_ascii=False)
    elif isinstance(value, (dict, list)):
        text = json.dumps(
            value,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
    elif value is None:
        text = "null"
    else:
        text = str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    return _shorten_text(text, limit)

def _format_tool_call_signature(tc: dict[str, Any]) -> str:
    name = _tool_call_name(tc)
    args = _tool_call_arguments(tc)
    if args is None:
        return f"{name}()"
    if isinstance(args, dict):
        parts: list[str] = []
        for idx, (key, value) in enumerate(args.items()):
            if idx >= 3:
                parts.append("...")
                break
            parts.append(f"{key}={_format_tool_signature_value(value)}")
        return f"{name}({', '.join(parts)})"
    return f"{name}({_format_tool_signature_value(args, limit=120)})"

def _tool_call_signature_map(
    tool_calls: list[dict[str, Any]] | None,
) -> dict[str, str]:
    labels: dict[str, str] = {}
    for tc in tool_calls or []:
        tc_id = _tool_call_id(tc)
        if tc_id:
            labels[tc_id] = _format_tool_call_signature(tc)
    return labels

def _collect_row_columns(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            key_s = str(key)
            if key_s not in seen:
                seen.add(key_s)
                columns.append(key_s)
    return columns

def _format_markdown_cell(value: Any) -> str:
    if value is None:
        text = "NULL"
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    text = text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    if len(text) > 120:
        return text[:117] + "..."
    return text

def _format_markdown_table(rows: list[dict[str, Any]]) -> list[str]:
    """Render JSON row dicts as a compact Markdown table."""

    if not rows:
        return ["Query returned 0 rows."]

    columns = _collect_row_columns(rows)
    if not columns:
        return [f"Query returned {len(rows)} row(s)."]

    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "|".join("---" for _ in columns) + "|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(_format_markdown_cell(row.get(col)) for col in columns)
            + " |"
        )
    return lines

def _query_baseline_extra(content: str) -> dict[str, Any] | None:
    stripped = _strip_code_fence(content.strip())
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return None
    payload = _deep_parse_json_strings(payload)
    if not isinstance(payload, dict) or payload.get("accepted") is not True:
        return None
    extra = payload.get("extra")
    if not isinstance(extra, dict):
        return None
    rows = extra.get("rows")
    if not isinstance(rows, list) or "sql_executed" not in extra:
        return None
    return extra

def _query_baseline_meta_lines(extra: dict[str, Any]) -> list[str]:
    meta: list[str] = []
    returned = extra.get("row_count_returned")
    total = extra.get("row_count_total")
    total_exact = extra.get("row_count_total_exact", True) is not False
    if returned is not None and total is not None:
        if total_exact:
            meta.append(f"Rows returned: {returned} (total: {total})")
        else:
            meta.append(f"Rows returned: {returned} (total: at least {total})")
    elif returned is not None:
        meta.append(f"Rows returned: {returned}")
    row_limit = extra.get("row_limit_applied")
    if row_limit is not None:
        meta.append(f"Row limit applied: {row_limit}")
    if extra.get("truncated"):
        meta.append("Result truncated.")
    sql_executed = str(extra.get("sql_executed") or "")
    if sql_executed:
        meta += ["SQL executed:", "```sql", *sql_executed.rstrip().splitlines(), "```"]
    return meta

def _format_query_baseline_result(content: str) -> list[str] | None:
    """Pretty-print successful ``query_baseline`` RegisterResult payloads."""

    extra = _query_baseline_extra(content)
    if extra is None:
        return None
    rows = extra.get("rows")
    row_dicts = [row for row in rows if isinstance(row, dict)]
    lines = _format_markdown_table(row_dicts)
    meta = _query_baseline_meta_lines(extra)
    if meta:
        lines += ["", *meta]
    return lines

def _format_json_text_block(content: str) -> list[str] | None:
    stripped = _strip_code_fence(content.strip())
    if not stripped.startswith(("{", "[")):
        return None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return None
    parsed = _deep_parse_json_strings(parsed)
    return ["```json", json.dumps(parsed, indent=2, ensure_ascii=False), "```"]

def _format_tool_results_md(
    tool_results: list[dict[str, Any]],
    *,
    max_chars: int = 0,
    tool_calls: list[dict[str, Any]] | None = None,
    tool_call_labels: dict[str, str] | None = None,
) -> list[str]:
    """Format tool results as markdown for agent session logging."""
    lines: list[str] = []
    labels = dict(tool_call_labels or {})
    labels.update(_tool_call_signature_map(tool_calls))
    for tr in tool_results:
        tc_id = str(tr.get("tool_call_id") or "")
        content = tr.get("content", "")
        if not isinstance(content, str):
            content = (
                json.dumps(content, indent=2, ensure_ascii=False)
                if isinstance(content, (dict, list))
                else str(content)
            )

        label = labels.get(tc_id)
        heading = f"#### {label} (`{tc_id}`)" if label else f"#### `{tc_id}`"
        lines += [heading, ""]
        rendered = _format_query_baseline_result(content)
        if rendered is not None:
            for line in rendered:
                lines.append(f"> {line}" if line else ">")
        else:
            json_block = _format_json_text_block(content)
            if json_block is not None:
                lines += json_block
            else:
                for line in content.splitlines():
                    lines.append(f"> {line}" if line else ">")
        lines.append("")
    return lines

_MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_MARKDOWN_LIST_RE = re.compile(r"^\s{0,3}(?:[-*+]|\d+[.)])\s+")
_DELIMITER_HEADER_RE = re.compile(
    r"^\s*(?:={3,}|-{3,})\s*\S.*?\S\s*(?:={3,}|-{3,})\s*$"
)

def _is_standalone_markdown_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if _MARKDOWN_HEADING_RE.match(line):
        return True
    if stripped.startswith(("```", "~~~")):
        return True
    if stripped.startswith("|") and stripped.endswith("|"):
        return True
    if _DELIMITER_HEADER_RE.match(line):
        return True
    return False

def _is_markdown_fence_line(line: str) -> bool:
    return line.strip().startswith(("```", "~~~"))

def _flush_soft_wrap_paragraph(output: list[str], paragraph: list[str]) -> None:
    if paragraph:
        output.append(" ".join(part.strip() for part in paragraph if part.strip()))
        paragraph.clear()

def _emit_unwrapped_markdown_line(
    line: str,
    output: list[str],
    paragraph: list[str],
) -> bool:
    stripped = line.strip()
    if not stripped:
        _flush_soft_wrap_paragraph(output, paragraph)
        output.append("")
        return True
    if _is_standalone_markdown_line(line):
        _flush_soft_wrap_paragraph(output, paragraph)
        output.append(line)
        return True
    if _MARKDOWN_LIST_RE.match(line):
        _flush_soft_wrap_paragraph(output, paragraph)
        output.append(line)
        return True
    # Indented lines (>=2 leading spaces or any tab) are pseudo-list
    # items in DynaDB prompts (e.g. ``  stg_xxx [staging] ...`` under
    # ``=== CROSS-GROUP REUSE OPPORTUNITIES ===``). Always flush the
    # current soft-wrap paragraph and emit them verbatim so a long
    # list of indented items doesn't collapse into a single line.
    if line.startswith("\t") or line.startswith("  "):
        _flush_soft_wrap_paragraph(output, paragraph)
        output.append(line)
        return True
    if line.startswith(" ") and not paragraph:
        output.append(line)
        return True
    return False

def _unwrap_soft_wrapped_text(text: str) -> str:
    """Remove source-format soft wraps while preserving markdown structure."""

    output: list[str] = []
    paragraph: list[str] = []
    in_fence = False

    for line in text.splitlines():
        if in_fence:
            output.append(line)
            if _is_markdown_fence_line(line):
                in_fence = False
            continue
        if _is_markdown_fence_line(line):
            _flush_soft_wrap_paragraph(output, paragraph)
            output.append(line)
            in_fence = True
            continue
        if _emit_unwrapped_markdown_line(line, output, paragraph):
            continue
        paragraph.append(line)

    _flush_soft_wrap_paragraph(output, paragraph)
    return "\n".join(output)

def _agent_payload_for_log(value: Any) -> str:
    """Serialize an agent message payload without line-by-line rewriting."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return str(value)

def _append_agent_payload_section(
    lines: list[str],
    title: str,
    value: Any,
    *,
    unwrap_soft_wrapped: bool = False,
) -> None:
    """Append one agent I/O section as blockquote without readability wrapping."""

    payload = _agent_payload_for_log(value)
    if payload == "":
        return
    if unwrap_soft_wrapped:
        payload = _unwrap_soft_wrapped_text(payload)
    lines += [title, ""]
    for line in payload.splitlines():
        lines.append(f"> {line}" if line else ">")
    lines.append("")

def _append_agent_message_section(
    lines: list[str],
    index: int,
    message: dict[str, Any],
    *,
    tool_call_labels: dict[str, str] | None = None,
) -> None:
    """Append one active history message in the same readable style as turns."""

    role = str(message.get("role") or "unknown")
    lines += [f"### Message {index}: {role.capitalize()}", ""]

    if role == "tool":
        tool_call_id = str(message.get("tool_call_id") or "")
        if tool_call_id:
            lines += [
                "| Field | Value |",
                "|-------|-------|",
                f"| Tool Call ID | `{tool_call_id}` |",
                "",
            ]
        lines += _format_tool_results_md(
            [
                {
                    "tool_call_id": tool_call_id,
                    "content": message.get("content", ""),
                }
            ],
            tool_call_labels=tool_call_labels,
        )
        return

    reasoning = (
        message.get("reasoning")
        or message.get("reasoning_content")
        or message.get("reasoning_raw")
    )
    _append_agent_payload_section(lines, "#### Reasoning", reasoning)
    _append_agent_payload_section(lines, "#### Content", message.get("content", ""))

    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        lines += ["#### Tool Calls", ""]
        lines += _format_tool_calls_md(tool_calls)

def _format_agent_messages_md(messages: list[dict[str, Any]]) -> list[str]:
    """Format active agent history as ordinary messages, not a JSON dump."""

    lines: list[str] = []
    tool_call_labels: dict[str, str] = {}
    for index, message in enumerate(messages, start=1):
        role = str(message.get("role") or "unknown")
        _append_agent_message_section(
            lines,
            index,
            message,
            tool_call_labels=tool_call_labels if role == "tool" else None,
        )
        if role == "assistant":
            tool_call_labels = _tool_call_signature_map(
                message.get("tool_calls") or []
            )
        elif role != "tool":
            tool_call_labels = {}
    return lines

def _format_tail_compact(
    messages: list[dict[str, Any]], *, max_chars_per_msg: int = 300
) -> list[str]:
    """Format tail messages as compact blockquoted one-liners for context snapshots."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "") or ""
        if role == "assistant" and m.get("tool_calls"):
            tc_names = [tc["function"]["name"] for tc in m["tool_calls"]]
            line = f"[assistant] called: {', '.join(tc_names)}"
            if content:
                preview = content[:100].replace("\n", " ")
                line += f" | {preview}"
        elif role == "tool":
            tc_id = m.get("tool_call_id", "")[:20]
            preview = content[:max_chars_per_msg].replace("\n", " ")
            if len(content) > max_chars_per_msg:
                preview += "..."
            line = f"[tool:{tc_id}] {preview}"
        else:
            preview = content[:max_chars_per_msg].replace("\n", " ")
            if len(content) > max_chars_per_msg:
                preview += "..."
            line = f"[{role}] {preview}"
        lines.append(f"> {line}")
    return lines

def _format_llm_request_as_markdown(record: dict[str, Any]) -> str:
    """Convert an LLM request record into a Markdown string (no response section)."""
    call_id = record.get("call_id", "unknown")
    lines: list[str] = [f"# LLM Call: {call_id}", ""]

    lines += [
        "## Metadata",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Stage | {record.get('stage', '')} |",
        f"| Task | {record.get('task_id', '')} |",
        f"| Model | {record.get('model', '')} |",
        f"| Request Timestamp | {record.get('timestamp', '')} |",
        f"| Temperature | {record.get('temperature')} |",
        "",
    ]

    lines += ["## Messages", ""]
    for msg in record.get("messages") or []:
        role = str(msg.get("role", "unknown")).capitalize()
        content = _unwrap_soft_wrapped_text(str(msg.get("content") or ""))
        lines += [f"### {role}", ""]
        for content_line in content.splitlines():
            lines.append(f"> {content_line}" if content_line else ">")
        lines.append("")

    tools = record.get("tools")
    if tools:
        lines += ["## Tools", ""]
        lines += ["```json", json.dumps(tools, indent=2, default=str), "```", ""]

    response_format = record.get("response_format")
    if response_format:
        lines += ["## Response Format", ""]
        lines += [
            "```json",
            json.dumps(response_format, indent=2, default=str),
            "```",
            "",
        ]

    return "\n".join(lines)

def _format_llm_response_as_markdown(
    *,
    response_raw: Any,
    usage: dict[str, int],
    finish_reason: str,
    cost_usd: float,
    cost_source: str | None = None,
    timestamp: str,
) -> str:
    """Format the response section as Markdown, suitable for appending to a request MD."""
    raw = response_raw if isinstance(response_raw, dict) else {}
    raw_usage = raw.get("usage") or {}
    total_tokens = raw_usage.get(
        "total_tokens",
        usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
    )
    cost_label = _format_logged_cost(
        cost_usd=cost_usd,
        cost_source=cost_source,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        raw_usage=raw_usage,
    )

    lines: list[str] = [
        "",
        "## Response",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Response Timestamp | {timestamp} |",
        f"| Finish Reason | {finish_reason} |",
        f"| Cost (USD) | {cost_label} |",
        f"| Prompt Tokens | {usage.get('prompt_tokens', '')} |",
        f"| Completion Tokens | {usage.get('completion_tokens', '')} |",
        f"| Cache Hit Tokens | {usage.get('cache_hit_tokens', '')} |",
        f"| Cache Miss Tokens | {usage.get('cache_miss_tokens', '')} |",
        f"| Total Tokens | {total_tokens} |",
        "",
    ]

    choices = raw.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}

        reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
        if not reasoning:
            for rd in msg.get("reasoning_details") or []:
                reasoning += rd.get("text", "")

        if reasoning:
            lines += ["### Reasoning", ""]
            for rline in reasoning.splitlines():
                lines.append(f"> {rline}" if rline else ">")
            lines.append("")

        content = msg.get("content") or ""
        if content:
            lines += ["### Content", ""]
            lines += _format_content_as_markdown(content)
            lines.append("")

        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            lines += ["### Tool Calls", ""]
            lines += _format_tool_calls_md(tool_calls)

    return "\n".join(lines)

def _format_logged_cost(
    *,
    cost_usd: float,
    cost_source: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    raw_usage: dict[str, Any] | None = None,
) -> str:
    """Render provider-missing cost as unknown instead of a fake zero."""

    if cost_source == "unavailable":
        return "unknown"
    raw_usage = raw_usage or {}
    has_tokens = (prompt_tokens + completion_tokens) > 0
    if (
        cost_source is None
        and has_tokens
        and cost_usd == 0.0
        and "cost" not in raw_usage
    ):
        return "unknown"
    return f"{cost_usd:.6f}"

def _format_seed_outcome_section(
    *,
    decision: str,
    reason: str,
    artifact_kind: str,
    fields: dict[str, Any],
) -> str:
    """Render the ``## Seed Phase Outcome`` section appended to a seed log.

    Pairs with the existing ``## Structured Parse Outcome`` convention so
    a single log file tells the reader whether an atom/scenario/window
    was accepted, rejected, or dropped — and why.
    """

    lines: list[str] = [
        "",
        "## Seed Phase Outcome",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Artifact | {artifact_kind} |",
        f"| Decision | {decision} |",
        f"| Reason | {reason} |",
    ]
    for key, value in fields.items():
        lines.append(f"| {key} | {value} |")
    lines.append("")
    return "\n".join(lines)

def _format_seed_audit_log(title: str, records: list[dict[str, Any]]) -> str:
    """Render a free-standing seed-phase summary log as markdown."""

    lines: list[str] = [f"# {title}", ""]
    if not records:
        lines += ["_No records._", ""]
        return "\n".join(lines)

    columns: list[str] = []
    seen: set[str] = set()
    for record in records:
        for key in record.keys():
            if key not in seen:
                seen.add(key)
                columns.append(key)

    header = "| " + " | ".join(columns) + " |"
    separator = "|" + "|".join("---" for _ in columns) + "|"
    lines += [header, separator]
    for record in records:
        row = "| " + " | ".join(
            str(record.get(col, "")) for col in columns
        ) + " |"
        lines.append(row)
    lines.append("")
    return "\n".join(lines)


__all__ = [name for name in globals() if not name.startswith("__")]
