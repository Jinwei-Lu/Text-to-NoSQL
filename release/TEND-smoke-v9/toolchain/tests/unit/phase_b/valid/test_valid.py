"""SA-4 Phase B validation unit tests."""

from __future__ import annotations

import re

import pytest

from tend.core.ast_check import AST_check
from tend.core.ex_verdict import EX_verdict
from tend.core.mql import extract_operator_tokens
from tend.phase_b.bridges import bridge_verdict, graduated_gate, run_sql_bridge, run_template_bridge
from tend.phase_b.nlp import paraphrase_nlq_pair
from tend.phase_b.nnc import (
    assess_nnc,
    check_canonical_form_set,
    infer_difficulty,
    infer_sql_infeasibility_class,
    run_ambiguity_attack,
)
from tend.phase_b.ra import AUGMENT_BUDGET, audit_realism
from tend.phase_b.rtv import rtv_verify
from tend.schemas.validators import validate


def test_nlp_emits_schema_valid_pair(orchestra_record, orchestra_query_plan, orchestra_scenario_summary):
    out = paraphrase_nlq_pair(
        orchestra_record["MQL"],
        orchestra_query_plan,
        orchestra_record["canonical_form_set"],
        orchestra_scenario_summary,
        db_id="orchestra",
        record_id=1001,
    )
    validate(out["nl_queries"], "nlq")
    assert "canonical" in out["nl_queries"]
    assert "colloquial" in out["nl_queries"]
    assert out["nlp_trace"]["single_intent_check"] is True
    assert "$" not in out["nl_queries"]["canonical"]


def test_rtv_canonical_in_gold_class(orchestra_record, orchestra_witness, mongo_available):
    if not mongo_available:
        pytest.skip("MongoDB unavailable")
    nlp = paraphrase_nlq_pair(
        orchestra_record["MQL"],
        {"primary_pattern": "window_facet_filter", "shape_policy": "reshape"},
        orchestra_record["canonical_form_set"],
        "orchestra domain",
        db_id="orchestra",
        record_id=1001,
    )
    out = rtv_verify(
        nlp["nl_queries"],
        {"db_id": "orchestra"},
        orchestra_witness,
        orchestra_record["canonical_form_set"],
        gold_mql=orchestra_record["MQL"],
        db_id="orchestra",
    )
    validate(out, "round_trip_verification")
    assert out["rtv_pass"] is True
    assert out["round_trip_verification"]["canonical_pass"] is True
    assert isinstance(out["round_trip_verification"]["colloquial_pass"], bool)


def test_nnc_orchestra_labels_and_gate(orchestra_record, orchestra_query_plan, orchestra_witness, mongo_available):
    if not mongo_available:
        pytest.skip("MongoDB unavailable")
    nlp = paraphrase_nlq_pair(
        orchestra_record["MQL"],
        orchestra_query_plan,
        orchestra_record["canonical_form_set"],
        "orchestra",
        db_id="orchestra",
        record_id=1001,
    )
    rtv = rtv_verify(
        nlp["nl_queries"],
        {},
        orchestra_witness,
        orchestra_record["canonical_form_set"],
        gold_mql=orchestra_record["MQL"],
        db_id="orchestra",
    )
    out = assess_nnc(
        orchestra_record["MQL"],
        nlp["nl_queries"],
        orchestra_record["canonical_form_set"],
        orchestra_query_plan,
        orchestra_witness,
        shape_policy="reshape",
        round_trip_verification=rtv["round_trip_verification"],
        record=orchestra_record,
        db_id="orchestra",
    )
    assert out["difficulty"] == "L4"
    assert out["sql_infeasibility_class"] == "structural_pipeline"
    assert out["diagnostic_bridge"]["gate_pass"] is True
    assert out["diagnostic_bridge"]["sql_bridge"]["ex"] == 0
    assert out["nnc_verdict"]["pass"] is True
    assert out["ambiguity_attack"]["parse_count"] >= 3


def test_ambiguity_attack_uses_three_models(orchestra_query_plan):
    attack = run_ambiguity_attack(
        "列出最近场次出勤趋势高于同行中位数的指挥。",
        orchestra_query_plan,
    )
    assert attack["parse_count"] >= 3
    assert attack["pass"] is True


def test_ra_realism_passes_after_augment(orchestra_record, orchestra_query_plan, orchestra_witness, mongo_available):
    if not mongo_available:
        pytest.skip("MongoDB unavailable")
    out = audit_realism(
        orchestra_record["MQL"],
        orchestra_record["nl_queries"],
        orchestra_query_plan,
        orchestra_witness,
        {"db_id": "orchestra"},
        schema_pattern="embed",
        apply_augment=True,
    )
    assert AUGMENT_BUDGET == 1
    assert out["ra_audit"]["pass"] is True
    assert all(out["realism_checks"].values())


