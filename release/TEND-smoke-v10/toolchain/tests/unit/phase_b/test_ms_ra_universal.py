"""Universal MS + RA yield fixes (duplicate _id, H0 ifNull)."""

from __future__ import annotations

import pytest

from tend.phase_b.ra import audit_realism
from tend.phase_b.templates.compile import compile_query_plan
from tend.phase_b.witness import prepare_witness_for_exec
from tend.phase_b.ms import ms_synthesize


def test_ms_simple_filter_on_duplicate_id_witness():
    """NormExec must not BOT when witness has duplicate string _id values."""
    raw = {
        "all_star": [
            {"_id": "gomezle01", "year": 2000, "field_a": "a0"},
            {"_id": "gomezle01", "year": 2001, "field_b": "b1"},
            {"_id": "other", "year": 2002, "field_a": "a2"},
            {"_id": "other2", "year": 2003, "field_b": "b3"},
        ]
    }
    schema = {
        "all_star": {
            "_id": "TEXT",
            "__variants": [
                {"discriminator": {"__type": "a"}, "fields": {"field_a": "TEXT"}},
                {"discriminator": {"__type": "b"}, "fields": {"field_b": "TEXT"}},
            ],
        }
    }
    exec_snapshot = prepare_witness_for_exec(raw)
    ids = [d["_id"] for d in exec_snapshot["all_star"]]
    assert len(ids) == len(set(ids))

    query_plan = {
        "primary_pattern": "simple_filter",
        "target_fields": ["field"],
        "null_missing_strategy": "none",
        "shape_policy": "preserve",
        "db_id": "baseball_1",
    }
    ms_out = ms_synthesize(query_plan, schema, raw, llm_stub=True)
    assert ms_out["synthesis_trace"]["converged"] is True
    assert "$field" not in ms_out["MQL"] or "field_a" in ms_out["MQL"] or "field_b" in ms_out["MQL"]


def test_compile_set_window_resolves_h0_field():
    query_plan = {
        "primary_pattern": "set_window",
        "target_fields": ["field"],
        "null_missing_strategy": "ifNull",
        "shape_policy": "preserve",
    }
    schema = {
        "world_1": {
            "_id": "INT",
            "__variants": [
                {"discriminator": {"__type": "a"}, "fields": {"field_a": "INT"}},
                {"discriminator": {"__type": "b"}, "fields": {"field_b": "INT"}},
            ],
        }
    }
    witness = {
        "world_1": [
            {"_id": 1, "__type": "a", "field_a": 10, "payload": {}},
            {"_id": 2, "__type": "b", "field_b": None, "payload": {}},
        ]
    }
    mql = compile_query_plan(query_plan, schema, witness=witness)
    assert '"$field"' not in mql
    assert "field_a" in mql or "field_b" in mql


def test_ra_h0_ifnull_coverage_with_augment():
    query_plan = {
        "primary_pattern": "set_window",
        "target_fields": ["field"],
        "null_missing_strategy": "ifNull",
        "shape_policy": "preserve",
    }
    schema = {"world_1": {"_id": "INT", "world_signature": "sig"}}
    witness = {
        "world_1": [
            {"_id": 1, "field_a": 10, "payload": {}},
            {"_id": 2, "field_b": 20, "payload": {}},
        ]
    }
    mql = compile_query_plan(query_plan, schema, witness=witness)
    ra_out = audit_realism(
        mql,
        {"canonical": "q", "colloquial": "q"},
        query_plan,
        witness,
        schema,
        schema_pattern="embed",
        schema_flex="polymorphic",
    )
    assert ra_out["realism_checks"]["embed_depth_matches_sra"] is True
    if not ra_out["ra_audit"]["pass"]:
        ra_out = audit_realism(
            mql,
            {"canonical": "q", "colloquial": "q"},
            query_plan,
            witness,
            schema,
            schema_pattern="embed",
            schema_flex="polymorphic",
            apply_augment=True,
        )
    assert ra_out["ra_audit"]["pass"] or ra_out.get("snapshot") is not None
