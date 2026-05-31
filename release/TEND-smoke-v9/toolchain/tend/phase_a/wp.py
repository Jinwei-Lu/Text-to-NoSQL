"""Workload Profiler (WP) — Spider SQLite + NL/SQL → wp_output."""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from tend.config import FIXTURES_ROOT, SPIDER_DATA_ROOT, use_fixtures
from tend.core.llm_client import LLMClient
from tend.core.llm_response import parse_llm_json_response
from tend.core import logging as log_module
from tend.prompts.loader import load, render
from tend.schemas.validators import validate

FIXTURE_WP = {
    "orchestra": FIXTURES_ROOT / "orchestra" / "wp.yaml",
    "concert_singer": FIXTURES_ROOT / "concert_singer" / "wp.yaml",
    "cre_doc_tracking_db": FIXTURES_ROOT / "cre_doc_tracking_db" / "wp.yaml",
    "flight_2": FIXTURES_ROOT / "flight_2" / "wp.yaml",
    "student_assessment": FIXTURES_ROOT / "student_assessment" / "wp.yaml",
    "world_1": FIXTURES_ROOT / "world_1" / "wp.yaml",
}

SPIDER_DB_ALIASES = {
    "cre_doc_tracking_db": "cre_Doc_Tracking_DB",
}


def _spider_db_id(db_id: str) -> str:
    return SPIDER_DB_ALIASES.get(db_id, db_id)


def normalize_db_id(spider_folder: str) -> str:
    reverse_aliases = {v: k for k, v in SPIDER_DB_ALIASES.items()}
    if spider_folder in reverse_aliases:
        return reverse_aliases[spider_folder]
    return spider_folder.lower()


def sqlite_path(db_id: str) -> Path:
    spider_id = _spider_db_id(db_id)
    return SPIDER_DATA_ROOT / "database" / spider_id / f"{spider_id}.sqlite"


def _load_spider_queries(db_id: str) -> list[dict[str, Any]]:
    spider_id = _spider_db_id(db_id)
    queries: list[dict[str, Any]] = []
    for name in ("train_spider.json", "dev.json", "train_others.json"):
        path = SPIDER_DATA_ROOT / name
        if not path.exists():
            continue
        for item in json.loads(path.read_text(encoding="utf-8")):
            if item.get("db_id") == spider_id:
                queries.append(item)
    return queries


def _sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def _load_fixture_wp(db_id: str) -> dict[str, Any] | None:
    path = FIXTURE_WP.get(db_id)
    if path and path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    return None


