"""Tests for H0 build-policy forced document-level schema flex."""

from __future__ import annotations

import pytest

from tend.orchestrate.mongo_schemaless import document_shape
from tend.phase_a.catalog import FIXTURE_DB_IDS, clear_catalog_cache, select_spider_dbs
from tend.phase_a.dm import migrate
from tend.phase_a.sra import design_schema, eval_triggers
from tend.phase_a.wp import profile_workload


def test_world_1_forced_h0_flex(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEND_FORCE_DOCUMENT_FLEX", "1")
    wp = profile_workload("world_1")
    report = eval_triggers(wp, db_id="world_1", force_document_flex=True, qualifying=True)
    assert report["selected"] == "H0"
    assert report["forced_h0"] is True
    assert report["schema_flex"] == "polymorphic"

    schema, rationale = design_schema(wp, db_id="world_1")
    root = next(iter(schema))
    assert "__variants" in schema[root]
    assert rationale["heterogenization"]["schema_flex"] == "polymorphic"

    data, _ = migrate("world_1", schema, rationale)
    docs = data[root]
    assert len(docs) >= 2
    shapes = {document_shape(doc) for doc in docs}
    assert len(shapes) >= 2


def test_orchestra_excluded_from_h0(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEND_FORCE_DOCUMENT_FLEX", "1")
    wp = profile_workload("orchestra", use_fixture=True)
    report = eval_triggers(
        wp,
        db_id="orchestra",
        force_document_flex=True,
        qualifying=True,
    )
    assert report["selected"] is None
    assert report["flex_eligible"] is False

    schema, rationale = design_schema(wp, db_id="orchestra")
    assert rationale["heterogenization"]["schema_flex"] == "none"
    assert not any(isinstance(coll, dict) and coll.get("__variants") for coll in schema.values())


def test_catalog_flex_ratio_under_force(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEND_FORCE_DOCUMENT_FLEX", "1")
    clear_catalog_cache()
    result = select_spider_dbs(min_queries=10, min_tables=2)
    selected = [e for e in result["catalog"]["databases"] if e["selected"]]
    flex_selected = [e for e in selected if e["flex_eligible"]]
    expected_min = len(FIXTURE_DB_IDS) - 1
    assert len(flex_selected) >= expected_min
    orchestra = next(e for e in selected if e["db_id"] == "orchestra")
    assert orchestra["flex_eligible"] is False


def test_force_off_preserves_legacy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEND_FORCE_DOCUMENT_FLEX", "0")
    wp = profile_workload("world_1")
    report = eval_triggers(wp, db_id="world_1", force_document_flex=False, qualifying=True)
    assert report["selected"] is None
    assert report["flex_eligible"] is False

    schema, rationale = design_schema(wp, db_id="world_1")
    root = next(iter(schema))
    assert "__variants" not in schema.get(root, {})
    assert rationale["heterogenization"]["schema_flex"] == "none"
