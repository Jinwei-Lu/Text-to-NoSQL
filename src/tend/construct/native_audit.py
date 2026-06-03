"""Structure audit for MongoDB-native DataWorld artifacts.

The audit is intentionally independent from Phase B. Its job is to inspect the
materialized MongoDB JSON and answer a concrete question: does this database contain deep,
query-bearing MongoDB-native structure, or only shallow table-shaped documents?
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DynamicKeyPath:
    path: str
    sample_keys: list[str] = field(default_factory=list)
    document_count: int = 0
    value_kinds: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NativeStructureAudit:
    db_id: str
    collection_counts: dict[str, int]
    max_depth: int
    dynamic_key_paths: list[DynamicKeyPath]
    nested_array_paths: list[str]
    array_lengths: dict[str, dict[str, float | int]]
    dynamic_array_object_paths: list[str]
    array_object_dynamic_paths: list[str]
    presence_state_counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_id": self.db_id,
            "collection_counts": dict(self.collection_counts),
            "max_depth": self.max_depth,
            "dynamic_key_paths": [item.__dict__ for item in self.dynamic_key_paths],
            "nested_array_paths": list(self.nested_array_paths),
            "array_lengths": dict(self.array_lengths),
            "dynamic_array_object_paths": list(self.dynamic_array_object_paths),
            "array_object_dynamic_paths": list(self.array_object_dynamic_paths),
            "presence_state_counts": dict(self.presence_state_counts),
        }


@dataclass(frozen=True)
class StructureGateResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "evidence": list(self.evidence),
        }


def audit_database_structure(
    db_id: str,
    data: dict[str, list[dict[str, Any]]],
) -> NativeStructureAudit:
    """Inspect materialized MongoDB documents for deep native structure."""
    collection_counts = {
        collection: len(docs) for collection, docs in sorted(data.items())
    }
    state = _AuditState()
    for docs in data.values():
        for doc in docs:
            if isinstance(doc, dict):
                _walk(doc, path=(), tokens=("object",), state=state, depth=1)

    dynamic_key_paths = [
        DynamicKeyPath(
            path=path,
            sample_keys=sorted(samples)[:8],
            document_count=state.dynamic_path_counts[path],
            value_kinds=sorted(state.dynamic_value_kinds[path]),
        )
        for path, samples in sorted(state.dynamic_key_samples.items())
    ]
    array_lengths = {
        path: _length_stats(lengths)
        for path, lengths in sorted(state.array_lengths.items())
    }
    return NativeStructureAudit(
        db_id=db_id,
        collection_counts=collection_counts,
        max_depth=max(0, state.max_depth - 1),
        dynamic_key_paths=dynamic_key_paths,
        nested_array_paths=sorted(state.array_lengths),
        array_lengths=array_lengths,
        dynamic_array_object_paths=sorted(state.dynamic_array_object_paths),
        array_object_dynamic_paths=sorted(state.array_object_dynamic_paths),
        presence_state_counts=dict(sorted(state.presence_state_counts.items())),
    )


def validate_structure_gate(
    audit: NativeStructureAudit,
    *,
    min_depth: int = 4,
) -> StructureGateResult:
    """Apply the high-nesting native complexity gate to one database audit."""
    errors: list[str] = []
    evidence: list[str] = []
    if audit.max_depth < min_depth:
        errors.append(f"maximum nested depth is {audit.max_depth}, expected at least {min_depth}")
    else:
        evidence.append(f"max_depth={audit.max_depth}")
    if not audit.dynamic_array_object_paths:
        errors.append("missing path with object -> dynamic key -> array -> object structure")
    else:
        evidence.append(f"dynamic_array_object_paths={len(audit.dynamic_array_object_paths)}")
    if not audit.array_object_dynamic_paths:
        errors.append("missing path with array -> object -> dynamic key or attribute bag structure")
    else:
        evidence.append(f"array_object_dynamic_paths={len(audit.array_object_dynamic_paths)}")
    if not _has_presence_states(audit.presence_state_counts):
        errors.append("missing explicit present/missing/empty/null presence-state evidence")
    else:
        evidence.append(f"presence_states={audit.presence_state_counts}")
    return StructureGateResult(ok=not errors, errors=errors, evidence=evidence)


@dataclass
class _AuditState:
    max_depth: int = 0
    dynamic_key_samples: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    dynamic_path_counts: Counter[str] = field(default_factory=Counter)
    dynamic_value_kinds: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    array_lengths: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    dynamic_array_object_paths: set[str] = field(default_factory=set)
    array_object_dynamic_paths: set[str] = field(default_factory=set)
    presence_state_counts: Counter[str] = field(default_factory=Counter)


def _walk(
    value: Any,
    *,
    path: tuple[str, ...],
    tokens: tuple[str, ...],
    state: _AuditState,
    depth: int,
) -> None:
    state.max_depth = max(state.max_depth, depth)
    if isinstance(value, dict):
        _collect_presence_states(value, state)
        is_dynamic = _is_dynamic_object(path, value)
        if is_dynamic and path:
            path_text = _format_path(path)
            state.dynamic_path_counts[path_text] += 1
            for key, item in value.items():
                state.dynamic_key_samples[path_text].add(str(key))
                state.dynamic_value_kinds[path_text].add(_kind(item))
        for key, item in value.items():
            key_text = "*" if is_dynamic else str(key)
            next_tokens = tokens + (("dynamic_key" if is_dynamic else "object"),)
            next_path = path + (key_text,)
            if is_dynamic and _has_ordered_shape(next_tokens, ("object", "dynamic_key", "array", "object")):
                state.dynamic_array_object_paths.add(_format_path(next_path))
            if is_dynamic and _has_ordered_shape(next_tokens, ("array", "object", "dynamic_key")):
                state.array_object_dynamic_paths.add(_format_path(next_path))
            _walk(item, path=next_path, tokens=next_tokens, state=state, depth=depth + 1)
    elif isinstance(value, list):
        path_text = _format_path(path + ("[]",))
        state.array_lengths[path_text].append(len(value))
        array_tokens = tokens + ("array",)
        for item in value:
            item_tokens = array_tokens + (("object",) if isinstance(item, dict) else ())
            if _has_ordered_shape(item_tokens, ("object", "dynamic_key", "array", "object")):
                state.dynamic_array_object_paths.add(path_text)
            _walk(item, path=path + ("[]",), tokens=item_tokens, state=state, depth=depth + 1)


def _collect_presence_states(doc: dict[str, Any], state: _AuditState) -> None:
    for value in doc.values():
        if isinstance(value, str) and value.lower() in {"present", "missing", "empty", "null"}:
            state.presence_state_counts[value.lower()] += 1


def _is_dynamic_object(path: tuple[str, ...], value: dict[str, Any]) -> bool:
    if not value:
        return False
    leaf = path[-1] if path else ""
    if leaf.startswith("_"):
        return False
    if leaf.startswith("by_") or "_by_" in leaf or leaf.endswith("_bag") or leaf.endswith("_index"):
        return True
    keys = [str(key) for key in value]
    if len(keys) >= 2 and all(_looks_dynamic_key(key) for key in keys):
        return True
    return False


def _looks_dynamic_key(key: str) -> bool:
    if key.isdigit():
        return True
    if any(char.isdigit() for char in key) and ("-" in key or "/" in key or ":" in key):
        return True
    if len(key) >= 24 and "-" in key:
        return True
    if " " in key:
        return True
    return False


def _has_ordered_shape(tokens: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    cursor = 0
    for token in tokens:
        if token == pattern[cursor]:
            cursor += 1
            if cursor == len(pattern):
                return True
    return False


def _has_presence_states(counts: dict[str, int]) -> bool:
    return bool(counts.get("present")) and (
        bool(counts.get("missing")) or bool(counts.get("empty")) or bool(counts.get("null"))
    )


def _length_stats(lengths: list[int]) -> dict[str, float | int]:
    if not lengths:
        return {"count": 0, "min": 0, "max": 0, "avg": 0.0}
    return {
        "count": len(lengths),
        "min": min(lengths),
        "max": max(lengths),
        "avg": round(sum(lengths) / len(lengths), 4),
    }


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


def _kind(value: Any) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if value is None:
        return "null"
    return type(value).__name__