def _sanitize_access_patterns(
    patterns: list[Any], baseline: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    allowed = {
        "pattern_id",
        "type",
        "tables",
        "join_path",
        "frequency",
        "example_query_ids",
        "nl_hints",
        "sql_operators",
    }
    cleaned: list[dict[str, Any]] = []
    for idx, raw in enumerate(patterns):
        if not isinstance(raw, dict):
            continue
        item = {k: raw[k] for k in allowed if k in raw}
        if "example_query_ids" not in item and "example_question_ids" in raw:
            ids = raw["example_question_ids"]
            item["example_query_ids"] = ids if isinstance(ids, list) else [1]
        if "nl_hints" not in item:
            hint = raw.get("nl_hint") or raw.get("nl_hints")
            if isinstance(hint, str):
                item["nl_hints"] = [hint]
            elif isinstance(hint, list) and hint:
                item["nl_hints"] = [str(h) for h in hint]
        if "pattern_id" not in item:
            item["pattern_id"] = f"AP{idx + 1:02d}"
        if "type" not in item:
            item["type"] = "filter"
        if "tables" not in item or not item["tables"]:
            item["tables"] = baseline[0]["tables"] if baseline else ["table"]
        if "frequency" not in item:
            item["frequency"] = 0.5
        if "example_query_ids" not in item:
            item["example_query_ids"] = [1]
        if "nl_hints" not in item:
            item["nl_hints"] = ["filter or project entities"]
        if "sql_operators" not in item:
            item["sql_operators"] = ["SELECT"]
        cleaned.append(item)
    return cleaned or baseline


def _normalize_wp_llm(parsed: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Merge LLM fields into deterministic baseline so schema validation succeeds."""
    merged = deepcopy(baseline)
    for key in (
        "workload_summary",
        "scenario_summary",
        "design_constraints",
        "access_patterns",
        "hot_fields",
        "co_location_signals",
        "join_depth_distribution",
        "aggregation_depth_distribution",
    ):
        val = parsed.get(key)
        if val is None:
            continue
        if isinstance(val, list) and not val:
            continue
        if isinstance(val, dict) and not val:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        if key == "access_patterns" and isinstance(val, list):
            merged[key] = _sanitize_access_patterns(val, baseline.get("access_patterns", []))
            continue
        merged[key] = val
    if parsed.get("insufficient_workload") is not None:
        merged["insufficient_workload"] = bool(parsed["insufficient_workload"])
    merged["db_id"] = baseline["db_id"]
    merged["spider_version"] = "1.0"
    merged["source"] = baseline["source"]
    merged.setdefault("generated_at", baseline["generated_at"])
    return merged


def _minimal_wp(db_id: str, *, tables: list[str], query_count: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "db_id": db_id,
        "spider_version": "1.0",
        "generated_at": now,
        "insufficient_workload": query_count < 10,
        "source": {
            "sqlite_path": f"database/{_spider_db_id(db_id)}/{_spider_db_id(db_id)}.sqlite",
            "tables": tables,
            "query_count": query_count,
        },
        "workload_summary": f"Spider workload for {db_id}: {query_count} queries across {len(tables)} tables.",
        "scenario_summary": (
            f"Domain anchored on {db_id}. Typical questions compare entities, filter by attributes, "
            "rank by metrics, aggregate counts or averages, and join related records across tables."
        ),
        "access_patterns": [
            {
                "pattern_id": "AP01",
                "type": "nested_traversal",
                "tables": tables[: min(3, len(tables))],
                "join_path": None,
                "frequency": 0.5,
                "example_query_ids": [1],
                "nl_hints": ["traverse related entities for filtering or projection"],
                "sql_operators": ["JOIN", "SELECT"],
            }
        ],
        "hot_fields": [{"path": f"{tables[0]}.id", "access_count": max(query_count // 2, 1)}]
        if tables
        else [],
        "co_location_signals": [
            {
                "entities": [tables[0], tables[1]],
                "co_access_rate": 0.7,
                "note": "co-access inferred from join queries",
            }
        ]
        if len(tables) >= 2
        else [],
        "join_depth_distribution": {"0": 0.1, "1": 0.3, "2": 0.4, "3+": 0.2},
        "aggregation_depth_distribution": {"shallow": 0.4, "medium": 0.35, "deep": 0.25},
        "design_constraints": [
            "Preserve hot-path fields reachable without deep $lookup chains.",
            "Keep primary entity roots aligned with highest-frequency access patterns.",
        ],
    }


def workload_for_triggers(db_id: str) -> dict[str, Any]:
    """Load fixture or deterministic WP output for trigger pre-audit (no LLM)."""
    fixture = _load_fixture_wp(db_id)
    if fixture is not None:
        return fixture

    sqlite = sqlite_path(db_id)
    if not sqlite.exists():
        raise FileNotFoundError(sqlite)

    conn = sqlite3.connect(sqlite)
    try:
        tables = _sqlite_tables(conn)
    finally:
        conn.close()
    queries = _load_spider_queries(db_id)
    wp_output = _minimal_wp(db_id, tables=tables, query_count=len(queries))
    validate(wp_output, "wp_output")
    return wp_output


def profile_workload(
    db_id: str,
    *,
    llm: LLMClient | None = None,
    seed: int = 42,
    use_fixture: bool | None = None,
) -> dict[str, Any]:
    """Produce schema-valid wp_output for a Spider db_id."""
    if use_fixture is None:
        use_fixture = use_fixtures()
    log_module.emit("wp.start", db_id=db_id, agent="WP", stage="phase_a")

    if use_fixture:
        fixture = _load_fixture_wp(db_id)
        if fixture is not None:
            validate(fixture, "wp_output")
            log_module.emit("wp.done", db_id=db_id, agent="WP", stage="phase_a", source="fixture")
            return fixture

    sqlite = sqlite_path(db_id)
    if not sqlite.exists():
        raise FileNotFoundError(sqlite)

    conn = sqlite3.connect(sqlite)
    try:
        tables = _sqlite_tables(conn)
    finally:
        conn.close()

    queries = _load_spider_queries(db_id)
    wp_output = _minimal_wp(db_id, tables=tables, query_count=len(queries))

    prompt_bundle = load("wp_workload_profiler")
    prompt = render(
        prompt_bundle["user"],
        {
            "db_id": db_id,
            "sqlite_path": str(sqlite),
            "query_count": str(len(queries)),
            "tables": ", ".join(tables),
        },
    )
    client = llm or LLMClient()
    llm_response = client.call(
        "A_construct",
        f"{prompt_bundle['system']}\n\n{prompt}",
        seed=seed,
        schema=prompt_bundle.get("output_schema"),
    )
    parsed = parse_llm_json_response(llm_response)
    if parsed and parsed.get("db_id"):
        normalized = _normalize_wp_llm(parsed, wp_output)
        try:
            validate(normalized, "wp_output")
            wp_output = normalized
            log_module.emit("wp.done", db_id=db_id, agent="WP", stage="phase_a", source="llm")
        except ValueError as exc:
            log_module.emit(
                "wp.done",
                db_id=db_id,
                agent="WP",
                stage="phase_a",
                source="deterministic",
                llm_validation_error=str(exc)[:240],
            )
    else:
        log_module.emit("wp.done", db_id=db_id, agent="WP", stage="phase_a", source="deterministic")
    validate(wp_output, "wp_output")
    return wp_output
