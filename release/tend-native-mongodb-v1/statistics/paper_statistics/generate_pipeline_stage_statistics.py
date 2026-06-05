#!/usr/bin/env python3
"""Generate detailed aggregation-pipeline stage statistics for TEND."""

from __future__ import annotations

import csv
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
RUN_DIR = SCRIPT_DIR.parents[1]
DATASET_DIR = RUN_DIR / "data"
OUT_DIR = SCRIPT_DIR

for parent in SCRIPT_DIR.parents:
    src_dir = parent / "src"
    if (src_dir / "tend").is_dir():
        sys.path.insert(0, str(src_dir))
        break

try:
    from tend.execution import parse_pipeline as parse_execution_pipeline
except Exception:  # pragma: no cover - script fallback for source-less release copies
    parse_execution_pipeline = None

AGG_RE = re.compile(r"^db\.([A-Za-z_][A-Za-z0-9_]*)\.aggregate\((\[.*\])\)\s*$", re.S)

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


def parse_pipeline(mql: str) -> tuple[str, list[dict[str, Any]]]:
    if parse_execution_pipeline is not None:
        return parse_execution_pipeline(mql)
    match = AGG_RE.match(mql.strip())
    if not match:
        raise ValueError("MQL is not db.<collection>.aggregate([...])")
    collection = match.group(1)
    pipeline = json.loads(match.group(2))
    if not isinstance(pipeline, list) or not all(isinstance(stage, dict) for stage in pipeline):
        raise ValueError("pipeline is not a list of objects")
    return collection, pipeline


def stage_name(stage: dict[str, Any]) -> str:
    if len(stage) == 1:
        return next(iter(stage))
    return "|".join(stage.keys())


def sequence(pipeline: list[dict[str, Any]]) -> list[str]:
    return [stage_name(stage) for stage in pipeline]


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


def top_rows(counter: Counter[Any], n: int = 30) -> list[dict[str, Any]]:
    total = sum(counter.values()) or 1
    return [{"key": key, "count": count, "share": round(count / total, 6)} for key, count in counter.most_common(n)]


def coverage(counter: Counter[Any]) -> dict[str, float]:
    values = [count for _, count in counter.most_common()]
    total = sum(values) or 1
    return {
        "top_1": round(sum(values[:1]) / total, 6),
        "top_3": round(sum(values[:3]) / total, 6),
        "top_5": round(sum(values[:5]) / total, 6),
        "top_10": round(sum(values[:10]) / total, 6),
        "top_20": round(sum(values[:20]) / total, 6),
    }


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


