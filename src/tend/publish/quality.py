"""Strict release-quality audit for NLQ/MQL/DB triplets.

The regular release validator is intentionally mostly file/static-contract based.
This module adds a stricter, Mongo-backed audit layer for defects that still pass
basic validation: field paths that execute as null/zero, unstable order-sensitive
gold results, and NLQ text that hides answer-changing constraints.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..execution.ast_check import parse_pipeline, root_ops
from ..execution.mongo import MongoExecutor
from ..execution.signature import canonical_json, mql_signature, mql_skeleton_signature, mql_skeleton_summary
from ..observability import RunLogger
from ..release_layout import resolve_release_dataset_layout


ERROR = "error"
WARNING = "warning"

_NON_FIELD_STRING_KEYS = frozenset({
    "as",
    "from",
    "foreignField",
    "timezone",
    "format",
    "unit",
})
_SYSTEM_FIELD_NAMES = frozenset({"ROOT", "CURRENT", "REMOVE"})
_NL_STOPWORDS = frozenset({
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "or",
    "per",
    "the",
    "to",
    "up",
    "with",
})
_TOP_WORDS = frozenset({
    "top",
    "highest",
    "largest",
    "greatest",
    "most",
    "maximum",
    "max",
    "lowest",
    "smallest",
    "least",
    "minimum",
    "min",
    "high",
    "low",
    "rank",
    "ranked",
    "strongest",
    "weakest",
    "heaviest",
})
_STATE_WORDS = frozenset({"present", "missing", "null", "empty"})
_OUTPUT_SHAPE_PRESERVING_OPS = frozenset({"$limit", "$match", "$skip", "$sort"})
_FIELD_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.$]*$")
_FIELD_SPLIT_RE = re.compile(r"\s*,\s*|\s+\band\b\s+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class QualityIssue:
    severity: str
    code: str
    db_id: str
    record_id: Any
    message: str
    track: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "db_id": self.db_id,
            "record_id": self.record_id,
            "track": self.track,
            "message": self.message,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class ReleaseQualityReport:
    ok: bool
    dataset_dir: str
    records_checked: int
    errors: int
    warnings: int
    by_code: dict[str, int]
    by_db: dict[str, int]
    issues: list[QualityIssue]
    paths: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dataset_dir": self.dataset_dir,
            "records_checked": self.records_checked,
            "errors": self.errors,
            "warnings": self.warnings,
            "by_code": self.by_code,
            "by_db": self.by_db,
            "issues": [issue.as_dict() for issue in self.issues],
            "paths": self.paths,
        }


def run_release_quality_audit(
    dataset_dir: str | Path,
    *,
    executor: MongoExecutor,
    out_dir: str | Path | None = None,
    logger: RunLogger | None = None,
    db_id: str | None = None,
    record_id: int | None = None,
    limit: int | None = None,
    repeat_order_sensitive: int = 2,
    check_nlq: bool = True,
    check_field_paths: bool = True,
) -> ReleaseQualityReport:
    """Run strict, Mongo-backed quality checks over a release dataset."""

    layout = resolve_release_dataset_layout(dataset_dir)
    records = _load_records(layout.tend_path if layout.tend_path.exists() else layout.test_path)
    if db_id:
        records = [record for record in records if str(record.get("db_id")) == db_id]
    if record_id is not None:
        records = [record for record in records if record.get("record_id") == record_id]
    if limit is not None:
        records = records[:max(0, int(limit))]

    issues: list[QualityIssue] = []
    by_db_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_db_records[str(record.get("db_id") or "")].append(record)

    for current_db_id, db_records in sorted(by_db_records.items()):
        data_path = layout.mongodb_data_dir / f"{current_db_id}.json"
        if not data_path.exists():
            for record in db_records:
                issues.append(_issue(
                    ERROR,
                    "MONGODB_DATA_MISSING",
                    record,
                    f"missing MongoDB data file for db_id={current_db_id}",
                    evidence={"path": str(data_path)},
                ))
            continue

        collections = json.loads(data_path.read_text(encoding="utf-8"))
        if not isinstance(collections, dict):
            for record in db_records:
                issues.append(_issue(
                    ERROR,
                    "MONGODB_DATA_INVALID",
                    record,
                    "MongoDB data file must be an object of collections",
                    evidence={"path": str(data_path)},
                ))
            continue

        executor.load_witness(current_db_id, collections)
        db = executor._connect()[executor._db_name(current_db_id)]  # noqa: SLF001 - audit needs prefix probes
        exists_cache: dict[tuple[str, str, str], bool] = {}

        for record in db_records:
            record_issues = _audit_record(
                record,
                collections=collections,
                mongo_db=db,
                executor=executor,
                repeat_order_sensitive=repeat_order_sensitive,
                check_nlq=check_nlq,
                check_field_paths=check_field_paths,
                exists_cache=exists_cache,
            )
            issues.extend(record_issues)
            if logger and record_issues:
                logger.info(
                    "quality_audit_record_issues",
                    db_id=current_db_id,
                    record_id=record.get("record_id"),
                    issue_count=len(record_issues),
                    codes=sorted({issue.code for issue in record_issues}),
                )

    report = _build_report(layout.root, len(records), issues)
    if out_dir is not None:
        report = _write_report(report, Path(out_dir))
    return report


def _load_records(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = raw.get("records", []) if isinstance(raw, dict) else raw
    return [record for record in records if isinstance(record, dict)]


def _audit_record(
    record: dict[str, Any],
    *,
    collections: dict[str, Any],
    mongo_db: Any,
    executor: MongoExecutor,
    repeat_order_sensitive: int,
    check_nlq: bool,
    check_field_paths: bool,
    exists_cache: dict[tuple[str, str, str], bool],
) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    db_id = str(record.get("db_id") or "")
    mql = str(record.get("MQL") or "")

    try:
        collection, pipeline = parse_pipeline(mql)
    except Exception as exc:  # noqa: BLE001 - release record quality issue
        return [_issue(
            ERROR,
            "MQL_PARSE_ERROR",
            record,
            f"gold MQL is not parseable: {exc}",
            evidence={"mql_preview": mql[:240]},
        )]

    if collection not in collections:
        issues.append(_issue(
            ERROR,
            "ROOT_COLLECTION_MISSING",
            record,
            f"root collection {collection!r} is absent from mongodb_data/{db_id}.json",
            evidence={"collection": collection, "available": sorted(collections)},
        ))
        return issues

    issues.extend(_signature_issues(record, collection, pipeline, mql))

    rows: list[dict[str, Any]] | None = None
    try:
        rows = executor.norm_exec(db_id, mql)
        if not rows:
            issues.append(_issue(
                ERROR,
                "GOLD_RESULT_EMPTY",
                record,
                "gold MQL executed but returned no rows",
                evidence={"collection": collection},
            ))
    except Exception as exc:  # noqa: BLE001 - release record quality issue
        issues.append(_issue(
            ERROR,
            "GOLD_EXECUTION_ERROR",
            record,
            f"gold MQL execution failed: {exc}",
            evidence={"collection": collection},
        ))

    if rows is not None and repeat_order_sensitive > 1 and _is_order_sensitive(pipeline):
        issues.extend(_stability_issues(
            record,
            executor=executor,
            mql=mql,
            first_rows=rows,
            repeats=repeat_order_sensitive,
            pipeline=pipeline,
        ))

    if check_field_paths:
        issues.extend(_field_path_issues(
            record,
            mongo_db=mongo_db,
            collection=collection,
            pipeline=pipeline,
            exists_cache=exists_cache,
        ))

    if rows is not None:
        issues.extend(_result_shape_issues(record, rows=rows, pipeline=pipeline))

    if check_nlq:
        for track, text in _nlq_tracks(record).items():
            issues.extend(_nlq_alignment_issues(record, track=track, text=text, pipeline=pipeline))

    return issues


def _signature_issues(
    record: dict[str, Any],
    collection: str,
    pipeline: list[dict[str, Any]],
    mql: str,
) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    checks = {
        "mql_signature": mql_signature(mql),
        "mql_skeleton_signature": mql_skeleton_signature(mql),
        "mql_skeleton_summary": mql_skeleton_summary(mql),
    }
    for key, expected in checks.items():
        stored = record.get(key)
        if stored is not None and stored != expected:
            issues.append(_issue(
                ERROR,
                "MQL_SIGNATURE_STALE",
                record,
                f"{key} does not match current MQL",
                evidence={"field": key, "stored": stored, "computed": expected},
            ))
    return issues


def _stability_issues(
    record: dict[str, Any],
    *,
    executor: MongoExecutor,
    mql: str,
    first_rows: list[dict[str, Any]],
    repeats: int,
    pipeline: list[dict[str, Any]],
) -> list[QualityIssue]:
    hashes = [_hash_rows(first_rows)]
    for _ in range(max(0, repeats - 1)):
        try:
            hashes.append(_hash_rows(executor.norm_exec(str(record.get("db_id") or ""), mql)))
        except Exception as exc:  # noqa: BLE001 - execution issue was already captured
            return [_issue(
                ERROR,
                "GOLD_REPEAT_EXECUTION_ERROR",
                record,
                f"gold MQL failed during repeat execution: {exc}",
                evidence={"hashes_before_failure": hashes},
            )]
    unique_hashes = sorted(set(hashes))
    if len(unique_hashes) > 1:
        return [_issue(
            ERROR,
            "GOLD_RESULT_NONDETERMINISTIC",
            record,
            "repeated gold MQL execution returned different normalized results",
            evidence={
                "hashes": hashes,
                "unique_hashes": unique_hashes,
                "sort_specs": _sort_specs(pipeline),
                "limit": _first_limit(pipeline),
            },
        )]
    return []


def _field_path_issues(
    record: dict[str, Any],
    *,
    mongo_db: Any,
    collection: str,
    pipeline: list[dict[str, Any]],
    exists_cache: dict[tuple[str, str, str], bool],
) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    for index, stage in enumerate(pipeline):
        prefix = pipeline[:index]
        prefix_key = canonical_json(prefix)
        for ref in sorted(_stage_input_refs(stage)):
            if not _field_exists_after_prefix(
                mongo_db,
                collection,
                prefix,
                ref,
                cache=exists_cache,
                prefix_key=prefix_key,
            ):
                issues.append(_issue(
                    ERROR,
                    "FIELD_PATH_MISSING",
                    record,
                    f"MQL references field path {ref!r} that is absent at stage input",
                    evidence={
                        "collection": collection,
                        "stage_index": index + 1,
                        "stage_op": _stage_op(stage),
                        "field_path": ref,
                    },
                ))
    issues.extend(_lookup_foreign_field_issues(record, collections=mongo_db, pipeline=pipeline))
    return issues


def _lookup_foreign_field_issues(
    record: dict[str, Any],
    *,
    collections: Any,
    pipeline: list[dict[str, Any]],
) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    for index, stage in enumerate(pipeline):
        lookup = stage.get("$lookup") if isinstance(stage, dict) else None
        if not isinstance(lookup, dict):
            continue
        from_collection = lookup.get("from")
        foreign_field = lookup.get("foreignField")
        if not isinstance(from_collection, str) or not isinstance(foreign_field, str):
            continue
        if from_collection not in collections.list_collection_names():
            issues.append(_issue(
                ERROR,
                "LOOKUP_COLLECTION_MISSING",
                record,
                f"$lookup.from collection {from_collection!r} is absent",
                evidence={"stage_index": index + 1, "from": from_collection},
            ))
            continue
        if not collections[from_collection].find_one({foreign_field: {"$exists": True}}, {"_id": 1}):
            issues.append(_issue(
                ERROR,
                "LOOKUP_FOREIGN_FIELD_MISSING",
                record,
                f"$lookup foreignField {foreign_field!r} is absent in {from_collection!r}",
                evidence={
                    "stage_index": index + 1,
                    "from": from_collection,
                    "foreignField": foreign_field,
                },
            ))
    return issues


def _field_exists_after_prefix(
    mongo_db: Any,
    collection: str,
    prefix: list[dict[str, Any]],
    field_path: str,
    *,
    cache: dict[tuple[str, str, str], bool],
    prefix_key: str,
) -> bool:
    key = (collection, prefix_key, field_path)
    if key in cache:
        return cache[key]
    probe = [
        *prefix,
        {"$match": {field_path: {"$exists": True}}},
        {"$limit": 1},
        {"$project": {"_id": 1}},
    ]
    try:
        exists = bool(list(mongo_db[collection].aggregate(probe, maxTimeMS=30_000)))
    except Exception:
        exists = False
    cache[key] = exists
    return exists


def _result_shape_issues(
    record: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    pipeline: list[dict[str, Any]],
) -> list[QualityIssue]:
    if not rows:
        return []
    fields = sorted({field for row in rows[:20] for field in _leaf_paths(row)})
    if not fields:
        return [_issue(
            ERROR,
            "GOLD_RESULT_SHAPE_EMPTY",
            record,
            "gold MQL returned rows with no inspectable fields",
            evidence={"sample_rows": min(len(rows), 20)},
        )]
    if _has_group(pipeline):
        numeric_fields = _numeric_result_fields(rows)
        flat_zero = [field for field, values in numeric_fields.items() if values and all(v == 0 for v in values)]
        if flat_zero and _nlq_mentions_any(record, {"sum", "total", "high", "highest", "top"}):
            return [_issue(
                WARNING,
                "AGGREGATE_FIELD_ALL_ZERO",
                record,
                "grouped result has numeric fields that are zero for every sampled row",
                evidence={"fields": sorted(flat_zero)[:12]},
            )]
    return []


def _nlq_alignment_issues(
    record: dict[str, Any],
    *,
    track: str,
    text: str,
    pipeline: list[dict[str, Any]],
) -> list[QualityIssue]:
    if not text.strip():
        return [_issue(WARNING, "NLQ_EMPTY", record, f"{track} NLQ is empty", track=track)]

    issues: list[QualityIssue] = []
    normalized = _normalize_text(text)

    issues.extend(_nlq_output_field_issues(record, track=track, text=text, pipeline=pipeline))

    sort_specs = _semantic_desc_sort_specs(pipeline)
    limit = _first_limit(pipeline)
    if sort_specs and limit is not None and not _nlq_mentions_sort(normalized, sort_specs):
        issues.append(_issue(
            WARNING,
            "NLQ_HIDDEN_TOP_BY",
            record,
            "NLQ says a bounded result but does not clearly state the top-by sorting metric",
            track=track,
            evidence={"sort_specs": sort_specs, "limit": limit, "nlq": text},
        ))

    for constant in sorted(_match_constants(pipeline)):
        token = _normalize_text(str(constant))
        if not token:
            continue
        code = "NLQ_HIDDEN_STATE_FILTER" if token in _STATE_WORDS else "NLQ_HIDDEN_FILTER_CONSTANT"
        if token not in normalized:
            issues.append(_issue(
                WARNING,
                code,
                record,
                f"NLQ does not mention answer-changing filter constant {constant!r}",
                track=track,
                evidence={"constant": constant, "nlq": text},
            ))

    for number in sorted(_threshold_numbers(pipeline)):
        literal = _format_number(number)
        normalized_literal = _normalize_text(literal)
        accepted_literals = {
            literal,
            literal.rstrip(".0"),
            normalized_literal,
            normalized_literal.rstrip(".0"),
        }
        if not any(candidate and candidate in normalized for candidate in accepted_literals):
            issues.append(_issue(
                WARNING,
                "NLQ_HIDDEN_NUMERIC_THRESHOLD",
                record,
                f"NLQ does not mention numeric threshold {literal}",
                track=track,
                evidence={"threshold": number, "nlq": text},
            ))
    return issues


def _nlq_output_field_issues(
    record: dict[str, Any],
    *,
    track: str,
    text: str,
    pipeline: list[dict[str, Any]],
) -> list[QualityIssue]:
    output_fields = _final_mql_output_fields(pipeline)
    declared = _declared_nlq_output_fields(track, text)
    if output_fields is None or declared is None:
        return []

    expected_fields, source = output_fields
    declared_fields, clause = declared
    missing = sorted(expected_fields - declared_fields)
    if not missing:
        return []

    return [_issue(
        ERROR,
        "NLQ_OUTPUT_FIELDS_MISSING",
        record,
        "NLQ output field clause does not fully declare the final MQL output shape",
        track=track,
        evidence={
            "missing_fields": missing,
            "mql_output_fields": sorted(expected_fields),
            "declared_fields": sorted(declared_fields),
            "declared_clause": clause,
            **source,
        },
    )]


def _final_mql_output_fields(
    pipeline: list[dict[str, Any]],
) -> tuple[set[str], dict[str, Any]] | None:
    for index in range(len(pipeline) - 1, -1, -1):
        stage = pipeline[index]
        if not isinstance(stage, dict) or not stage:
            return None
        op = _stage_op(stage)
        if op in _OUTPUT_SHAPE_PRESERVING_OPS:
            continue
        if op == "$project":
            fields = _project_output_fields(
                stage.get("$project"),
                _id_fields_before_stage(pipeline, index),
            )
        elif op == "$group":
            fields = _group_output_fields(stage.get("$group"))
        else:
            return None
        if not fields:
            return None
        return fields, {"stage_index": index + 1, "stage_op": op}
    return None


def _project_output_fields(body: Any, id_fields: set[str] | None = None) -> set[str]:
    if not isinstance(body, dict):
        return set()
    prior_id_fields = set(id_fields or {"_id"})
    fields = {
        str(key)
        for key, value in body.items()
        if str(key) != "_id" and not _project_excludes_field(value)
    }
    if not fields:
        return set()
    if not _project_excludes_field(body.get("_id", 1)):
        if "_id" in body and body.get("_id") not in (1, True):
            fields.add("_id")
        else:
            fields.update(prior_id_fields)
    return fields


def _group_output_fields(body: Any) -> set[str]:
    if not isinstance(body, dict):
        return set()
    fields = {str(key) for key in body if str(key) != "_id"}
    fields.update(_group_id_fields(body.get("_id")))
    return fields


def _id_fields_before_stage(pipeline: list[dict[str, Any]], stage_index: int) -> set[str]:
    id_fields = {"_id"}
    for stage in pipeline[:stage_index]:
        if not isinstance(stage, dict) or not stage:
            continue
        op = _stage_op(stage)
        if op == "$project":
            body = stage.get("$project")
            if not isinstance(body, dict):
                continue
            if "_id" in body:
                if _project_excludes_field(body.get("_id")):
                    id_fields = set()
                elif body.get("_id") not in (1, True):
                    id_fields = {"_id"}
            elif _project_has_inclusions(body):
                # Inclusion projections retain _id only if it still exists in the input.
                id_fields = set(id_fields)
        elif op == "$group":
            body = stage.get("$group")
            if isinstance(body, dict):
                id_fields = _group_id_fields(body.get("_id"))
        elif op in {"$replaceRoot", "$replaceWith"}:
            id_fields = {"_id"}
    return id_fields


def _project_has_inclusions(body: dict[str, Any]) -> bool:
    return any(str(key) != "_id" and not _project_excludes_field(value)
               for key, value in body.items())


def _group_id_fields(group_id: Any) -> set[str]:
    if isinstance(group_id, dict):
        return {f"_id.{key}" for key in group_id}
    return {"_id"}


def _project_excludes_field(value: Any) -> bool:
    return value is False or value == 0


def _declared_nlq_output_fields(track: str, text: str) -> tuple[set[str], str] | None:
    if track == "canonical":
        marker = "output fields"
    elif track == "colloquial":
        marker = "with fields"
    else:
        return None

    lower = text.lower()
    start = lower.rfind(marker)
    if start < 0:
        return None

    clause = text[start + len(marker):].strip().lstrip(":")
    if ";" in clause:
        clause = clause.split(";", 1)[0].strip()
    clause = clause.strip()
    if not clause:
        return None

    fields: set[str] = set()
    for raw_token in _FIELD_SPLIT_RE.split(clause):
        token = _clean_declared_field_token(raw_token)
        if not token:
            continue
        if not _FIELD_TOKEN_RE.fullmatch(token):
            return None
        fields.add(token)
    if not fields:
        return None
    return fields, clause


def _clean_declared_field_token(value: str) -> str:
    token = value.strip().strip("`'\"")
    while token and token[-1] in ".:":
        token = token[:-1].rstrip()
    return token.strip().strip("`'\"")


def _nlq_tracks(record: dict[str, Any]) -> dict[str, str]:
    raw = record.get("nl_queries")
    if isinstance(raw, dict):
        return {
            str(track): str(text)
            for track, text in raw.items()
            if track in {"canonical", "colloquial"} and isinstance(text, str)
        }
    tracks: dict[str, str] = {}
    if isinstance(record.get("NLQ"), str):
        tracks["canonical"] = str(record["NLQ"])
    if isinstance(record.get("NLQ_colloquial"), str):
        tracks["colloquial"] = str(record["NLQ_colloquial"])
    return tracks


def _stage_input_refs(stage: dict[str, Any]) -> set[str]:
    if not isinstance(stage, dict) or not stage:
        return set()
    op = _stage_op(stage)
    body = stage.get(op)
    if op == "$match" and isinstance(body, dict):
        return _match_field_refs(body)
    if op == "$sort" and isinstance(body, dict):
        return {_normalize_field_path(key) for key in body if not str(key).startswith("$")}
    if op == "$project" and isinstance(body, dict):
        refs: set[str] = set()
        for key, value in body.items():
            if key == "_id":
                continue
            if value is True or value == 1:
                refs.add(_normalize_field_path(str(key)))
            elif value not in (False, 0):
                _collect_string_field_refs(value, refs, parent_key=None)
        return refs
    if op in {"$addFields", "$set", "$group", "$replaceRoot", "$replaceWith"}:
        refs: set[str] = set()
        _collect_string_field_refs(body, refs, parent_key=None)
        return refs
    if op == "$unwind":
        refs: set[str] = set()
        if isinstance(body, str):
            refs.add(_normalize_field_path(body))
        elif isinstance(body, dict) and isinstance(body.get("path"), str):
            refs.add(_normalize_field_path(body["path"]))
        return refs
    if op == "$lookup" and isinstance(body, dict):
        refs = set()
        local_field = body.get("localField")
        if isinstance(local_field, str):
            refs.add(_normalize_field_path(local_field))
        let_spec = body.get("let")
        if isinstance(let_spec, dict):
            _collect_string_field_refs(let_spec, refs, parent_key=None)
        return refs
    refs = set()
    _collect_string_field_refs(body, refs, parent_key=None)
    return refs


def _match_field_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if key_text.startswith("$"):
                _collect_string_field_refs(child, refs, parent_key=key_text)
            else:
                refs.add(_normalize_field_path(key_text))
                refs.update(_match_field_refs(child))
    elif isinstance(value, list):
        for item in value:
            refs.update(_match_field_refs(item))
    return refs


def _collect_string_field_refs(value: Any, refs: set[str], *, parent_key: str | None) -> None:
    if isinstance(value, str):
        if value.startswith("$$"):
            return
        if value.startswith("$") and parent_key not in _NON_FIELD_STRING_KEYS:
            path = _normalize_field_path(value)
            if path and path not in _SYSTEM_FIELD_NAMES:
                refs.add(path)
        elif parent_key == "localField":
            refs.add(_normalize_field_path(value))
        return
    if isinstance(value, list):
        for item in value:
            _collect_string_field_refs(item, refs, parent_key=parent_key)
        return
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if key_text == "$literal":
                continue
            _collect_string_field_refs(child, refs, parent_key=key_text)


def _match_constants(pipeline: list[dict[str, Any]]) -> set[str]:
    constants: set[str] = set()
    for stage in pipeline:
        match = stage.get("$match") if isinstance(stage, dict) else None
        if isinstance(match, dict):
            _collect_match_constants(match, constants)
    return constants


def _collect_match_constants(value: Any, constants: set[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in {"$gte", "$gt", "$lte", "$lt"}:
                continue
            _collect_match_constants(child, constants)
    elif isinstance(value, list):
        for item in value:
            _collect_match_constants(item, constants)
    elif isinstance(value, str):
        if not value.startswith("$") and not value.startswith("$$"):
            constants.add(value)


def _threshold_numbers(pipeline: list[dict[str, Any]]) -> set[float]:
    numbers: set[float] = set()
    _collect_threshold_numbers(pipeline, numbers, parent_key=None)
    return numbers


def _collect_threshold_numbers(value: Any, numbers: set[float], *, parent_key: str | None) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _collect_threshold_numbers(child, numbers, parent_key=str(key))
    elif isinstance(value, list):
        for item in value:
            _collect_threshold_numbers(item, numbers, parent_key=parent_key)
    elif parent_key in {"$gte", "$gt", "$lte", "$lt"} and isinstance(value, (int, float)):
        numbers.add(float(value))


def _semantic_desc_sort_specs(pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for stage in pipeline:
        sort = stage.get("$sort") if isinstance(stage, dict) else None
        if not isinstance(sort, dict):
            continue
        for field, direction in sort.items():
            try:
                direction_number = int(direction)
            except (TypeError, ValueError):
                continue
            field_text = str(field)
            if direction_number < 0 and not field_text.startswith("_id"):
                specs.append({"field": field_text, "direction": direction_number})
    return specs


def _sort_specs(pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for stage in pipeline:
        sort = stage.get("$sort") if isinstance(stage, dict) else None
        if isinstance(sort, dict):
            specs.append(dict(sort))
    return specs


def _first_limit(pipeline: list[dict[str, Any]]) -> int | None:
    for stage in pipeline:
        limit = stage.get("$limit") if isinstance(stage, dict) else None
        if isinstance(limit, int):
            return limit
    return None


def _is_order_sensitive(pipeline: list[dict[str, Any]]) -> bool:
    return bool(root_ops(pipeline) & {"$sort", "$limit", "$skip", "$setWindowFields"})


def _has_group(pipeline: list[dict[str, Any]]) -> bool:
    return any("$group" in stage for stage in pipeline if isinstance(stage, dict))


def _stage_op(stage: dict[str, Any]) -> str:
    if not isinstance(stage, dict) or not stage:
        return "?"
    return str(next(iter(stage)))


def _normalize_field_path(value: str) -> str:
    text = str(value).strip()
    while text.startswith("$"):
        text = text[1:]
    return text


def _normalize_text(value: str) -> str:
    text = str(value).lower().replace("_", " ").replace("-", " ")
    return " ".join("".join(ch if ch.isalnum() or ch == "." else " " for ch in text).split())


def _nlq_mentions_sort(normalized_nl: str, sort_specs: list[dict[str, Any]]) -> bool:
    words = set(normalized_nl.split())
    if not words & _TOP_WORDS:
        return False
    sort_tokens: set[str] = set()
    for spec in sort_specs:
        sort_tokens.update(_field_tokens(str(spec.get("field") or "")))
    return bool((words - _NL_STOPWORDS) & sort_tokens)


def _field_tokens(field_path: str) -> set[str]:
    normalized = _normalize_text(field_path.replace(".", " "))
    return {part for part in normalized.split() if part and part not in _NL_STOPWORDS}


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _nlq_mentions_any(record: dict[str, Any], words: set[str]) -> bool:
    joined = " ".join(_nlq_tracks(record).values()).lower()
    return any(word in joined for word in words)


def _leaf_paths(value: Any, *, prefix: str = "") -> set[str]:
    if isinstance(value, dict):
        paths: set[str] = set()
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            paths.update(_leaf_paths(child, prefix=child_prefix))
        return paths
    if isinstance(value, list):
        paths: set[str] = set()
        for item in value:
            paths.update(_leaf_paths(item, prefix=prefix))
        return paths or ({prefix} if prefix else set())
    return {prefix} if prefix else set()


def _numeric_result_fields(rows: list[dict[str, Any]]) -> dict[str, list[float]]:
    fields: dict[str, list[float]] = defaultdict(list)
    for row in rows[:50]:
        _collect_numeric_fields(row, fields, prefix="")
    return fields


def _collect_numeric_fields(value: Any, fields: dict[str, list[float]], *, prefix: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            _collect_numeric_fields(child, fields, prefix=child_prefix)
    elif isinstance(value, list):
        for item in value:
            _collect_numeric_fields(item, fields, prefix=prefix)
    elif isinstance(value, bool):
        return
    elif isinstance(value, (int, float)) and prefix:
        fields[prefix].append(float(value))


def _hash_rows(rows: list[dict[str, Any]]) -> str:
    import hashlib

    digest = hashlib.sha256(canonical_json(rows).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _issue(
    severity: str,
    code: str,
    record: dict[str, Any],
    message: str,
    *,
    track: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> QualityIssue:
    return QualityIssue(
        severity=severity,
        code=code,
        db_id=str(record.get("db_id") or ""),
        record_id=record.get("record_id"),
        track=track,
        message=message,
        evidence=dict(evidence or {}),
    )


def _build_report(dataset_dir: Path, records_checked: int, issues: list[QualityIssue]) -> ReleaseQualityReport:
    errors = sum(1 for issue in issues if issue.severity == ERROR)
    warnings = sum(1 for issue in issues if issue.severity == WARNING)
    by_code = Counter(issue.code for issue in issues)
    by_db = Counter(issue.db_id for issue in issues)
    return ReleaseQualityReport(
        ok=errors == 0,
        dataset_dir=str(dataset_dir),
        records_checked=records_checked,
        errors=errors,
        warnings=warnings,
        by_code=dict(sorted(by_code.items())),
        by_db=dict(sorted(by_db.items())),
        issues=issues,
    )


def _write_report(report: ReleaseQualityReport, out_dir: Path) -> ReleaseQualityReport:
    out_dir.mkdir(parents=True, exist_ok=True)
    issues_path = out_dir / "issues.jsonl"
    report_json_path = out_dir / "report.json"
    report_md_path = out_dir / "report.md"

    with issues_path.open("w", encoding="utf-8") as fp:
        for issue in report.issues:
            fp.write(json.dumps(issue.as_dict(), ensure_ascii=False, default=str) + "\n")
    paths = {
        "out_dir": str(out_dir),
        "issues_jsonl": str(issues_path),
        "report_json": str(report_json_path),
        "report_md": str(report_md_path),
    }
    written = ReleaseQualityReport(
        ok=report.ok,
        dataset_dir=report.dataset_dir,
        records_checked=report.records_checked,
        errors=report.errors,
        warnings=report.warnings,
        by_code=report.by_code,
        by_db=report.by_db,
        issues=report.issues,
        paths=paths,
    )
    report_json_path.write_text(
        json.dumps(written.as_dict(), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    report_md_path.write_text(_render_markdown(written), encoding="utf-8")
    return written


def _render_markdown(report: ReleaseQualityReport) -> str:
    lines = [
        "# TEND Release Quality Audit",
        "",
        f"- status: {'OK' if report.ok else 'INVALID'}",
        f"- dataset: `{report.dataset_dir}`",
        f"- records_checked: {report.records_checked}",
        f"- errors: {report.errors}",
        f"- warnings: {report.warnings}",
        "",
        "## Issues By Code",
        "",
    ]
    for code, count in sorted(report.by_code.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- `{code}`: {count}")
    lines.extend(["", "## First Issues", ""])
    for issue in report.issues[:80]:
        track = f" track={issue.track}" if issue.track else ""
        lines.append(
            f"- [{issue.severity}] `{issue.code}` db={issue.db_id} "
            f"record={issue.record_id}{track}: {issue.message}"
        )
    if len(report.issues) > 80:
        lines.append(f"- ... {len(report.issues) - 80} more")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "QualityIssue",
    "ReleaseQualityReport",
    "run_release_quality_audit",
]
