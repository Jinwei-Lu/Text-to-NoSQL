#!/usr/bin/env python3
"""Generate public lean-release contract issue statistics."""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
RELEASE_DIR = SCRIPT_DIR.parents[1]
DATA_DIR = RELEASE_DIR / "data"
OUT_DIR = SCRIPT_DIR
PUBLIC_RECORD_FIELDS = ["record_id", "db_id", "NLQ", "NLQ_colloquial", "MQL"]
AGG_RE = re.compile(r"^db\.([A-Za-z_][A-Za-z0-9_]*)\.aggregate\((\[.*\])\)\s*$", re.S)

for parent in SCRIPT_DIR.parents:
    src_dir = parent / "src"
    if (src_dir / "tend").is_dir():
        sys.path.insert(0, str(src_dir))
        break

try:
    from tend.execution import parse_pipeline as parse_execution_pipeline
except Exception:  # pragma: no cover - script fallback for source-less release copies
    parse_execution_pipeline = None


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def categorize(issue: str) -> str:
    if "field shape" in issue:
        return "public_field_shape"
    if "record count" in issue or "db coverage" in issue or "expected 110" in issue:
        return "composition"
    if "duplicate" in issue:
        return "duplicate_identifier"
    if "MQL parse failed" in issue:
        return "mql_parse"
    if "non-empty string" in issue:
        return "empty_public_field"
    if "distinct" in issue:
        return "diversity"
    return "other"


def validate_public_records(records: Any) -> tuple[list[dict[str, Any]], Counter[str], Counter[tuple[str, str]], dict[str, Any]]:
    issue_rows: list[dict[str, Any]] = []
    by_category: Counter[str] = Counter()
    by_db_category: Counter[tuple[str, str]] = Counter()

    def add(issue: str, *, record_id: Any = None, db_id: str = "unknown") -> None:
        category = categorize(issue)
        by_category[category] += 1
        by_db_category[(db_id, category)] += 1
        issue_rows.append({"record_id": record_id, "db_id": db_id, "category": category, "issue": issue})

    if not isinstance(records, list):
        add("TEND_lean.json must be a JSON list")
        return issue_rows, by_category, by_db_category, {"records": 0, "db_ids": []}

    seen_ids: set[Any] = set()
    mqls: Counter[str] = Counter()
    canonical: Counter[str] = Counter()
    pairs: Counter[tuple[str, str, str]] = Counter()
    by_db: Counter[str] = Counter()

    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            add(f"[row {idx}] record must be an object")
            continue
        rid = rec.get("record_id")
        db_id = str(rec.get("db_id") or "unknown")
        keys = list(rec.keys())
        if keys != PUBLIC_RECORD_FIELDS:
            add(f"[row {idx}] field shape {keys!r} != {PUBLIC_RECORD_FIELDS!r}", record_id=rid, db_id=db_id)
        if rid in seen_ids:
            add(f"[row {idx}] duplicate record_id {rid!r}", record_id=rid, db_id=db_id)
        seen_ids.add(rid)
        if not isinstance(rid, int):
            add(f"[row {idx}] record_id must be an integer", record_id=rid, db_id=db_id)
        if not isinstance(rec.get("db_id"), str) or not rec.get("db_id", "").strip():
            add(f"[row {idx}] db_id must be a non-empty string", record_id=rid, db_id=db_id)
        for key in ("NLQ", "NLQ_colloquial", "MQL"):
            if not isinstance(rec.get(key), str) or not rec.get(key, "").strip():
                add(f"[row {idx} r{rid}] {key} must be a non-empty string", record_id=rid, db_id=db_id)
        mql = str(rec.get("MQL", ""))
        if parse_execution_pipeline is not None:
            try:
                _, pipeline = parse_execution_pipeline(mql)
                if not isinstance(pipeline, list) or not all(isinstance(stage, dict) for stage in pipeline):
                    add(f"[row {idx} r{rid}] MQL parse failed: pipeline is not a list of objects", record_id=rid, db_id=db_id)
            except Exception as exc:  # noqa: BLE001 - public contract diagnostics
                add(f"[row {idx} r{rid}] MQL parse failed: {exc}", record_id=rid, db_id=db_id)
        else:
            match = AGG_RE.match(mql.strip())
            if not match:
                add(f"[row {idx} r{rid}] MQL parse failed: not db.<collection>.aggregate([...])", record_id=rid, db_id=db_id)
            else:
                try:
                    pipeline = json.loads(match.group(2))
                    if not isinstance(pipeline, list) or not all(isinstance(stage, dict) for stage in pipeline):
                        add(f"[row {idx} r{rid}] MQL parse failed: pipeline is not a list of objects", record_id=rid, db_id=db_id)
                except json.JSONDecodeError as exc:
                    add(f"[row {idx} r{rid}] MQL parse failed: {exc}", record_id=rid, db_id=db_id)
        if db_id != "unknown":
            by_db[db_id] += 1
        mqls[mql] += 1
        canonical[str(rec.get("NLQ", ""))] += 1
        pairs[(db_id, str(rec.get("NLQ", "")), mql)] += 1

    if len(records) != 1210:
        add(f"record count {len(records)} != 1210")
    if len(by_db) != 11:
        add(f"db coverage {len(by_db)} != 11")
    for db_id, count in sorted(by_db.items()):
        if count != 110:
            add(f"db {db_id} has {count} records, expected 110", db_id=db_id)
    if len(mqls) != len(records):
        add(f"distinct MQL strings {len(mqls)} != records {len(records)}")
    if len(canonical) != len(records):
        add(f"distinct NLQ strings {len(canonical)} != records {len(records)}")
    if len(pairs) != len(records):
        add(f"distinct db_id+NLQ+MQL pairs {len(pairs)} != records {len(records)}")

    composition = {
        "records": len(records),
        "db_ids": sorted(by_db),
        "records_per_db": dict(sorted(by_db.items())),
        "distinct_mql_strings": len(mqls),
        "distinct_canonical_nl": len(canonical),
        "distinct_db_nl_mql_pairs": len(pairs),
    }
    return issue_rows, by_category, by_db_category, composition


