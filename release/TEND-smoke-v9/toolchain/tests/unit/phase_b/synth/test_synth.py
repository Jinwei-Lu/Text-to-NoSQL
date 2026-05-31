"""Unit tests for Phase B synthesis (QPS / MS / MUT / PV)."""

from __future__ import annotations

import json

import pytest

from tend.config import FIXTURES_ROOT
from tend.core import AST_check, EX_verdict, NormExec, equiv_rec
from tend.schemas.validators import validate

from tend.phase_b.derive_cfs import derive_canonical_form_set
from tend.phase_b.ms import ms_synthesize
from tend.phase_b.mut import generate_mutations, validate_mutations
from tend.phase_b.pv import verify_properties
from tend.phase_b.qps import sample_query_plan
from tend.phase_b.templates.compile import PRIMARY_PATTERNS, compile_query_plan


@pytest.fixture(scope="module")
def fixture_record():
    path = FIXTURES_ROOT / "orchestra" / "record.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_all_primary_pattern_templates_exist(orchestra_schema):
    for pattern in PRIMARY_PATTERNS:
        plan = {
            "primary_pattern": pattern,
            "target_fields": ["conductor.field"],
            "null_missing_strategy": "ifNull" if pattern == "window_facet_filter" else "none",
            "shape_policy": "reshape" if pattern == "window_facet_filter" else "preserve",
        }
        mql = compile_query_plan(plan, orchestra_schema, strategy="direct")
        assert mql.startswith("db.")


def test_qps_orchestra_window_facet_filter():
    qps = sample_query_plan("orchestra", plan_pattern="window_facet_filter")
    validate(qps, "query_plan")
    assert qps["query_plan"]["primary_pattern"] == "window_facet_filter"
    assert qps["query_plan"]["shape_policy"] == "reshape"


def test_derive_cfs_orchestra_matches_fixture(fixture_record):
    qps = sample_query_plan("orchestra", plan_pattern="window_facet_filter")
    cfs = derive_canonical_form_set(qps["query_plan"])
    expected = fixture_record["canonical_form_set"]
    assert set(cfs["must_contain"]) == set(expected["must_contain"])
    assert set(cfs["must_contain_at_root"]) == set(expected["must_contain_at_root"])
    assert set(cfs["must_not_contain_at_root"]) == set(expected["must_not_contain_at_root"])


def test_ms_direct_path_ex_equiv_orchestra(orchestra_witness, orchestra_schema, fixture_record):
    qps = sample_query_plan("orchestra", plan_pattern="window_facet_filter")
    ms_out = ms_synthesize(qps["query_plan"], orchestra_schema, orchestra_witness, llm_stub=True)

    assert ms_out["synthesis_trace"]["converged"] is True

    assert AST_check(ms_out["MQL"], ms_out["canonical_form_set"]) == "pass"
    assert EX_verdict(ms_out["MQL"], fixture_record, orchestra_witness)

    gold = NormExec(fixture_record["MQL"], orchestra_witness)
    synth = NormExec(ms_out["MQL"], orchestra_witness)
    assert equiv_rec(gold, synth, order_sensitive=False)


def test_mutations_cover_a_through_e_and_all_ex_fail(orchestra_witness, fixture_record):
    qps = sample_query_plan("orchestra", plan_pattern="window_facet_filter")
    ms_out = ms_synthesize(qps["query_plan"], {"collections": ["conductor"]}, orchestra_witness)
    mutations = generate_mutations(
        qps["query_plan"],
        ms_out["MQL"],
        ms_out["canonical_form_set"],
        min_n=5,
        max_n=5,
    )
    assert len(mutations) == 5
    assert {m["dimension"] for m in mutations} == {"A", "B", "C", "D", "E"}

    record = {
        "record_id": 1001,
        "db_id": "orchestra",
        "MQL": fixture_record["MQL"],
        "canonical_form_set": fixture_record["canonical_form_set"],
    }
    validate_mutations(mutations, record, orchestra_witness)
    for mutation in mutations:
        assert EX_verdict(mutation["MQL"], record, orchestra_witness) is False


def test_pv_pass_orchestra(orchestra_witness):
    qps = sample_query_plan("orchestra", plan_pattern="window_facet_filter")
    ms_out = ms_synthesize(qps["query_plan"], {"collections": ["conductor"]}, orchestra_witness)
    mutations = generate_mutations(
        qps["query_plan"],
        ms_out["MQL"],
        ms_out["canonical_form_set"],
        min_n=5,
        max_n=5,
    )
    pv = verify_properties(qps["query_plan"], ms_out, mutations, orchestra_witness)
    validate(pv, "property_verification")
    assert pv["pv_pass"] is True
    assert pv["property_verification"]["gold_ex"] is True
    assert pv["property_verification"]["mutations_ex_all_reject"] is True
