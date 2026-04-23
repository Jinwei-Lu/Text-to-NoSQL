from __future__ import annotations

from collections import Counter
from typing import Any
import re

import json5


FORBIDDEN_OPERATORS = {
    "$sample",
    "$rand",
    "$$NOW",
    "$out",
    "$merge",
    "$function",
}

OPERATOR_RE = re.compile(r"\$\$?[A-Za-z_][A-Za-z0-9_]*")
STRING_LITERAL_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_])-?\d+(?:\.\d+)?(?![A-Za-z0-9_])")
FIELD_REF_RE = re.compile(r'["\']\$(?!\$)([A-Za-z_][A-Za-z0-9_\.]*)["\']')
FIELD_KEY_RE = re.compile(r'["\']?([A-Za-z_][A-Za-z0-9_\.]*)["\']?\s*:')
QUERY_RE = re.compile(
    r"^\s*db\.([A-Za-z_][A-Za-z0-9_]*)\.(aggregate|find)\((.*)\)\s*$",
    re.DOTALL,
)
UNSUPPORTED_LITERAL_RE = re.compile(r"\b(?:ISODate|ObjectId|NumberLong|NumberInt|Timestamp)\s*\(")

IGNORED_FIELD_KEYS = {
    "_id",
    "path",
    "preserveNullAndEmptyArrays",
    "partitionBy",
    "sortBy",
    "output",
    "window",
    "documents",
    "input",
    "as",
    "cond",
    "median",
    "moving_avg_attendance",
    "kept",
    "vals",
}


def parse_ok(query: str) -> bool:
    query = query.strip()
    return query.startswith("db.") and ("aggregate(" in query or "find(" in query)


def canonical_text(query: str) -> str:
    parts = re.split(r"\s+", query.strip())
    return " ".join(part for part in parts if part)


def extract_operator_tokens(query: str) -> list[str]:
    return OPERATOR_RE.findall(query)


def contains_forbidden_operator(query: str) -> bool:
    return any(token in FORBIDDEN_OPERATORS for token in extract_operator_tokens(query))


def _extract_aggregate_body(query: str) -> str:
    anchor = "aggregate("
    start = query.find(anchor)
    if start == -1:
        return ""
    body = query[start + len(anchor) :]
    depth = 0
    in_string: str | None = None
    escaped = False
    out: list[str] = []
    for ch in body:
        out.append(ch)
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue
        if ch in {"'", '"'}:
            in_string = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                out.pop()
                break
            depth -= 1
    return "".join(out)


def split_top_level_arguments(raw_args: str) -> list[str]:
    arguments: list[str] = []
    current: list[str] = []
    brace_depth = 0
    bracket_depth = 0
    paren_depth = 0
    in_string: str | None = None
    escaped = False

    for ch in raw_args:
        if in_string:
            current.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue

        if ch in {"'", '"'}:
            in_string = ch
            current.append(ch)
            continue
        if ch == "{":
            brace_depth += 1
            current.append(ch)
            continue
        if ch == "}":
            brace_depth -= 1
            current.append(ch)
            continue
        if ch == "[":
            bracket_depth += 1
            current.append(ch)
            continue
        if ch == "]":
            bracket_depth -= 1
            current.append(ch)
            continue
        if ch == "(":
            paren_depth += 1
            current.append(ch)
            continue
        if ch == ")":
            paren_depth -= 1
            current.append(ch)
            continue
        if ch == "," and brace_depth == 0 and bracket_depth == 0 and paren_depth == 0:
            snippet = "".join(current).strip()
            if snippet:
                arguments.append(snippet)
            current = []
            continue
        current.append(ch)

    tail = "".join(current).strip()
    if tail:
        arguments.append(tail)
    return arguments


def _split_top_level_stages(array_like: str) -> list[str]:
    start = array_like.find("[")
    if start == -1:
        return []
    items: list[str] = []
    current: list[str] = []
    brace_depth = 0
    bracket_depth = 0
    in_string: str | None = None
    escaped = False
    for ch in array_like[start + 1 :]:
        if in_string:
            current.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue
        if ch in {"'", '"'}:
            in_string = ch
            current.append(ch)
            continue
        if ch == "{":
            brace_depth += 1
            current.append(ch)
            continue
        if ch == "}":
            brace_depth -= 1
            current.append(ch)
            continue
        if ch == "[":
            bracket_depth += 1
            current.append(ch)
            continue
        if ch == "]":
            if brace_depth == 0 and bracket_depth == 0:
                if "".join(current).strip():
                    items.append("".join(current).strip().rstrip(","))
                break
            bracket_depth -= 1
            current.append(ch)
            continue
        if ch == "," and brace_depth == 0 and bracket_depth == 0:
            item = "".join(current).strip()
            if item:
                items.append(item)
            current = []
            continue
        current.append(ch)
    return [item for item in items if item]


def extract_root_stage_tokens(query: str) -> list[str]:
    body = _extract_aggregate_body(query)
    tokens: list[str] = []
    for stage in _split_top_level_stages(body):
        match = re.search(r"\$[A-Za-z_][A-Za-z0-9_]*", stage)
        if match:
            tokens.append(match.group(0))
    return tokens


def operator_counter(query: str) -> Counter[str]:
    return Counter(extract_operator_tokens(query))


def mask_fields_and_literals(query: str) -> str:
    masked = STRING_LITERAL_RE.sub("<STR>", query)
    masked = NUMBER_RE.sub("<NUM>", masked)
    masked = re.sub(r"\$[A-Za-z_][A-Za-z0-9_\.]*", "<FIELD_OR_OP>", masked)
    return canonical_text(masked)


def structure_signature(query: str) -> tuple[list[str], Counter[str], str]:
    return extract_root_stage_tokens(query), operator_counter(query), mask_fields_and_literals(query)


def extract_field_paths(query: str) -> set[str]:
    fields = set(FIELD_REF_RE.findall(query))
    for candidate in FIELD_KEY_RE.findall(query):
        if candidate.startswith("$"):
            continue
        if candidate in IGNORED_FIELD_KEYS:
            continue
        if candidate in {"db", "aggregate", "find"}:
            continue
        if candidate[0].islower() and "." not in candidate:
            continue
        fields.add(candidate)
    return fields


def parse_shell_literal(snippet: str) -> Any:
    if UNSUPPORTED_LITERAL_RE.search(snippet):
        raise ValueError("Unsupported shell literal in query. Extend parser for BSON constructors first.")
    return json5.loads(snippet)


def parse_mql_query(query: str) -> tuple[str, str, Any]:
    match = QUERY_RE.match(query)
    if not match:
        raise ValueError("Unsupported query shape. Expected db.<collection>.aggregate(...) or find(...).")

    collection_name, operation, raw_args = match.groups()
    arguments = split_top_level_arguments(raw_args)
    if operation == "aggregate":
        if len(arguments) != 1:
            raise ValueError("aggregate(...) expects exactly one pipeline argument.")
        return collection_name, operation, parse_shell_literal(arguments[0])

    if operation == "find":
        if not arguments:
            raise ValueError("find(...) expects at least one filter argument.")
        query_filter = parse_shell_literal(arguments[0])
        projection = parse_shell_literal(arguments[1]) if len(arguments) >= 2 else None
        return collection_name, operation, {"filter": query_filter, "projection": projection}

    raise ValueError(f"Unsupported operation: {operation}")
