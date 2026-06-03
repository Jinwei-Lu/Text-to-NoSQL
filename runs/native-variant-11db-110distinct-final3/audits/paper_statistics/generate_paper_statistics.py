#!/usr/bin/env python3
"""Generate paper-oriented statistics for the final TEND benchmark artifact."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
RUN_DIR = SCRIPT_DIR.parents[1]
DATASET_DIR = RUN_DIR / "dataset"
OUT_DIR = SCRIPT_DIR


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


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def size_human(path: Path) -> str:
    n = path.stat().st_size
    units = ["B", "KB", "MB", "GB"]
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{n}B"


def quantiles(values: list[float | int]) -> dict[str, float | int | None]:
    if not values:
        return {k: None for k in ["n", "min", "p10", "p25", "median", "p75", "p90", "p95", "max", "mean", "stdev"]}
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
        if abs(v - round(v)) < 1e-9:
            return int(round(v))
        return round(v, 4)

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
    probs = [c / total for c in counter.values()]
    ent = -sum(p * math.log2(p) for p in probs)
    max_ent = math.log2(len(counter)) if counter else 0.0
    return {
        "entropy_bits": round(ent, 4),
        "normalized_entropy": round(ent / max_ent, 4) if max_ent else 0.0,
        "effective_families": round(2**ent, 4),
    }


def gini(counter: Counter[Any]) -> float:
    xs = sorted(counter.values())
    n = len(xs)
    if n == 0:
        return 0.0
    total = sum(xs)
    if total == 0:
        return 0.0
    weighted = sum((i + 1) * x for i, x in enumerate(xs))
    return round((2 * weighted) / (n * total) - (n + 1) / n, 4)


def top_with_share(counter: Counter[Any], n: int = 20) -> list[dict[str, Any]]:
    total = sum(counter.values()) or 1
    return [{"key": k, "count": v, "share": round(v / total, 4)} for k, v in counter.most_common(n)]


def coverage_at(counter: Counter[Any], cutoffs: tuple[int, ...] = (1, 3, 5, 10, 20)) -> dict[str, float]:
    vals = [v for _, v in counter.most_common()]
    total = sum(vals) or 1
    return {f"top_{k}": round(sum(vals[:k]) / total, 4) for k in cutoffs}


def parse_mql(mql: str) -> tuple[str | None, list[dict[str, Any]] | None, str | None]:
    match = AGG_RE.match(mql.strip())
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
        for k, v in obj.items():
            yield k, v
            yield from walk(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk(item)


def operator_counts(pipeline: list[dict[str, Any]]) -> tuple[Counter[str], set[str]]:
    c: Counter[str] = Counter()
    for k, _ in walk(pipeline):
        if isinstance(k, str) and k.startswith("$"):
            c[k] += 1
    return c, set(c)


def stage_sequence(pipeline: list[dict[str, Any]]) -> list[str]:
    seq = []
    for stage in pipeline:
        if len(stage) == 1:
            seq.append(next(iter(stage)))
        else:
            seq.append("|".join(stage.keys()))
    return seq


def collect_field_paths(obj: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and not k.startswith("$") and "." in k:
                paths.append(k)
            paths.extend(collect_field_paths(v))
    elif isinstance(obj, list):
        for item in obj:
            paths.extend(collect_field_paths(item))
    elif isinstance(obj, str) and obj.startswith("$") and not obj.startswith("$$"):
        path = obj[1:]
        if "." in path:
            paths.append(path)
    return paths


def count_limit_values(pipeline: list[dict[str, Any]]) -> list[int]:
    vals = []
    for stage in pipeline:
        if "$limit" in stage and isinstance(stage["$limit"], int):
            vals.append(stage["$limit"])
    return vals


def nl_word_count(text: str) -> int:
    return len(text.split())


def latex_escape(s: Any) -> str:
    text = str(s)
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
        " & ".join(latex_escape(h) for h in headers) + " \\\\",
        "\\midrule",
    ]
    for row in rows:
        lines.append(" & ".join(latex_escape(x) for x in row) + " \\\\")
    lines.extend(["\\bottomrule", "\\end{tabular}", "\\end{table}", ""])
    return "\n".join(lines)


def load_schema_stats(schema_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    by_db: dict[str, dict[str, Any]] = {}
    total_schema_collections = 0
    total_docs = 0
    audit_depths = []
    dynamic_paths = 0
    nested_array_paths = 0
    dynamic_array_object_paths = 0
    array_object_dynamic_paths = 0
    audited_dbs = 0
    presence_counts: Counter[str] = Counter()
    collection_doc_counts = []

    for path in sorted(schema_dir.glob("*.json")):
        schema = read_json(path)
        db = schema.get("db_id", path.stem)
        collections = schema.get("collections") or {}
        source_tables = schema.get("source_tables") or []
        collection_count = len(collections)
        docs = 0
        for name, meta in collections.items():
            doc_count = int((meta or {}).get("document_count") or 0)
            docs += doc_count
            collection_doc_counts.append(doc_count)
        audit = schema.get("structure_audit") or {}
        has_audit = bool(audit)
        if has_audit:
            audited_dbs += 1
            audit_depths.append(audit.get("max_depth", 0))
            dynamic_paths += len(audit.get("dynamic_key_paths") or [])
            nested_array_paths += len(audit.get("nested_array_paths") or [])
            dynamic_array_object_paths += len(audit.get("dynamic_array_object_paths") or [])
            array_object_dynamic_paths += len(audit.get("array_object_dynamic_paths") or [])
            presence_counts.update(audit.get("presence_state_counts") or {})
        total_schema_collections += collection_count
        total_docs += docs
        by_db[db] = {
            "source_table_count": len(source_tables),
            "source_tables": source_tables,
            "schema_collection_count": collection_count,
            "schema_collections": sorted(collections.keys()),
            "document_count": docs,
            "max_depth": audit.get("max_depth"),
            "structure_audit_available": has_audit,
            "dynamic_key_path_count": len(audit.get("dynamic_key_paths") or []),
            "nested_array_path_count": len(audit.get("nested_array_paths") or []),
            "dynamic_array_object_path_count": len(audit.get("dynamic_array_object_paths") or []),
            "array_object_dynamic_path_count": len(audit.get("array_object_dynamic_paths") or []),
            "presence_state_counts": dict(audit.get("presence_state_counts") or {}),
        }

    overall = {
        "schema_collection_count": total_schema_collections,
        "document_count": total_docs,
        "collection_document_count": quantiles(collection_doc_counts),
        "structure_audit_db_count": audited_dbs,
        "structure_audit_db_denominator": len(by_db),
        "max_depth": quantiles(audit_depths),
        "dynamic_key_path_count": dynamic_paths,
        "nested_array_path_count": nested_array_paths,
        "dynamic_array_object_path_count": dynamic_array_object_paths,
        "array_object_dynamic_path_count": array_object_dynamic_paths,
        "presence_state_counts": dict(presence_counts),
    }
    return by_db, overall


def load_manifest_stats(manifest_dir: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    by_db: dict[str, dict[str, Any]] = {}
    total_features = 0
    type_counter: Counter[str] = Counter()
    required_construct_counter: Counter[str] = Counter()
    query_patterns: set[str] = set()
    provenance_ref_counts = []

    for path in sorted(manifest_dir.glob("*.yaml")):
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        db = manifest.get("db_id", path.stem)
        features = manifest.get("features") or []
        total_features += len(features)
        feature_types = Counter()
        constructs = Counter()
        patterns = set()
        refs = []
        for feature in features:
            feature_types[feature.get("type", "unknown")] += 1
            for op in feature.get("required_native_constructs") or []:
                required_construct_counter[op] += 1
                constructs[op] += 1
            for pattern in feature.get("supported_query_patterns") or []:
                query_patterns.add(pattern)
                patterns.add(pattern)
            ref_count = len(feature.get("provenance_refs") or [])
            refs.append(ref_count)
            provenance_ref_counts.append(ref_count)
            type_counter[feature.get("type", "unknown")] += 1
        by_db[db] = {
            "manifest_feature_count": len(features),
            "manifest_feature_types": dict(feature_types),
            "manifest_required_constructs": dict(constructs),
            "manifest_query_pattern_count": len(patterns),
            "manifest_provenance_refs_per_feature": quantiles(refs),
        }

    overall = {
        "manifest_feature_count": total_features,
        "manifest_feature_type": dict(type_counter),
        "manifest_required_constructs": required_construct_counter.most_common(),
        "manifest_query_pattern_count": len(query_patterns),
        "manifest_provenance_refs_per_feature": quantiles(provenance_ref_counts),
    }
    return overall, by_db


def main() -> None:
    full = read_json(DATASET_DIR / "TEND.json")
    test = read_json(DATASET_DIR / "test.json")
    lean = read_json(DATASET_DIR / "TEND_lean.json")
    lean_jsonl = [json.loads(line) for line in (DATASET_DIR / "TEND_lean.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]

    parse_errors: list[dict[str, Any]] = []
    per_record: list[dict[str, Any]] = []
    by_db_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    stage_occurrence: Counter[str] = Counter()
    stage_presence: Counter[str] = Counter()
    operator_occurrence: Counter[str] = Counter()
    operator_presence: Counter[str] = Counter()
    stage_sequence_counter: Counter[str] = Counter()
    skeleton_counter: Counter[str] = Counter()
    db_scoped_skeleton_counter: Counter[tuple[str, str]] = Counter()
    collection_counter: Counter[tuple[str, str]] = Counter()
    collection_presence: Counter[tuple[str, str]] = Counter()
    native_query_pattern_counter: Counter[str] = Counter()
    archetype_counter: Counter[str] = Counter()
    native_feature_counter: Counter[str] = Counter()
    schema_feature_counter: Counter[str] = Counter()
    world_signature_counter: Counter[str] = Counter()
    canonical_counter: Counter[str] = Counter()
    colloquial_counter: Counter[str] = Counter()
    nl_pair_counter: Counter[tuple[str, str]] = Counter()
    nl_text_counter: Counter[str] = Counter()
    forbidden_presence: Counter[str] = Counter()
    anti_sql_evidence_counter: Counter[str] = Counter()
    native_verification_errors: Counter[str] = Counter()

    distributions: dict[str, list[float | int]] = defaultdict(list)
    semantic_presence = Counter()
    complexity_buckets = Counter()

    for rec in full:
        db = rec["db_id"]
        by_db_records[db].append(rec)
        canonical = rec.get("nl_queries", {}).get("canonical", "")
        colloquial = rec.get("nl_queries", {}).get("colloquial", "")
        canonical_counter[canonical] += 1
        colloquial_counter[colloquial] += 1
        nl_pair_counter[(canonical, colloquial)] += 1
        nl_text_counter[canonical] += 1
        nl_text_counter[colloquial] += 1
        native_query_pattern_counter[rec.get("native_query_pattern", "")] += 1
        archetype_counter[rec.get("archetype", "")] += 1
        native_feature_counter[rec.get("native_feature_id", "")] += 1
        schema_feature_counter[rec.get("schema_feature", "")] += 1
        world_signature_counter[rec.get("world_signature", "")] += 1
        skeleton = rec.get("mql_skeleton_signature", "")
        skeleton_counter[skeleton] += 1
        db_scoped_skeleton_counter[(db, skeleton)] += 1
        for evidence in rec.get("anti_sql_transfer_evidence") or []:
            anti_sql_evidence_counter[evidence] += 1
        for err in (rec.get("native_verification") or {}).get("errors") or []:
            native_verification_errors[err] += 1

        collection, pipeline, err = parse_mql(rec.get("MQL", ""))
        if err or pipeline is None:
            parse_errors.append({"record_id": rec.get("record_id"), "db_id": db, "error": err})
            continue

        collection_counter[(db, collection or "")] += 1
        collection_presence[(db, collection or "")] += 1
        seq = stage_sequence(pipeline)
        seq_string = ">".join(seq)
        stage_sequence_counter[seq_string] += 1
        stage_occurrence.update(seq)
        stage_presence.update(set(seq))
        op_counts, op_set = operator_counts(pipeline)
        operator_occurrence.update(op_counts)
        operator_presence.update(op_set)
        field_paths = collect_field_paths(pipeline)
        max_path_depth = max((len(path.split(".")) for path in field_paths), default=0)
        limits = count_limit_values(pipeline)

        dynamic_by_operator = bool(op_set & DYNAMIC_KEY_OPS)
        array_by_operator = bool(op_set & ARRAY_SEMANTIC_OPS)
        grouping_by_operator = bool(op_set & GROUPING_OPS)
        expr_match = bool(op_set & EXPR_MATCH_OPS)
        nested_path = max_path_depth >= 2
        forbidden = sorted(op for op in op_set if op in DETERMINISM_FORBIDDEN_OPS)
        forbidden_presence.update(forbidden)
        complexity = "high"
        if len(seq) >= 8 or len(op_set) >= 14 or dynamic_by_operator + array_by_operator + grouping_by_operator + nested_path >= 3:
            complexity = "high"
        if len(seq) >= 10 or len(op_set) >= 17:
            complexity = "very_high"
        complexity_buckets[complexity] += 1

        for name, present in {
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
            "has_lookup": "$lookup" in op_set,
            "has_filter_or_map": bool(op_set & {"$filter", "$map"}),
            "has_conditional": bool(op_set & {"$cond", "$switch"}),
        }.items():
            if present:
                semantic_presence[name] += 1

        distributions["stage_count"].append(len(seq))
        distributions["stage_operator_occurrence_count"].append(sum(op_counts.values()))
        distributions["unique_operator_count"].append(len(op_set))
        distributions["mql_chars"].append(len(rec.get("MQL", "")))
        distributions["canonical_nl_words"].append(nl_word_count(canonical))
        distributions["colloquial_nl_words"].append(nl_word_count(colloquial))
        distributions["field_path_reference_count"].append(len(field_paths))
        distributions["max_field_path_depth"].append(max_path_depth)
        distributions["limit_value"].extend(limits)

        per_record.append(
            {
                "record_id": rec.get("record_id"),
                "db_id": db,
                "collection": collection,
                "stage_count": len(seq),
                "stage_sequence": seq_string,
                "operator_occurrence_count": sum(op_counts.values()),
                "unique_operator_count": len(op_set),
                "mql_chars": len(rec.get("MQL", "")),
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
                "mql_skeleton_signature": skeleton,
                "mql_skeleton_summary": rec.get("mql_skeleton_summary"),
                "native_query_pattern": rec.get("native_query_pattern"),
                "native_feature_id": rec.get("native_feature_id"),
                "schema_flex": rec.get("schema_flex"),
                "shape_policy": rec.get("shape_policy"),
                "anti_sql_transfer_level": rec.get("anti_sql_transfer_level"),
                "native_verification_ok": (rec.get("native_verification") or {}).get("ok"),
            }
        )

    schema_by_db, schema_overall = load_schema_stats(DATASET_DIR / "mongodb_schema")
    manifest_overall, manifest_by_db = load_manifest_stats(DATASET_DIR / "native_feature_manifest")

    by_db: dict[str, dict[str, Any]] = {}
    for db in sorted(by_db_records):
        recs = by_db_records[db]
        metrics = [r for r in per_record if r["db_id"] == db]
        db_stage_sequences = Counter(r["stage_sequence"] for r in metrics)
        db_skeletons = Counter(r["mql_skeleton_signature"] for r in metrics)
        db_collections = Counter(r["collection"] for r in metrics)
        db_ops_presence = Counter()
        db_ops_occurrence = Counter()
        for rec_metric in metrics:
            _, pipeline, _ = parse_mql(next(r["MQL"] for r in recs if r["record_id"] == rec_metric["record_id"]))
            if pipeline is not None:
                op_counts, op_set = operator_counts(pipeline)
                db_ops_occurrence.update(op_counts)
                db_ops_presence.update(op_set)
        schema = schema_by_db.get(db, {})
        queried_schema_intersection = sorted(set(db_collections) & set(schema.get("schema_collections") or []))
        by_db[db] = {
            "records": len(recs),
            "distinct_mql": len({r["MQL"] for r in recs}),
            "distinct_mql_signature": len({r.get("mql_signature") for r in recs}),
            "distinct_canonical_nl": len({r.get("nl_queries", {}).get("canonical", "") for r in recs}),
            "distinct_colloquial_nl": len({r.get("nl_queries", {}).get("colloquial", "") for r in recs}),
            "distinct_nl_texts": len(
                {r.get("nl_queries", {}).get("canonical", "") for r in recs}
                | {r.get("nl_queries", {}).get("colloquial", "") for r in recs}
            ),
            "global_skeleton_signatures_in_db": len(db_skeletons),
            "db_scoped_skeleton_families": len(db_skeletons),
            "max_db_scoped_skeleton_family": max(db_skeletons.values()) if db_skeletons else 0,
            "stage_sequence_families": len(db_stage_sequences),
            "max_stage_sequence_family": max(db_stage_sequences.values()) if db_stage_sequences else 0,
            "native_query_patterns": len({r.get("native_query_pattern") for r in recs}),
            "native_feature_ids": len({r.get("native_feature_id") for r in recs}),
            "schema_features": len({r.get("schema_feature") for r in recs}),
            "queried_collection_count": len(db_collections),
            "queried_collections": dict(db_collections),
            "schema_collection_count": schema.get("schema_collection_count", 0),
            "schema_queried_collection_coverage": round(len(queried_schema_intersection) / schema.get("schema_collection_count", 1), 4)
            if schema.get("schema_collection_count")
            else None,
            "document_count": schema.get("document_count", 0),
            "source_table_count": schema.get("source_table_count", 0),
            "structure_audit_available": schema.get("structure_audit_available", False),
            "max_depth": schema.get("max_depth"),
            "dynamic_key_path_count": schema.get("dynamic_key_path_count", 0),
            "nested_array_path_count": schema.get("nested_array_path_count", 0),
            "dynamic_array_object_path_count": schema.get("dynamic_array_object_path_count", 0),
            "array_object_dynamic_path_count": schema.get("array_object_dynamic_path_count", 0),
            "schema_flex": dict(Counter(r.get("schema_flex") for r in recs)),
            "native_feature_type": dict(Counter(r.get("native_feature_type") for r in recs)),
            "shape_policy": dict(Counter(r.get("shape_policy") for r in recs)),
            "anti_sql_transfer_level": dict(Counter(r.get("anti_sql_transfer_level") for r in recs)),
            "native_verification_ok": dict(Counter((r.get("native_verification") or {}).get("ok") for r in recs)),
            "stage_count": quantiles([m["stage_count"] for m in metrics]),
            "unique_operator_count": quantiles([m["unique_operator_count"] for m in metrics]),
            "operator_occurrence_count": quantiles([m["operator_occurrence_count"] for m in metrics]),
            "mql_chars": quantiles([m["mql_chars"] for m in metrics]),
            "canonical_nl_words": quantiles([m["canonical_nl_words"] for m in metrics]),
            "colloquial_nl_words": quantiles([m["colloquial_nl_words"] for m in metrics]),
            "field_path_reference_count": quantiles([m["field_path_reference_count"] for m in metrics]),
            "max_field_path_depth": quantiles([m["max_field_path_depth"] for m in metrics]),
            "semantic_presence": {
                k: sum(1 for m in metrics if m.get(k))
                for k in [
                    "dynamic_key_operator",
                    "array_operator",
                    "grouping_operator",
                    "expr_match",
                    "nested_dotted_path",
                ]
            },
            "top_operator_presence": db_ops_presence.most_common(20),
            "top_operator_occurrence": db_ops_occurrence.most_common(20),
            "top_stage_sequences": top_with_share(db_stage_sequences, 10),
            "top_global_skeleton_families_in_db": top_with_share(db_skeletons, 10),
            **manifest_by_db.get(db, {}),
        }

    lean_key_shapes = Counter(tuple(sorted(row.keys())) for row in lean)
    full_key_coverage = {key: sum(1 for row in full if key in row and row.get(key) is not None) for key in sorted(set().union(*(r.keys() for r in full)))}
    lean_alignment = {
        "count_match_full": len(lean) == len(full),
        "jsonl_count_match_full": len(lean_jsonl) == len(full),
        "record_id_sequence_match_full": [r.get("record_id") for r in lean] == [r.get("record_id") for r in full],
        "jsonl_record_id_sequence_match_full": [r.get("record_id") for r in lean_jsonl] == [r.get("record_id") for r in full],
    }

    total_records = len(full)
    exact_execution_path = RUN_DIR / "audits" / "surgery" / "post_surgery_exact_execution.json"
    exact_execution = read_json(exact_execution_path) if exact_execution_path.exists() else None
    fresh_execution_path = OUT_DIR / "fresh_exact_execution_by_db_verification.json"
    fresh_execution = read_json(fresh_execution_path) if fresh_execution_path.exists() else None
    surgical_path = RUN_DIR / "audits" / "surgery" / "surgical_nl_mql_patch_report.json"
    surgical_patch = read_json(surgical_path) if surgical_path.exists() else None
    validator_snapshot_path = OUT_DIR / "release_validator_snapshot.json"
    validator_snapshot = read_json(validator_snapshot_path) if validator_snapshot_path.exists() else None
    validator_issue_path = OUT_DIR / "release_validator_issue_statistics.json"
    validator_issue_stats = read_json(validator_issue_path) if validator_issue_path.exists() else None
    pipeline_stage_stats_path = OUT_DIR / "pipeline_stage_detailed_statistics.json"
    pipeline_stage_stats = read_json(pipeline_stage_stats_path) if pipeline_stage_stats_path.exists() else None

    cross_tabs = {
        "schema_flex_by_shape_policy": {
            flex: dict(Counter(r.get("shape_policy") for r in full if r.get("schema_flex") == flex))
            for flex in sorted({r.get("schema_flex") for r in full})
        },
        "schema_flex_by_native_feature_type": {
            flex: dict(Counter(r.get("native_feature_type") for r in full if r.get("schema_flex") == flex))
            for flex in sorted({r.get("schema_flex") for r in full})
        },
        "native_feature_type_by_shape_policy": {
            typ: dict(Counter(r.get("shape_policy") for r in full if r.get("native_feature_type") == typ))
            for typ in sorted({r.get("native_feature_type") for r in full})
        },
        "anti_sql_level_by_schema_flex": {
            level: dict(Counter(r.get("schema_flex") for r in full if r.get("anti_sql_transfer_level") == level))
            for level in sorted({r.get("anti_sql_transfer_level") for r in full})
        },
    }

    skeleton_global = {
        "distinct_global_skeleton_signatures": len(skeleton_counter),
        "distinct_db_scoped_skeleton_families": len(db_scoped_skeleton_counter),
        "max_global_skeleton_family": max(skeleton_counter.values()) if skeleton_counter else 0,
        "max_db_scoped_skeleton_family": max(db_scoped_skeleton_counter.values()) if db_scoped_skeleton_counter else 0,
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

    metadata_distributions = {
        "difficulty": dict(Counter(r.get("difficulty") for r in full)),
        "mechanism": dict(Counter(r.get("mechanism") for r in full)),
        "schema_flex": dict(Counter(r.get("schema_flex") for r in full)),
        "shape_policy": dict(Counter(r.get("shape_policy") for r in full)),
        "native_feature_type": dict(Counter(r.get("native_feature_type") for r in full)),
        "sql_infeasibility_class": dict(Counter(r.get("sql_infeasibility_class") for r in full)),
        "anti_sql_transfer_level": dict(Counter(r.get("anti_sql_transfer_level") for r in full)),
        "native_verification_ok": dict(Counter((r.get("native_verification") or {}).get("ok") for r in full)),
        "native_verification_errors": dict(native_verification_errors),
        "anti_sql_transfer_evidence_top": top_with_share(anti_sql_evidence_counter, 50),
    }

    dataset_files = {}
    for name in ["test.json", "TEND.json", "test_lean.json", "TEND_lean.json", "TEND_lean.jsonl"]:
        path = DATASET_DIR / name
        if path.exists():
            entry: dict[str, Any] = {"path": str(path.relative_to(RUN_DIR)), "size": path.stat().st_size, "size_human": size_human(path), "sha256": sha256(path)}
            if path.suffix == ".json":
                entry["records"] = len(read_json(path))
            elif path.suffix == ".jsonl":
                entry["lines"] = len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])
            dataset_files[name] = entry

    overall = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(RUN_DIR),
        "dataset_source": "TEND.json for full metadata statistics; TEND_lean.json/TEND_lean.jsonl for released lean evaluation rows.",
        "metric_definitions": {
            "operator_occurrence": "Counts every MongoDB operator key beginning with '$' across all stages and nested expressions.",
            "operator_presence": "Counts each MongoDB operator at most once per record.",
            "stage_occurrence": "Counts top-level aggregation pipeline stages; a record can contribute multiple occurrences of the same stage.",
            "stage_presence": "Counts each top-level aggregation stage at most once per record.",
            "global_skeleton_signature": "Counts mql_skeleton_signature across the entire dataset.",
            "db_scoped_skeleton_family": "Counts (db_id, mql_skeleton_signature), matching the H11-style per-database diversity cap.",
            "stage_sequence": "Counts top-level stage operator sequences, ignoring nested expression details.",
            "nested_dotted_path": "Approximate static MQL signal: record has at least one dotted field path reference in the parsed pipeline.",
            "nl_word_count": "Whitespace token count, so schema/query identifiers containing underscores remain a single token.",
            "array_operator": "Core array traversal/array construction operators: $unwind, $size, $filter, $map, $isArray, $addToSet, $push, $slice, and $arrayElemAt. The scalar membership operator $in is reported separately in operator statistics.",
        },
        "dataset_files": dataset_files,
        "scale": {
            "records": total_records,
            "db_count": len(by_db_records),
            "records_per_db": dict(sorted((db, len(recs)) for db, recs in by_db_records.items())),
            "canonical_nl_utterances": len(full),
            "colloquial_nl_utterances": len(full),
            "total_nl_utterances": len(full) * 2,
            "distinct_canonical_nl": len(canonical_counter),
            "distinct_colloquial_nl": len(colloquial_counter),
            "distinct_nl_texts": len(nl_text_counter),
            "distinct_nl_pairs": len(nl_pair_counter),
            "distinct_mql_strings": len({r.get("MQL") for r in full}),
            "distinct_mql_signatures": len({r.get("mql_signature") for r in full}),
            "distinct_world_signatures": len(world_signature_counter),
        },
        "lean_release": {
            "lean_key_shapes": [{"keys": list(keys), "count": count} for keys, count in lean_key_shapes.items()],
            "alignment": lean_alignment,
        },
        "mongo_corpus": {
            **schema_overall,
            "queried_collection_pairs": len(collection_counter),
            "queried_collection_pair_counts_top": [
                {"db_id": db, "collection": coll, "count": count}
                for (db, coll), count in collection_counter.most_common(50)
            ],
            "schema_collection_coverage_by_query": round(
                len(set(collection_counter)) / schema_overall["schema_collection_count"], 4
            )
            if schema_overall["schema_collection_count"]
            else None,
        },
        "native_feature_manifest": manifest_overall,
        "mql_parse": {
            "aggregate_pipeline_records": total_records - len(parse_errors),
            "parse_errors": parse_errors,
            "all_records_parse_as_aggregate": len(parse_errors) == 0,
        },
        "complexity_distributions": {name: quantiles(vals) for name, vals in sorted(distributions.items())},
        "semantic_presence": {
            key: {"records": value, "share": round(value / total_records, 4)} for key, value in sorted(semantic_presence.items())
        },
        "metadata_distributions": metadata_distributions,
        "cross_tabs": cross_tabs,
        "diversity": {
            "native_query_patterns": len(native_query_pattern_counter),
            "archetypes": len(archetype_counter),
            "native_feature_ids": len(native_feature_counter),
            "schema_features": len(schema_feature_counter),
            "top_native_query_patterns": top_with_share(native_query_pattern_counter, 50),
            "top_native_feature_ids": top_with_share(native_feature_counter, 50),
            "top_schema_features": top_with_share(schema_feature_counter, 50),
            **skeleton_global,
        },
        "operators": {
            "stage_occurrence": top_with_share(stage_occurrence, 100),
            "stage_presence": top_with_share(stage_presence, 100),
            "operator_occurrence": top_with_share(operator_occurrence, 150),
            "operator_presence": top_with_share(operator_presence, 150),
            "forbidden_or_nondeterministic_operator_presence": dict(forbidden_presence),
        },
        "validity": {
            "native_verification_ok": dict(Counter((r.get("native_verification") or {}).get("ok") for r in full)),
            "native_verification_errors": dict(native_verification_errors),
            "exact_execution_report": exact_execution,
            "fresh_exact_execution_by_db_report": fresh_execution,
            "release_validator_snapshot": validator_snapshot,
            "release_validator_issue_statistics": validator_issue_stats,
            "surgical_patch_summary": {
                "changed_records": surgical_patch.get("changed_records") if isinstance(surgical_patch, dict) else None,
                "db_changed_counts": surgical_patch.get("db_changed_counts") if isinstance(surgical_patch, dict) else None,
            },
        },
        "detailed_pipeline_stage_statistics": pipeline_stage_stats,
        "complexity_buckets": dict(complexity_buckets),
        "by_db": by_db,
    }

    write_json(OUT_DIR / "paper_dataset_statistics.json", overall)

    with (OUT_DIR / "paper_statistics_by_db.csv").open("w", newline="", encoding="utf-8") as f:
        fields = [
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
            "native_query_patterns",
            "native_feature_ids",
            "schema_features",
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
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for db, row in by_db.items():
            sem = row["semantic_presence"]
            writer.writerow(
                {
                    "db_id": db,
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
                    "native_query_patterns": row["native_query_patterns"],
                    "native_feature_ids": row["native_feature_ids"],
                    "schema_features": row["schema_features"],
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

    with (OUT_DIR / "operator_statistics.csv").open("w", newline="", encoding="utf-8") as f:
        fields = ["operator", "occurrence_count", "occurrence_share", "record_presence_count", "record_presence_share"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        ops = sorted(set(operator_occurrence) | set(operator_presence))
        total_occ = sum(operator_occurrence.values()) or 1
        for op in ops:
            writer.writerow(
                {
                    "operator": op,
                    "occurrence_count": operator_occurrence[op],
                    "occurrence_share": round(operator_occurrence[op] / total_occ, 6),
                    "record_presence_count": operator_presence[op],
                    "record_presence_share": round(operator_presence[op] / total_records, 6),
                }
            )

    with (OUT_DIR / "stage_statistics.csv").open("w", newline="", encoding="utf-8") as f:
        fields = ["stage", "occurrence_count", "occurrence_share", "record_presence_count", "record_presence_share"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        total_stage_occ = sum(stage_occurrence.values()) or 1
        for stage in sorted(set(stage_occurrence) | set(stage_presence)):
            writer.writerow(
                {
                    "stage": stage,
                    "occurrence_count": stage_occurrence[stage],
                    "occurrence_share": round(stage_occurrence[stage] / total_stage_occ, 6),
                    "record_presence_count": stage_presence[stage],
                    "record_presence_share": round(stage_presence[stage] / total_records, 6),
                }
            )

    with (OUT_DIR / "skeleton_concentration.csv").open("w", newline="", encoding="utf-8") as f:
        fields = ["family_type", "key", "count", "share"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for key, count in skeleton_counter.most_common(50):
            writer.writerow({"family_type": "global_skeleton_signature", "key": key, "count": count, "share": round(count / total_records, 6)})
        for key, count in stage_sequence_counter.most_common(50):
            writer.writerow({"family_type": "stage_sequence", "key": key, "count": count, "share": round(count / total_records, 6)})

    with (OUT_DIR / "feature_statistics.csv").open("w", newline="", encoding="utf-8") as f:
        fields = ["field", "value", "count", "share"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for field in ["schema_flex", "shape_policy", "native_feature_type", "anti_sql_transfer_level", "difficulty", "sql_infeasibility_class"]:
            c = Counter(r.get(field) for r in full)
            for value, count in c.most_common():
                writer.writerow({"field": field, "value": value, "count": count, "share": round(count / total_records, 6)})

    execution_for_table = fresh_execution or exact_execution or {}
    main_rows = [
        ["Databases", overall["scale"]["db_count"]],
        ["NL-MQL tasks", total_records],
        ["NL utterances", overall["scale"]["total_nl_utterances"]],
        ["Schema collections / queried collections", f"{schema_overall['schema_collection_count']} / {len(collection_counter)}"],
        ["MongoDB documents", f"{schema_overall['document_count']:,}"],
        ["Aggregation pipelines", overall["mql_parse"]["aggregate_pipeline_records"]],
        ["Median / max stages", f"{overall['complexity_distributions']['stage_count']['median']} / {overall['complexity_distributions']['stage_count']['max']}"],
        ["Median unique operators", overall["complexity_distributions"]["unique_operator_count"]["median"]],
        ["Unique MQL signatures", overall["scale"]["distinct_mql_signatures"]],
        ["Global / DB-scoped skeleton families", f"{skeleton_global['distinct_global_skeleton_signatures']} / {skeleton_global['distinct_db_scoped_skeleton_families']}"],
        ["Native query patterns", overall["diversity"]["native_query_patterns"]],
        ["Native feature ids", overall["diversity"]["native_feature_ids"]],
        ["Dynamic-key operator records", f"{semantic_presence['dynamic_key_operator']} ({semantic_presence['dynamic_key_operator']/total_records:.1%})"],
        ["Array-operator records", f"{semantic_presence['array_operator']} ({semantic_presence['array_operator']/total_records:.1%})"],
        ["Nested dotted-path records", f"{semantic_presence['nested_dotted_path']} ({semantic_presence['nested_dotted_path']/total_records:.1%})"],
        ["Fresh exact execution", f"{execution_for_table.get('executed', 'NA')}/{execution_for_table.get('total', 'NA')}"],
        ["Native verification metadata", dict(Counter((r.get("native_verification") or {}).get("ok") for r in full))],
    ]

    per_db_rows = [
        [
            db,
            row["records"],
            row["queried_collection_count"],
            f"{row['document_count']:,}",
            row["global_skeleton_signatures_in_db"],
            row["max_db_scoped_skeleton_family"],
            row["stage_count"]["median"],
            row["unique_operator_count"]["median"],
            row["native_query_patterns"],
            row["native_feature_ids"],
        ]
        for db, row in by_db.items()
    ]

    latex = "\n".join(
        [
            latex_table(["Statistic", "Value"], main_rows, "TEND dataset overview statistics.", "tab:tend-overview"),
            latex_table(
                [
                    "DB",
                    "Pairs",
                    "Queried coll.",
                    "Docs",
                    "Skeletons",
                    "Max family",
                    "Med. stages",
                    "Med. ops",
                    "Patterns",
                    "Features",
                ],
                per_db_rows,
                "Per-database TEND benchmark statistics.",
                "tab:tend-per-db",
            ),
        ]
    )
    (OUT_DIR / "paper_tables.tex").write_text(latex, encoding="utf-8")

    md_lines = [
        "# Paper-Level TEND Dataset Statistics",
        "",
        f"- Generated at: `{overall['created_at']}`",
        f"- Run directory: `{RUN_DIR}`",
        "- Primary statistics source: `dataset/TEND.json`; lean release shape checked against `TEND_lean.json` and `TEND_lean.jsonl`.",
        "",
        "## Main-Text Candidate Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for metric, value in main_rows:
        md_lines.append(f"| {metric} | `{value}` |")
    md_lines.extend(
        [
            "",
            "## Diversity And Concentration",
            "",
            f"- Exact MQL signatures: `{overall['scale']['distinct_mql_signatures']}/{total_records}`.",
            f"- Distinct NL texts: `{overall['scale']['distinct_nl_texts']}/{overall['scale']['total_nl_utterances']}`; distinct NL pairs: `{overall['scale']['distinct_nl_pairs']}/{total_records}`.",
            f"- Global skeleton signatures: `{skeleton_global['distinct_global_skeleton_signatures']}`; db-scoped skeleton families: `{skeleton_global['distinct_db_scoped_skeleton_families']}`.",
            f"- Max global skeleton family: `{skeleton_global['max_global_skeleton_family']}`; max db-scoped skeleton family: `{skeleton_global['max_db_scoped_skeleton_family']}`.",
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
    for name in ["stage_count", "unique_operator_count", "stage_operator_occurrence_count", "mql_chars", "canonical_nl_words", "colloquial_nl_words", "field_path_reference_count", "max_field_path_depth", "limit_value"]:
        q = overall["complexity_distributions"][name]
        md_lines.append(f"| `{name}` | {q['min']} | {q['p25']} | {q['median']} | {q['p75']} | {q['p90']} | {q['max']} | {q['mean']} |")
    if pipeline_stage_stats:
        ps = pipeline_stage_stats["overall"]
        md_lines.extend(
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
                "- Full detail is in `pipeline_stage_detailed_statistics.md` and the `pipeline_stage_*.csv` files.",
            ]
        )
    md_lines.extend(
        [
            "",
            "## Mongo-Native Semantic Signals",
            "",
            "| Signal | Records | Share |",
            "|---|---:|---:|",
        ]
    )
    for key, value in overall["semantic_presence"].items():
        md_lines.append(f"| `{key}` | {value['records']} | {value['share']:.1%} |")
    md_lines.extend(
        [
            "",
            "## Metadata Distributions",
            "",
        ]
    )
    for field in ["schema_flex", "shape_policy", "native_feature_type", "anti_sql_transfer_level", "difficulty", "sql_infeasibility_class", "native_verification_ok"]:
        md_lines.append(f"- `{field}`: `{metadata_distributions[field]}`")
    if validator_issue_stats:
        md_lines.extend(
            [
                "",
                "## Release Validator Caveat",
                "",
                f"- `tend validate` status: `{'OK' if validator_issue_stats['ok'] else 'INVALID'}`.",
                f"- Record-level validator issues: `{validator_issue_stats['record_violations']}`; schema issues: `{validator_issue_stats['schema_violations']}`; file issues: `{validator_issue_stats['file_violations']}`.",
                "- Category counts:",
                "",
                "| Category | Issues | Affected records | Issue share | Record share |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for row in validator_issue_stats["issue_categories"]:
            md_lines.append(
                f"| `{row['category']}` | {row['issues']} | {row['affected_records']} | {row['issue_share']:.1%} | {row['record_share']:.1%} |"
            )
        md_lines.extend(
            [
                "",
                "This validator gate is distinct from exact Mongo execution: the validator issues are primarily metadata/provenance/native-feature gate mismatches, while the exact execution report records runtime query execution.",
            ]
        )
    md_lines.extend(
        [
            "",
            "## Per-Database Table",
            "",
            "| DB | Pairs | Queried collections | Schema collections | Docs | Skeletons | Max family | Median stages | Median unique ops | Query patterns | Native features |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for db, row in by_db.items():
        md_lines.append(
            f"| `{db}` | {row['records']} | {row['queried_collection_count']} | {row['schema_collection_count']} | {row['document_count']:,} | {row['global_skeleton_signatures_in_db']} | {row['max_db_scoped_skeleton_family']} | {row['stage_count']['median']} | {row['unique_operator_count']['median']} | {row['native_query_patterns']} | {row['native_feature_ids']} |"
        )
    md_lines.extend(
        [
            "",
            "## Notes For Paper Wording",
            "",
            "- Report exact query diversity and skeleton-level concentration together. The dataset has fully unique exact MQL strings, but skeleton families are intentionally reused across semantically different MongoDB domains.",
            "- Distinguish global skeleton signatures from db-scoped skeleton families. The H11-style diversity cap is db-scoped; global family concentration is a separate descriptive statistic.",
            "- Distinguish operator occurrence from operator presence. Occurrence counts nested expression operators repeatedly; presence counts each operator at most once per record.",
            "- Distinguish exact Mongo execution from native-verification metadata. The execution report records runtime execution status; `native_verification.ok` records a metadata/native-feature gate and currently has one flagged record.",
            "",
            "## Generated Files",
            "",
            "- `paper_dataset_statistics.json`: complete machine-readable statistics.",
            "- `paper_statistics_by_db.csv`: per-database table for papers/appendix.",
            "- `operator_statistics.csv`: operator occurrence and per-record presence.",
            "- `stage_statistics.csv`: stage occurrence and per-record presence.",
            "- `skeleton_concentration.csv`: global skeleton and stage-sequence concentration.",
            "- `feature_statistics.csv`: schema/feature/anti-SQL metadata distributions.",
            "- `paper_tables.tex`: LaTeX tables using booktabs.",
            "- `release_validator_snapshot.txt/.json`: full CLI validator snapshot.",
            "- `release_validator_issue_statistics.json`: structured validator issue summary.",
            "- `release_validator_issue_categories.csv`: validator issues by category.",
            "- `release_validator_issue_by_db_category.csv`: validator issues by DB/category.",
            "- `release_validator_issue_samples.csv`: first 250 validator issue samples.",
            "- `fresh_exact_execution_by_db_verification.json`: fresh exact Mongo execution verification run by DB.",
            "- `pipeline_stage_detailed_statistics.json/.md`: detailed top-level aggregation stage complexity statistics.",
            "- `pipeline_stage_summary.csv`: per-stage occurrence, presence, repetition, and boundary-position counts.",
            "- `pipeline_stage_by_db.csv`: per-DB stage-complexity summary.",
            "- `pipeline_stage_occurrence_by_db.csv`: per-DB/per-stage occurrence and presence.",
            "- `pipeline_stage_count_histogram.csv` and `pipeline_stage_count_by_db.csv`: pipeline-length distributions.",
            "- `pipeline_stage_position_distribution.csv` and `pipeline_stage_normalized_position_distribution.csv`: absolute and normalized stage positions.",
            "- `pipeline_stage_transition_distribution.csv`: adjacent internal stage bigrams.",
            "- `pipeline_stage_sequence_distribution_detailed.csv`: full stage-sequence families.",
            "- `pipeline_stage_structural_buckets.csv`, `pipeline_stage_depth_buckets.csv`, and `pipeline_stage_role_distribution.csv`: derived stage-complexity families.",
            "- `pipeline_stage_tables.tex`: LaTeX tables for stage complexity.",
        ]
    )
    (OUT_DIR / "paper_dataset_statistics.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "records": total_records,
                "db_count": len(by_db_records),
                "schema_collections": schema_overall["schema_collection_count"],
                "queried_collection_pairs": len(collection_counter),
                "documents": schema_overall["document_count"],
                "distinct_mql_signatures": overall["scale"]["distinct_mql_signatures"],
                "distinct_nl_texts": overall["scale"]["distinct_nl_texts"],
                "global_skeleton_signatures": skeleton_global["distinct_global_skeleton_signatures"],
                "db_scoped_skeleton_families": skeleton_global["distinct_db_scoped_skeleton_families"],
                "max_global_skeleton_family": skeleton_global["max_global_skeleton_family"],
                "max_db_scoped_skeleton_family": skeleton_global["max_db_scoped_skeleton_family"],
                "median_stage_count": overall["complexity_distributions"]["stage_count"]["median"],
                "median_unique_operator_count": overall["complexity_distributions"]["unique_operator_count"]["median"],
                "dynamic_key_operator_records": semantic_presence["dynamic_key_operator"],
                "array_operator_records": semantic_presence["array_operator"],
                "native_verification_ok": metadata_distributions["native_verification_ok"],
                "parse_errors": len(parse_errors),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
