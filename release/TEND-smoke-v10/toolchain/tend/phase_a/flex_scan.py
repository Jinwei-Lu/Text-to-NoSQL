"""Scan Spider databases for H1–H4 schema-flex eligibility (SC pre-audit rules)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tend.config import SPIDER_DATA_ROOT, force_document_flex
from tend.phase_a.catalog import (
    _discover_db_ids,
    is_catalog_qualifying,
    normalize_db_id,
)
from tend.phase_a.sra import eval_triggers
from tend.phase_a.wp import workload_for_triggers


def _qualifying(db_id: str, *, min_tables: int, min_queries: int) -> tuple[bool, str | None]:
    if not is_catalog_qualifying(db_id, min_tables=min_tables, min_queries=min_queries):
        from tend.phase_a.wp import sqlite_path

        sqlite = sqlite_path(db_id)
        if not sqlite.exists():
            return False, "sqlite_missing"
        from tend.phase_a.catalog import _load_spider_queries, _non_empty_tables, _table_count

        if _table_count(db_id) < min_tables:
            return False, f"table_count<{min_tables}"
        if len(_load_spider_queries(db_id)) < min_queries:
            return False, f"query_count<{min_queries}"
        if not _non_empty_tables(db_id):
            return False, "empty_table"
        return False, "not_qualifying"
    return True, None


def scan_db_flex(
    db_id: str,
    *,
    force_document_flex_override: bool | None = None,
    qualifying: bool | None = None,
    min_tables: int = 2,
    min_queries: int = 10,
) -> dict[str, Any]:
    """Return flex eligibility + trigger detail for one Spider db_id."""
    force_active = force_document_flex() if force_document_flex_override is None else force_document_flex_override
    if qualifying is None:
        qualifying = is_catalog_qualifying(db_id, min_tables=min_tables, min_queries=min_queries)

    row: dict[str, Any] = {
        "db_id": db_id,
        "flex_eligible": False,
        "schema_flex": "none",
        "error": None,
        "force_policy_active": force_active,
        "natural_flex": False,
        "forced_h0": False,
    }
    try:
        wp = workload_for_triggers(db_id)
        report = eval_triggers(
            wp,
            db_id=db_id,
            force_document_flex=force_active,
            qualifying=qualifying,
        )
        natural_selected = report.get("natural_selected")
        row.update(
            {
                "flex_eligible": bool(report["flex_eligible"]),
                "schema_flex": report.get("schema_flex", "none"),
                "selected_trigger": report.get("selected"),
                "natural_flex": natural_selected is not None,
                "forced_h0": bool(report.get("forced_h0")),
                "triggers": report.get("triggers", []),
            }
        )
    except (FileNotFoundError, ValueError, OSError) as exc:
        row["error"] = str(exc)
    return row


def scan_spider_flex_report(
    *,
    spider_root: Path | None = None,
    min_tables: int = 2,
    min_queries: int = 10,
    qualifying_only: bool = False,
    force_document_flex_override: bool | None = None,
) -> dict[str, Any]:
    """Scan all Spider db_ids and summarize flex eligibility."""
    spider_root = spider_root or SPIDER_DATA_ROOT
    force_active = force_document_flex() if force_document_flex_override is None else force_document_flex_override
    db_ids = sorted({normalize_db_id(d) for d in _discover_db_ids(spider_root)})

    rows: list[dict[str, Any]] = []
    for db_id in db_ids:
        qualifies, reject_reason = _qualifying(db_id, min_tables=min_tables, min_queries=min_queries)
        if qualifying_only and not qualifies:
            continue
        detail = scan_db_flex(
            db_id,
            force_document_flex_override=force_active,
            qualifying=qualifies,
            min_tables=min_tables,
            min_queries=min_queries,
        )
        detail["qualifying"] = qualifies
        detail["reject_reason"] = reject_reason
        rows.append(detail)

    flex_yes = [r for r in rows if r.get("flex_eligible")]
    flex_no = [r for r in rows if not r.get("flex_eligible") and not r.get("error")]
    errors = [r for r in rows if r.get("error")]
    natural_flex = [r for r in rows if r.get("natural_flex")]
    forced_h0 = [r for r in rows if r.get("forced_h0")]
    qualifying_rows = [r for r in rows if r.get("qualifying")]
    qual_flex_yes = [r for r in qualifying_rows if r.get("flex_eligible")]
    qual_flex_no = [r for r in qualifying_rows if not r.get("flex_eligible") and not r.get("error")]

    by_flex_type: dict[str, int] = {}
    for r in flex_yes:
        key = str(r.get("schema_flex", "none"))
        by_flex_type[key] = by_flex_type.get(key, 0) + 1

    return {
        "spider_root": str(spider_root),
        "force_document_flex": force_active,
        "total_discovered": len(db_ids),
        "scanned": len(rows),
        "qualifying_only": qualifying_only,
        "selection_policy": {"min_tables": min_tables, "min_queries": min_queries},
        "natural_flex_count": len(natural_flex),
        "forced_h0_count": len(forced_h0),
        "all_scanned": {
            "flex_eligible": len(flex_yes),
            "not_flex_eligible": len(flex_no),
            "scan_errors": len(errors),
            "flex_eligible_ratio": round(len(flex_yes) / max(len(rows), 1), 4),
        },
        "qualifying_subset": {
            "count": len(qualifying_rows),
            "flex_eligible": len(qual_flex_yes),
            "not_flex_eligible": len(qual_flex_no),
            "flex_eligible_ratio": round(len(qual_flex_yes) / max(len(qualifying_rows), 1), 4),
            "meets_min_flex_db_ratio_0_30": len(qual_flex_yes) / max(len(qualifying_rows), 1) >= 0.30,
        },
        "by_schema_flex_type": by_flex_type,
        "not_flex_eligible_dbs": sorted(r["db_id"] for r in flex_no),
        "flex_eligible_dbs": [
            {
                "db_id": r["db_id"],
                "schema_flex": r.get("schema_flex"),
                "selected_trigger": r.get("selected_trigger"),
                "natural_flex": r.get("natural_flex"),
                "forced_h0": r.get("forced_h0"),
                "evidence": next(
                    (t["evidence"] for t in r.get("triggers", []) if t.get("fired")),
                    None,
                ),
            }
            for r in sorted(flex_yes, key=lambda x: x["db_id"])
        ],
        "details": rows,
    }
