"""Actionable diagnostics for malformed JSON emitted by LLM tool calls."""

from __future__ import annotations

import json
import re
from typing import Any


_CODE_FENCE_RE = re.compile(r"^```(?:json|jsonc|javascript|js)?\s*\n(.*?)```\s*$", re.DOTALL)


def strip_json_code_fence(raw: str) -> tuple[str, bool]:
    """Return JSON text if *raw* is wrapped in a markdown code fence."""

    stripped = raw.strip()
    match = _CODE_FENCE_RE.match(stripped)
    if match is None:
        return raw, False
    return match.group(1).strip(), True


def preview_json_text(raw: str, *, limit: int = 1200) -> str:
    """Return a bounded preview of raw JSON-like text."""

    text = raw.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "...[truncated]"


def json_error_excerpt(raw: str, pos: int, *, radius: int = 240) -> str:
    """Return the raw argument excerpt around a JSON parser failure."""

    if not raw:
        return ""
    start = max(0, pos - radius)
    end = min(len(raw), pos + radius)
    prefix = "...[snip] " if start > 0 else ""
    suffix = " ...[snip]" if end < len(raw) else ""
    excerpt = raw[start:end]
    caret_col = len(prefix) + max(0, min(pos - start, len(excerpt)))
    return f"{prefix}{excerpt}{suffix}\n{' ' * caret_col}^"


def json_error_likely_cause(exc: json.JSONDecodeError) -> str:
    """Translate common JSON parser messages into actionable guidance."""

    msg = exc.msg.lower()
    if "expecting ',' delimiter" in msg:
        return (
            "A comma is missing between fields/items, or a nested object/array "
            "was closed with the wrong number of braces. Check the value just "
            "before the caret and make sure every object field is separated by "
            "a comma."
        )
    if "expecting value" in msg:
        return (
            "A value is missing or is not valid JSON. Common causes: an "
            "unquoted prose/string value, a dangling comma, or a Python token "
            "such as True/False/None instead of true/false/null."
        )
    if "expecting property name" in msg:
        return (
            "Object keys must be double-quoted JSON strings. Remove trailing "
            "commas and do not use comments."
        )
    if "unterminated string" in msg:
        return (
            "A string value was not closed or contains an unescaped quote. "
            "Escape internal quotes as \\\"."
        )
    if "invalid control character" in msg:
        return (
            "A string contains a raw newline or control character. Encode "
            "newlines as \\n inside JSON strings."
        )
    if "extra data" in msg:
        return (
            "The tool arguments contain more than one JSON value or prose "
            "outside the JSON object. Send exactly one JSON object."
        )
    return (
        "The JSON is not strict JSON. Send exactly one raw JSON object with "
        "double-quoted keys and string values."
    )


def build_json_argument_error_payload(
    tool_name: str,
    raw: str,
    exc: json.JSONDecodeError,
    *,
    code_fence_stripped: bool = False,
) -> dict[str, Any]:
    """Build the LLM-visible payload for malformed tool arguments."""

    fence_note = (
        " A markdown code fence was stripped before parsing, but the JSON inside "
        "the fence is still invalid."
        if code_fence_stripped
        else ""
    )
    return {
        "error_class": "tool_arguments_invalid_json",
        "error": f"Invalid JSON in tool arguments for '{tool_name}'.",
        "hint": (
            "Retry the same tool call with STRICT JSON arguments only: one raw "
            "object, double-quoted keys, double-quoted string values, no markdown "
            "fences, no prose, no comments, no trailing commas. Preserve the "
            "same schema fields and fix the syntax at the marked position."
        ),
        "detail": (
            f"JSON parser error at line {exc.lineno}, column {exc.colno}, "
            f"character {exc.pos}: {exc.msg}.{fence_note}"
        ),
        "likely_cause": json_error_likely_cause(exc),
        "expected_format": (
            f"{tool_name}({{\"field_name\": \"value\", "
            "\"other_required_field\": []}})"
        ),
        "raw_arguments_error_excerpt": json_error_excerpt(raw, exc.pos),
        "raw_arguments_preview": preview_json_text(raw),
    }


def best_effort_json_decode_error(raw: str) -> json.JSONDecodeError | None:
    """Return a useful JSONDecodeError for raw JSON-like text, if available."""

    candidates = [raw, raw.strip()]
    stripped, changed = strip_json_code_fence(raw)
    if changed:
        candidates.append(stripped)

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            json.loads(candidate)
        except json.JSONDecodeError as exc:
            return exc
        except TypeError:
            return None
    return None


def format_json_parse_failure(
    label: str,
    raw: str,
    *,
    exc: json.JSONDecodeError | None = None,
) -> str:
    """Return a readable parse failure message for non-agent structured paths."""

    decode_error = exc or best_effort_json_decode_error(raw)
    if decode_error is None:
        return (
            f"{label} is not parseable JSON.\n"
            f"Raw preview: {preview_json_text(raw)}"
        )
    return (
        f"{label} is not parseable JSON.\n"
        f"Detail: line {decode_error.lineno}, column {decode_error.colno}, "
        f"character {decode_error.pos}: {decode_error.msg}.\n"
        f"Likely cause: {json_error_likely_cause(decode_error)}\n"
        "Error excerpt:\n"
        f"{json_error_excerpt(raw, decode_error.pos)}\n"
        f"Raw preview: {preview_json_text(raw)}"
    )
