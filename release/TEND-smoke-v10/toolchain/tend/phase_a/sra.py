"""Schema Re-architect (SRA) — Stage A patterns + Stage B H1–H4 triggers."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from typing import Any

from tend.core import logging as log_module
from tend.phase_a.wp import SPIDER_DB_ALIASES, _load_spider_queries, sqlite_path
from tend.schemas.validators import validate

TYPE_DISCRIMINATOR_COLS = {"type", "category", "assessment_type", "kind", "variant"}
H0_EXCLUDE_DB_IDS = frozenset({"orchestra"})
H0_EVIDENCE = "build_policy: force_document_flex (no natural H1-H4)"
PATTERN_MENU = [
    "embed",
    "extended_reference",
    "polymorphic",
    "attribute",
    "bucket",
    "computed",
    "subset",
    "tree",
    "outlier",
    "schema_versioning",
    "mixed",
]

ORCHESTRA_SCHEMA: dict[str, Any] = {
    "conductor": {
        "_id": "INT",
        "Name": "TEXT",
        "Age": "INT",
        "Nationality": "TEXT",
        "Year_of_Work": "INT",
        "orchestra": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "fields": {
                    "Orchestra_ID": "INT",
                    "Orchestra": "TEXT",
                    "Record_Company": "TEXT",
                    "Year_of_Founded": "REAL",
                    "Major_Record_Format": "TEXT",
                    "performance": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "fields": {
                                "Performance_ID": "INT",
                                "Type": "TEXT",
                                "Date": "TEXT",
                                "Official_ratings_(millions)": "REAL",
                                "Weekly_rank": "TEXT",
                                "Share": "TEXT",
                                "Attendance": "REAL",
                            },
                        },
                    },
                },
            },
        },
    }
}

ORCHESTRA_RATIONALE: dict[str, Any] = {
    "db_id": "orchestra",
    "source_spider_tables": ["conductor", "orchestra", "performance", "show"],
    "patterns_applied": ["embed", "mixed"],
    "rationale_summary": (
        "Single conductor-rooted collection embeds orchestra and performance arrays; "
        "show.Attendance denormalized onto performance for WP hot-path coverage."
    ),
    "decisions": [
        {
            "id": "D01",
            "type": "embed",
            "parent": "conductor",
            "child": "orchestra",
            "rationale": "WP AP01 co_access 0.89; orchestra never queried without conductor.",
            "reference": "access_patterns.AP01",
        },
        {
            "id": "D02",
            "type": "embed",
            "parent": "orchestra",
            "child": "performance",
            "rationale": "WP AP01 nested_traversal 0.62 requires performance under orchestra path.",
            "reference": "access_patterns.AP01",
        },
        {
            "id": "D03",
            "type": "extended_reference",
            "parent": "performance",
            "child": "show",
            "rationale": "WP hot_field show.Attendance; denormalize Attendance to avoid $lookup.",
            "reference": "hot_fields.show.Attendance",
        },
    ],
    "heterogenization": {
        "schema_flex": "none",
        "triggers": [
            {"id": "H1", "fired": False, "evidence": "no type-conditional branch >= 30%"},
            {"id": "H2", "fired": False, "evidence": "sparse null cols < 3"},
            {"id": "H3", "fired": False, "evidence": "no rename/add column pair"},
            {"id": "H4", "fired": False, "evidence": "no EAV table shape"},
        ],
    },
}


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _column_null_rates(conn: sqlite3.Connection, table: str) -> dict[str, float]:
    cols = _table_columns(conn, table)
    if not cols:
        return {}
    total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if total == 0:
        return {col: 0.0 for col in cols}
    rates: dict[str, float] = {}
    for col in cols:
        nulls = conn.execute(
            f'SELECT COUNT(*) FROM {table} WHERE "{col}" IS NULL'
        ).fetchone()[0]
        rates[col] = nulls / total
    return rates


def _detect_column_rename_pair(cols: list[str]) -> bool:
    lowered = {c.lower(): c for c in cols}
    for col in cols:
        base = re.sub(r"(_old|_new|_legacy|_v\d+)$", "", col.lower())
        for suffix in ("_old", "_new", "_legacy"):
            candidate = base + suffix
            if candidate in lowered and candidate != col.lower():
                return True
    return False


def _find_col(cols: list[str], *, suffixes: list[str]) -> str | None:
    for col in cols:
        low = col.lower()
        if any(low.endswith(s) or s in low for s in suffixes):
            return col
    return None


def _is_eav_table(conn: sqlite3.Connection, table: str) -> bool:
    cols = _table_columns(conn, table)
    name_col = _find_col(cols, suffixes=["attribute_name", "attr_name", "property_name"])
    val_col = _find_col(cols, suffixes=["attribute_value", "attr_value", "property_value"])
    if not (name_col and val_col):
        return False
    entity_col = _find_col(cols, suffixes=["entity_id", "document_id", "_id", "id"])
    if entity_col is None:
        entity_col = cols[0]
    distinct = conn.execute(
        f'SELECT COUNT(DISTINCT "{entity_col}") FROM {table}'
    ).fetchone()[0]
    return distinct >= 3


def _count_type_conditional_queries(queries: list[dict[str, Any]], table: str, disc_col: str) -> int:
    hits = 0
    pattern = re.compile(rf"\b{re.escape(table)}\b.*\b{re.escape(disc_col)}\b", re.IGNORECASE)
    for query in queries:
        sql = query.get("query", "")
        if pattern.search(sql) or disc_col.lower() in sql.lower():
            hits += 1
    return hits


def eval_triggers(
    wp_output: dict[str, Any],
    *,
    db_id: str,
    sqlite_conn: sqlite3.Connection | None = None,
    force_document_flex: bool = False,
    qualifying: bool = True,
) -> dict[str, Any]:
    """Deterministic H1–H4 pre-audit / Stage B evaluation; optional H0 build-policy fallback."""
    queries = _load_spider_queries(db_id)
    tables = wp_output.get("source", {}).get("tables", [])
    conn = sqlite_conn
    own_conn = False
    if conn is None:
        path = sqlite_path(db_id)
        conn = sqlite3.connect(path)
        own_conn = True

    try:
        if not tables:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]

        spider_columns = {table: _table_columns(conn, table) for table in tables}

        h4 = any(_is_eav_table(conn, table) for table in tables)
        h1 = False
        h1_evidence = "no discriminator column with type-conditional queries >= 30%"
        for pattern in wp_output.get("access_patterns", []):
            pattern_type = str(pattern.get("type", "")).lower()
            if "type_conditional" in pattern_type or "conditional" in pattern_type:
                freq = float(pattern.get("frequency", 0.0))
                if freq >= 0.30:
                    h1 = True
                    h1_evidence = f"WP {pattern.get('pattern_id')}; frequency={freq:.2f}"
                    break
        if not h1:
            for table, cols in spider_columns.items():
                discs = [c for c in cols if c.lower() in TYPE_DISCRIMINATOR_COLS]
                for disc in discs:
                    type_cond = _count_type_conditional_queries(queries, table, disc)
                    rate = type_cond / max(len(queries), 1)
                    if rate >= 0.30:
                        h1 = True
                        h1_evidence = f"{table}.{disc}; type_conditional_rate={rate:.2f}"
                        break
                if h1:
                    break

        widest = max(tables, key=lambda t: len(spider_columns.get(t, []))) if tables else ""
        null_rates = _column_null_rates(conn, widest) if widest else {}
        sparse_count = sum(1 for rate in null_rates.values() if rate > 0.50)
        h2 = sparse_count >= 3
        h2_evidence = f"sparse_null_cols={sparse_count}"

        main_table = tables[0] if tables else ""
        cols = spider_columns.get(main_table, [])
        has_time = any("date" in c.lower() or "time" in c.lower() for c in cols)
        h3 = has_time and _detect_column_rename_pair(cols)
        h3_evidence = "time column + rename pair" if h3 else "no column rename pair detected"

        h4_evidence = "EAV attribute_name/value table detected" if h4 else "no EAV table shape"

        selected: str | None = None
        if h4:
            selected = "H4"
        elif h1:
            selected = "H1"
        elif h2:
            selected = "H2"
        elif h3:
            selected = "H3"

        natural_selected = selected
        triggers: list[dict[str, Any]] = [
            {"id": "H1", "fired": h1 and selected == "H1", "evidence": h1_evidence},
            {"id": "H2", "fired": h2 and selected == "H2", "evidence": h2_evidence},
            {"id": "H3", "fired": h3 and selected == "H3", "evidence": h3_evidence},
            {"id": "H4", "fired": h4 and selected == "H4", "evidence": h4_evidence},
        ]

        forced_h0 = False
        if (
            selected is None
            and force_document_flex
            and qualifying
            and db_id not in H0_EXCLUDE_DB_IDS
        ):
            selected = "H0"
            forced_h0 = True
            triggers.append({"id": "H0", "fired": True, "evidence": H0_EVIDENCE})

        schema_flex_map = {
            "H0": "polymorphic",
            "H1": "polymorphic",
            "H2": "attribute_bag",
            "H3": "schema_versioning",
            "H4": "dynamic_key",
        }

        return {
            "selected": selected,
            "natural_selected": natural_selected,
            "forced_h0": forced_h0,
            "flex_eligible": selected is not None,
            "schema_flex": schema_flex_map.get(selected, "none") if selected else "none",
            "triggers": triggers,
        }
    finally:
        if own_conn:
            conn.close()


def _trigger_evidence(trigger_report: dict[str, Any], trigger_id: str) -> str:
    for trigger in trigger_report.get("triggers", []):
        if trigger.get("id") == trigger_id:
            return str(trigger.get("evidence", ""))
    return ""


def _write_polymorphic_variants(
    schema: dict[str, Any],
    root: str,
    *,
    prefix: str,
    evidence: str,
) -> None:
    schema[root].setdefault(
        "__variants",
        [
            {
                "discriminator": {"__type": "variant_a"},
                "fields": {"field_a": "TEXT"},
                "coverage": 0.5,
                "source_signal": f"{prefix}: {evidence}",
            },
            {
                "discriminator": {"__type": "variant_b"},
                "fields": {"field_b": "TEXT"},
                "coverage": 0.5,
                "source_signal": f"{prefix}: {evidence}",
            },
        ],
    )


def _apply_stage_b(schema: dict[str, Any], rationale: dict[str, Any], trigger_report: dict[str, Any]) -> None:
    selected = trigger_report["selected"]
    if not selected:
        rationale["heterogenization"] = {
            "schema_flex": "none",
            "triggers": trigger_report["triggers"],
        }
        return

    rationale["heterogenization"] = {
        "schema_flex": trigger_report["schema_flex"],
        "triggers": trigger_report["triggers"],
    }
    root = next(iter(schema))
    if selected in ("H0", "H1"):
        evidence = _trigger_evidence(trigger_report, selected)
        _write_polymorphic_variants(schema, root, prefix=selected, evidence=evidence)
    elif selected == "H2":
        evidence = _trigger_evidence(trigger_report, "H2")
        schema[root].setdefault("attributes", {"type": "ARRAY", "items": {"type": "OBJECT"}})
        _write_polymorphic_variants(schema, root, prefix="H2", evidence=evidence)
    elif selected == "H3":
        evidence = _trigger_evidence(trigger_report, "H3")
        schema[root].setdefault(
            "__variants",
            [
                {
                    "discriminator": {"__version": "v1"},
                    "fields": {"payload": {"type": "OBJECT", "fields": {}}},
                    "coverage": 0.5,
                    "source_signal": f"H3: {evidence}",
                },
                {
                    "discriminator": {"__version": "v2"},
                    "fields": {"payload": {"type": "OBJECT", "fields": {}}},
                    "coverage": 0.5,
                    "source_signal": f"H3: {evidence}",
                },
            ],
        )
    elif selected == "H4":
        schema[root]["metrics"] = {"type": "OBJECT", "fields": {}}
        _write_polymorphic_variants(
            schema,
            root,
            prefix="H4",
            evidence=_trigger_evidence(trigger_report, "H4"),
        )


def design_schema(
    wp_output: dict[str, Any],
    *,
    db_id: str | None = None,
    revision: int = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (mongodb_schema, agent_design_rationale)."""
    db_id = db_id or wp_output["db_id"]
    log_module.emit("sra.start", db_id=db_id, agent="SRA", stage="phase_a", revision=revision)

    if db_id == "orchestra":
        schema = ORCHESTRA_SCHEMA.copy()
        rationale = {**ORCHESTRA_RATIONALE, "db_id": db_id}
    else:
        tables = wp_output.get("source", {}).get("tables", [])
        root = tables[0] if tables else db_id
        schema = {
            root: {
                "_id": "INT",
                "payload": {
                    "type": "ARRAY",
                    "items": {"type": "OBJECT", "fields": {"value": "TEXT"}},
                },
            }
        }
        rationale = {
            "db_id": db_id,
            "source_spider_tables": tables,
            "patterns_applied": ["embed"],
            "rationale_summary": f"Baseline embed layout for {db_id} (revision {revision}).",
            "decisions": [
                {
                    "id": "D01",
                    "type": "embed",
                    "parent": root,
                    "child": tables[1] if len(tables) > 1 else root,
                    "rationale": "Primary WP access pattern embed candidate.",
                    "reference": "access_patterns.AP01",
                }
            ],
        }

    from tend.config import force_document_flex
    from tend.phase_a.catalog import is_catalog_qualifying

    trigger_report = eval_triggers(
        wp_output,
        db_id=db_id,
        force_document_flex=force_document_flex(),
        qualifying=is_catalog_qualifying(db_id),
    )
    _apply_stage_b(schema, rationale, trigger_report)
    validate(rationale, "agent_design_rationale")
    log_module.emit("sra.done", db_id=db_id, agent="SRA", stage="phase_a", schema_flex=trigger_report["schema_flex"])
    return schema, rationale
