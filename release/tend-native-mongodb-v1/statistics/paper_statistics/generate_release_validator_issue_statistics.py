#!/usr/bin/env python3
"""Generate structured issue statistics from tend.publish.validate.validate_release."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tend.publish.validate import validate_release


SCRIPT_DIR = Path(__file__).resolve().parent
RUN_DIR = SCRIPT_DIR.parents[1]
DATASET_DIR = RUN_DIR / "dataset"
OUT_DIR = SCRIPT_DIR


RID_RE = re.compile(r"\br(\d+)\b")


def categorize(issue: str) -> str:
    if "unresolved provenance refs" in issue:
        return "unresolved_provenance_refs"
    if "does not access feature path" in issue:
        return "feature_path_not_accessed"
    if "requires $objectToArray over" in issue:
        return "dynamic_key_requires_objectToArray_path"
    if "claimed native constructs absent" in issue:
        return "claimed_native_constructs_absent"
    if "requires shape-preserving $filter" in issue or "shape-preserving $filter" in issue:
        return "nested_event_filter_requirement"
    if "requires $switch or discriminator branch" in issue:
        return "polymorphic_discriminator_requirement"
    if "gold MQL fails its own AST_check" in issue:
        return "ast_allowlist_check"
    if issue.startswith("[C"):
        return "record_contract_check"
    if issue.startswith("[native"):
        return "other_native_check"
    return "other"


def rid(issue: str) -> int | None:
    match = RID_RE.search(issue)
    return int(match.group(1)) if match else None


def main() -> None:
    records = json.loads((DATASET_DIR / "TEND.json").read_text(encoding="utf-8"))
    rid_to_db = {int(rec["record_id"]): rec["db_id"] for rec in records}
    report = validate_release(DATASET_DIR)

    issue_rows: list[dict[str, Any]] = []
    by_category: Counter[str] = Counter()
    by_db_category: Counter[tuple[str, str]] = Counter()
    affected_records_by_category: dict[str, set[int]] = defaultdict(set)
    affected_records_by_db: dict[str, set[int]] = defaultdict(set)

    for issue in report.record_violations:
        record_id = rid(issue)
        db_id = rid_to_db.get(record_id, "unknown") if record_id is not None else "unknown"
        category = categorize(issue)
        by_category[category] += 1
        by_db_category[(db_id, category)] += 1
        if record_id is not None:
            affected_records_by_category[category].add(record_id)
            affected_records_by_db[db_id].add(record_id)
        issue_rows.append({"record_id": record_id, "db_id": db_id, "category": category, "issue": issue})

    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "command_equivalent": f"validate_release({DATASET_DIR})",
        "ok": report.ok,
        "records": report.n_records,
        "record_violations": len(report.record_violations),
        "schema_violations": len(report.schema_violations),
        "file_violations": len(report.file_violations),
        "composition": report.composition.__dict__,
        "diversity": report.diversity.__dict__,
        "issue_categories": [
            {
                "category": category,
                "issues": count,
                "affected_records": len(affected_records_by_category.get(category, set())),
                "issue_share": round(count / max(1, len(report.record_violations)), 6),
                "record_share": round(len(affected_records_by_category.get(category, set())) / max(1, report.n_records), 6),
            }
            for category, count in by_category.most_common()
        ],
        "affected_records_by_db": {
            db: {"records": len(rows), "share": round(len(rows) / 110, 6) if db != "unknown" else None}
            for db, rows in sorted(affected_records_by_db.items())
        },
        "top_issue_samples": issue_rows[:50],
    }

    (OUT_DIR / "release_validator_issue_statistics.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with (OUT_DIR / "release_validator_issue_categories.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["category", "issues", "affected_records", "issue_share", "record_share"])
        writer.writeheader()
        for row in summary["issue_categories"]:
            writer.writerow(row)

    with (OUT_DIR / "release_validator_issue_by_db_category.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["db_id", "category", "issues"])
        writer.writeheader()
        for (db_id, category), count in sorted(by_db_category.items()):
            writer.writerow({"db_id": db_id, "category": category, "issues": count})

    with (OUT_DIR / "release_validator_issue_samples.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["record_id", "db_id", "category", "issue"])
        writer.writeheader()
        for row in issue_rows[:250]:
            writer.writerow(row)

    print(
        json.dumps(
            {
                "ok": report.ok,
                "record_violations": len(report.record_violations),
                "schema_violations": len(report.schema_violations),
                "file_violations": len(report.file_violations),
                "categories": summary["issue_categories"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
