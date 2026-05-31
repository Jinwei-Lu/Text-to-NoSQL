from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tend.agents import AgentContext
from tend.config import Settings
from tend.execution.ast_check import parse_pipeline, scan_disabled
from tend.execution.mongo import MongoExecutor
from tend.llm import LLMClient
from tend.observability import setup_logging
from tend.solver.per_stage import CheckpointCode, CheckpointSpec, run_per_stage_check
from tend.solver.guards import SolverBoundary
import tend.solver.workflow as solver_workflow
from tend.solver.workflow import (
    _MongoPrefixExecutor,
    SolverFailure,
    load_solver_release_inputs,
    smart_solve_record,
)
from tend.stubs import stub_fn
from tend.workflow import Workflow


@pytest.fixture(scope="module")
def stub_settings() -> Settings:
    return Settings.from_env(overrides={"TEND_LLM_STUB": "1"}, run_id="solver-workflow-test")


@pytest.fixture()
def logger(tmp_path: Path):
    log = setup_logging(tmp_path / "run", console=False)
    try:
        yield log
    finally:
        log.close()


def test_solver_boundary_removes_gold_fields(stub_settings: Settings, logger) -> None:
    boundary = SolverBoundary.from_settings(stub_settings, logger=logger)
    record = {
        "record_id": 1001,
        "db_id": "financial",
        "nl_queries": {"canonical": "attach score"},
        "MQL": "db.x.aggregate([])",
        "canonical_form_set": {},
        "shape_policy": "preserve",
        "agent_design_rationale_ref": "audit/x",
    }

    safe = boundary.sanitize_test_record(record)

    assert safe == {
        "record_id": 1001,
        "db_id": "financial",
        "nl_queries": {"canonical": "attach score"},
    }


