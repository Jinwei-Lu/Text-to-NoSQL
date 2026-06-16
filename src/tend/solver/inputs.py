"""Shared public input helpers for solver-like runtimes."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..errors import PromptAnomalyError
from ..release_layout import resolve_release_dataset_layout
from .introspection import introspect_solver_database

if TYPE_CHECKING:
    from ..workflow import Workflow

DEFAULT_INPUT_SAMPLE_SIZE = 3
DEFAULT_WITNESS_K = 3
NlqTrack = Literal["record", "canonical", "colloquial"]

_WITNESS_MAX_STRING_CHARS = 160
_WITNESS_MAX_LIST_ITEMS = 8
_WITNESS_MAX_DICT_ITEMS = 24
_WITNESS_MAX_DEPTH = 5


@dataclass(frozen=True)
class NlqDbSolverInput:
    record: dict[str, Any]
    schema: dict[str, Any]
    local_data: dict[str, list[dict[str, Any]]]


async def build_nlq_db_solver_input(
    wf: "Workflow",
    *,
    db_id: str,
    nlq: str,
    record_id: int | None = None,
    sample_size: int = DEFAULT_INPUT_SAMPLE_SIZE,
    witness_k: int | None = None,
) -> NlqDbSolverInput:
    """Derive the public solver input from only a MongoDB database and an NLQ."""
    effective_sample_size = witness_k if witness_k is not None else sample_size
    snapshot = await asyncio.to_thread(
        introspect_solver_database,
        wf.ctx.mongo,
        db_id,
        sample_size=max(1, effective_sample_size),
    )
    record: dict[str, Any] = {
        "db_id": db_id,
        "nl_queries": {"canonical": nlq},
    }
    if record_id is not None:
        record["record_id"] = record_id
    return NlqDbSolverInput(
        record=record,
        schema=snapshot.schema,
        local_data=snapshot.local_data,
    )


def load_solver_release_inputs(
    dataset_dir: Path,
    *,
    db_id: str | None = None,
    record_id: int | None = None,
    limit: int | None = None,
    nlq_track: NlqTrack = "record",
) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]]:
    """Load release records plus public schema/data assets."""
    layout = resolve_release_dataset_layout(dataset_dir)
    out: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]] = []
    for record in select_solver_release_records(
        dataset_dir,
        db_id=db_id,
        record_id=record_id,
        limit=limit,
        nlq_track=nlq_track,
    ):
        rid = record["db_id"]
        schema_path = layout.mongodb_schema_dir / f"{rid}.json"
        schema = (
            json.loads(schema_path.read_text(encoding="utf-8"))
            if schema_path.exists()
            else {}
        )
        data_path = layout.mongodb_data_dir / f"{rid}.json"
        data = json.loads(data_path.read_text(encoding="utf-8")) if data_path.exists() else None
        out.append((record, schema, data))
    return out


def select_solver_release_records(
    dataset_dir: Path,
    *,
    db_id: str | None = None,
    record_id: int | None = None,
    limit: int | None = None,
    nlq_track: NlqTrack = "record",
) -> list[dict[str, Any]]:
    """Select release records without loading schema/data assets."""
    layout = resolve_release_dataset_layout(dataset_dir)
    records = json.loads(layout.test_path.read_text(encoding="utf-8"))
    out: list[dict[str, Any]] = []
    for record in records:
        if db_id and record.get("db_id") != db_id:
            continue
        if record_id is not None and record.get("record_id") != record_id:
            continue
        out.append(_record_for_nlq_track(record, nlq_track))
        if limit is not None and len(out) >= limit:
            break
    return out


def _record_for_nlq_track(record: dict[str, Any], nlq_track: NlqTrack) -> dict[str, Any]:
    if nlq_track == "record":
        return record
    nl_queries = record.get("nl_queries")
    selected = nl_queries.get(nlq_track) if isinstance(nl_queries, dict) else None
    if selected is None:
        lean_key = "NLQ" if nlq_track == "canonical" else "NLQ_colloquial"
        selected = record.get(lean_key)
    if not isinstance(selected, str) or not selected.strip():
        raise PromptAnomalyError(
            f"record missing {nlq_track} NLQ track",
            context={"record_id": record.get("record_id"), "db_id": record.get("db_id")},
        )
    out = dict(record)
    out["nl_queries"] = {"canonical": selected}
    out["nlq_track"] = nlq_track
    return out


def build_witness_digest(
    data: dict[str, list[dict[str, Any]]] | None,
    witness_k: int,
    *,
    redact_values: bool = False,
) -> dict[str, Any]:
    """Build the small prompt-visible witness digest allowed in non-EG solver prompts.

    With ``redact_values`` the digest keeps document *structure* (field names, nesting,
    array shapes) but replaces every scalar with its type tag -- a fair "you can see the
    shape, not the answer rows" view that mirrors what the EG solver induces by redacted
    exploration, for comparisons that should not hand baselines raw sample values.
    """
    if not data:
        return {}
    k = max(0, witness_k)
    digest: dict[str, Any] = {}
    for collection, docs in sorted(data.items()):
        if not isinstance(docs, list):
            continue
        sample = [_compact_witness_value(doc, redact_values=redact_values) for doc in docs[:k]]
        entry: dict[str, Any] = {"sample_count": len(sample)}
        if redact_values:
            entry["structure_documents"] = sample
            entry["values_redacted"] = True
        else:
            entry["sample_documents"] = sample
            entry["string_values_in_sample"] = _string_values_in_sample(sample)
        digest[collection] = entry
    return digest


def _compact_witness_value(value: Any, *, depth: int = 0, redact_values: bool = False) -> Any:
    if redact_values and isinstance(value, (str, bool, int, float)):
        return f"<{type(value).__name__}>"
    if redact_values and value is None:
        return "<null>"
    if isinstance(value, str):
        if len(value) <= _WITNESS_MAX_STRING_CHARS:
            return value
        return value[: _WITNESS_MAX_STRING_CHARS - 3] + "..."
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= _WITNESS_MAX_DEPTH:
        if isinstance(value, dict):
            return {
                "__truncated_depth__": True,
                "__keys__": sorted(str(key) for key in value)[:_WITNESS_MAX_DICT_ITEMS],
            }
        if isinstance(value, list):
            return {
                "__truncated_depth__": True,
                "__item_count__": len(value),
            }
        return str(value)[:_WITNESS_MAX_STRING_CHARS]
    if isinstance(value, list):
        preview = [
            _compact_witness_value(item, depth=depth + 1, redact_values=redact_values)
            for item in value[:_WITNESS_MAX_LIST_ITEMS]
        ]
        if len(value) > _WITNESS_MAX_LIST_ITEMS:
            preview.append({"__truncated_items__": len(value) - _WITNESS_MAX_LIST_ITEMS})
        return preview
    if isinstance(value, dict):
        items = list(value.items())
        preview = {
            str(key): _compact_witness_value(child, depth=depth + 1, redact_values=redact_values)
            for key, child in items[:_WITNESS_MAX_DICT_ITEMS]
        }
        if len(items) > _WITNESS_MAX_DICT_ITEMS:
            preview["__truncated_keys__"] = len(items) - _WITNESS_MAX_DICT_ITEMS
            preview["__keys__"] = sorted(str(key) for key in value)[:_WITNESS_MAX_DICT_ITEMS]
        return preview
    return str(value)[:_WITNESS_MAX_STRING_CHARS]


def _string_values_in_sample(docs: list[dict[str, Any]]) -> dict[str, list[str]]:
    values: dict[str, set[str]] = {}
    for doc in docs:
        for key, value in doc.items():
            if isinstance(value, str):
                values.setdefault(key, set()).add(value)
            elif isinstance(value, dict):
                for subkey, subvalue in value.items():
                    if isinstance(subvalue, str):
                        values.setdefault(f"{key}.{subkey}", set()).add(subvalue)
    return {key: sorted(vals)[:24] for key, vals in sorted(values.items())}


def _canonical_nlq(record: dict[str, Any], *, use_colloquial: bool = True) -> str:
    nl_queries = record.get("nl_queries")
    if isinstance(nl_queries, dict):
        candidates = [nl_queries.get("canonical")]
        if use_colloquial:
            candidates.append(nl_queries.get("colloquial"))
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate
    for key in ("NLQ", "query"):
        candidate = record.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    raise PromptAnomalyError(
        "solver record missing natural language question",
        context={"record_id": record.get("record_id"), "db_id": record.get("db_id")},
    )


__all__ = [
    "DEFAULT_INPUT_SAMPLE_SIZE",
    "DEFAULT_WITNESS_K",
    "NlqDbSolverInput",
    "NlqTrack",
    "_canonical_nlq",
    "build_nlq_db_solver_input",
    "build_witness_digest",
    "load_solver_release_inputs",
    "select_solver_release_records",
]
