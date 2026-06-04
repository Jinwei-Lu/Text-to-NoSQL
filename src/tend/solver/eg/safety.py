"""Safety helpers for bounded SMART-EG environment observations."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any, Iterable, Mapping

DEFAULT_DOC_LIMIT = 20
MAX_DOC_LIMIT = 100
DEFAULT_VALUE_LIMIT = 12
MAX_VALUE_LIMIT = 50
MAX_REDACT_DEPTH = 5
MAX_REDACT_ITEMS = 8
MAX_LITERAL_VALUE_BUCKETS = 12
MAX_LITERAL_STRING_LENGTH = 80

_MISSING = object()
_DATE_LIKE_RE = re.compile(
    r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r"(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)
_OBJECT_ID_LIKE_RE = re.compile(r"^[0-9a-fA-F]{24}$")


def bounded_limit(value: int | None, *, default: int, maximum: int) -> int:
    """Return a positive limit capped to the public EG observation bound."""
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return min(parsed, maximum)


def value_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def redact_scalar(value: Any) -> dict[str, Any]:
    kind = value_kind(value)
    out: dict[str, Any] = {"type": kind, "hash": stable_hash(value)}
    if isinstance(value, str):
        out["length"] = len(value)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        out["numeric_class"] = _numeric_class(value)
    return out


def value_token(value: Any) -> str:
    return f"value:{value_kind(value)}:{stable_hash(value).split(':', 1)[1]}"


def redact_value(value: Any, *, depth: int = 0) -> Any:
    """Return a JSON-safe redacted summary of a value, preserving shape but not raw scalars."""
    if depth >= MAX_REDACT_DEPTH:
        if isinstance(value, Mapping):
            return {
                "type": "object",
                "hash": stable_hash(value),
                "key_count": len(value),
                "truncated_depth": True,
            }
        if isinstance(value, list):
            return {
                "type": "array",
                "hash": stable_hash(value),
                "length": len(value),
                "truncated_depth": True,
            }
        return redact_scalar(value)
    if isinstance(value, Mapping):
        items = list(value.items())
        out = {
            str(key): redact_value(child, depth=depth + 1)
            for key, child in items[:MAX_REDACT_ITEMS]
        }
        if len(items) > MAX_REDACT_ITEMS:
            out["__truncated_keys__"] = len(items) - MAX_REDACT_ITEMS
        return out
    if isinstance(value, list):
        out = [redact_value(item, depth=depth + 1) for item in value[:MAX_REDACT_ITEMS]]
        if len(value) > MAX_REDACT_ITEMS:
            out.append({"__truncated_items__": len(value) - MAX_REDACT_ITEMS})
        return out
    return redact_scalar(value)


def summarize_redacted_value(
    value: Any,
    *,
    expose_literal: bool = False,
    include_proof: bool = False,
) -> dict[str, Any]:
    literal_exposed = False
    if not isinstance(value, (Mapping, list)):
        out = redact_scalar(value)
        if expose_literal:
            literal = observable_literal(value)
            if literal is not _MISSING:
                out["literal"] = literal
                literal_exposed = True
        if include_proof:
            out["token"] = value_token(value)
            out["proof"] = value_proof(value, literal_exposed=literal_exposed)
        return out
    out = {
        "type": value_kind(value),
        "hash": stable_hash(value),
        "size": len(value),
    }
    if include_proof:
        out["token"] = value_token(value)
        out["proof"] = value_proof(value, literal_exposed=False)
    return out


def value_proof(value: Any, *, literal_exposed: bool = False) -> dict[str, Any]:
    proof: dict[str, Any] = {
        "type": value_kind(value),
        "hash": stable_hash(value),
        "token": value_token(value),
        "literal_policy": "observable_short_printable" if literal_exposed else "redacted",
    }
    if isinstance(value, str):
        proof["length"] = len(value)
        string_format = _string_format(value)
        if string_format:
            proof["string_format"] = string_format
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        proof["numeric_class"] = _numeric_class(value)
    elif isinstance(value, (Mapping, list)):
        proof["size"] = len(value)
    return proof


def observable_literal(value: Any) -> Any:
    if isinstance(value, str):
        if (
            0 < len(value) <= MAX_LITERAL_STRING_LENGTH
            and value.isprintable()
            and _string_format(value) is None
        ):
            return value
    if isinstance(value, bool) or value is None:
        return value
    return _MISSING


def _string_format(value: str) -> str | None:
    stripped = value.strip()
    if _OBJECT_ID_LIKE_RE.fullmatch(stripped):
        return "object_id_like"
    if _DATE_LIKE_RE.fullmatch(stripped):
        return "date_like"
    return None


def parse_path(path: str) -> tuple[str, ...]:
    parts: list[str] = []
    for raw in str(path).split("."):
        if not raw:
            continue
        while raw.endswith("[]"):
            stem = raw[:-2]
            if stem:
                parts.append(stem)
            parts.append("[]")
            raw = ""
        if raw:
            parts.append(raw)
    return tuple(parts)


def extract_path_values(doc: Mapping[str, Any], path: str) -> list[Any]:
    values = [_extract_one(doc, parse_path(path))]
    return [value for value in values if value is not _MISSING]


def _extract_one(value: Any, parts: tuple[str, ...]) -> Any:
    if not parts:
        return value
    head, tail = parts[0], parts[1:]
    if head == "[]":
        if not isinstance(value, list):
            return _MISSING
        out: list[Any] = []
        for item in value:
            child = _extract_one(item, tail)
            if child is _MISSING:
                continue
            if isinstance(child, list):
                out.extend(child)
            else:
                out.append(child)
        return out if out else _MISSING
    if head == "*":
        if not isinstance(value, Mapping):
            return _MISSING
        out = []
        for item in value.values():
            child = _extract_one(item, tail)
            if child is _MISSING:
                continue
            if isinstance(child, list):
                out.extend(child)
            else:
                out.append(child)
        return out if out else _MISSING
    if isinstance(value, list):
        out = []
        for item in value:
            child = _extract_one(item, parts)
            if child is _MISSING:
                continue
            if isinstance(child, list):
                out.extend(child)
            else:
                out.append(child)
        return out if out else _MISSING
    if isinstance(value, Mapping) and head in value:
        return _extract_one(value[head], tail)
    return _MISSING


def flatten_extracted_values(values: Iterable[Any]) -> list[Any]:
    out: list[Any] = []
    for value in values:
        if isinstance(value, list):
            out.extend(flatten_extracted_values(value))
        else:
            out.append(value)
    return out


def walk_document_paths(doc: Mapping[str, Any]) -> dict[str, list[Any]]:
    paths: dict[str, list[Any]] = {}
    _walk_paths(doc, (), paths)
    return paths


def summarize_path_map(path_values: dict[str, list[Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path, values in sorted(path_values.items()):
        counts = Counter(value_kind(value) for value in values)
        out[path] = {
            "value_count": len(values),
            "type_counts": dict(sorted(counts.items())),
        }
    return out


def summarize_type_counts(values: Iterable[Any]) -> dict[str, int]:
    return dict(sorted(Counter(value_kind(value) for value in values).items()))


def _walk_paths(value: Any, path: tuple[str, ...], paths: dict[str, list[Any]]) -> None:
    if isinstance(value, Mapping):
        if path:
            paths.setdefault(_format_path(path), []).append(value)
        for key, child in value.items():
            _walk_paths(child, path + (str(key),), paths)
        return
    if isinstance(value, list):
        if path:
            paths.setdefault(_format_path(path), []).append(value)
        for item in value:
            if isinstance(item, Mapping):
                _walk_paths(item, path + ("[]",), paths)
            else:
                paths.setdefault(_format_path(path + ("[]",)), []).append(item)
        return
    if path:
        paths.setdefault(_format_path(path), []).append(value)


def _format_path(path: tuple[str, ...]) -> str:
    out: list[str] = []
    for part in path:
        if part == "[]":
            if out:
                out[-1] += "[]"
            else:
                out.append("[]")
        else:
            out.append(part)
    return ".".join(out)


def _numeric_class(value: int | float) -> str:
    if value == 0:
        return "zero"
    if value > 0:
        return "positive"
    return "negative"