def main() -> None:
    records = read_json(DATA_DIR / "TEND_lean.json")
    issue_rows, by_category, by_db_category, composition = validate_public_records(records)
    affected_by_category: dict[str, set[Any]] = defaultdict(set)
    affected_by_db: dict[str, set[Any]] = defaultdict(set)
    for row in issue_rows:
        if row["record_id"] is not None:
            affected_by_category[row["category"]].add(row["record_id"])
            affected_by_db[row["db_id"]].add(row["record_id"])

    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset": str(DATA_DIR / "TEND_lean.json"),
        "validator": "public_lean_release_contract",
        "public_record_fields": PUBLIC_RECORD_FIELDS,
        "ok": not issue_rows,
        "records": composition["records"],
        "record_violations": len(issue_rows),
        "schema_violations": 0,
        "file_violations": 0,
        "composition": composition,
        "issue_categories": [
            {
                "category": category,
                "issues": count,
                "affected_records": len(affected_by_category.get(category, set())),
                "issue_share": round(count / max(1, len(issue_rows)), 6),
                "record_share": round(len(affected_by_category.get(category, set())) / max(1, composition["records"]), 6),
            }
            for category, count in by_category.most_common()
        ],
        "affected_records_by_db": {
            db_id: {"records": len(rows), "share": round(len(rows) / 110, 6)}
            for db_id, rows in sorted(affected_by_db.items())
        },
        "top_issue_samples": issue_rows[:50],
    }

    (OUT_DIR / "release_validator_issue_statistics.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (OUT_DIR / "release_validator_snapshot.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    snapshot_lines = [
        "TEND lean public release validator snapshot",
        f"dataset: {DATA_DIR / 'TEND_lean.json'}",
        f"ok: {summary['ok']}",
        f"records: {summary['records']}",
        f"record_violations: {summary['record_violations']}",
        f"schema_violations: {summary['schema_violations']}",
        f"file_violations: {summary['file_violations']}",
    ]
    (OUT_DIR / "release_validator_snapshot.txt").write_text("\n".join(snapshot_lines) + "\n", encoding="utf-8")

    with (OUT_DIR / "release_validator_issue_categories.csv").open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=["category", "issues", "affected_records", "issue_share", "record_share"])
        writer.writeheader()
        for row in summary["issue_categories"]:
            writer.writerow(row)

    with (OUT_DIR / "release_validator_issue_by_db_category.csv").open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=["db_id", "category", "issues"])
        writer.writeheader()
        for (db_id, category), count in sorted(by_db_category.items()):
            writer.writerow({"db_id": db_id, "category": category, "issues": count})

    with (OUT_DIR / "release_validator_issue_samples.csv").open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=["record_id", "db_id", "category", "issue"])
        writer.writeheader()
        for row in issue_rows[:250]:
            writer.writerow(row)

    print(
        json.dumps(
            {
                "ok": summary["ok"],
                "records": summary["records"],
                "record_violations": summary["record_violations"],
                "categories": summary["issue_categories"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