def stage_depth_bucket(n: int) -> str:
    if n <= 5:
        return "short_3_to_5"
    if n <= 7:
        return "medium_6_to_7"
    if n <= 9:
        return "long_8_to_9"
    return "very_long_10_plus"


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


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
    records = read_json(DATASET_DIR / "TEND_lean.json")
    total_records = len(records)

    parsed_rows: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []

    stage_occurrence: Counter[str] = Counter()
    stage_presence: Counter[str] = Counter()
    stage_count_histogram: Counter[int] = Counter()
    stage_sequence_counter: Counter[str] = Counter()
    first_stage_counter: Counter[str] = Counter()
    last_stage_counter: Counter[str] = Counter()
    second_stage_counter: Counter[str] = Counter()
    penultimate_stage_counter: Counter[str] = Counter()
    transition_counter: Counter[tuple[str, str]] = Counter()
    start_transition_counter: Counter[tuple[str, str]] = Counter()
    end_transition_counter: Counter[tuple[str, str]] = Counter()
    position_counter: Counter[tuple[str, int]] = Counter()
    normalized_position_counter: Counter[tuple[str, str]] = Counter()
    stage_role_occurrence: Counter[str] = Counter()
    stage_role_presence: Counter[str] = Counter()
    structural_bucket_counter: Counter[str] = Counter()
    depth_bucket_counter: Counter[str] = Counter()
    db_stage_occurrence: Counter[tuple[str, str]] = Counter()
    db_stage_presence: Counter[tuple[str, str]] = Counter()
    db_stage_count_histogram: Counter[tuple[str, int]] = Counter()
    db_sequence_counter: Counter[tuple[str, str]] = Counter()
    db_bucket_counter: Counter[tuple[str, str]] = Counter()
    collection_bucket_counter: Counter[tuple[str, str, str]] = Counter()
    stage_repetition_records: Counter[str] = Counter()
    max_stage_repetitions: Counter[str] = Counter()
    repeated_stage_combination_counter: Counter[str] = Counter()

    by_db_stage_counts: dict[str, list[int]] = defaultdict(list)
    by_db_unique_stage_counts: dict[str, list[int]] = defaultdict(list)
    by_db_records: Counter[str] = Counter()

    for index, rec in enumerate(records):
        db_id = rec["db_id"]
        by_db_records[db_id] += 1
        try:
            collection, pipeline = parse_pipeline(rec["MQL"])
        except Exception as exc:  # noqa: BLE001
            parse_errors.append({"index": index, "record_id": rec.get("record_id"), "db_id": db_id, "error": str(exc)})
            continue

        seq = sequence(pipeline)
        seq_key = ">".join(seq)
        seq_counter = Counter(seq)
        roles = [STAGE_ROLES.get(stage, "other") for stage in seq]
        role_set = set(roles)
        repeated_stages = sorted(stage for stage, count in seq_counter.items() if count > 1)
        repeated_key = "+".join(repeated_stages) if repeated_stages else "none"
        bucket = structural_bucket(seq)
        depth_bucket = stage_depth_bucket(len(seq))

        parsed_rows.append(
            {
                "record_id": rec.get("record_id"),
                "db_id": db_id,
                "collection": collection,
                "stage_count": len(seq),
                "unique_stage_count": len(seq_counter),
                "stage_sequence": seq_key,
                "first_stage": seq[0],
                "last_stage": seq[-1],
                "structural_bucket": bucket,
                "stage_depth_bucket": depth_bucket,
                "has_unwind": "$unwind" in seq_counter,
                "has_group": "$group" in seq_counter,
                "has_unwind_and_group": "$unwind" in seq_counter and "$group" in seq_counter,
                "project_count": seq_counter.get("$project", 0),
                "match_count": seq_counter.get("$match", 0),
                "unwind_count": seq_counter.get("$unwind", 0),
                "addfields_count": seq_counter.get("$addFields", 0),
                "group_count": seq_counter.get("$group", 0),
                "sort_count": seq_counter.get("$sort", 0),
                "limit_count": seq_counter.get("$limit", 0),
                "repeated_stages": repeated_key,
            }
        )

        stage_occurrence.update(seq)
        stage_presence.update(seq_counter.keys())
        stage_count_histogram[len(seq)] += 1
        stage_sequence_counter[seq_key] += 1
        first_stage_counter[seq[0]] += 1
        last_stage_counter[seq[-1]] += 1
        if len(seq) > 1:
            second_stage_counter[seq[1]] += 1
            penultimate_stage_counter[seq[-2]] += 1
        structural_bucket_counter[bucket] += 1
        depth_bucket_counter[depth_bucket] += 1
        db_bucket_counter[(db_id, bucket)] += 1
        collection_bucket_counter[(db_id, collection, bucket)] += 1
        repeated_stage_combination_counter[repeated_key] += 1
        by_db_stage_counts[db_id].append(len(seq))
        by_db_unique_stage_counts[db_id].append(len(seq_counter))
        db_stage_count_histogram[(db_id, len(seq))] += 1
        db_sequence_counter[(db_id, seq_key)] += 1

        for stage, count in seq_counter.items():
            db_stage_presence[(db_id, stage)] += 1
            db_stage_occurrence[(db_id, stage)] += count
            if count > 1:
                stage_repetition_records[stage] += 1
            max_stage_repetitions[stage] = max(max_stage_repetitions[stage], count)
        for role in roles:
            stage_role_occurrence[role] += 1
        for role in role_set:
            stage_role_presence[role] += 1

        for pos, stage in enumerate(seq, start=1):
            position_counter[(stage, pos)] += 1
            if pos == 1:
                normalized = "first"
            elif pos == len(seq):
                normalized = "last"
            elif pos <= math.ceil(len(seq) / 3):
                normalized = "early"
            elif pos > math.floor(2 * len(seq) / 3):
                normalized = "late"
            else:
                normalized = "middle"
            normalized_position_counter[(stage, normalized)] += 1

        transitions = list(zip(seq, seq[1:]))
        transition_counter.update(transitions)
        start_transition_counter[("<START>", seq[0])] += 1
        end_transition_counter[(seq[-1], "<END>")] += 1

    total_stage_occurrences = sum(stage_occurrence.values())
    total_transitions = sum(transition_counter.values())

    stage_summary_rows: list[dict[str, Any]] = []
    for stage in sorted(stage_occurrence):
        occurrence = stage_occurrence[stage]
        presence = stage_presence[stage]
        stage_summary_rows.append(
            {
                "stage": stage,
                "role": STAGE_ROLES.get(stage, "other"),
                "occurrence_count": occurrence,
                "occurrence_share": round(occurrence / total_stage_occurrences, 6),
                "record_presence_count": presence,
                "record_presence_share": round(presence / total_records, 6),
                "mean_occurrences_per_record": round(occurrence / total_records, 6),
                "mean_occurrences_when_present": round(occurrence / presence, 6) if presence else 0,
                "records_with_repeated_stage": stage_repetition_records.get(stage, 0),
                "max_repetitions_in_one_pipeline": max_stage_repetitions.get(stage, 0),
                "first_stage_count": first_stage_counter.get(stage, 0),
                "last_stage_count": last_stage_counter.get(stage, 0),
                "second_stage_count": second_stage_counter.get(stage, 0),
                "penultimate_stage_count": penultimate_stage_counter.get(stage, 0),
            }
        )

    stage_count_rows = [
        {
            "stage_count": n,
            "records": count,
            "share": round(count / total_records, 6),
            "cumulative_records": sum(c for k, c in stage_count_histogram.items() if k <= n),
            "cumulative_share": round(sum(c for k, c in stage_count_histogram.items() if k <= n) / total_records, 6),
        }
        for n, count in sorted(stage_count_histogram.items())
    ]

    by_db_rows: list[dict[str, Any]] = []
    for db_id in sorted(by_db_records):
        values = by_db_stage_counts[db_id]
        unique_values = by_db_unique_stage_counts[db_id]
        db_sequences = Counter({seq: count for (db, seq), count in db_sequence_counter.items() if db == db_id})
        db_buckets = Counter({bucket: count for (db, bucket), count in db_bucket_counter.items() if db == db_id})
        by_db_rows.append(
            {
                "db_id": db_id,
                "records": by_db_records[db_id],
                "stage_count_min": min(values),
                "stage_count_median": quantiles(values)["median"],
                "stage_count_p90": quantiles(values)["p90"],
                "stage_count_max": max(values),
                "stage_count_mean": round(statistics.fmean(values), 6),
                "unique_stage_count_median": quantiles(unique_values)["median"],
                "distinct_stage_sequences": len(db_sequences),
                "max_stage_sequence_family": max(db_sequences.values()) if db_sequences else 0,
                "top_stage_sequence_share": round(max(db_sequences.values()) / by_db_records[db_id], 6) if db_sequences else 0,
                "dominant_structural_bucket": db_buckets.most_common(1)[0][0] if db_buckets else "",
                "dominant_structural_bucket_count": db_buckets.most_common(1)[0][1] if db_buckets else 0,
                "unwind_records": db_stage_presence.get((db_id, "$unwind"), 0),
                "group_records": db_stage_presence.get((db_id, "$group"), 0),
                "unwind_group_records": sum(1 for row in parsed_rows if row["db_id"] == db_id and row["has_unwind_and_group"]),
                "project_occurrences": db_stage_occurrence.get((db_id, "$project"), 0),
                "match_occurrences": db_stage_occurrence.get((db_id, "$match"), 0),
                "unwind_occurrences": db_stage_occurrence.get((db_id, "$unwind"), 0),
                "group_occurrences": db_stage_occurrence.get((db_id, "$group"), 0),
                "addfields_occurrences": db_stage_occurrence.get((db_id, "$addFields"), 0),
            }
        )

    db_stage_rows: list[dict[str, Any]] = []
    for db_id in sorted(by_db_records):
        for stage in sorted(stage_occurrence):
            occurrence = db_stage_occurrence.get((db_id, stage), 0)
            presence = db_stage_presence.get((db_id, stage), 0)
            db_stage_rows.append(
                {
                    "db_id": db_id,
                    "stage": stage,
                    "role": STAGE_ROLES.get(stage, "other"),
                    "occurrence_count": occurrence,
                    "record_presence_count": presence,
                    "record_presence_share": round(presence / by_db_records[db_id], 6),
                    "mean_occurrences_per_record": round(occurrence / by_db_records[db_id], 6),
                }
            )

    position_rows = [
        {
            "stage": stage,
            "position_1_based": pos,
            "count": count,
            "share_of_stage_occurrences": round(count / stage_occurrence[stage], 6),
            "share_of_all_records": round(count / total_records, 6),
        }
        for (stage, pos), count in sorted(position_counter.items())
    ]

    normalized_position_rows = [
        {
            "stage": stage,
            "position_bucket": bucket,
            "count": count,
            "share_of_stage_occurrences": round(count / stage_occurrence[stage], 6),
        }
        for (stage, bucket), count in sorted(normalized_position_counter.items())
    ]

    first_last_rows = []
    for stage in sorted(stage_occurrence):
        first_last_rows.append(
            {
                "stage": stage,
                "first_count": first_stage_counter.get(stage, 0),
                "first_share": round(first_stage_counter.get(stage, 0) / total_records, 6),
                "second_count": second_stage_counter.get(stage, 0),
                "second_share": round(second_stage_counter.get(stage, 0) / total_records, 6),
                "penultimate_count": penultimate_stage_counter.get(stage, 0),
                "penultimate_share": round(penultimate_stage_counter.get(stage, 0) / total_records, 6),
                "last_count": last_stage_counter.get(stage, 0),
                "last_share": round(last_stage_counter.get(stage, 0) / total_records, 6),
            }
        )

    transition_rows = [
        {
            "from_stage": a,
            "to_stage": b,
            "count": count,
            "share_of_all_internal_transitions": round(count / total_transitions, 6) if total_transitions else 0,
        }
        for (a, b), count in transition_counter.most_common()
    ]

    boundary_transition_rows = []
    for (a, b), count in start_transition_counter.most_common():
        boundary_transition_rows.append({"from_stage": a, "to_stage": b, "count": count, "boundary": "start"})
    for (a, b), count in end_transition_counter.most_common():
        boundary_transition_rows.append({"from_stage": a, "to_stage": b, "count": count, "boundary": "end"})

    sequence_rows = []
    for seq_key, count in stage_sequence_counter.most_common():
        stages = seq_key.split(">")
        sequence_rows.append(
            {
                "stage_sequence": seq_key,
                "count": count,
                "share": round(count / total_records, 6),
                "stage_count": len(stages),
                "unique_stage_count": len(set(stages)),
                "structural_bucket": structural_bucket(stages),
                "has_unwind": "$unwind" in stages,
                "has_group": "$group" in stages,
                "has_unwind_and_group": "$unwind" in stages and "$group" in stages,
            }
        )

    structural_bucket_rows = [
        {"structural_bucket": bucket, "records": count, "share": round(count / total_records, 6)}
        for bucket, count in structural_bucket_counter.most_common()
    ]
    depth_bucket_rows = [
        {"stage_depth_bucket": bucket, "records": count, "share": round(count / total_records, 6)}
        for bucket, count in depth_bucket_counter.most_common()
    ]
    repeated_stage_rows = [
        {"repeated_stage_combination": key, "records": count, "share": round(count / total_records, 6)}
        for key, count in repeated_stage_combination_counter.most_common()
    ]

    role_rows = []
    for role in sorted(stage_role_occurrence):
        role_rows.append(
            {
                "role": role,
                "stage_occurrences": stage_role_occurrence[role],
                "occurrence_share": round(stage_role_occurrence[role] / total_stage_occurrences, 6),
                "record_presence_count": stage_role_presence[role],
                "record_presence_share": round(stage_role_presence[role] / total_records, 6),
            }
        )

    stage_count_by_db_rows = [
        {
            "db_id": db_id,
            "stage_count": n,
            "records": count,
            "share_within_db": round(count / by_db_records[db_id], 6),
        }
        for (db_id, n), count in sorted(db_stage_count_histogram.items())
    ]

    collection_bucket_rows = [
        {
            "db_id": db_id,
            "collection": collection,
            "structural_bucket": bucket,
            "records": count,
        }
        for (db_id, collection, bucket), count in sorted(collection_bucket_counter.items())
    ]

    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset": str(DATASET_DIR / "TEND_lean.json"),
        "dataset_source": "public lean release records with fields: record_id, db_id, NLQ, NLQ_colloquial, MQL",
        "records": total_records,
        "parse_errors": parse_errors,
        "metric_definitions": {
            "stage_occurrence": "Top-level aggregation stage occurrences; a record can contribute multiple occurrences of the same stage.",
            "stage_presence": "Per-record stage presence; each stage counted at most once per record.",
            "position_1_based": "Absolute 1-based index in the aggregation pipeline.",
            "position_bucket": "first, early, middle, late, or last position normalized by pipeline length.",
            "transition": "Adjacent top-level stage pair inside a pipeline.",
            "structural_bucket": "Coarse stage-structure family derived from presence of $unwind, $group, repeated unwind, $addFields, and repeated projection.",
        },
        "overall": {
            "stage_count": quantiles([row["stage_count"] for row in parsed_rows]),
            "unique_stage_count": quantiles([row["unique_stage_count"] for row in parsed_rows]),
            "total_stage_occurrences": total_stage_occurrences,
            "distinct_stage_types": len(stage_occurrence),
            "stage_count_histogram": dict(sorted(stage_count_histogram.items())),
            "distinct_stage_sequences": len(stage_sequence_counter),
            "stage_sequence_entropy": entropy(stage_sequence_counter),
            "stage_sequence_top_coverage": coverage(stage_sequence_counter),
            "max_stage_sequence_family": max(stage_sequence_counter.values()) if stage_sequence_counter else 0,
            "internal_transition_count": total_transitions,
            "distinct_internal_transitions": len(transition_counter),
            "structural_buckets": dict(structural_bucket_counter),
            "depth_buckets": dict(depth_bucket_counter),
            "repeated_stage_combinations": dict(repeated_stage_combination_counter),
        },
        "stage_summary": stage_summary_rows,
        "top_stage_sequences": sequence_rows[:30],
        "top_transitions": transition_rows[:30],
        "first_stage_distribution": top_rows(first_stage_counter, 20),
        "last_stage_distribution": top_rows(last_stage_counter, 20),
        "structural_bucket_distribution": structural_bucket_rows,
        "depth_bucket_distribution": depth_bucket_rows,
        "role_distribution": role_rows,
        "by_db": by_db_rows,
    }

    write_json(OUT_DIR / "pipeline_stage_detailed_statistics.json", summary)
    write_csv(
        OUT_DIR / "pipeline_stage_summary.csv",
        stage_summary_rows,
        [
            "stage",
            "role",
            "occurrence_count",
            "occurrence_share",
            "record_presence_count",
            "record_presence_share",
            "mean_occurrences_per_record",
            "mean_occurrences_when_present",
            "records_with_repeated_stage",
            "max_repetitions_in_one_pipeline",
            "first_stage_count",
            "last_stage_count",
            "second_stage_count",
            "penultimate_stage_count",
        ],
    )
    write_csv(OUT_DIR / "pipeline_stage_count_histogram.csv", stage_count_rows, ["stage_count", "records", "share", "cumulative_records", "cumulative_share"])
    write_csv(OUT_DIR / "pipeline_stage_count_by_db.csv", stage_count_by_db_rows, ["db_id", "stage_count", "records", "share_within_db"])
    write_csv(
        OUT_DIR / "pipeline_stage_by_db.csv",
        by_db_rows,
        [
            "db_id",
            "records",
            "stage_count_min",
            "stage_count_median",
            "stage_count_p90",
            "stage_count_max",
            "stage_count_mean",
            "unique_stage_count_median",
            "distinct_stage_sequences",
            "max_stage_sequence_family",
            "top_stage_sequence_share",
            "dominant_structural_bucket",
            "dominant_structural_bucket_count",
            "unwind_records",
            "group_records",
            "unwind_group_records",
            "project_occurrences",
            "match_occurrences",
            "unwind_occurrences",
            "group_occurrences",
            "addfields_occurrences",
        ],
    )
    write_csv(OUT_DIR / "pipeline_stage_occurrence_by_db.csv", db_stage_rows, ["db_id", "stage", "role", "occurrence_count", "record_presence_count", "record_presence_share", "mean_occurrences_per_record"])
    write_csv(OUT_DIR / "pipeline_stage_position_distribution.csv", position_rows, ["stage", "position_1_based", "count", "share_of_stage_occurrences", "share_of_all_records"])
    write_csv(OUT_DIR / "pipeline_stage_normalized_position_distribution.csv", normalized_position_rows, ["stage", "position_bucket", "count", "share_of_stage_occurrences"])
    write_csv(OUT_DIR / "pipeline_stage_first_last_distribution.csv", first_last_rows, ["stage", "first_count", "first_share", "second_count", "second_share", "penultimate_count", "penultimate_share", "last_count", "last_share"])
    write_csv(OUT_DIR / "pipeline_stage_transition_distribution.csv", transition_rows, ["from_stage", "to_stage", "count", "share_of_all_internal_transitions"])
    write_csv(OUT_DIR / "pipeline_stage_boundary_transition_distribution.csv", boundary_transition_rows, ["from_stage", "to_stage", "count", "boundary"])
    write_csv(OUT_DIR / "pipeline_stage_sequence_distribution_detailed.csv", sequence_rows, ["stage_sequence", "count", "share", "stage_count", "unique_stage_count", "structural_bucket", "has_unwind", "has_group", "has_unwind_and_group"])
    write_csv(OUT_DIR / "pipeline_stage_structural_buckets.csv", structural_bucket_rows, ["structural_bucket", "records", "share"])
    write_csv(OUT_DIR / "pipeline_stage_depth_buckets.csv", depth_bucket_rows, ["stage_depth_bucket", "records", "share"])
    write_csv(OUT_DIR / "pipeline_stage_repeated_stage_combinations.csv", repeated_stage_rows, ["repeated_stage_combination", "records", "share"])
    write_csv(OUT_DIR / "pipeline_stage_role_distribution.csv", role_rows, ["role", "stage_occurrences", "occurrence_share", "record_presence_count", "record_presence_share"])
    write_csv(OUT_DIR / "pipeline_stage_complexity_by_record.csv", parsed_rows, list(parsed_rows[0].keys()) if parsed_rows else [])
    write_csv(OUT_DIR / "pipeline_stage_collection_bucket_distribution.csv", collection_bucket_rows, ["db_id", "collection", "structural_bucket", "records"])

    latex = "\n".join(
        [
            latex_table(
                ["Stages", "Records", "Share", "Cum. share"],
                [[row["stage_count"], row["records"], f"{row['share']:.1%}", f"{row['cumulative_share']:.1%}"] for row in stage_count_rows],
                "Distribution of MongoDB aggregation pipeline lengths in TEND.",
                "tab:tend-stage-counts",
            ),
            latex_table(
                ["Stage", "Role", "Occ.", "Presence", "Repeated recs."],
                [
                    [
                        row["stage"],
                        row["role"],
                        row["occurrence_count"],
                        f"{row['record_presence_count']} ({row['record_presence_share']:.1%})",
                        row["records_with_repeated_stage"],
                    ]
                    for row in sorted(stage_summary_rows, key=lambda r: r["occurrence_count"], reverse=True)
                ],
                "Top-level aggregation stage occurrence and per-record presence.",
                "tab:tend-stage-types",
            ),
            latex_table(
                ["Structural bucket", "Records", "Share"],
                [[row["structural_bucket"], row["records"], f"{row['share']:.1%}"] for row in structural_bucket_rows],
                "Coarse aggregation-pipeline structural families.",
                "tab:tend-stage-structural-buckets",
            ),
        ]
    )
    (OUT_DIR / "pipeline_stage_tables.tex").write_text(latex, encoding="utf-8")

    md = [
        "# Detailed Pipeline Stage Statistics",
        "",
        f"- Generated at: `{summary['created_at']}`",
        f"- Records parsed as aggregation pipelines: `{len(parsed_rows)}/{total_records}`",
        f"- Total top-level stage occurrences: `{total_stage_occurrences}`",
        f"- Distinct stage types: `{len(stage_occurrence)}`",
        f"- Distinct full stage sequences: `{len(stage_sequence_counter)}`",
        f"- Max stage-sequence family: `{summary['overall']['max_stage_sequence_family']}`",
        "",
        "## Stage Count Distribution",
        "",
        "| Stage count | Records | Share | Cumulative share |",
        "|---:|---:|---:|---:|",
    ]
    for row in stage_count_rows:
        md.append(f"| {row['stage_count']} | {row['records']} | {row['share']:.1%} | {row['cumulative_share']:.1%} |")
    md.extend(
        [
            "",
            "## Stage Occurrence And Presence",
            "",
            "| Stage | Role | Occurrences | Occurrence share | Record presence | Presence share | Repeated in records | Max repetitions |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in sorted(stage_summary_rows, key=lambda r: r["occurrence_count"], reverse=True):
        md.append(
            f"| `{row['stage']}` | `{row['role']}` | {row['occurrence_count']} | {row['occurrence_share']:.1%} | {row['record_presence_count']} | {row['record_presence_share']:.1%} | {row['records_with_repeated_stage']} | {row['max_repetitions_in_one_pipeline']} |"
        )
    md.extend(
        [
            "",
            "## Structural Buckets",
            "",
            "| Bucket | Records | Share |",
            "|---|---:|---:|",
        ]
    )
    for row in structural_bucket_rows:
        md.append(f"| `{row['structural_bucket']}` | {row['records']} | {row['share']:.1%} |")
    md.extend(
        [
            "",
            "## Depth Buckets",
            "",
            "| Bucket | Records | Share |",
            "|---|---:|---:|",
        ]
    )
    for row in depth_bucket_rows:
        md.append(f"| `{row['stage_depth_bucket']}` | {row['records']} | {row['share']:.1%} |")
    md.extend(
        [
            "",
            "## Top Stage Sequences",
            "",
            "| Stage sequence | Records | Share | Stage count | Bucket |",
            "|---|---:|---:|---:|---|",
        ]
    )
    for row in sequence_rows[:20]:
        md.append(f"| `{row['stage_sequence']}` | {row['count']} | {row['share']:.1%} | {row['stage_count']} | `{row['structural_bucket']}` |")
    md.extend(
        [
            "",
            "## Top Internal Stage Transitions",
            "",
            "| From | To | Count | Share of internal transitions |",
            "|---|---|---:|---:|",
        ]
    )
    for row in transition_rows[:20]:
        md.append(f"| `{row['from_stage']}` | `{row['to_stage']}` | {row['count']} | {row['share_of_all_internal_transitions']:.1%} |")
    md.extend(
        [
            "",
            "## Per-DB Stage Complexity",
            "",
            "| DB | Records | Median stages | P90 stages | Max stages | Distinct seq. | Max seq. family | Top seq. share | Unwind records | Group records | Unwind+group records |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in by_db_rows:
        md.append(
            f"| `{row['db_id']}` | {row['records']} | {row['stage_count_median']} | {row['stage_count_p90']} | {row['stage_count_max']} | {row['distinct_stage_sequences']} | {row['max_stage_sequence_family']} | {row['top_stage_sequence_share']:.1%} | {row['unwind_records']} | {row['group_records']} | {row['unwind_group_records']} |"
        )
    md.extend(
        [
            "",
            "## Generated CSV Files",
            "",
            "- `pipeline_stage_summary.csv`: per-stage occurrence, presence, role, repetition, and boundary-position counts.",
            "- `pipeline_stage_count_histogram.csv`: global stage-count histogram.",
            "- `pipeline_stage_count_by_db.csv`: stage-count histogram within each DB.",
            "- `pipeline_stage_by_db.csv`: per-DB stage-complexity summary.",
            "- `pipeline_stage_occurrence_by_db.csv`: per-DB/per-stage occurrence and presence.",
            "- `pipeline_stage_position_distribution.csv`: absolute 1-based stage-position distribution.",
            "- `pipeline_stage_normalized_position_distribution.csv`: first/early/middle/late/last position distribution.",
            "- `pipeline_stage_first_last_distribution.csv`: first, second, penultimate, and last-stage distributions.",
            "- `pipeline_stage_transition_distribution.csv`: adjacent internal stage bigrams.",
            "- `pipeline_stage_boundary_transition_distribution.csv`: start and end boundary stage distributions.",
            "- `pipeline_stage_sequence_distribution_detailed.csv`: full sequence families with structural buckets.",
            "- `pipeline_stage_structural_buckets.csv`: coarse structural stage families.",
            "- `pipeline_stage_depth_buckets.csv`: short/medium/long/very-long stage-count buckets.",
            "- `pipeline_stage_repeated_stage_combinations.csv`: repeated stage combinations inside a pipeline.",
            "- `pipeline_stage_role_distribution.csv`: stage role occurrence and record presence.",
            "- `pipeline_stage_complexity_by_record.csv`: record-level stage complexity labels and counts.",
            "- `pipeline_stage_collection_bucket_distribution.csv`: collection-level structural bucket counts.",
            "- `pipeline_stage_tables.tex`: LaTeX tables for stage-count, stage-type, and structural-bucket statistics.",
        ]
    )
    (OUT_DIR / "pipeline_stage_detailed_statistics.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "records": total_records,
                "parsed": len(parsed_rows),
                "parse_errors": len(parse_errors),
                "total_stage_occurrences": total_stage_occurrences,
                "stage_count": summary["overall"]["stage_count"],
                "stage_count_histogram": summary["overall"]["stage_count_histogram"],
                "distinct_stage_sequences": len(stage_sequence_counter),
                "max_stage_sequence_family": summary["overall"]["max_stage_sequence_family"],
                "top_stage_sequences_coverage": summary["overall"]["stage_sequence_top_coverage"],
                "structural_buckets": summary["overall"]["structural_buckets"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
