"""Test suite for the TEND construction pipeline (new package under src/tend).

Async agents/workflow are driven via ``asyncio.run`` inside sync tests (no pytest-asyncio
dependency). Tests that need a live MongoDB are skipped when none is reachable.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tend.agents import Agent, AgentContext, LLMAgent, get_agent, register
from tend.config import Settings
from tend.errors import GateError, PromptAnomalyError, ResponseParseError, SchemaValidationError
from tend.execution import (
    mql_signature,
    derive_canonical_form_set,
    parse_pipeline,
    scan_disabled,
    world_signature,
)
from tend.execution.mongo import MongoExecutor, equiv_rec
from tend.llm import LLMClient
from tend.observability import setup_logging
from tend.source import BirdSource
from tend.workflow import Workflow

ANCHOR_MQL = (
    'db.account.aggregate([\n'
    '  { $lookup: { from: "trans", let: { aid: "$_id" }, pipeline: ['
    '      { $match: { $expr: { $and: [ { $eq: ["$account_id", "$$aid"] },'
    '        { $eq: ["$type", "PRIJEM"] } ] } } },'
    '      { $group: { _id: null, credit_sum: { $sum: "$amount" } } } ], as: "_credit" } },\n'
    '  { $addFields: { loan_to_credit_ratio: { $cond: [ { $ne: [ { $type: "$loan" }, "missing" ] },'
    '      { $divide: [ "$loan.amount", { $max: [ { $ifNull: [ { $arrayElemAt: ["$_credit.credit_sum", 0] }, 0 ] }, 1 ] } ] }, 0 ] } } },\n'
    '  { $project: { _credit: 0 } }\n])'
)


@pytest.fixture(scope="module")
def stub_settings() -> Settings:
    return Settings.from_env(overrides={"TEND_LLM_STUB": "1"}, run_id="pytest")


@pytest.fixture()
def logger(tmp_path: Path):
    return setup_logging(tmp_path / "run")


# --------------------------------------------------------------------------- #
# config + source
# --------------------------------------------------------------------------- #
def test_settings_from_env(stub_settings: Settings):
    assert stub_settings.stub is True
    assert stub_settings.paths.bird_root.exists()


def test_bird_source_financial(stub_settings: Settings):
    src = BirdSource(stub_settings.paths.bird_root)
    try:
        assert len(src.db_ids) == 11
        fin = src.schema("financial")
        assert fin.domain == "finance" and fin.table_count == 8
        loan_fk = next(fk for fk in fin.foreign_keys
                       if fk.child_table == "loan" and fk.parent_table == "account")
        assert abs(src.fk_coverage("financial", loan_fk) - 0.152) < 0.01
        assert src.distinct_count("financial", "trans", "type") == 3
        assert len(src.workload("financial")) == 32
    finally:
        src.close()


# --------------------------------------------------------------------------- #
# execution layer
# --------------------------------------------------------------------------- #
def test_parse_and_disabled_scan():
    coll, pipeline = parse_pipeline(ANCHOR_MQL)
    assert coll == "account" and len(pipeline) == 3
    assert scan_disabled(ANCHOR_MQL) == []
    assert scan_disabled('db.x.aggregate([{ $out: "y" }])') == ["$out"]


def test_mql_signature_canonicalizes_equivalent_pipeline_syntax():
    compact = 'db.account.aggregate([{"$group":{"_id":"$frequency","count":{"$sum":1}}}])'
    js_style = "db.account.aggregate([ { $group: { _id: '$frequency', count: { $sum: 1 } } } ])"

    assert mql_signature(compact) == mql_signature(js_style)


def test_thin_cfs_derivation():
    cfs = derive_canonical_form_set(ANCHOR_MQL, "preserve")
    # RAR thin: only the structural invariant $lookup; replaceable idioms excluded
    assert cfs["must_contain"] == ["$lookup"]
    # C6: must_not_contain carries all 6 banned operators
    assert set(cfs["must_not_contain"]) == {
        "$$NOW", "$function", "$merge", "$out", "$rand", "$sample"}
    assert cfs["must_not_contain_at_root"] == ["$group", "$unwind"]


def test_mql_schema_ref_gate_allows_nested_and_transient_fields():
    from tend.agents.phase_b import (
        _computed_field_quality_reasons,
        _structural_schema_flex_reasons,
        _structural_schema_flex_result_reasons,
        _unknown_schema_refs,
    )

    schema = {
        "collections": {
            "account": {
                "_id": "INT",
                "loan": {
                    "type": "OBJECT",
                    "fields": {"amount": "REAL", "duration": "INT"},
                },
            },
            "trans": {"account_id": "INT", "amount": "REAL", "type": "TEXT"},
        }
    }
    assert _unknown_schema_refs(
        'db.account.aggregate([{ "$addFields": { "x": "$loan.amount" } }])',
        schema,
        "account",
    ) == []
    assert _unknown_schema_refs(
        'db.account.aggregate([{ "$addFields": { "x": "$loan.apr" } }])',
        schema,
        "account",
    ) == ["loan.apr"]
    # Fields produced inside the pipeline, including lookup aliases, are not source-schema refs.
    assert _unknown_schema_refs(
        'db.account.aggregate([{ "$lookup": { "from": "trans", "localField": "_id", '
        '"foreignField": "account_id", "as": "_credit" } }, '
        '{ "$addFields": { "ratio": { "$arrayElemAt": ["$_credit.amount", 0] } } }])',
        schema,
        "account",
    ) == []
    assert _unknown_schema_refs(
        'db.account.aggregate([{ "$project": { "docs": ['
        '{ "_id": "present", "value": "$loan.amount" }, '
        '{ "_id": "absent", "value": 0 } ] } }, '
        '{ "$unwind": "$docs" }, { "$replaceRoot": { "newRoot": "$docs" } }])',
        schema,
        "account",
    ) == []
    assert _unknown_schema_refs(
        'db.account.aggregate([{ "$lookup": { "from": "trans", "let": { "aid": "$_id" }, '
        '"pipeline": [{ "$match": { "$expr": { "$and": ['
        '{ "$eq": ["$account_id", "$$aid"] }, { "$eq": ["$type", "PRIJEM"] }] } } }, '
        '{ "$group": { "_id": null, "total": { "$sum": "$amount" } } }, '
        '{ "$project": { "_id": 0, "total_credit": "$total" } }], '
        '"as": "_credit" } }])',
        schema,
        "account",
    ) == []
    assert _unknown_schema_refs(
        'db.account.aggregate([{ "$lookup": { "from": "trans", '
        '"pipeline": [{ "$group": { "_id": null, "total": { "$sum": "$bogus" } } }], '
        '"as": "_credit" } }])',
        schema,
        "account",
    ) == ["trans.bogus"]
    assert _computed_field_quality_reasons(
        [{"_id": 1, "x": 1}, {"_id": 2, "x": None}, {"_id": 3}],
        {"_id"},
    ) == ["computed field 'x' produced null/missing values"]
    assert _computed_field_quality_reasons(
        [{"_id": 1, "x": []}],
        {"_id"},
    ) == ["computed field 'x' produced non-scalar values"]
    assert _computed_field_quality_reasons(
        [{"_id": 1, "x": 1, "tmp": 2}],
        {"_id"},
        {"x"},
    ) == [
        "preserve leaked helper fields: 'tmp'; final preserve output may only add 'x'"
    ]
    structural_inputs = {
        "target_sql_infeasibility_class": "structural_schema_flex",
        "target_schema_flex": "polymorphic",
        "intent": {"archetype": "schema_flex_variant_summary"},
    }
    assert _structural_schema_flex_reasons(
        'db.account.aggregate([{ "$addFields": { "x": "$loan.amount" } }])',
        schema,
        "account",
        structural_inputs,
    ) == [
        "target structural_schema_flex but 'account' has no __variants",
        "structural_schema_flex target requires $type or $objectToArray",
        "structural_schema_flex target requires explicit variant branch dispatch",
        "schema_flex_variant_summary target requires a $group summary",
    ]
    schema["collections"]["account"]["__variants"] = [{"discriminator": {"loan": "present"}}]
    assert _structural_schema_flex_reasons(
        'db.account.aggregate([{ "$addFields": { "variant": { "$cond": ['
        '{ "$eq": [{ "$type": "$loan" }, "missing"] }, "missing", "present"] } } }, '
        '{ "$group": { "_id": "$variant", "n": { "$sum": 1 } } }])',
        schema,
        "account",
        structural_inputs,
    ) == []
    structural_inputs["intent"]["analytical_op"] = {"target_field": "avg_loan_amount"}
    assert _structural_schema_flex_result_reasons(
        [{"variant_label": "present", "count": 1, "avg_loan_amount": 10}],
        structural_inputs,
    ) == ["schema_flex_variant_summary result must expose field 'variant'"]
    assert _structural_schema_flex_result_reasons(
        [{"variant": "has_loan", "count": 1, "avg_loan_amount": 10}],
        structural_inputs,
    ) == [
        "schema_flex_variant_summary result variant values must be exactly 'present' "
        "and 'missing', got ['has_loan']"
    ]
    assert _structural_schema_flex_result_reasons(
        [
            {"variant": "present", "count": 1, "avg_loan_amount": 10},
            {"variant": "missing", "count": 1, "avg_loan_amount": 0},
        ],
        structural_inputs,
    ) == []


def test_qps_contract_rejects_incomplete_oracle_and_null_preserve_default():
    from tend.agents.phase_b import QueryPlanSampler

    output = {
        "intent": {
            "shape_policy": "preserve",
            "analytical_op": {
                "target_field": "loan_amount",
                "missing_default": None,
            },
            "output": {"fields": ["_id", "loan_amount"]},
        },
        "reference_oracle": {
            "template": "present_missing_projection",
            "params": {
                "parent_collection": "account",
                "embed_field": "loan",
                "numerator_path": "loan.amount",
                "target_field": "loan_amount",
            },
        },
    }
    violations = QueryPlanSampler().check_contract(
        None,
        {
            "archetype": "subtype_cond_projection",
            "target_sql_infeasibility_class": "structural_schema_flex",
        },
        output,
    )

    assert (
        "preserve structural_schema_flex computed fields must use non-null "
        "missing/default values: analytical_op.missing_default"
    ) in violations
    assert (
        "reference_oracle.params missing required keys: ['denom']"
    ) in violations

    output["intent"]["analytical_op"] = {
        "target_field": "loan_amount",
        "output": {"missing": 0},
    }
    output["reference_oracle"]["params"]["denom"] = {
        "collection": "trans",
        "local_id": "_id",
        "foreign_field": "account_id",
        "sum_field": "amount",
    }
    violations = QueryPlanSampler().check_contract(
        None,
        {
            "archetype": "subtype_cond_projection",
            "target_sql_infeasibility_class": "structural_schema_flex",
        },
        output,
    )
    assert not any("missing/default" in v for v in violations)


def test_qps_prompt_includes_archetype_oracle_allowlist():
    from tend.agents.phase_b import QueryPlanSampler

    text = QueryPlanSampler().render_inputs(None, {
        "archetype": "has_vs_absent_compare",
        "target_sql_infeasibility_class": "structural_schema_flex",
        "schema": {
            "account": {
                "_id": "INT",
                "loan": {"type": "OBJECT", "fields": {"amount": "INT"}},
                "__variants": [{"discriminator": {"loan": "present"}}],
            }
        },
    })

    assert "has_vs_absent_compare params {parent_collection, embed_field" in text
    assert "Do not emit presence_count" in text


def test_qps_default_contract_accepts_output_and_oracle_defaults():
    from tend.agents.phase_b import QueryPlanSampler

    output = {
        "intent": {
            "shape_policy": "preserve",
            "analytical_op": {
                "target_field": "loan_to_credit_ratio",
                "output": {"missing_default": 0},
            },
        },
        "reference_oracle": {
            "template": "present_missing_projection",
            "params": {
                "parent_collection": "account",
                "embed_field": "loan",
                "numerator_path": "amount",
                "target_field": "loan_to_credit_ratio",
                "absent_value": 0,
                "denom": {
                    "collection": "trans",
                    "local_id": "_id",
                    "foreign_field": "account_id",
                    "sum_field": "amount",
                },
            },
        },
    }

    violations = QueryPlanSampler().check_contract(
        None,
        {
            "archetype": "present_missing_projection",
            "target_sql_infeasibility_class": "structural_schema_flex",
        },
        output,
    )

    assert not any("missing/default" in v for v in violations)


def test_ms_prompt_spells_out_present_missing_denominator_zero_value():
    from tend.agents.phase_b import MqlSynthesizer

    text = MqlSynthesizer().render_inputs(None, {
        "intent": {"shape_policy": "preserve"},
        "reference_oracle": {
            "template": "present_missing_projection",
            "params": {
                "target_field": "loan_to_credit_ratio",
                "absent_value": 0,
                "denom": {"zero_value": 1},
            },
        },
        "target_sql_infeasibility_class": "structural_schema_flex",
    })
    assert "use denom.zero_value (1) as the divisor, not 0 as the final answer" in text
    assert "representative MQL field itself must contain $type or $objectToArray" in text


def test_world_signature_deterministic():
    data = {"account": [{"_id": 1, "x": 1.0}, {"_id": 2}]}
    assert world_signature(data) == world_signature(json.loads(json.dumps(data)))
    assert world_signature(data) != world_signature({"account": [{"_id": 1, "x": 2.0}]})


def test_equiv_rec():
    a = [{"k": 1}, {"k": 2}]
    assert equiv_rec(a, list(reversed(a)), order_sensitive=False)
    assert not equiv_rec(a, list(reversed(a)), order_sensitive=True)
    assert not equiv_rec(a, [{"k": 1}], order_sensitive=False)


# --------------------------------------------------------------------------- #
# deterministic migration
# --------------------------------------------------------------------------- #
def test_migration_financial(stub_settings: Settings):
    from tend.construct import build_plan, migrate

    src = BirdSource(stub_settings.paths.bird_root)
    try:
        plan = build_plan(src, "financial")
        assert "account" in plan.roots
        data = migrate(src, "financial", plan)
        assert len(data["account"]) == 4500
        with_loan = sum(1 for d in data["account"] if "loan" in d)
        assert with_loan == 682              # BIRD ground truth: sparse optional embed
    finally:
        src.close()


def test_financial_dm_exposes_schema_less_features_and_large_seed_pool(
    stub_settings: Settings,
):
    from tend.agents.dm import DataMigrator
    from tend.cli import _artifact_slot_pool
    from tend.construct import build_plan, migrate
    from tend.workflow.flows import DbArtifacts

    src = BirdSource(stub_settings.paths.bird_root)
    try:
        plan = build_plan(src, "financial")
        data = migrate(src, "financial", plan)
        schema = DataMigrator()._derive_schema("financial", plan, data)
    finally:
        src.close()

    families = {
        feature["family"]
        for node in schema.values()
        if isinstance(node, dict)
        for feature in node.get("__schema_less_features", [])
    }
    assert {"optional_embed", "nested_array", "sparse_scalar"} <= families

    artifact = DbArtifacts(
        db_id="financial",
        mongodb_schema=schema,
        mongodb_data=data,
        rationale={},
        world_signature=world_signature(data),
        scenario_summary="financial benchmark",
        query_bearing=True,
    )
    pool = _artifact_slot_pool(artifact, seed=stub_settings.seed)
    assert len(pool) >= 100
    assert len({spec["diversity_key"] for spec in pool}) == len(pool)
    assert {"simple_filter", "topn", "join_nested_group"} <= {
        spec["archetype"] for spec in pool
    }


def test_complete_rationale_replaces_null_heterogenization():
    from tend.workflow.flows import _complete_rationale

    out = _complete_rationale(
        db_id="financial",
        rationale={"heterogenization": None},
        schema={"account": {"_id": "INT", "__variants": [{"discriminator": {"loan": "present"}}]}},
        migration_log={"embeds": {"account": ["loan"]}},
        source_schema=SimpleNamespace(tables={"account": object(), "loan": object()}),
        sc={"verdict": "accept", "issues": []},
    )
    assert out["heterogenization"]["schema_flex"] == "polymorphic"
    assert out["heterogenization"]["triggers"][0]["mechanism"] == "sparse"
    assert out["heterogenization"]["triggers"][0]["fired"] is True

    out = _complete_rationale(
        db_id="financial",
        rationale={
            "heterogenization": {
                "schema_flex": "sparse",
                "triggers": [
                    {"mechanism": "sparse", "fired": True, "evidence": "optional loan embed"}
                ],
            }
        },
        schema={"account": {"_id": "INT", "__variants": [{"discriminator": {"loan": "present"}}]}},
        migration_log={},
        source_schema=None,
        sc={"verdict": "accept", "issues": []},
    )
    assert out["heterogenization"]["schema_flex"] == "polymorphic"


def test_phase_a_sc_reviews_dm_artifacts_not_sra_schema(stub_settings, logger):
    from tend.workflow.flows import _phase_a_one_db

    actual_schema = {"account": {"_id": "INT", "loan": {"type": "OBJECT", "fields": {}}}}
    actual_data = {"account": [{"_id": 1, "loan": {"amount": 10}}, {"_id": 2}]}

    class _Source:
        def schema(self, db_id):
            return SimpleNamespace(
                domain="finance",
                sqlite_path=Path("/tmp/financial.sqlite"),
                table_count=2,
                tables=["account", "loan"],
            )

        def workload(self, db_id):
            return [
                SimpleNamespace(
                    question_id=1,
                    difficulty="simple",
                    question="Which accounts have loans?",
                    evidence="loan table is queried with account",
                    sql="SELECT account_id FROM loan",
                )
            ]

    class _WF:
        def __init__(self):
            self.ctx = AgentContext(settings=stub_settings, llm=None, log=logger, source=_Source())
            self.sc_inputs = []

        def context(self, **fields):
            return self.ctx.bind(**fields)

        async def agent(self, agent_id, inputs, ctx=None):
            if agent_id == "wp":
                return {
                    "scenario_summary": "finance workload with loan-bearing account questions",
                    "access_patterns": [{"path": "account->loan"}],
                    "design_constraints": [],
                }
            if agent_id == "sra":
                return {
                    "mongodb_schema": {"stale_sra": {"made_up": "TEXT"}},
                    "agent_design_rationale": {"decisions": [{"id": "D01"}]},
                }
            if agent_id == "dm":
                return {
                    "mongodb_schema": actual_schema,
                    "mongodb_data": actual_data,
                    "world_signature": "sha256:" + "1" * 64,
                    "migration_log": {"embeds": {"account": ["loan"]}, "references": []},
                }
            if agent_id == "sc":
                self.sc_inputs.append(inputs)
                return {
                    "verdict": "pass",
                    "query_bearing": True,
                    "issues": [],
                    "coverage_gaps": [],
                    "suggested_fixes": [],
                }
            raise AssertionError(agent_id)

    wf = _WF()
    art = asyncio.run(_phase_a_one_db(wf, "financial"))

    assert art.mongodb_schema == actual_schema
    assert art.mongodb_data == actual_data
    assert wf.sc_inputs
    assert wf.sc_inputs[0]["mongodb_schema"] == actual_schema
    assert wf.sc_inputs[0]["mongodb_data"] == actual_data
    assert "schema" not in wf.sc_inputs[0]
    assert wf.sc_inputs[0]["query_evidence"][0]["sql"] == "SELECT account_id FROM loan"


def test_coverage_request_adapter_targets_l4_schema_flex():
    from tend.cli import _slot_from_request
    from tend.source.census import CoverageRequest
    from tend.workflow.flows import _target_violations

    slot = _slot_from_request(
        CoverageRequest(
            db_id="financial",
            mechanism="sparse_embed",
            archetype="present_missing_projection",
            target_difficulty="L4",
            sql_infeasibility_class="structural_schema_flex",
            shape_policy="preserve",
        ),
        record_id=1001,
    )
    assert slot.db_id == "financial"
    assert slot.record_id == 1001
    assert slot.target_difficulty == "L4"
    assert slot.target_sql_infeasibility_class == "structural_schema_flex"
    assert slot.target_schema_flex == "polymorphic"
    assert _target_violations(
        slot,
        {"difficulty": "L0", "sql_infeasibility_class": "feasible"},
        {"schema_flex": "none"},
    ) == [
        "difficulty 'L0' != target 'L4'",
        "sql_infeasibility_class 'feasible' != target 'structural_schema_flex'",
        "schema_flex 'none' != target 'polymorphic'",
    ]


def test_artifact_diversity_planner_populates_seeded_slot_metadata():
    from tend.cli import _artifact_diversity_slots_for
    from tend.workflow.flows import DbArtifacts

    artifact = DbArtifacts(
        db_id="financial",
        mongodb_schema={
            "account": {
                "_id": "INT",
                "frequency": "TEXT",
                "loan": {"type": "OBJECT", "fields": {"amount": "REAL"}},
                "trans": {
                    "type": "ARRAY",
                    "items": {"type": "OBJECT", "fields": {"amount": "REAL", "type": "TEXT"}},
                },
                "__variants": [
                    {"discriminator": {"loan": "present"}, "fields": {}},
                    {"discriminator": {"loan": "missing"}, "fields": {}},
                    {"discriminator": {"frequency": "present"}, "fields": {}},
                    {"discriminator": {"frequency": "missing"}, "fields": {}},
                ],
            }
        },
        mongodb_data={
            "account": [
                {"_id": 1, "frequency": "monthly", "loan": {"amount": 10.0}},
                {"_id": 2, "frequency": "weekly", "trans": [{"amount": 3.0, "type": "PRIJEM"}]},
            ]
        },
        rationale={},
        world_signature="sha256:" + "4" * 64,
        scenario_summary="financial schema-flex",
        query_bearing=True,
    )

    slots = _artifact_diversity_slots_for(
        {"financial": artifact},
        n_records=3,
        seed=7,
        records_per_db=3,
    )

    assert len(slots) == 3
    assert [slot.record_id for slot in slots] == [1001, 1002, 1003]
    assert [slot.slot_index for slot in slots] == [0, 1, 2]
    assert all(slot.db_id == "financial" for slot in slots)
    assert all(slot.reference_oracle_seed for slot in slots)
    assert all(slot.diversity_key and slot.schema_feature for slot in slots)


def test_build_record_preserves_qps_reference_oracle_into_ms(stub_settings, logger):
    from tend.workflow.flows import CoverageSlot, DbArtifacts, _build_record

    reference = {
        "template": "group_count",
        "params": {"collection": "account", "group_by": "frequency"},
    }
    data = {"account": [{"_id": 1, "frequency": "monthly"}]}
    ms_inputs = []

    class _WF:
        def __init__(self):
            self.ctx = AgentContext(settings=stub_settings, llm=None, log=logger)

        def context(self, **fields):
            return self.ctx.bind(**fields)

        async def agent(self, agent_id, inputs, ctx=None):
            if agent_id == "qps":
                return {
                    "intent": {
                        "seed_mechanism": "baseline",
                        "archetype": "group_count",
                        "shape_policy": "reduce",
                        "analytical_op": {"group_by": "frequency"},
                    },
                    "reference_oracle": reference,
                }
            if agent_id == "ms":
                ms_inputs.append(inputs)
                return {
                    "gold_locked": True,
                    "MQL": "db.account.aggregate([])",
                    "canonical_form_set": {"must_contain": []},
                    "shape_policy": "reduce",
                    "schema_flex": "polymorphic",
                }
            if agent_id == "mut":
                return {"mutations": [{"mutation_id": f"m{i}", "MQL": "x"} for i in range(5)]}
            if agent_id == "pv":
                return {"pv_pass": True, "property_verification": {}}
            if agent_id == "nlp":
                return {"nl_queries": {"canonical": "Group accounts.", "colloquial": "Group them."}}
            if agent_id == "rtv":
                return {"rtv_pass": True}
            if agent_id == "nnc":
                return {
                    "gate_pass": True,
                    "difficulty": "L4",
                    "sql_infeasibility_class": "structural_schema_flex",
                }
            if agent_id == "ra":
                return {"ra_pass": True}
            raise AssertionError(agent_id)

    artifacts = {
        "financial": DbArtifacts(
            db_id="financial",
            mongodb_schema={"account": {"_id": "INT", "frequency": "TEXT"}},
            mongodb_data=data,
            rationale={},
            world_signature="sha256:" + "2" * 64,
            scenario_summary="finance account grouping",
            query_bearing=True,
        )
    }
    slot = CoverageSlot(
        db_id="financial",
        mechanism="baseline",
        archetype="group_count",
        record_id=42,
        target_difficulty="L4",
        target_sql_infeasibility_class="structural_schema_flex",
        target_schema_flex="polymorphic",
    )

    record = asyncio.run(_build_record(_WF(), artifacts, slot))

    assert record is not None
    assert record["mechanism"] == "baseline"
    assert record["archetype"] == "group_count"
    assert record["mql_signature"] == mql_signature(record["MQL"])
    assert ms_inputs
    assert ms_inputs[0]["reference_oracle"] == reference
    assert ms_inputs[0]["intent"]["reference_oracle"] == reference
    assert ms_inputs[0]["mongodb_data"] == data


def test_build_record_rejects_duplicate_mql_before_side_branches(stub_settings, logger):
    from tend.workflow.flows import CoverageSlot, DbArtifacts, _build_record

    reference = {
        "template": "group_count",
        "params": {"collection": "account", "group_by": "frequency"},
    }
    mql = 'db.account.aggregate([{ "$group": { "_id": "$frequency", "count": { "$sum": 1 } } }])'
    calls: dict[str, int] = {}

    class _WF:
        def __init__(self):
            self.ctx = AgentContext(settings=stub_settings, llm=None, log=logger)

        def context(self, **fields):
            return self.ctx.bind(**fields)

        async def agent(self, agent_id, inputs, ctx=None):
            calls[agent_id] = calls.get(agent_id, 0) + 1
            if agent_id == "qps":
                return {
                    "intent": {
                        "seed_mechanism": "baseline",
                        "archetype": "group_count",
                        "shape_policy": "reduce",
                    },
                    "reference_oracle": reference,
                }
            if agent_id == "ms":
                return {
                    "gold_locked": True,
                    "MQL": mql,
                    "canonical_form_set": {"must_contain": []},
                    "shape_policy": "reduce",
                    "schema_flex": "polymorphic",
                }
            if agent_id == "mut":
                return {"mutations": [{"mutation_id": f"m{i}", "MQL": "x"} for i in range(5)]}
            if agent_id == "pv":
                return {"pv_pass": True, "property_verification": {}}
            if agent_id == "nlp":
                return {"nl_queries": {"canonical": "Group accounts.", "colloquial": "Group them."}}
            if agent_id == "rtv":
                return {"rtv_pass": True}
            if agent_id == "nnc":
                return {
                    "gate_pass": True,
                    "difficulty": "L4",
                    "sql_infeasibility_class": "structural_schema_flex",
                }
            if agent_id == "ra":
                return {"ra_pass": True}
            raise AssertionError(agent_id)

    artifacts = {
        "financial": DbArtifacts(
            db_id="financial",
            mongodb_schema={"account": {"_id": "INT", "frequency": "TEXT"}},
            mongodb_data={"account": [{"_id": 1, "frequency": "monthly"}]},
            rationale={},
            world_signature="sha256:" + "3" * 64,
            scenario_summary="finance account grouping",
            query_bearing=True,
        )
    }
    seen: dict[tuple[str, str], int] = {}

    async def run():
        lock = asyncio.Lock()
        first = await _build_record(
            _WF(),
            artifacts,
            CoverageSlot("financial", "none", "group_count", 1001),
            seen_mql=seen,
            mql_lock=lock,
        )
        second = await _build_record(
            _WF(),
            artifacts,
            CoverageSlot("financial", "none", "group_count", 1002),
            seen_mql=seen,
            mql_lock=lock,
        )
        return first, second

    first, second = asyncio.run(run())

    assert first is not None
    assert first["mql_signature"] == mql_signature(mql)
    assert second is None
    assert calls["qps"] == 2 and calls["ms"] == 2
    assert calls["mut"] == 1 and calls["nlp"] == 1 and calls["ra"] == 1
    events = [
        json.loads(line) for line in (logger.run_dir / "events.jsonl").read_text().splitlines()
    ]
    dup = next(e for e in events if e["event"] == "duplicate_mql_rejected")
    assert dup["record_id"] == 1002
    assert dup["duplicate_of_record_id"] == 1001
    assert dup["mql_signature"] == mql_signature(mql)
    drop = next(e for e in events if e["event"] == "record_dropped")
    assert (
        drop["reason"] == "duplicate MQL rejected"
        and drop["duplicate_of_record_id"] == 1001
    )
    assert (logger.run_dir / "anomalies.jsonl").read_text() == ""


def test_nlp_reflux_respects_reshape_shape_policy(stub_settings, logger):
    from tend.agents.phase_b import NlParaphraser, _nl_shape_contract_violations

    intent = {
        "archetype": "schema_flex_variant_summary",
        "shape_policy": "reshape",
        "output": {"fields": ["variant", "count", "avg_loan_amount"]},
    }
    ctx = AgentContext(settings=stub_settings, llm=None, log=logger)
    prompt = NlParaphraser().render_inputs(
        ctx,
        {
            "intent": intent,
            "rtv_feedback": {
                "rtv_reason": "round-trip MQL is not equivalent to gold "
                              "(round_trip_rows=4500, gold_rows=2)"
            },
        },
    )
    assert "reshape/reduce task" in prompt
    assert "exact literal values 'present' and 'missing'" in prompt
    assert "do not rewrite it as a per-document add-field task" in prompt
    assert "For this preserve task" not in prompt
    assert _nl_shape_contract_violations(
        intent,
        "Add a field named avg_loan_amount to each document and keep all other fields unchanged.",
    ) == [
        "reshape canonical NLQ must not describe preserve/add-field semantics",
        "schema_flex_variant_summary canonical NLQ must name output fields: variant, count",
        "schema_flex_variant_summary canonical NLQ must name exact variant label 'present'",
        "schema_flex_variant_summary canonical NLQ must name exact variant label 'missing'",
    ]
    assert _nl_shape_contract_violations(
        intent,
        "Summarize account documents by loan presence variant and output variant, count, "
        "and avg_loan_amount, using exact variant labels present and missing and 0 for "
        "missing loan amounts.",
    ) == []


def test_stub_nlp_respects_reduce_shape_contract():
    from tend.agents.phase_b import _nl_shape_contract_violations
    from tend.stubs import stub_fn

    intent = {
        "shape_policy": "reduce",
        "archetype": "group_count",
        "seed_signal": {"collection": "account", "field": "frequency"},
        "output": {"fields": ["frequency", "count"]},
    }
    out = stub_fn(
        "nlp",
        [{"role": "user", "content": "## intent\n```json\n" + json.dumps(intent) + "\n```"}],
        None,
    )
    canonical = out["nl_queries"]["canonical"]

    assert _nl_shape_contract_violations(intent, canonical) == []
    assert "each document" not in canonical.lower()


# --------------------------------------------------------------------------- #
# logging + anomaly capture
# --------------------------------------------------------------------------- #
def test_logging_anomaly_stream(tmp_path: Path):
    log = setup_logging(tmp_path / "run")
    alog = log.bind(db_id="financial", agent="ms", record_id=1001)
    alog.info("hello")
    ref = alog.save_transcript("ms", "c1", {"messages": [], "response": "x"})
    diagnostics_ref = ref.replace(".md", ".diagnostics.json")
    alog.anomaly(SchemaValidationError("missing MQL", context={"missing": ["MQL"]}),
                 transcript_ref=ref)
    log.close()
    events = [json.loads(line) for line in (tmp_path / "run" / "events.jsonl").read_text().splitlines()]
    anoms = [json.loads(line) for line in (tmp_path / "run" / "anomalies.jsonl").read_text().splitlines()]
    assert len(anoms) == 1 and anoms[0]["anomaly"] == "schema_invalid"
    assert anoms[0]["transcript_ref"] == ref and anoms[0]["missing"] == ["MQL"]
    assert anoms[0]["diagnostics_ref"] == diagnostics_ref
    assert len(events) >= 2
    assert ref == "llm/ms/c1.md"
    assert (tmp_path / "run" / diagnostics_ref).exists()
    transcript_md = (tmp_path / "run" / ref).read_text(encoding="utf-8")
    assert "# LLM Call: c1" in transcript_md
    assert "Full structured payload: `llm/ms/c1.diagnostics.json`" in transcript_md


# --------------------------------------------------------------------------- #
# LLM client (stub)
# --------------------------------------------------------------------------- #
def test_llm_schema_validation_and_prompt_anomaly(stub_settings, logger):
    client = LLMClient(stub_settings, logger)
    schema = {"type": "object", "required": ["foo"],
              "properties": {"foo": {"type": "string"}}, "additionalProperties": False}

    async def run():
        client.set_stub(lambda a, m, s: {"foo": "ok"})
        r = await client.complete(agent="t", messages=[{"role": "user", "content": "go"}],
                                  schema=schema)
        assert r.data == {"foo": "ok"}
        assert r.transcript_ref.endswith(".md")
        transcript_md = (logger.run_dir / r.transcript_ref).read_text(encoding="utf-8")
        assert "## Messages" in transcript_md
        assert "> go" in transcript_md
        assert "### Content" in transcript_md
        diagnostics_ref = r.diagnostics_ref
        assert diagnostics_ref.endswith(".diagnostics.json")
        transcript = json.loads((logger.run_dir / diagnostics_ref).read_text(encoding="utf-8"))
        assert transcript["messages"] == [{"role": "user", "content": "go"}]
        assert transcript["response_text"] == '{"foo": "ok"}'
        assert transcript["parsed"] == {"foo": "ok"}

        client.set_stub(lambda a, m, s: {"bad": 1})
        with pytest.raises(SchemaValidationError):
            await client.complete(agent="t", messages=[{"role": "user", "content": "go"}],
                                  schema=schema, json_repair_retries=1)
        anomalies = [
            json.loads(line) for line in (logger.run_dir / "anomalies.jsonl").read_text().splitlines()
        ]
        schema_anomaly = next(a for a in reversed(anomalies) if a["anomaly"] == "schema_invalid")
        assert schema_anomaly["transcript_ref"].endswith(".md")
        assert schema_anomaly["diagnostics_ref"].endswith(".diagnostics.json")
        assert schema_anomaly["context"]["diagnostics_ref"] == schema_anomaly["diagnostics_ref"]
        failed_md = (logger.run_dir / schema_anomaly["transcript_ref"]).read_text(
            encoding="utf-8"
        )
        assert "output failed schema validation" in failed_md
        failed_diagnostics_ref = schema_anomaly["diagnostics_ref"]
        failed = json.loads((logger.run_dir / failed_diagnostics_ref).read_text(
            encoding="utf-8"
        ))
        assert failed["messages"][0] == {"role": "user", "content": "go"}
        assert any(attempt.get("response") == '{"bad": 1}' for attempt in failed["attempts"])

        with pytest.raises(PromptAnomalyError):
            await client.complete(agent="t", messages=[{"role": "user", "content": "  "}])

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# agent base + workflow engine
# --------------------------------------------------------------------------- #
def test_workflow_parallel_pipeline_isolation(stub_settings, logger):
    @register
    class _Echo(Agent):
        id = "t_echo"
        phase = "B"
        title = "echo"

        async def run(self, ctx, inputs):
            if inputs["n"] == 3:
                raise GateError("boom", context={})
            return {"n": inputs["n"], "sq": inputs["n"] ** 2}

    client = LLMClient(stub_settings, logger)
    ctx = AgentContext(settings=stub_settings, llm=client, log=logger)
    wf = Workflow(ctx)

    async def run():
        outs = await wf.parallel([lambda i=i: wf.agent("t_echo", {"n": i}) for i in range(5)],
                                 isolate=True)
        assert [o["n"] if o else None for o in outs] == [0, 1, 2, None, 4]
        res = await wf.pipeline([2], lambda n: wf.agent("t_echo", {"n": n}),
                                lambda r: wf.agent("t_echo", {"n": r["sq"]}))
        assert res[0]["sq"] == 16

    asyncio.run(run())


def test_llm_agent_retries_retryable_postprocess_errors(stub_settings, logger):
    class _RetryPostprocess(LLMAgent):
        id = "t_retry_postprocess"
        phase = "B"
        title = "retry postprocess"
        prompt_file = "unused.md"
        contract_retries = 1
        output_schema = {
            "type": "object",
            "required": ["MQL"],
            "properties": {"MQL": {"type": "string"}},
            "additionalProperties": True,
        }

        def prompt_text(self, ctx):
            return "return a Mongo query"

        def postprocess(self, ctx, inputs, output, result):
            if output["MQL"] == "bad":
                raise ResponseParseError("bad generated MQL", context={"preview": "bad"})
            return {"MQL": output["MQL"], "ok": True}

    responses = iter([{"MQL": "bad"}, {"MQL": "good"}])
    client = LLMClient(stub_settings, logger)
    client.set_stub(lambda agent, messages, schema: next(responses))
    ctx = AgentContext(settings=stub_settings, llm=client, log=logger, phase="B")

    out = asyncio.run(_RetryPostprocess()(ctx, {}))
    assert out == {"MQL": "good", "ok": True}

    events = [
        json.loads(line) for line in (logger.run_dir / "events.jsonl").read_text().splitlines()
    ]
    retry = next(e for e in events if e["event"] == "agent_postprocess_retry")
    assert retry["error_type"] == "ResponseParseError"
    assert retry["reason"] == "bad generated MQL"
    assert retry["transcript_ref"].endswith(".md")
    assert retry["diagnostics_ref"].endswith(".diagnostics.json")
    anomalies = (logger.run_dir / "anomalies.jsonl").read_text()
    assert "bad generated MQL" not in anomalies


def test_llm_agent_final_postprocess_failure_keeps_transcript_refs(stub_settings, logger):
    class _FinalPostprocessFailure(LLMAgent):
        id = "t_final_postprocess_failure"
        phase = "B"
        title = "final postprocess failure"
        prompt_file = "unused.md"
        contract_retries = 0
        output_schema = {
            "type": "object",
            "required": ["MQL"],
            "properties": {"MQL": {"type": "string"}},
            "additionalProperties": True,
        }

        def prompt_text(self, ctx):
            return "return a Mongo query"

        def postprocess(self, ctx, inputs, output, result):
            raise ResponseParseError(
                "bad generated MQL",
                context={"preview": output["MQL"]},
            )

    client = LLMClient(stub_settings, logger)
    client.set_stub(lambda agent, messages, schema: {"MQL": "bad"})
    ctx = AgentContext(settings=stub_settings, llm=client, log=logger, phase="B")

    with pytest.raises(ResponseParseError) as excinfo:
        asyncio.run(_FinalPostprocessFailure()(ctx, {"source": "unit"}))

    assert excinfo.value.context["agent"] == "t_final_postprocess_failure"
    assert excinfo.value.context["transcript_ref"].endswith(".md")
    assert excinfo.value.context["diagnostics_ref"].endswith(".diagnostics.json")
    anomalies = [
        json.loads(line) for line in (logger.run_dir / "anomalies.jsonl").read_text().splitlines()
    ]
    anomaly = next(a for a in anomalies if a["message"] == "bad generated MQL")
    assert anomaly["transcript_ref"].endswith(".md")
    assert anomaly["diagnostics_ref"].endswith(".diagnostics.json")
    assert anomaly["context"]["agent"] == "t_final_postprocess_failure"
    assert anomaly["context"]["preview"] == "bad"


def test_phase_b_parse_errors_are_gate_failures(stub_settings, logger):
    from tend.agents.phase_b import PropertyVerifier, RoundTripVerifier

    class _Mongo:
        def available(self):
            return True

        def norm_exec(self, db_id, mql):
            if "bad_parse" in mql:
                raise ResponseParseError("bad generated MQL")
            if "gold" in mql or "same" in mql:
                return [{"_id": 1, "x": 1}]
            return [{"_id": 1, "x": 2}]

    ctx = AgentContext(
        settings=replace(stub_settings, stub=False),
        llm=None,
        log=logger,
        mongo=_Mongo(),
        db_id="financial",
        record_id=1001,
        phase="B",
    )

    async def run_pv():
        return await PropertyVerifier().run(ctx, {
            "MQL": "gold",
            "mutations": [{"mutation_id": f"m{i}", "MQL": "bad_parse"} for i in range(5)],
        })

    pv = asyncio.run(run_pv())
    assert pv["pv_pass"] is True
    assert len(pv["verified_mutations"]) == 5
    events = [
        json.loads(line) for line in (logger.run_dir / "events.jsonl").read_text().splitlines()
    ]
    exec_fail_events = [e for e in events if e["event"] == "pv_mutation_exec_fail"]
    assert len(exec_fail_events) == 5
    assert exec_fail_events[0]["mutation_id"] == "m0"
    assert (logger.run_dir / "anomalies.jsonl").read_text() == ""

    rtv = RoundTripVerifier().postprocess(
        ctx,
        {"MQL": "gold"},
        {"mql_round_trip_canonical": "bad_parse"},
        result=None,
    )
    assert rtv["rtv_pass"] is False
    assert rtv["rtv_reason"] == "bad generated MQL"
    rtv = RoundTripVerifier().postprocess(
        ctx,
        {"MQL": "gold"},
        {"mql_round_trip_canonical": "different"},
        result=None,
    )
    assert rtv["rtv_pass"] is False
    assert rtv["rtv_reason"] == (
        "round-trip MQL is not equivalent to gold (round_trip_rows=1, gold_rows=1)"
    )


def test_ms_oracle_divergence_prevents_gold_lock(stub_settings, logger):
    from tend.agents.phase_b import MqlSynthesizer

    class _Mongo:
        def available(self):
            return True

        def norm_exec(self, db_id, mql):
            return [{"_id": "monthly", "count": 2}]

        def count(self, db_id, collection):
            return 2

        def sample_fields(self, db_id, collection):
            return {"_id", "frequency"}

    reference = {
        "template": "group_count",
        "params": {"collection": "account", "group_by": "frequency"},
    }
    ctx = AgentContext(
        settings=replace(stub_settings, stub=False),
        llm=None,
        log=logger,
        mongo=_Mongo(),
        db_id="financial",
        record_id=1002,
        phase="B",
    )
    out = MqlSynthesizer().postprocess(
        ctx,
        {
            "intent": {"shape_policy": "reduce", "reference_oracle": reference},
            "reference_oracle": reference,
            "schema": {"account": {"_id": "INT", "frequency": "TEXT"}},
            "mongodb_data": {
                "account": [
                    {"_id": 1, "frequency": "monthly"},
                    {"_id": 2, "frequency": "weekly"},
                ]
            },
            "target_sql_infeasibility_class": "feasible",
            "target_schema_flex": "none",
        },
        {
            "MQL": (
                'db.account.aggregate([{ "$group": { "_id": "$frequency", '
                '"count": { "$sum": 1 } } }])'
            ),
            "shape_policy": "reduce",
        },
        result=None,
    )

    assert out["gold_locked"] is False
    assert "reference_oracle divergence" in out["gold_lock_reason"]
    assert "group_count" in out["gold_lock_reason"]


def test_ms_oracle_divergence_reports_target_field_mismatch(stub_settings, logger):
    from tend.agents.phase_b import MqlSynthesizer

    class _Mongo:
        def available(self):
            return True

        def norm_exec(self, db_id, mql):
            return [{"_id": 1, "loan": {"amount": 90}, "computed_loan_amount": 100}]

        def count(self, db_id, collection):
            return 1

        def sample_fields(self, db_id, collection):
            return {"_id", "loan"}

    reference = {
        "template": "optional_embed_projection",
        "params": {
            "parent_collection": "account",
            "embed_field": "loan",
            "value_path": "amount",
            "target_field": "computed_loan_amount",
            "missing_default": 0,
        },
    }
    ctx = AgentContext(
        settings=replace(stub_settings, stub=False),
        llm=None,
        log=logger,
        mongo=_Mongo(),
        db_id="financial",
        record_id=1003,
        phase="B",
    )
    out = MqlSynthesizer().postprocess(
        ctx,
        {
            "intent": {"shape_policy": "preserve", "reference_oracle": reference},
            "reference_oracle": reference,
            "schema": {"account": {"_id": "INT", "loan": {"amount": "INT"}}},
            "mongodb_data": {"account": [{"_id": 1, "loan": {"amount": 90}}]},
            "target_sql_infeasibility_class": "feasible",
            "target_schema_flex": "none",
        },
        {
            "MQL": 'db.account.aggregate([{ "$addFields": { "computed_loan_amount": 100 } }])',
            "shape_policy": "preserve",
        },
        result=None,
    )

    assert out["gold_locked"] is False
    assert (
        "first_mismatch _id=1 field='computed_loan_amount' mql=100 oracle=90"
        in out["gold_lock_reason"]
    )


def test_ms_promotes_structural_alt_before_gold_lock(stub_settings, logger):
    from tend.agents.phase_b import MqlSynthesizer

    calls = []

    class _Mongo:
        def available(self):
            return True

        def norm_exec(self, db_id, mql):
            calls.append(mql)
            return [{"_id": 1, "loan": {"amount": 90}, "computed_loan_amount": 90}]

        def count(self, db_id, collection):
            return 1

        def sample_fields(self, db_id, collection):
            return {"_id", "loan"}

    reference = {
        "template": "optional_embed_projection",
        "params": {
            "parent_collection": "account",
            "embed_field": "loan",
            "value_path": "amount",
            "target_field": "computed_loan_amount",
            "missing_default": 0,
        },
    }
    schema = {
        "account": {
            "_id": "INT",
            "loan": {"type": "OBJECT", "fields": {"amount": "INT"}},
            "__variants": [{"discriminator": {"loan": "present"}}],
        }
    }
    primary = 'db.account.aggregate([{ "$addFields": { "computed_loan_amount": "$loan.amount" } }])'
    alt = (
        'db.account.aggregate([{ "$addFields": { "computed_loan_amount": { "$cond": ['
        '{ "$eq": [{ "$type": "$loan" }, "missing"] }, 0, "$loan.amount"] } } }])'
    )
    ctx = AgentContext(
        settings=replace(stub_settings, stub=False),
        llm=None,
        log=logger,
        mongo=_Mongo(),
        db_id="financial",
        record_id=1004,
        phase="B",
    )

    out = MqlSynthesizer().postprocess(
        ctx,
        {
            "intent": {"shape_policy": "preserve", "reference_oracle": reference},
            "reference_oracle": reference,
            "schema": schema,
            "mongodb_data": {"account": [{"_id": 1, "loan": {"amount": 90}}]},
            "target_sql_infeasibility_class": "structural_schema_flex",
            "target_schema_flex": "polymorphic",
        },
        {"MQL": primary, "mql_alt": alt, "shape_policy": "preserve"},
        result=None,
    )

    assert out["gold_locked"] is True
    # the LLM alt is still promoted over the primary (recorded as provenance), but the
    # deterministic canonical gold for this archetype now supersedes it as the locked MQL
    assert out["representative_mql_promoted_from_alt"] is True
    assert out["reference_oracle_canonicalized"] is True
    assert out["llm_MQL"] == alt
    assert out["MQL"] == calls[0]  # the canonical gold is what gets gold-locked


def test_ms_canonicalizes_has_vs_absent_compare(stub_settings, logger):
    from tend.agents.phase_b import MqlSynthesizer

    calls = []

    class _Mongo:
        def available(self):
            return True

        def norm_exec(self, db_id, mql):
            calls.append(mql)
            assert "__tend_presence" in mql
            return [{"_id": "present", "value": 80}, {"_id": "absent", "value": 0}]

        def count(self, db_id, collection):
            return 3

        def sample_fields(self, db_id, collection):
            return {"_id", "loan"}

    reference = {
        "template": "has_vs_absent_compare",
        "params": {
            "parent_collection": "account",
            "embed_field": "loan",
            "metric_field": "amount",
            "agg": "avg",
        },
    }
    ctx = AgentContext(
        settings=replace(stub_settings, stub=False),
        llm=None,
        log=logger,
        mongo=_Mongo(),
        db_id="financial",
        record_id=1014,
        phase="B",
    )

    out = MqlSynthesizer().postprocess(
        ctx,
        {
            "intent": {"shape_policy": "reduce", "reference_oracle": reference},
            "reference_oracle": reference,
            "schema": {
                "account": {
                    "_id": "INT",
                    "loan": {"type": "OBJECT", "fields": {"loan_id": "INT", "amount": "INT"}},
                    "__variants": [{"discriminator": {"loan": "present"}}],
                }
            },
            "mongodb_data": {
                "account": [
                    {"_id": 1, "loan": {"amount": 100}},
                    {"_id": 2},
                    {"_id": 3, "loan": {"amount": 60}},
                ]
            },
            "target_sql_infeasibility_class": "structural_schema_flex",
            "target_schema_flex": "polymorphic",
        },
        {
            "MQL": 'db.account.aggregate([{ "$project": { "branch": "bad" } }])',
            "shape_policy": "reduce",
        },
        result=None,
    )

    assert out["gold_locked"] is True
    assert out["reference_oracle_canonicalized"] is True
    assert out["llm_MQL"].startswith("db.account.aggregate")
    assert calls and calls[0] == out["MQL"]


def test_ms_canonical_has_vs_absent_falls_back_to_embed_metric(stub_settings, logger):
    from tend.agents.phase_b import MqlSynthesizer

    calls = []

    class _Mongo:
        def available(self):
            return True

        def norm_exec(self, db_id, mql):
            calls.append(mql)
            assert "$loan.amount" in mql
            assert "$balance" not in mql
            return [{"_id": "present", "value": 100}, {"_id": "absent", "value": 0}]

        def count(self, db_id, collection):
            return 2

        def sample_fields(self, db_id, collection):
            return {"_id", "loan"}

    reference = {
        "template": "has_vs_absent_compare",
        "params": {
            "parent_collection": "account",
            "embed_field": "loan",
            "metric_field": "balance",
            "agg": "avg",
        },
    }
    ctx = AgentContext(
        settings=replace(stub_settings, stub=False),
        llm=None,
        log=logger,
        mongo=_Mongo(),
        db_id="financial",
        record_id=1030,
        phase="B",
    )

    out = MqlSynthesizer().postprocess(
        ctx,
        {
            "intent": {"shape_policy": "reduce", "reference_oracle": reference},
            "reference_oracle": reference,
            "schema": {
                "account": {
                    "_id": "INT",
                    "loan": {"type": "OBJECT", "fields": {"amount": "INT"}},
                    "__variants": [{"discriminator": {"loan": "present"}}],
                }
            },
            "mongodb_data": {
                "account": [{"_id": 1, "loan": {"amount": 100}}, {"_id": 2}]
            },
            "target_sql_infeasibility_class": "structural_schema_flex",
            "target_schema_flex": "polymorphic",
        },
        {"MQL": 'db.account.aggregate([])', "shape_policy": "reduce"},
        result=None,
    )

    assert out["gold_locked"] is True
    assert out["reference_oracle_canonicalized"] is True
    assert calls and calls[0] == out["MQL"]


_PMP_PARAMS = {
    "parent_collection": "account",
    "embed_field": "loan",
    "numerator_path": "loan.amount",
    "target_field": "ratio",
    "absent_value": 0,
    "denom": {
        "collection": "trans", "local_id": "_id", "foreign_field": "account_id",
        "sum_field": "amount", "zero_value": 1,
    },
}


def test_canonical_present_missing_projection_mql_structure():
    from tend.agents.phase_b import (
        _canonical_present_missing_projection_mql,
        _canonical_reference_mql,
    )

    mql = _canonical_present_missing_projection_mql(_PMP_PARAMS, {})
    coll, pipeline = parse_pipeline(mql)
    text = json.dumps(pipeline)
    assert coll == "account"
    # joins the denominator collection, branches on embed presence via $type/$cond,
    # divides numerator by the denom sum, and projects helper fields away
    assert '"from": "trans"' in text and '"foreignField": "account_id"' in text
    assert '"$type"' in text and '"$cond"' in text and '"$divide"' in text
    assert pipeline[-1] == {"$project": {"__tend_denom": 0, "__tend_denom_sum": 0}}
    assert '"ratio"' in text
    # dispatch routes the template to the builder and reports the implied shape_policy
    dispatched = _canonical_reference_mql(
        {"reference_oracle": {"template": "present_missing_projection", "params": _PMP_PARAMS}}
    )
    assert dispatched == (mql, "preserve")
    # unknown template -> no canonical gold (falls back to the LLM MQL)
    assert _canonical_reference_mql(
        {"reference_oracle": {"template": "totally_unknown", "params": {"collection": "x"}}}
    ) is None


def test_canonical_present_missing_projection_locks_against_oracle(stub_settings, logger):
    """The deterministic gold MQL is ≡_rec to the reference oracle, against live Mongo."""
    from tend.agents.phase_b import _canonical_present_missing_projection_mql
    from tend.execution.mongo import MongoExecutor, _normalize_doc, equiv_rec
    from tend.mechanisms.oracles import reference_oracle

    mongo = MongoExecutor(replace(stub_settings, stub=False), logger)
    if not mongo.available():
        pytest.skip("MongoDB not reachable")
    snapshot = {
        "account": [{"_id": 1, "loan": {"amount": 100}}, {"_id": 2}, {"_id": 3, "loan": {"amount": 60}}],
        "trans": [{"account_id": 1, "amount": 50}, {"account_id": 1, "amount": 50},
                  {"account_id": 3, "amount": 30}],
    }
    try:
        mongo.load_witness("pmp_fixture", snapshot)
        mql = _canonical_present_missing_projection_mql(_PMP_PARAMS, {})
        got = [_normalize_doc(d) for d in mongo.norm_exec("pmp_fixture", mql)]
        want = [_normalize_doc(d) for d in reference_oracle("present_missing_projection")(snapshot, _PMP_PARAMS)]
        assert equiv_rec(got, want, order_sensitive=False)
        ratios = {row["_id"]: row.get("ratio") for row in got}
        assert ratios == {1: 1.0, 2: 0, 3: 2.0}  # 100/100, absent_value, 60/30
    finally:
        mongo.close()


# (template, params, snapshot) — each canonical builder must be ≡_rec to its oracle.
_BUILDER_CASES = [
    ("simple_filter", {"collection": "c", "predicates": [{"field": "s", "op": "ne", "value": "B"}]},
     {"c": [{"_id": 1, "s": "A"}, {"_id": 2, "s": "B"}, {"_id": 3}]}),
    ("existence_count", {"collection": "c", "field": "loan"},
     {"c": [{"_id": 1, "loan": 1}, {"_id": 2}, {"_id": 3, "loan": None}]}),
    ("group_count", {"collection": "c", "group_by": "t"},
     {"c": [{"_id": 1, "t": "x"}, {"_id": 2, "t": "x"}, {"_id": 3}]}),
    ("null_coalesce_agg", {"collection": "c", "field": "v", "agg": "sum", "default": 0},
     {"c": [{"_id": 1, "v": 10}, {"_id": 2}, {"_id": 3, "v": "nan"}, {"_id": 4, "v": 5}]}),
    ("subtype_specific_field", {"collection": "c", "discriminator": "k", "subtype_value": "loan", "field": "amount"},
     {"c": [{"_id": 1, "k": "loan", "amount": 100}, {"_id": 2, "k": "card"}]}),
    ("cross_keyset_value", {"collection": "c", "key": "promo"},
     {"c": [{"_id": 1, "promo": "X"}, {"_id": 2}]}),
    ("dynamic_key_fold", {"collection": "c", "name_field": "a", "value_field": "v", "agg": "sum"},
     {"c": [{"_id": 1, "a": "h", "v": 3}, {"_id": 2, "a": "h", "v": "x"}, {"_id": 3, "a": "w", "v": 5}]}),
    ("cross_version_agg", {"collection": "c", "field_candidates": ["v2", "v1"], "agg": "sum", "default": 0},
     {"c": [{"_id": 1, "v1": 10}, {"_id": 2, "v2": 20}, {"_id": 3}]}),
    ("per_subtype_agg", {"collection": "c", "discriminator": "k", "field_by_subtype": {"loan": "amount", "card": "limit"}, "agg": "sum"},
     {"c": [{"_id": 1, "k": "loan", "amount": 100}, {"_id": 2, "k": "card", "limit": 200}, {"_id": 3, "k": "other"}]}),
    ("cross_subtype_compare", {"collection": "c", "discriminator": "k", "field_by_subtype": {"loan": "amount"}, "agg": "avg"},
     {"c": [{"_id": 1, "k": "loan", "amount": 100}, {"_id": 2, "k": "loan", "amount": 60}]}),
    ("join_nested_group", {"collection": "o", "array_field": "items", "group_by": "sku"},
     {"o": [{"_id": 1, "items": [{"sku": "a"}, {"sku": "a"}, {"sku": "b"}]}, {"_id": 2}]}),
    ("subtype_cond_projection", {"collection": "c", "discriminator": "k", "field_by_subtype": {"loan": "amount"}, "target_field": "val", "default": 0},
     {"c": [{"_id": 1, "k": "loan", "amount": 100}, {"_id": 2, "k": "other"}]}),
    ("optional_embed_projection", {"parent_collection": "a", "embed_field": "loan", "value_path": "amount", "target_field": "amt", "missing_default": 0},
     {"a": [{"_id": 1, "loan": {"amount": 500}}, {"_id": 2}]}),
    ("fk_rollup", {"parent_collection": "a", "child_collection": "c", "parent_key": "_id", "foreign_key": "aid", "agg": "sum", "value_field": "amt"},
     {"a": [{"_id": 1}, {"_id": 2}, {"_id": 3}], "c": [{"_id": 11, "aid": 1, "amt": 10}, {"_id": 12, "aid": 1, "amt": 20}, {"_id": 13, "aid": 2, "amt": 5}]}),
]

# archetypes the planner can tag structural_schema_flex must use $type/$objectToArray + $switch/$cond
_STRUCTURAL_ARCHETYPES = {
    "present_missing_projection", "has_vs_absent_compare", "optional_embed_projection",
    "subtype_cond_projection", "subtype_specific_field", "per_subtype_agg",
}


def test_canonical_builders_match_oracles(stub_settings, logger):
    """Every canonical gold builder is ≡_rec to its reference oracle (and structural ones
    carry the $type/$switch ops the MS gold-lock gate requires)."""
    from tend.agents.phase_b import _canonical_reference_mql, _pipeline_operator_set
    from tend.execution import parse_pipeline
    from tend.execution.mongo import MongoExecutor, _normalize_doc, equiv_rec
    from tend.mechanisms.oracles import reference_oracle

    mongo = MongoExecutor(replace(stub_settings, stub=False), logger)
    if not mongo.available():
        pytest.skip("MongoDB not reachable")
    try:
        for idx, (template, params, snap) in enumerate(_BUILDER_CASES):
            res = _canonical_reference_mql({"reference_oracle": {"template": template, "params": params}})
            assert res is not None, f"{template}: builder returned None"
            mql, _shape = res
            mongo.load_witness(f"cbm{idx}", snap)
            got = [_normalize_doc(d) for d in mongo.norm_exec(f"cbm{idx}", mql)]
            want = [_normalize_doc(d) for d in reference_oracle(template)(snap, params)]
            assert equiv_rec(got, want, order_sensitive=False), f"{template}: gold not ≡_rec oracle"
            if template in _STRUCTURAL_ARCHETYPES:
                ops = _pipeline_operator_set(parse_pipeline(mql)[1])
                assert {"$type", "$objectToArray"} & ops, f"{template}: needs $type/$objectToArray"
                assert {"$switch", "$cond"} & ops, f"{template}: needs $switch/$cond"
    finally:
        mongo.close()


# --------------------------------------------------------------------------- #
# integration: anchor MQL executes on migrated witness (needs MongoDB)
# --------------------------------------------------------------------------- #
def test_anchor_mql_executes(stub_settings, logger):
    from tend.construct import build_plan, migrate

    mongo = MongoExecutor(stub_settings, logger)
    if not mongo.available():
        pytest.skip("MongoDB not reachable")
    src = BirdSource(stub_settings.paths.bird_root)
    try:
        data = migrate(src, "financial", build_plan(src, "financial"))
        mongo.load_witness("financial", data)
        res = mongo.norm_exec("financial", ANCHOR_MQL)
        assert len(res) == 4500                       # preserve: one doc per account
        nonzero = [d for d in res if d.get("loan_to_credit_ratio", 0) != 0]
        assert len(nonzero) == 682                     # loan-present variant
    finally:
        src.close()
        mongo.close()
