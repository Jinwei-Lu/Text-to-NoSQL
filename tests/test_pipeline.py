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
    assert ms_inputs
    assert ms_inputs[0]["reference_oracle"] == reference
    assert ms_inputs[0]["intent"]["reference_oracle"] == reference
    assert ms_inputs[0]["mongodb_data"] == data


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
    wf = Workflow(ctx, max_concurrency=4)

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
    anomalies = (logger.run_dir / "anomalies.jsonl").read_text()
    assert "bad generated MQL" not in anomalies


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
