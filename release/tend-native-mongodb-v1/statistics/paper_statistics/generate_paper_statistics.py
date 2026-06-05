#!/usr/bin/env python3
"""Generate paper-oriented statistics from the lean public TEND release."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
RELEASE_DIR = SCRIPT_DIR.parents[1]
DATA_DIR = RELEASE_DIR / "data"
SCHEMA_DIR = RELEASE_DIR / "schema" / "mongodb_schema"
OUT_DIR = SCRIPT_DIR
PUBLIC_RECORD_FIELDS = ["record_id", "db_id", "NLQ", "NLQ_colloquial", "MQL"]

for parent in SCRIPT_DIR.parents:
    src_dir = parent / "src"
    if (src_dir / "tend").is_dir():
        sys.path.insert(0, str(src_dir))
        break

try:
    from tend.execution import (
        mql_signature,
        mql_skeleton_signature,
        mql_skeleton_summary,
        parse_pipeline as parse_execution_pipeline,
    )
except Exception:  # pragma: no cover - script fallback for source-less release copies
    mql_signature = None
    mql_skeleton_signature = None
    mql_skeleton_summary = None
    parse_execution_pipeline = None


AGG_RE = re.compile(r"^db\.([A-Za-z_][A-Za-z0-9_]*)\.aggregate\((\[.*\])\)\s*$", re.S)

ARRAY_SEMANTIC_OPS = {
    "$unwind",
    "$size",
    "$filter",
    "$map",
    "$isArray",
    "$addToSet",
    "$push",
    "$slice",
    "$arrayElemAt",
}
DYNAMIC_KEY_OPS = {"$objectToArray", "$arrayToObject", "$getField", "$setField"}
GROUPING_OPS = {"$group", "$sum", "$avg", "$min", "$max", "$count", "$addToSet", "$push"}
EXPR_MATCH_OPS = {"$expr"}
DETERMINISM_FORBIDDEN_OPS = {"$$NOW", "$function", "$merge", "$out", "$rand", "$sample"}

STAGE_ROLES = {
    "$match": "filter",
    "$project": "projection",
    "$addFields": "enrichment",
    "$unwind": "array_traversal",
    "$group": "aggregation",
    "$sort": "ordering",
    "$limit": "result_bound",
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def size_human(path: Path) -> str:
    size = float(path.stat().st_size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)}B" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{path.stat().st_size}B"


def quantiles(values: list[float | int]) -> dict[str, float | int | None]:
    if not values:
        return {key: None for key in ("n", "min", "p10", "p25", "median", "p75", "p90", "p95", "max", "mean", "stdev")}
    xs = sorted(float(v) for v in values)

    def pct(p: float) -> float:
        if len(xs) == 1:
            return xs[0]
        pos = (len(xs) - 1) * p
        lo = math.floor(pos)
        hi = math.ceil(pos)
        if lo == hi:
            return xs[lo]
        return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)

    def nice(v: float) -> float | int:
        return int(round(v)) if abs(v - round(v)) < 1e-9 else round(v, 4)

    return {
        "n": len(xs),
        "min": nice(xs[0]),
        "p10": nice(pct(0.10)),
        "p25": nice(pct(0.25)),
        "median": nice(pct(0.50)),
        "p75": nice(pct(0.75)),
        "p90": nice(pct(0.90)),
        "p95": nice(pct(0.95)),
        "max": nice(xs[-1]),
        "mean": round(statistics.fmean(xs), 4),
        "stdev": round(statistics.pstdev(xs), 4),
    }


def entropy(counter: Counter[Any]) -> dict[str, float | int]:
    total = sum(counter.values())
    if total <= 0:
        return {"entropy_bits": 0.0, "normalized_entropy": 0.0, "effective_families": 0.0}
    probs = [count / total for count in counter.values()]
    ent = -sum(p * math.log2(p) for p in probs)
    max_ent = math.log2(len(counter)) if counter else 0.0
    return {
        "entropy_bits": round(ent, 4),
        "normalized_entropy": round(ent / max_ent, 4) if max_ent else 0.0,
        "effective_families": round(2**ent, 4),
    }


def gini(counter: Counter[Any]) -> float:
    xs = sorted(counter.values())
    if not xs:
        return 0.0
    total = sum(xs)
    if total == 0:
        return 0.0
    n = len(xs)
    weighted = sum((i + 1) * x for i, x in enumerate(xs))
    return round((2 * weighted) / (n * total) - (n + 1) / n, 4)


def top_with_share(counter: Counter[Any], n: int = 20) -> list[dict[str, Any]]:
    total = sum(counter.values()) or 1
    return [{"key": key, "count": count, "share": round(count / total, 6)} for key, count in counter.most_common(n)]


def coverage_at(counter: Counter[Any], cutoffs: tuple[int, ...] = (1, 3, 5, 10, 20)) -> dict[str, float]:
    vals = [count for _, count in counter.most_common()]
    total = sum(vals) or 1
    return {f"top_{k}": round(sum(vals[:k]) / total, 6) for k in cutoffs}


def parse_mql(mql: str) -> tuple[str | None, list[dict[str, Any]] | None, str | None]:
    if parse_execution_pipeline is not None:
        try:
            collection, pipeline = parse_execution_pipeline(str(mql))
            return collection, pipeline, None
        except Exception as exc:  # noqa: BLE001 - converted into statistics parse diagnostics
            return None, None, str(exc)
    match = AGG_RE.match(str(mql).strip())
    if not match:
        return None, None, "not_db_collection_aggregate"
    collection = match.group(1)
    try:
        pipeline = json.loads(match.group(2))
    except json.JSONDecodeError as exc:
        return collection, None, f"json_decode:{exc}"
    if not isinstance(pipeline, list) or not all(isinstance(stage, dict) for stage in pipeline):
        return collection, None, "pipeline_not_list_of_objects"
    return collection, pipeline, None


def walk(obj: Any):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk(item)


def operator_counts(pipeline: list[dict[str, Any]]) -> tuple[Counter[str], set[str]]:
    counts: Counter[str] = Counter()
    for key, _ in walk(pipeline):
        if isinstance(key, str) and key.startswith("$"):
            counts[key] += 1
    return counts, set(counts)


def stage_sequence(pipeline: list[dict[str, Any]]) -> list[str]:
    seq: list[str] = []
    for stage in pipeline:
        if len(stage) == 1:
            seq.append(next(iter(stage)))
        else:
            seq.append("|".join(stage.keys()))
    return seq


def structural_bucket(seq: list[str]) -> str:
    has_unwind = "$unwind" in seq
    has_group = "$group" in seq
    has_add = "$addFields" in seq
    project_count = seq.count("$project")
    unwind_count = seq.count("$unwind")
    if has_unwind and has_group and unwind_count >= 2:
        return "multi_unwind_grouped"
    if has_unwind and has_group:
        return "unwind_grouped"
    if has_unwind and not has_group:
        return "unwind_filter_project"
    if has_group and not has_unwind:
        return "group_without_unwind"
    if has_add and project_count >= 2:
        return "enrich_filter_project"
    return "linear_filter_project"


def collect_field_paths(obj: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and not key.startswith("$") and "." in key:
                paths.append(key)
            paths.extend(collect_field_paths(value))
    elif isinstance(obj, list):
        for item in obj:
            paths.extend(collect_field_paths(item))
    elif isinstance(obj, str) and obj.startswith("$") and not obj.startswith("$$"):
        path = obj[1:]
        if "." in path:
            paths.append(path)
    return paths


def count_limit_values(pipeline: list[dict[str, Any]]) -> list[int]:
    values: list[int] = []
    for stage in pipeline:
        limit = stage.get("$limit")
        if isinstance(limit, int):
            values.append(limit)
    return values


def nl_word_count(text: str) -> int:
    return len(str(text).split())


def fallback_signature(prefix: str, text: str) -> str:
    return f"{prefix}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def public_mql_signature(mql: str) -> str:
    if mql_signature is None:
        return fallback_signature("sha256", mql)
    return str(mql_signature(mql))


def public_skeleton_signature(mql: str, seq_key: str) -> str:
    if mql_skeleton_signature is None:
        return fallback_signature("stage-seq-sha256", seq_key)
    try:
        return str(mql_skeleton_signature(mql))
    except Exception:
        return fallback_signature("stage-seq-sha256", seq_key)


def public_skeleton_summary(mql: str, seq_key: str) -> str:
    if mql_skeleton_summary is None:
        return seq_key
    try:
        return str(mql_skeleton_summary(mql))
    except Exception:
        return seq_key


def validate_public_records(records: Any) -> dict[str, Any]:
    issues: list[str] = []
    if not isinstance(records, list):
        return {"ok": False, "issues": ["TEND_lean.json must be a JSON list"], "public_record_fields": PUBLIC_RECORD_FIELDS}

    seen_ids: set[Any] = set()
    pair_counter: Counter[tuple[str, str, str]] = Counter()
    mql_counter: Counter[str] = Counter()
    canonical_counter: Counter[str] = Counter()
    by_db: Counter[str] = Counter()

    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            issues.append(f"[row {idx}] record must be an object")
            continue
        keys = list(rec.keys())
        if keys != PUBLIC_RECORD_FIELDS:
            issues.append(f"[row {idx}] field order/shape {keys!r} != {PUBLIC_RECORD_FIELDS!r}")
        rid = rec.get("record_id")
        db_id = rec.get("db_id")
        if rid in seen_ids:
            issues.append(f"[row {idx}] duplicate record_id {rid!r}")
        seen_ids.add(rid)
        if not isinstance(rid, int):
            issues.append(f"[row {idx}] record_id must be an integer")
        if not isinstance(db_id, str) or not db_id.strip():
            issues.append(f"[row {idx}] db_id must be a non-empty string")
        for key in ("NLQ", "NLQ_colloquial", "MQL"):
            if not isinstance(rec.get(key), str) or not rec.get(key, "").strip():
                issues.append(f"[row {idx} r{rid}] {key} must be a non-empty string")
        _, pipeline, err = parse_mql(str(rec.get("MQL", "")))
        if err or pipeline is None:
            issues.append(f"[row {idx} r{rid}] MQL parse failed: {err}")
        if isinstance(db_id, str):
            by_db[db_id] += 1
        pair_counter[(str(db_id), str(rec.get("NLQ", "")), str(rec.get("MQL", "")))] += 1
        mql_counter[str(rec.get("MQL", ""))] += 1
        canonical_counter[str(rec.get("NLQ", ""))] += 1

    if len(by_db) != 11:
        issues.append(f"db coverage {len(by_db)} != 11")
    for db_id, count in sorted(by_db.items()):
        if count != 110:
            issues.append(f"db {db_id} has {count} records, expected 110")
    if len(records) != 1210:
        issues.append(f"record count {len(records)} != 1210")
    if len(mql_counter) != len(records):
        issues.append(f"distinct MQL strings {len(mql_counter)} != records {len(records)}")
    if len(canonical_counter) != len(records):
        issues.append(f"distinct NLQ strings {len(canonical_counter)} != records {len(records)}")
    if len(pair_counter) != len(records):
        issues.append(f"distinct db_id+NLQ+MQL pairs {len(pair_counter)} != records {len(records)}")

    return {
        "ok": not issues,
        "issues": issues,
        "public_record_fields": PUBLIC_RECORD_FIELDS,
        "record_count": len(records),
        "db_count": len(by_db),
        "records_per_db": dict(sorted(by_db.items())),
        "distinct_mql_strings": len(mql_counter),
        "distinct_canonical_nl": len(canonical_counter),
        "distinct_db_nl_mql_pairs": len(pair_counter),
    }


def load_schema_stats() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    by_db: dict[str, dict[str, Any]] = {}
    total_collections = 0
    total_docs = 0
    collection_doc_counts: list[int] = []
    dynamic_paths = 0
    nested_array_paths = 0
    max_depths: list[int] = []

    for path in sorted(SCHEMA_DIR.glob("*.json")):
        schema = read_json(path)
        db_id = schema.get("db_id") or path.stem
        collections = schema.get("collections") or {}
        source_tables = schema.get("source_tables") or []
        doc_count = 0
        collection_names = sorted(collections.keys())
        for meta in collections.values():
            docs = int((meta or {}).get("document_count") or 0)
            doc_count += docs
            collection_doc_counts.append(docs)
        audit = schema.get("structure_audit") or {}
        dynamic_count = len(audit.get("dynamic_key_paths") or [])
        nested_count = len(audit.get("nested_array_paths") or [])
        dynamic_paths += dynamic_count
        nested_array_paths += nested_count
        if isinstance(audit.get("max_depth"), int):
            max_depths.append(audit["max_depth"])
        total_collections += len(collections)
        total_docs += doc_count
        by_db[db_id] = {
            "source_table_count": len(source_tables),
            "source_tables": source_tables,
            "schema_collection_count": len(collections),
            "schema_collections": collection_names,
            "document_count": doc_count,
            "max_depth": audit.get("max_depth"),
            "dynamic_key_path_count": dynamic_count,
            "nested_array_path_count": nested_count,
        }

    return by_db, {
        "schema_collection_count": total_collections,
        "document_count": total_docs,
        "collection_document_count": quantiles(collection_doc_counts),
        "max_depth": quantiles(max_depths),
        "dynamic_key_path_count": dynamic_paths,
        "nested_array_path_count": nested_array_paths,
    }


def latex_escape(value: Any) -> str:
    text = str(value)
    return (
        text.replace("\\", "\\textbackslash{}")
        .replace("_", "\\_")
        .replace("%", "\\%")
        .replace("&", "\\&")
        .replace("#", "\\#")
        .replace("$", "\\$")
        .replace("{", "\\{")
        .replace("}", "\\}")
    )


def latex_table(headers: list[str], rows: list[list[Any]], caption: str, label: str) -> str:
    cols = "l" + "r" * (len(headers) - 1)
    lines = [
        "\\begin{table}[t]",
        "\\centering",
        f"\\caption{{{latex_escape(caption)}}}",
        f"\\label{{{latex_escape(label)}}}",
        f"\\begin{{tabular}}{{{cols}}}",
        "\\toprule",
        " & ".join(latex_escape(header) for header in headers) + " \\\\",
        "\\midrule",
    ]
    for row in rows:
        lines.append(" & ".join(latex_escape(cell) for cell in row) + " \\\\")
    lines.extend(["\\bottomrule", "\\end{tabular}", "\\end{table}", ""])
    return "\n".join(lines)


def main() -> None:
    lean_path = DATA_DIR / "TEND_lean.json"
    records = read_json(lean_path)
    public_contract = validate_public_records(records)
    total_records = len(records)

    schema_by_db, schema_overall = load_schema_stats()
    parse_errors: list[dict[str, Any]] = []
    per_record: list[dict[str, Any]] = []

    by_db_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    canonical_counter: Counter[str] = Counter()
    colloquial_counter: Counter[str] = Counter()
    nl_pair_counter: Counter[tuple[str, str]] = Counter()
    nl_text_counter: Counter[str] = Counter()
    mql_counter: Counter[str] = Counter()
    mql_signature_counter: Counter[str] = Counter()
    skeleton_counter: Counter[str] = Counter()
    db_skeleton_counter: Counter[tuple[str, str]] = Counter()
    stage_sequence_counter: Counter[str] = Counter()
    stage_occurrence: Counter[str] = Counter()
    stage_presence: Counter[str] = Counter()
    operator_occurrence: Counter[str] = Counter()
    operator_presence: Counter[str] = Counter()
    collection_counter: Counter[tuple[str, str]] = Counter()
    structural_bucket_counter: Counter[str] = Counter()
    forbidden_presence: Counter[str] = Counter()
    semantic_presence: Counter[str] = Counter()
    distributions: dict[str, list[float | int]] = defaultdict(list)

    for rec in records:
        db_id = rec["db_id"]
        by_db_records[db_id].append(rec)
        canonical = rec.get("NLQ", "")
        colloquial = rec.get("NLQ_colloquial", "")
        mql = rec.get("MQL", "")
        canonical_counter[canonical] += 1
        colloquial_counter[colloquial] += 1
        nl_pair_counter[(canonical, colloquial)] += 1
        nl_text_counter[canonical] += 1
        nl_text_counter[colloquial] += 1
        mql_counter[mql] += 1
        sig = public_mql_signature(mql)
        mql_signature_counter[sig] += 1

        collection, pipeline, err = parse_mql(mql)
        if err or pipeline is None:
            parse_errors.append({"record_id": rec.get("record_id"), "db_id": db_id, "error": err})
            continue

        seq = stage_sequence(pipeline)
        seq_key = ">".join(seq)
        skeleton = public_skeleton_signature(mql, seq_key)
        skeleton_summary = public_skeleton_summary(mql, seq_key)
        op_counts, op_set = operator_counts(pipeline)
        field_paths = collect_field_paths(pipeline)
        max_path_depth = max((len(path.split(".")) for path in field_paths), default=0)
        limits = count_limit_values(pipeline)
        bucket = structural_bucket(seq)

        dynamic_by_operator = bool(op_set & DYNAMIC_KEY_OPS)
        array_by_operator = bool(op_set & ARRAY_SEMANTIC_OPS)
        grouping_by_operator = bool(op_set & GROUPING_OPS)
        expr_match = bool(op_set & EXPR_MATCH_OPS)
        nested_path = max_path_depth >= 2
        forbidden = sorted(op for op in op_set if op in DETERMINISM_FORBIDDEN_OPS)

        collection_counter[(db_id, collection or "")] += 1
        stage_sequence_counter[seq_key] += 1
        skeleton_counter[skeleton] += 1
        db_skeleton_counter[(db_id, skeleton)] += 1
        stage_occurrence.update(seq)
        stage_presence.update(set(seq))
        operator_occurrence.update(op_counts)
        operator_presence.update(op_set)
        structural_bucket_counter[bucket] += 1
        forbidden_presence.update(forbidden)

        semantic_flags = {
            "dynamic_key_operator": dynamic_by_operator,
            "array_operator": array_by_operator,
            "grouping_operator": grouping_by_operator,
            "expr_match": expr_match,
            "nested_dotted_path": nested_path,
            "has_unwind": "$unwind" in op_set,
            "has_group": "$group" in op_set,
            "has_project": "$project" in op_set,
            "has_match": "$match" in op_set,
            "has_sort": "$sort" in op_set,
            "has_limit": "$limit" in op_set,
            "has_filter_or_map": bool(op_set & {"$filter", "$map"}),
            "has_conditional": bool(op_set & {"$cond", "$switch"}),
        }
        for key, present in semantic_flags.items():
            if present:
                semantic_presence[key] += 1

        distributions["stage_count"].append(len(seq))
        distributions["stage_operator_occurrence_count"].append(sum(op_counts.values()))
        distributions["unique_operator_count"].append(len(op_set))
        distributions["mql_chars"].append(len(mql))
        distributions["canonical_nl_words"].append(nl_word_count(canonical))
        distributions["colloquial_nl_words"].append(nl_word_count(colloquial))
        distributions["field_path_reference_count"].append(len(field_paths))
        distributions["max_field_path_depth"].append(max_path_depth)
        distributions["limit_value"].extend(limits)

        per_record.append(
            {
                "record_id": rec.get("record_id"),
                "db_id": db_id,
                "collection": collection,
                "stage_count": len(seq),
                "stage_sequence": seq_key,
                "operator_occurrence_count": sum(op_counts.values()),
                "unique_operator_count": len(op_set),
                "mql_chars": len(mql),
                "canonical_nl_words": nl_word_count(canonical),
                "colloquial_nl_words": nl_word_count(colloquial),
                "field_path_reference_count": len(field_paths),
                "max_field_path_depth": max_path_depth,
                "dynamic_key_operator": dynamic_by_operator,
                "array_operator": array_by_operator,
                "grouping_operator": grouping_by_operator,
                "expr_match": expr_match,
                "nested_dotted_path": nested_path,
                "limit_values": limits,
                "mql_signature": sig,
                "mql_skeleton_signature": skeleton,
                "mql_skeleton_summary": skeleton_summary,
                "structural_bucket": bucket,
            }
        )

    by_db: dict[str, dict[str, Any]] = {}
    for db_id in sorted(by_db_records):
        recs = by_db_records[db_id]
        metrics = [row for row in per_record if row["db_id"] == db_id]
        db_stage_sequences = Counter(row["stage_sequence"] for row in metrics)
        db_skeletons = Counter(row["mql_skeleton_signature"] for row in metrics)
        db_collections = Counter(row["collection"] for row in metrics)
        schema = schema_by_db.get(db_id, {})
        schema_collections = set(schema.get("schema_collections") or [])
        queried_schema_intersection = set(db_collections) & schema_collections
        sem = {
            key: sum(1 for row in metrics if row.get(key))
            for key in [
                "dynamic_key_operator",
                "array_operator",
                "grouping_operator",
                "expr_match",
                "nested_dotted_path",
            ]
        }
        by_db[db_id] = {
            "records": len(recs),
            "distinct_mql": len({r["MQL"] for r in recs}),
            "distinct_mql_signature": len({public_mql_signature(r["MQL"]) for r in recs}),
            "distinct_canonical_nl": len({r["NLQ"] for r in recs}),
            "distinct_colloquial_nl": len({r["NLQ_colloquial"] for r in recs}),
            "distinct_nl_texts": len({r["NLQ"] for r in recs} | {r["NLQ_colloquial"] for r in recs}),
            "global_skeleton_signatures_in_db": len(db_skeletons),
            "db_scoped_skeleton_families": len(db_skeletons),
            "max_db_scoped_skeleton_family": max(db_skeletons.values()) if db_skeletons else 0,
            "stage_sequence_families": len(db_stage_sequences),
            "max_stage_sequence_family": max(db_stage_sequences.values()) if db_stage_sequences else 0,
            "queried_collection_count": len(db_collections),
            "queried_collections": dict(db_collections),
            "schema_collection_count": schema.get("schema_collection_count", 0),
            "schema_queried_collection_coverage": round(len(queried_schema_intersection) / schema.get("schema_collection_count", 1), 4)
            if schema.get("schema_collection_count")
            else None,
            "document_count": schema.get("document_count", 0),
            "source_table_count": schema.get("source_table_count", 0),
            "max_depth": schema.get("max_depth"),
            "dynamic_key_path_count": schema.get("dynamic_key_path_count", 0),
            "nested_array_path_count": schema.get("nested_array_path_count", 0),
            "stage_count": quantiles([row["stage_count"] for row in metrics]),
            "unique_operator_count": quantiles([row["unique_operator_count"] for row in metrics]),
            "operator_occurrence_count": quantiles([row["operator_occurrence_count"] for row in metrics]),
            "mql_chars": quantiles([row["mql_chars"] for row in metrics]),
            "canonical_nl_words": quantiles([row["canonical_nl_words"] for row in metrics]),
            "colloquial_nl_words": quantiles([row["colloquial_nl_words"] for row in metrics]),
            "field_path_reference_count": quantiles([row["field_path_reference_count"] for row in metrics]),
            "max_field_path_depth": quantiles([row["max_field_path_depth"] for row in metrics]),
            "semantic_presence": sem,
            "top_stage_sequences": top_with_share(db_stage_sequences, 10),
            "top_global_skeleton_families_in_db": top_with_share(db_skeletons, 10),
        }

    dataset_files = {}
    for name in ["TEND_lean.json"]:
        path = DATA_DIR / name
        if path.exists():
            dataset_files[name] = {
                "path": str(path.relative_to(RELEASE_DIR)),
                "size": path.stat().st_size,
                "size_human": size_human(path),
                "sha256": sha256(path),
                "records": len(read_json(path)),
            }

    fresh_execution_path = OUT_DIR / "fresh_exact_execution_by_db_verification.json"
    fresh_execution = read_json(fresh_execution_path) if fresh_execution_path.exists() else None
    pipeline_stage_stats_path = OUT_DIR / "pipeline_stage_detailed_statistics.json"
    pipeline_stage_stats = read_json(pipeline_stage_stats_path) if pipeline_stage_stats_path.exists() else None

    skeleton_global = {
        "distinct_global_skeleton_signatures": len(skeleton_counter),
        "distinct_db_scoped_skeleton_families": len(db_skeleton_counter),
        "max_global_skeleton_family": max(skeleton_counter.values()) if skeleton_counter else 0,
        "max_db_scoped_skeleton_family": max(db_skeleton_counter.values()) if db_skeleton_counter else 0,
        "global_skeleton_entropy": entropy(skeleton_counter),
        "global_skeleton_gini": gini(skeleton_counter),
        "global_skeleton_top_coverage": coverage_at(skeleton_counter),
        "top_global_skeleton_families": top_with_share(skeleton_counter, 20),
        "distinct_stage_sequences": len(stage_sequence_counter),
        "max_stage_sequence_family": max(stage_sequence_counter.values()) if stage_sequence_counter else 0,
        "stage_sequence_entropy": entropy(stage_sequence_counter),
        "stage_sequence_gini": gini(stage_sequence_counter),
        "stage_sequence_top_coverage": coverage_at(stage_sequence_counter),
        "top_stage_sequences": top_with_share(stage_sequence_counter, 20),
    }

    overall = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "release_dir": str(RELEASE_DIR),
        "dataset_source": "data/TEND_lean.json",
        "public_record_fields": PUBLIC_RECORD_FIELDS,
        "metric_definitions": {
            "operator_occurrence": "Counts every MongoDB operator key beginning with '$' across all stages and nested expressions.",
            "operator_presence": "Counts each MongoDB operator at most once per record.",
            "stage_occurrence": "Counts top-level aggregation pipeline stages; a record can contribute multiple occurrences of the same stage.",
            "stage_presence": "Counts each top-level aggregation stage at most once per record.",
            "mql_skeleton_signature": "Field/literal-abstracted MQL skeleton signature computed from the public MQL.",
            "stage_sequence": "Top-level stage operator sequence derived from the public MQL.",
            "nested_dotted_path": "Approximate static MQL signal: record has at least one dotted field path reference in the parsed pipeline.",
            "nl_word_count": "Whitespace token count.",
        },
        "dataset_files": dataset_files,
        "public_contract": public_contract,
        "scale": {
            "records": total_records,
            "db_count": len(by_db_records),
            "records_per_db": dict(sorted((db, len(recs)) for db, recs in by_db_records.items())),
            "canonical_nl_utterances": total_records,
            "colloquial_nl_utterances": total_records,
            "total_nl_utterances": total_records * 2,
            "distinct_canonical_nl": len(canonical_counter),
            "distinct_colloquial_nl": len(colloquial_counter),
            "distinct_nl_texts": len(nl_text_counter),
            "distinct_nl_pairs": len(nl_pair_counter),
            "distinct_mql_strings": len(mql_counter),
            "distinct_mql_signatures": len(mql_signature_counter),
        },
        "mongo_corpus": {
            **schema_overall,
            "queried_collection_pairs": len(collection_counter),
            "queried_collection_pair_counts_top": [
                {"db_id": db_id, "collection": collection, "count": count}
                for (db_id, collection), count in collection_counter.most_common(50)
            ],
            "schema_collection_coverage_by_query": round(
                len(set(collection_counter)) / schema_overall["schema_collection_count"], 4
            )
            if schema_overall["schema_collection_count"]
            else None,
        },
        "mql_parse": {
            "aggregate_pipeline_records": total_records - len(parse_errors),
            "parse_errors": parse_errors,
            "all_records_parse_as_aggregate": len(parse_errors) == 0,
        },
        "complexity_distributions": {name: quantiles(vals) for name, vals in sorted(distributions.items())},
        "semantic_presence": {
            key: {"records": value, "share": round(value / total_records, 4)}
            for key, value in sorted(semantic_presence.items())
        },
        "diversity": {
            **skeleton_global,
        },
        "operators": {
            "stage_occurrence": top_with_share(stage_occurrence, 100),
            "stage_presence": top_with_share(stage_presence, 100),
            "operator_occurrence": top_with_share(operator_occurrence, 150),
            "operator_presence": top_with_share(operator_presence, 150),
            "forbidden_or_nondeterministic_operator_presence": dict(forbidden_presence),
        },
        "structural_buckets": dict(structural_bucket_counter),
        "validity": {
            "public_contract_ok": public_contract["ok"],
            "fresh_exact_execution_by_db_report": fresh_execution,
        },
        "detailed_pipeline_stage_statistics": pipeline_stage_stats,
        "by_db": by_db,
    }

    write_json(OUT_DIR / "paper_dataset_statistics.json", overall)

    by_db_fields = [
        "db_id",
        "records",
        "distinct_mql",
        "distinct_nl_texts",
        "queried_collection_count",
        "schema_collection_count",
        "document_count",
        "source_table_count",
        "global_skeleton_signatures_in_db",
        "max_db_scoped_skeleton_family",
        "stage_sequence_families",
        "max_stage_sequence_family",
        "stage_median",
        "stage_p90",
        "unique_operator_median",
        "operator_occurrence_median",
        "mql_chars_median",
        "canonical_nl_words_median",
        "dynamic_key_operator_records",
        "array_operator_records",
        "grouping_operator_records",
        "nested_dotted_path_records",
        "expr_match_records",
        "max_depth",
        "dynamic_key_path_count",
        "nested_array_path_count",
    ]
    by_db_rows: list[dict[str, Any]] = []
    for db_id, row in by_db.items():
        sem = row["semantic_presence"]
        by_db_rows.append(
            {
                "db_id": db_id,
                "records": row["records"],
                "distinct_mql": row["distinct_mql"],
                "distinct_nl_texts": row["distinct_nl_texts"],
                "queried_collection_count": row["queried_collection_count"],
                "schema_collection_count": row["schema_collection_count"],
                "document_count": row["document_count"],
                "source_table_count": row["source_table_count"],
                "global_skeleton_signatures_in_db": row["global_skeleton_signatures_in_db"],
                "max_db_scoped_skeleton_family": row["max_db_scoped_skeleton_family"],
                "stage_sequence_families": row["stage_sequence_families"],
                "max_stage_sequence_family": row["max_stage_sequence_family"],
                "stage_median": row["stage_count"]["median"],
                "stage_p90": row["stage_count"]["p90"],
                "unique_operator_median": row["unique_operator_count"]["median"],
                "operator_occurrence_median": row["operator_occurrence_count"]["median"],
                "mql_chars_median": row["mql_chars"]["median"],
                "canonical_nl_words_median": row["canonical_nl_words"]["median"],
                "dynamic_key_operator_records": sem["dynamic_key_operator"],
                "array_operator_records": sem["array_operator"],
                "grouping_operator_records": sem["grouping_operator"],
                "nested_dotted_path_records": sem["nested_dotted_path"],
                "expr_match_records": sem["expr_match"],
                "max_depth": row["max_depth"],
                "dynamic_key_path_count": row["dynamic_key_path_count"],
                "nested_array_path_count": row["nested_array_path_count"],
            }
        )
    write_csv(OUT_DIR / "paper_statistics_by_db.csv", by_db_rows, by_db_fields)

    op_rows = []
    total_op_occ = sum(operator_occurrence.values()) or 1
    for op in sorted(set(operator_occurrence) | set(operator_presence)):
        op_rows.append(
            {
                "operator": op,
                "occurrence_count": operator_occurrence[op],
                "occurrence_share": round(operator_occurrence[op] / total_op_occ, 6),
                "record_presence_count": operator_presence[op],
                "record_presence_share": round(operator_presence[op] / total_records, 6),
            }
        )
    write_csv(OUT_DIR / "operator_statistics.csv", op_rows, ["operator", "occurrence_count", "occurrence_share", "record_presence_count", "record_presence_share"])

    stage_rows = []
    total_stage_occ = sum(stage_occurrence.values()) or 1
    for stage in sorted(set(stage_occurrence) | set(stage_presence)):
        stage_rows.append(
            {
                "stage": stage,
                "role": STAGE_ROLES.get(stage, "other"),
                "occurrence_count": stage_occurrence[stage],
                "occurrence_share": round(stage_occurrence[stage] / total_stage_occ, 6),
                "record_presence_count": stage_presence[stage],
                "record_presence_share": round(stage_presence[stage] / total_records, 6),
            }
        )
    write_csv(OUT_DIR / "stage_statistics.csv", stage_rows, ["stage", "role", "occurrence_count", "occurrence_share", "record_presence_count", "record_presence_share"])

    skeleton_rows = []
    for key, count in skeleton_counter.most_common(50):
        skeleton_rows.append({"family_type": "mql_skeleton_signature", "key": key, "count": count, "share": round(count / total_records, 6)})
    for key, count in stage_sequence_counter.most_common(50):
        skeleton_rows.append({"family_type": "stage_sequence", "key": key, "count": count, "share": round(count / total_records, 6)})
    write_csv(OUT_DIR / "skeleton_concentration.csv", skeleton_rows, ["family_type", "key", "count", "share"])

    feature_rows = []
    for key, value in sorted(semantic_presence.items()):
        feature_rows.append({"field": "semantic_presence", "value": key, "count": value, "share": round(value / total_records, 6)})
    for key, value in structural_bucket_counter.most_common():
        feature_rows.append({"field": "structural_bucket", "value": key, "count": value, "share": round(value / total_records, 6)})
    write_csv(OUT_DIR / "feature_statistics.csv", feature_rows, ["field", "value", "count", "share"])

    execution = fresh_execution or {}
    main_rows = [
        ["Databases", overall["scale"]["db_count"]],
        ["NL-MQL tasks", total_records],
        ["Public record fields", ", ".join(PUBLIC_RECORD_FIELDS)],
        ["NL utterances", overall["scale"]["total_nl_utterances"]],
        ["Schema collections / queried collections", f"{schema_overall['schema_collection_count']} / {len(collection_counter)}"],
        ["MongoDB documents", f"{schema_overall['document_count']:,}"],
        ["Aggregation pipelines", overall["mql_parse"]["aggregate_pipeline_records"]],
        ["Median / max stages", f"{overall['complexity_distributions']['stage_count']['median']} / {overall['complexity_distributions']['stage_count']['max']}"],
        ["Median unique operators", overall["complexity_distributions"]["unique_operator_count"]["median"]],
        ["Unique MQL signatures", overall["scale"]["distinct_mql_signatures"]],
        ["Global / DB-scoped skeleton families", f"{skeleton_global['distinct_global_skeleton_signatures']} / {skeleton_global['distinct_db_scoped_skeleton_families']}"],
        ["Dynamic-key operator records", f"{semantic_presence['dynamic_key_operator']} ({semantic_presence['dynamic_key_operator'] / total_records:.1%})"],
        ["Array-operator records", f"{semantic_presence['array_operator']} ({semantic_presence['array_operator'] / total_records:.1%})"],
        ["Nested dotted-path records", f"{semantic_presence['nested_dotted_path']} ({semantic_presence['nested_dotted_path'] / total_records:.1%})"],
        ["Public contract", "OK" if public_contract["ok"] else "INVALID"],
        ["Fresh exact execution", f"{execution.get('executed', 'NA')}/{execution.get('total', 'NA')}"],
    ]

    per_db_rows = [
        [
            db_id,
            row["records"],
            row["queried_collection_count"],
            f"{row['document_count']:,}",
            row["global_skeleton_signatures_in_db"],
            row["max_db_scoped_skeleton_family"],
            row["stage_count"]["median"],
            row["unique_operator_count"]["median"],
        ]
        for db_id, row in by_db.items()
    ]
    latex = "\n".join(
        [
            latex_table(["Statistic", "Value"], main_rows, "TEND lean public dataset overview statistics.", "tab:tend-overview"),
            latex_table(
                ["DB", "Pairs", "Queried coll.", "Docs", "Skeletons", "Max family", "Med. stages", "Med. ops"],
                per_db_rows,
                "Per-database TEND lean benchmark statistics.",
                "tab:tend-per-db",
            ),
        ]
    )
    (OUT_DIR / "paper_tables.tex").write_text(latex, encoding="utf-8")

    md = [
        "# Paper-Level TEND Dataset Statistics",
        "",
        f"- Generated at: `{overall['created_at']}`",
        f"- Release directory: `{RELEASE_DIR}`",
        "- Primary statistics source: `data/TEND_lean.json`.",
        f"- Public record fields: `{PUBLIC_RECORD_FIELDS}`.",
        "",
        "## Main-Text Candidate Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for metric, value in main_rows:
        md.append(f"| {metric} | `{value}` |")
    md.extend(
        [
            "",
            "## Public Contract",
            "",
            f"- Status: `{'OK' if public_contract['ok'] else 'INVALID'}`.",
            f"- Records: `{public_contract['record_count']}`.",
            f"- DB count: `{public_contract['db_count']}`.",
            f"- Distinct MQL strings: `{public_contract['distinct_mql_strings']}`.",
            f"- Distinct canonical NLQ strings: `{public_contract['distinct_canonical_nl']}`.",
        ]
    )
    if public_contract["issues"]:
        md.extend(["", "### Issues", ""])
        md.extend(f"- {issue}" for issue in public_contract["issues"])
    md.extend(
        [
            "",
            "## Diversity And Concentration",
            "",
            f"- Exact MQL strings: `{overall['scale']['distinct_mql_strings']}/{total_records}`.",
            f"- Exact MQL signatures: `{overall['scale']['distinct_mql_signatures']}/{total_records}`.",
            f"- Distinct NL texts: `{overall['scale']['distinct_nl_texts']}/{overall['scale']['total_nl_utterances']}`; distinct NL pairs: `{overall['scale']['distinct_nl_pairs']}/{total_records}`.",
            f"- Global skeleton signatures: `{skeleton_global['distinct_global_skeleton_signatures']}`; DB-scoped skeleton families: `{skeleton_global['distinct_db_scoped_skeleton_families']}`.",
            f"- Max global skeleton family: `{skeleton_global['max_global_skeleton_family']}`; max DB-scoped skeleton family: `{skeleton_global['max_db_scoped_skeleton_family']}`.",
            f"- Top-5 global skeleton coverage: `{skeleton_global['global_skeleton_top_coverage']['top_5']:.1%}`; top-10 coverage: `{skeleton_global['global_skeleton_top_coverage']['top_10']:.1%}`.",
            f"- Distinct stage sequences: `{skeleton_global['distinct_stage_sequences']}`; max stage-sequence family: `{skeleton_global['max_stage_sequence_family']}`.",
            f"- Top-5 stage-sequence coverage: `{skeleton_global['stage_sequence_top_coverage']['top_5']:.1%}`; top-10 coverage: `{skeleton_global['stage_sequence_top_coverage']['top_10']:.1%}`.",
            "",
            "## Complexity Distributions",
            "",
            "| Metric | Min | P25 | Median | P75 | P90 | Max | Mean |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for name in [
        "stage_count",
        "unique_operator_count",
        "stage_operator_occurrence_count",
        "mql_chars",
        "canonical_nl_words",
        "colloquial_nl_words",
        "field_path_reference_count",
        "max_field_path_depth",
        "limit_value",
    ]:
        q = overall["complexity_distributions"][name]
        md.append(f"| `{name}` | {q['min']} | {q['p25']} | {q['median']} | {q['p75']} | {q['p90']} | {q['max']} | {q['mean']} |")
    if pipeline_stage_stats:
        ps = pipeline_stage_stats["overall"]
        md.extend(
            [
                "",
                "## Detailed Pipeline Stage Summary",
                "",
                f"- Total top-level stage occurrences: `{ps['total_stage_occurrences']}`.",
                f"- Distinct stage types: `{ps['distinct_stage_types']}`.",
                f"- Distinct full stage sequences: `{ps['distinct_stage_sequences']}`; max sequence family: `{ps['max_stage_sequence_family']}`.",
                f"- Top-5 stage-sequence coverage: `{ps['stage_sequence_top_coverage']['top_5']:.1%}`; top-10 coverage: `{ps['stage_sequence_top_coverage']['top_10']:.1%}`.",
                f"- Stage-count histogram: `{ps['stage_count_histogram']}`.",
                f"- Structural buckets: `{ps['structural_buckets']}`.",
            ]
        )
    md.extend(
        [
            "",
            "## Mongo-Native Semantic Signals",
            "",
            "| Signal | Records | Share |",
            "|---|---:|---:|",
        ]
    )
    for key, value in overall["semantic_presence"].items():
        md.append(f"| `{key}` | {value['records']} | {value['share']:.1%} |")
    md.extend(
        [
            "",
            "## Per-DB Summary",
            "",
            "| DB | Records | Collections | Docs | Skeletons | Max family | Median stages | Median unique ops | Dynamic-key records | Array records | Grouping records |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for db_id, row in by_db.items():
        sem = row["semantic_presence"]
        md.append(
            f"| `{db_id}` | {row['records']} | {row['queried_collection_count']} | {row['document_count']:,} | {row['global_skeleton_signatures_in_db']} | {row['max_db_scoped_skeleton_family']} | {row['stage_count']['median']} | {row['unique_operator_count']['median']} | {sem['dynamic_key_operator']} | {sem['array_operator']} | {sem['grouping_operator']} |"
        )
    md.extend(
        [
            "",
            "## Generated Files",
            "",
            "- `paper_dataset_statistics.json`: complete lean public dataset statistics.",
            "- `paper_statistics_by_db.csv`: per-DB summary derived from public MQL/NLQ plus schema document counts.",
            "- `operator_statistics.csv`: MongoDB operator occurrence and per-record presence.",
            "- `stage_statistics.csv`: top-level aggregation stage occurrence and per-record presence.",
            "- `skeleton_concentration.csv`: MQL skeleton and stage-sequence concentration.",
            "- `feature_statistics.csv`: derived semantic-signal and structural-bucket counts.",
            "- `pipeline_stage_detailed_statistics.*` and `pipeline_stage_*.csv`: detailed stage distributions generated from the same lean source.",
        ]
    )
    (OUT_DIR / "paper_dataset_statistics.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "records": total_records,
                "dbs": len(by_db_records),
                "public_contract_ok": public_contract["ok"],
                "parse_errors": len(parse_errors),
                "distinct_mql_signatures": overall["scale"]["distinct_mql_signatures"],
                "distinct_skeletons": skeleton_global["distinct_global_skeleton_signatures"],
                "distinct_stage_sequences": skeleton_global["distinct_stage_sequences"],
                "max_skeleton_family": skeleton_global["max_global_skeleton_family"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
