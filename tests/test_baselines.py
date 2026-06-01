from __future__ import annotations

import asyncio
import json
from pathlib import Path

from tend.agents import AgentContext
from tend.baselines import BASELINE_IDS, run_baseline_record, run_baseline_suite
from tend.baselines.strategies import resolve_baselines
from tend.config import Settings
from tend.llm import LLMClient
from tend.observability import setup_logging
from tend.solver.workflow import load_solver_release_inputs
from tend.stubs import stub_fn
from tend.workflow import Workflow


def _settings() -> Settings:
    return Settings.from_env(
        overrides={"TEND_LLM_STUB": "1"},
        run_id="baseline-test",
        require_bird=False,
    )


def _workflow(settings: Settings, run_dir: Path) -> tuple[Workflow, object]:
    log = setup_logging(run_dir, console=False)
    client = LLMClient(settings, log)
    client.set_stub(stub_fn)
    ctx = AgentContext(settings=settings, llm=client, log=log)
    return Workflow(ctx, max_concurrency=2), log


def test_baseline_registry_has_six_constrained_strategies() -> None:
    assert BASELINE_IDS == (
        "direct",
        "schema_direct",
        "sql_pivot",
        "plan_then_mql",
        "react_lite",
        "static_self_debug",
    )
    specs = resolve_baselines("all")
    assert len(specs) == 6
    assert all(spec.steps for spec in specs)
    assert all(spec.limitations for spec in specs)


def test_baseline_suite_stub_logs_markdown_transcripts(tmp_path: Path) -> None:
    settings = _settings()
    wf, log = _workflow(settings, tmp_path / "run")
    dataset_dir = settings.paths.repo_root / "tests" / "fixtures" / "smoke_release"

    try:
        outputs = asyncio.run(
            run_baseline_suite(
                wf,
                dataset_dir=dataset_dir,
                baseline_selection="all",
                db_id="financial",
                record_id=1001,
                limit=1,
            )
        )
    finally:
        log.close()

    assert len(outputs) == 6
    assert {item["baseline_id"] for item in outputs} == set(BASELINE_IDS)
    assert all(item["status"] == "ok" for item in outputs)
    assert all(item["disclosure"]["uses_gold_mql"] is False for item in outputs)
    assert all("disjointness_ok" in item["disclosure"] for item in outputs)

    run_dir = tmp_path / "run"
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    llm_ok = [event for event in events if event["event"] == "llm_call_ok"]
    assert len(llm_ok) == 10
    for event in llm_ok:
        assert event["baseline_id"]
        assert event["baseline_step"]
        transcript_ref = event["transcript_ref"]
        diagnostics_ref = event["diagnostics_ref"]
        assert transcript_ref.endswith(".md")
        assert diagnostics_ref.endswith(".diagnostics.json")
        md = (run_dir / transcript_ref).read_text(encoding="utf-8")
        diagnostics = json.loads((run_dir / diagnostics_ref).read_text(encoding="utf-8"))
        assert "## Messages" in md
        assert "## Response" in md
        assert "## Diagnostics" in md
        assert diagnostics["baseline_id"] == event["baseline_id"]
        assert diagnostics["baseline_step"] == event["baseline_step"]
        prompt_text = "\n".join(message["content"] for message in diagnostics["messages"])
        assert "canonical_form_set" not in prompt_text
        assert "shape_policy" not in prompt_text
        assert "agent_design_rationale_ref" not in prompt_text


def test_baseline_record_requires_canonical_nlq(tmp_path: Path) -> None:
    settings = _settings()
    wf, log = _workflow(settings, tmp_path / "run")
    dataset_dir = settings.paths.repo_root / "tests" / "fixtures" / "smoke_release"
    record, schema, data = load_solver_release_inputs(
        dataset_dir,
        db_id="financial",
        record_id=1001,
        limit=1,
    )[0]
    record = {
        "record_id": record["record_id"],
        "db_id": record["db_id"],
        "nl_queries": {"colloquial": "Do the same task casually."},
    }
    spec = resolve_baselines("direct")[0]

    try:
        result = asyncio.run(run_baseline_record(wf, spec, record, schema, local_data=data))
    finally:
        log.close()

    payload = result.to_json()
    assert payload["result_type"] == "baseline_failure"
    assert payload["status"] == "failed"
    assert payload["error_code"] == "prompt_malformed"
    anomalies = [
        json.loads(line)
        for line in (tmp_path / "run" / "anomalies.jsonl").read_text().splitlines()
    ]
    assert anomalies[0]["anomaly"] == "prompt_malformed"
    assert anomalies[0]["baseline_id"] == "direct"