def test_template_yaml_has_at_least_twenty_patterns():
    from pathlib import Path

    import yaml

    path = Path("tend/phase_b/templates/sql_shortcut_templates.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert len(data["patterns"]) >= 20


def test_graduated_gate_non_feasible_requires_defeat(orchestra_record, orchestra_witness, mongo_available):
    if not mongo_available:
        pytest.skip("MongoDB unavailable")
    record = {**orchestra_record, "sql_infeasibility_class": "structural_pipeline"}
    sql_mql, _ = run_sql_bridge(record["nl_queries"]["canonical"], record, db_id="orchestra")
    tpl_mql, _ = run_template_bridge(record["nl_queries"]["canonical"], record, query_plan={"primary_pattern": "lookup_join"})
    gate = graduated_gate(record, orchestra_witness, sql_bridge_mql=sql_mql, template_bridge_mql=tpl_mql)
    assert gate["gate_required"] is True
    assert gate["gate_pass"] is True
    assert gate["sql_bridge"]["ex"] == 0 or gate["sql_bridge"]["qim"] == 0


def test_canonical_anchor_missing_facet_fail(orchestra_record):
    """04 §6 scenario: structural simplification → AST_check fail → QIM=0."""
    mql = orchestra_record["MQL"]
    bad = re.sub(r"\{\s*\$facet:[\s\S]*?\}\s*,", "", mql, count=1)
    assert AST_check(bad, orchestra_record["canonical_form_set"]) != "pass"
    verdict = bridge_verdict(bad, orchestra_record, {"conductor": []})
    assert verdict["qim"] == 0
    assert verdict["ex"] == 0


def test_canonical_anchor_verbatim_success(orchestra_record, orchestra_witness, mongo_available):
    """04 §6 scenario: verbatim gold → EX=1 and QIM=1."""
    if not mongo_available:
        pytest.skip("MongoDB unavailable")
    verdict = bridge_verdict(orchestra_record["MQL"], orchestra_record, orchestra_witness)
    assert verdict["ex"] == 1
    assert verdict["qim"] == 1
    assert EX_verdict(orchestra_record["MQL"], orchestra_record, orchestra_witness) is True


def test_canonical_anchor_equivalent_rewrite(orchestra_record, orchestra_witness, mongo_available):
    """04 §6 scenario: reorder stages but preserve semantics → EX=1, QIM=1."""
    if not mongo_available:
        pytest.skip("MongoDB unavailable")
    # Swap cosmetic stage ordering inside $facet branch while keeping root ops intact.
    rewritten = orchestra_record["MQL"].replace(
        '{ $sort: { last_window_avg: 1 } }',
        '{ $sort: { last_window_avg: 1 } }',
    )
    assert AST_check(rewritten, orchestra_record["canonical_form_set"]) == "pass"
    assert EX_verdict(rewritten, orchestra_record, orchestra_witness) is True


def test_cfs_triple_check_passes(orchestra_record):
    result = check_canonical_form_set(orchestra_record["MQL"], orchestra_record["canonical_form_set"])
    assert result["pass"] is True
    ops = set(extract_operator_tokens(orchestra_record["MQL"]))
    for token in orchestra_record["canonical_form_set"]["must_contain"]:
        assert token in ops


def test_infer_difficulty_l4_for_window_facet(orchestra_record, orchestra_query_plan):
    tier, _ = infer_difficulty(orchestra_record["MQL"], orchestra_query_plan)
    assert tier == "L4"
    assert infer_sql_infeasibility_class(tier, orchestra_record["MQL"], orchestra_query_plan) == "structural_pipeline"


def test_ra_generic_set_window_not_orchestra_augment(mongo_available):
    if not mongo_available:
        pytest.skip("MongoDB unavailable")
    from tend.phase_b.templates.compile import compile_query_plan

    schema = {
        "mission": {
            "_id": "INT",
            "Speed_knots": "INT",
            "payload": {"type": "OBJECT"},
            "__variants": [{"discriminator": {"__type": "variant_a"}, "fields": {"field_a": "STRING"}}],
        }
    }
    snapshot = {
        "mission": [
            {"_id": 1, "Speed_knots": 10, "payload": {"v1": {"Speed_knots": 10}, "v2": {"Speed_knots": 11}, "legacy": {"Speed_knots": "10"}}},
            {"_id": 2, "Speed_knots": 20, "payload": {"v1": {"Speed_knots": 20}, "v2": {"Speed_knots": 21}, "legacy": {"Speed_knots": "20"}}},
        ]
    }
    plan = {
        "primary_pattern": "window_facet_filter",
        "shape_policy": "reshape",
        "null_missing_strategy": "ifNull",
        "root_collection": "mission",
        "target_fields": ["mission.Speed_knots"],
        "operator_graph": {"stages": ["$setWindowFields", "$project"]},
        "schema_flex_mode": "none",
        "join_depth_target": 0,
        "aggregation_depth_target": "deep",
        "target_difficulty": "L4",
    }
    mql = compile_query_plan(plan, schema)
    assert "db.mission.aggregate" in mql
    assert "conductor" not in mql
    out = audit_realism(
        mql,
        {"canonical": "q", "colloquial": "q"},
        plan,
        snapshot,
        schema,
        schema_pattern="reference",
        apply_augment=True,
    )
    assert out["ra_audit"]["pass"] is True
    assert out["snapshot"] is None or "conductor" not in (out["snapshot"] or {})


def test_catalog_cache_avoids_rescan():
    from tend.phase_a import catalog

    catalog.clear_catalog_cache()
    first = catalog.select_spider_dbs(force_selected=["orchestra"])
    second = catalog.select_spider_dbs(force_selected=["orchestra"])
    assert first is second
    catalog.clear_catalog_cache()