def test_smart_solver_stub_end_to_end(stub_settings: Settings, tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    log = setup_logging(run_dir, console=False)
    client = LLMClient(stub_settings, log)
    client.set_stub(stub_fn)
    mongo = MongoExecutor(stub_settings, log)
    ctx = AgentContext(settings=stub_settings, llm=client, log=log, mongo=mongo)
    wf = Workflow(ctx, max_concurrency=4)
    (record, schema, data) = load_solver_release_inputs(
        stub_settings.paths.repo_root / "tests" / "fixtures" / "smoke_release",
        db_id="financial",
        record_id=1001,
        limit=1,
    )[0]

    pred = asyncio.run(smart_solve_record(wf, record, schema, local_data=data))
    log.close()
    mongo.close()

    assert pred.db_id == "financial"
    assert pred.record_id == 1001
    assert pred.disclosure.backbone == "deepseek-v4-flash"
    assert pred.disclosure.no_training is True
    assert pred.disclosure.uses_train_json is False
    assert pred.logical_spec["shape_policy"] == "preserve"
    assert pred.physical_plan["variant_handling"]
    coll, pipeline = parse_pipeline(pred.MQL)
    assert coll == "account"
    assert len(pipeline) == 3
    assert scan_disabled(pred.MQL) == []
    assert (run_dir / "llm" / "smart_intent").is_dir()
    assert (run_dir / "llm" / "smart_plan").is_dir()

    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert any(e["event"] == "smart_solver_start" for e in events)
    assert any(e["event"] == "solver_per_stage_prefix" for e in events)


def test_smart_solver_exhaustion_returns_typed_failure_without_dummy_mql(
    stub_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    log = setup_logging(run_dir, console=False)
    client = LLMClient(stub_settings, log)
    ctx = AgentContext(settings=stub_settings, llm=client, log=log)
    wf = Workflow(ctx, max_concurrency=1)
    realization_calls: list[dict] = []

    async def fake_comprehend_shapes(_wf, _ctx, _nlq, _schema):
        return {
            "collections": {
                "account": {
                    "variants": [{"id": "default", "discriminator": {}}],
                    "field_locus": {},
                }
            },
            "coverage_gaps": [],
            "shape_flex_signature": [],
        }

    async def fake_agent(agent_id, inputs, *, ctx=None):
        if agent_id == "smart_intent":
            return {
                "entity": "account",
                "per": "account",
                "shape_policy": "preserve",
                "target_fields": ["score"],
                "output": {"target_fields": ["score"]},
            }
        if agent_id == "smart_plan":
            return {
                "collection": "account",
                "stages": [
                    {
                        "op": "$project",
                        "note": "drop target field",
                        "stage": {"$project": {"_id": 1}},
                    }
                ],
                "variant_handling": [],
            }
        raise AssertionError(agent_id)

    def fake_realize(_ctx, _boundary, **kwargs):
        realization_calls.append(kwargs)
        return {
            "ok": False,
            "mql": None,
            "feedback": {
                "error_code": "TARGET_FIELD_MISSING",
                "stage_index": 1,
                "failing_variant": "default",
                "suspect_field": "score",
                "message": "required target field is absent",
            },
        }

    monkeypatch.setattr(solver_workflow, "comprehend_shapes", fake_comprehend_shapes)
    monkeypatch.setattr(solver_workflow, "realize_plan_per_stage", fake_realize)
    wf.agent = fake_agent  # type: ignore[method-assign]

    try:
        result = asyncio.run(
            smart_solve_record(
                wf,
                {
                    "record_id": 7,
                    "db_id": "financial",
                    "nl_queries": {"canonical": "attach score"},
                },
                {"collections": {"account": {"_id": "INT"}}},
                local_data={"account": [{"_id": 1}]},
                r_max=1,
            )
        )
    finally:
        log.close()

    assert isinstance(result, SolverFailure)
    assert result.error_code == "SOLVER_EXHAUSTED"
    assert len(result.feedback) == 2
    assert [entry["attempt"] for entry in result.feedback] == [0, 1]
    assert len(realization_calls) == 2
    payload = result.to_json()
    assert "MQL" not in payload
    assert json.dumps(payload, default=str) != "[]"

    anomalies = [
        json.loads(line) for line in (run_dir / "anomalies.jsonl").read_text().splitlines()
    ]
    assert [entry["anomaly"] for entry in anomalies] == ["solver_exhausted"]


def test_mongo_prefix_executor_stratifies_shape_variants_for_feedback() -> None:
    class FakeMongo:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def norm_exec(self, _db_id: str, mql: str) -> list[dict]:
            self.calls.append(mql)
            _collection, pipeline = parse_pipeline(mql)
            selector = pipeline[0]["$match"]
            if selector == {"loan": {"$exists": True}}:
                return [{"_id": 1, "score": 10}]
            if selector == {"loan": {"$exists": False}}:
                return [{"_id": 2}]
            raise AssertionError(selector)

    fake_mongo = FakeMongo()
    executor = _MongoPrefixExecutor(
        fake_mongo,
        shape_model={
            "collections": {
                "account": {
                    "variants": [
                        {"id": "loan-present", "discriminator": {"loan": "present"}},
                        {"id": "loan-missing", "discriminator": {"loan": "missing"}},
                    ]
                }
            }
        },
        local_data={"account": [{"_id": 1, "loan": {"amount": 10}}, {"_id": 2}]},
    )

    result = run_per_stage_check(
        db_id="financial",
        mql='db.account.aggregate([{"$addFields":{"score":"$loan.amount"}}])',
        executor=executor,
        checkpoint=CheckpointSpec(required_fields_by_stage={1: ("score",)}),
    )

    assert result.ok is False
    assert result.feedback is not None
    assert result.feedback.error_code == CheckpointCode.TARGET_FIELD_MISSING
    assert result.feedback.failing_variant == "loan-missing"
    assert result.feedback.suspect_field == "score"
    assert len(fake_mongo.calls) == 2
    variants = result.feedback.context["variants"]
    assert [variant["variant"] for variant in variants] == ["loan-present", "loan-missing"]
