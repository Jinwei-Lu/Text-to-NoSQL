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
from tend.solver.guards import SolverBoundary
from tend.solver.workflow import load_solver_release_inputs, smart_solve_record
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
        stub_settings.paths.dataset_out,
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
