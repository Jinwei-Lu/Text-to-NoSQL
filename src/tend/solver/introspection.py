"""Solver-side database introspection for NLQ+DB-only solving."""
from __future__ import annotations

from collections import Counter
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import ExecutionError

_PRESENCE_STATES = {"present", "missing", "empty", "null"}


@dataclass(frozen=True)
class SolverDatabaseSnapshot:
    schema: dict[str, Any]
    local_data: dict[str, list[dict[str, Any]]]


def introspect_solver_database(
    mongo: Any,
    db_id: str,
    *,
    sample_size: int,
) -> SolverDatabaseSnapshot:
    """Query MongoDB for a bounded solver-visible schema and witness snapshot."""
    if mongo is None:
        raise ExecutionError("solver NLQ+DB mode requires MongoDB", context={"db_id": db_id})
    snapshotter = getattr(mongo, "snapshot_database", None)
    if not callable(snapshotter):
        raise ExecutionError(
            "Mongo executor does not expose solver database introspection",
            context={"db_id": db_id, "missing": "snapshot_database"},
        )
    local_data = snapshotter(db_id, max(1, sample_size))
    if not isinstance(local_data, dict):
        raise ExecutionError(
            "Mongo introspection returned invalid snapshot",
            context={"db_id": db_id, "got_type": type(local_data).__name__},
        )
    clean_data = {
        str(collection): [doc for doc in docs if isinstance(doc, dict)]
        for collection, docs in local_data.items()
        if isinstance(docs, list)
    }
    return SolverDatabaseSnapshot(
        schema={
            "db_id": db_id,
            "collections": {
                collection: _summarize_collection(docs)
                for collection, docs in sorted(clean_data.items())
            },
        },
        local_data=clean_data,
    )


def _summarize_collection(docs: list[dict[str, Any]]) -> dict[str, Any]:
    fields: dict[str, str] = {}
    dynamic_key_paths: set[str] = set()
    dynamic_key_samples: dict[str, set[str]] = defaultdict(set)
    array_paths: set[str] = set()
    dynamic_array_object_paths: set[str] = set()
    array_object_dynamic_paths: set[str] = set()
    presence_counts: Counter[str] = Counter()
    for doc in docs:
        _walk(
            doc,
            path=(),
            tokens=("object",),
            fields=fields,
            dynamic_key_paths=dynamic_key_paths,
            dynamic_key_samples=dynamic_key_samples,
            array_paths=array_paths,
            dynamic_array_object_paths=dynamic_array_object_paths,
            array_object_dynamic_paths=array_object_dynamic_paths,
            presence_counts=presence_counts,
        )
    # NOTE: changes measured behavior; affected ablation/leaderboard numbers need re-run (review fix introspection-schema_flex)
    schema_flex = "native_deep" if dynamic_key_paths or presence_counts else "none"
    return {
        "doc_count": len(docs),
        "schema_flex": schema_flex,
        "fields": fields,
        "dynamic_key_paths": sorted(dynamic_key_paths),
        "dynamic_key_samples": {
            path: sorted(samples)[:8]
            for path, samples in sorted(dynamic_key_samples.items())
        },
        "array_paths": sorted(array_paths),
        "dynamic_array_object_paths": sorted(dynamic_array_object_paths),
        "array_object_dynamic_paths": sorted(array_object_dynamic_paths),
        "presence_state_counts": dict(sorted(presence_counts.items())),
    }


def _walk(
    value: Any,
    *,
    path: tuple[str, ...],
    tokens: tuple[str, ...],
    fields: dict[str, str],
    dynamic_key_paths: set[str],
    dynamic_key_samples: dict[str, set[str]],
    array_paths: set[str],
    dynamic_array_object_paths: set[str],
    array_object_dynamic_paths: set[str],
    presence_counts: Counter[str],
) -> None:
    if isinstance(value, Mapping):
        is_dynamic = bool(path) and _is_dynamic_object(path, value)
        if path:
            fields.setdefault(_format_path(path), "object")
            if is_dynamic:
                dynamic_key_paths.add(_format_path(path))
                if _has_ordered_shape(tokens + ("dynamic_key",), ("array", "object", "dynamic_key")):
                    array_object_dynamic_paths.add(_format_path(path))
        for key, item in value.items():
            if is_dynamic:
                dynamic_key_samples[_format_path(path)].add(str(key))
            key_text = "*" if is_dynamic else str(key)
            next_path = path + (key_text,)
            _walk(
                item,
                path=next_path,
                tokens=tokens + (("dynamic_key" if is_dynamic else "object"),),
                fields=fields,
                dynamic_key_paths=dynamic_key_paths,
                dynamic_key_samples=dynamic_key_samples,
                array_paths=array_paths,
                dynamic_array_object_paths=dynamic_array_object_paths,
                array_object_dynamic_paths=array_object_dynamic_paths,
                presence_counts=presence_counts,
            )
        return
    if isinstance(value, list):
        if path:
            text = _format_path(path)
            fields.setdefault(text, "array")
            array_paths.add(text)
            if any(isinstance(item, Mapping) for item in value) and _has_ordered_shape(
                tokens + ("array", "object"),
                ("object", "dynamic_key", "array", "object"),
            ):
                dynamic_array_object_paths.add(_format_path(path + ("[]",)))
        for item in value[:8]:
            _walk(
                item,
                path=path + ("[]",) if isinstance(item, Mapping) else path,
                tokens=tokens + ("array",) + (("object",) if isinstance(item, Mapping) else ()),
                fields=fields,
                dynamic_key_paths=dynamic_key_paths,
                dynamic_key_samples=dynamic_key_samples,
                array_paths=array_paths,
                dynamic_array_object_paths=dynamic_array_object_paths,
                array_object_dynamic_paths=array_object_dynamic_paths,
                presence_counts=presence_counts,
            )
        return
    if path:
        fields.setdefault(_format_path(path), _scalar_kind(value))
    if isinstance(value, str):
        state = value.strip().lower()
        if state in _PRESENCE_STATES:
            presence_counts[state] += 1


def _is_dynamic_object(path: tuple[str, ...], value: Mapping[str, Any]) -> bool:
    if not value:
        return False
    leaf = path[-1] if path else ""
    if leaf.startswith("by_") or "_by_" in leaf or leaf.endswith("_bag") or leaf.endswith("_index"):
        return True
    keys = [str(key) for key in value]
    return len(keys) >= 2 and all(_looks_dynamic_key(key) for key in keys)


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
        if token != pattern[cursor]:
            continue
        cursor += 1
        if cursor == len(pattern):
            return True
    return False


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


def _scalar_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return type(value).__name__
