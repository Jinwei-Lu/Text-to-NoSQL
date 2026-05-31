"""Unit tests for Phase A agents."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from tend.core.signatures import world_signature
from tend.phase_a.catalog import FIXTURE_DB_IDS, load_domain_map, select_spider_dbs
from tend.phase_a.dm import migrate
from tend.phase_a.sc import critique_schema, review_schema
from tend.phase_a.sra import design_schema, eval_triggers
from tend.phase_a.wp import profile_workload
from tend.schemas.validators import validate


def test_fixture_wp_validates():
    wp = profile_workload("orchestra", use_fixture=True)
    validate(wp, "wp_output")
    assert wp["db_id"] == "orchestra"
    assert wp["insufficient_workload"] is False
    assert "scenario_summary" in wp


def test_orchestra_sra_schema():
    wp = profile_workload("orchestra", use_fixture=True)
    schema, rationale = design_schema(wp)
    assert "conductor" in schema
    assert rationale["patterns_applied"][:2] == ["embed", "mixed"]
    validate(rationale, "agent_design_rationale")


def test_orchestra_sc_passes():
    wp = profile_workload("orchestra", use_fixture=True)
    schema, rationale = design_schema(wp)
    schema, rationale, verdict = review_schema(wp, schema, rationale)
    assert verdict.verdict == "pass"
    assert rationale["anti_pattern_checks"]["pass"] is True


def test_orchestra_dm_migration():
    wp = profile_workload("orchestra", use_fixture=True)
    schema, rationale = design_schema(wp)
    data, migration_log = migrate("orchestra", schema, rationale)
    assert len(data["conductor"]) == 12
    assert migration_log["world_signature"] == world_signature(data)
    assert migration_log["stats"]["target_documents"] == 12
    validate(migration_log, "migration_log")


def test_student_assessment_flex_eligible():
    wp = profile_workload("student_assessment", use_fixture=True)
    report = eval_triggers(wp, db_id="student_assessment")
    assert report["flex_eligible"] is True
    assert report["selected"] == "H1"


def test_catalog_marks_fixtures_selected():
    result = select_spider_dbs(min_queries=10, min_tables=2)
    catalog = result["catalog"]
    selected = [entry for entry in catalog["databases"] if entry["selected"]]
    selected_ids = {entry["db_id"] for entry in selected}
    assert selected_ids >= set(FIXTURE_DB_IDS)
    flex_selected = [entry for entry in selected if entry["flex_eligible"]]
    assert len(flex_selected) >= 1


def test_domain_map_covers_fixtures():
    domain_map = load_domain_map()
    assert domain_map["orchestra"] == "performance_arts"
    assert domain_map["concert_singer"] == "entertainment"
    assert domain_map["student_assessment"] == "education"


def test_build_phase_a_cli_orchestra(tmp_path: Path):
    from tend.cli.build_phase_a import build_phase_a

    out_root = tmp_path / "TEND"
    paths = build_phase_a("orchestra", out_root, llm_stub=True)
    schema = json.loads(paths["schema"].read_text(encoding="utf-8"))
    data = json.loads(paths["data"].read_text(encoding="utf-8"))
    rationale = yaml.safe_load(paths["rationale"].read_text(encoding="utf-8"))
    assert "conductor" in schema
    assert len(data["conductor"]) == 12
    assert rationale["patterns_applied"][0] == "embed"

    from tend.tests.assert_phase_a_equiv import assert_equivalent

    assert assert_equivalent("orchestra", out_root) == []
