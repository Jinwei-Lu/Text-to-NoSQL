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
            }
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
    assert _computed_field_quality_reasons(
        [{"_id": 1, "x": 1}, {"_id": 2, "x": None}, {"_id": 3}],
        {"_id"},
    ) == ["computed field 'x' produced null/missing values"]
    assert _computed_field_quality_reasons(
        [{"_id": 1, "x": []}],
        {"_id"},
    ) == ["computed field 'x' produced non-scalar values"]
    structural_inputs = {
        "target_sql_infeasibility_class": "structural_schema_flex",
        "target_schema_flex": "polymorphic",
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
    assert out["heterogenization"]["triggers"][0]["fired"] is True


def test_slots_target_l4_schema_flex():
    from tend.cli import _slots_for
    from tend.workflow.flows import DbArtifacts, _target_violations

    artifacts = {
        "financial": DbArtifacts(
            db_id="financial",
            mongodb_schema={"account": {"_id": "INT", "__variants": [{}]}},
            mongodb_data={},
            rationale={},
            world_signature="sha256:" + "0" * 64,
            scenario_summary="",
            query_bearing=False,
        )
    }
    slots = _slots_for(artifacts, 2)
    assert [s.db_id for s in slots] == ["financial", "financial"]
    assert all(s.target_difficulty == "L4" for s in slots)
    assert all(s.target_sql_infeasibility_class == "structural_schema_flex" for s in slots)
    assert all(s.target_schema_flex == "polymorphic" for s in slots)
    assert _target_violations(
        slots[0],
        {"difficulty": "L0", "sql_infeasibility_class": "feasible"},
        {"schema_flex": "none"},
    ) == [
        "difficulty 'L0' != target 'L4'",
        "sql_infeasibility_class 'feasible' != target 'structural_schema_flex'",
        "schema_flex 'none' != target 'polymorphic'",
    ]


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
    assert "do not rewrite it as a per-document add-field task" in prompt
    assert "For this preserve task" not in prompt
    assert _nl_shape_contract_violations(
        intent,
        "Add a field named avg_loan_amount to each document and keep all other fields unchanged.",
    ) == [
        "reshape canonical NLQ must not describe preserve/add-field semantics",
        "schema_flex_variant_summary canonical NLQ must name output fields: variant, count",
    ]
    assert _nl_shape_contract_violations(
        intent,
        "Summarize account documents by loan presence variant and output variant, count, "
        "and avg_loan_amount, using 0 for missing loan amounts.",
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
