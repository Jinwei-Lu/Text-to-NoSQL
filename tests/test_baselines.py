from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import tend.baselines.workflow as baseline_workflow
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
    return Workflow(ctx), log


class _SnapshotMongo:
    def __init__(self, docs: dict[str, list[dict[str, Any]]]) -> None:
        self.docs = docs
        self.calls: list[tuple[str, int]] = []

    def snapshot_database(self, db_id: str, sample_size: int) -> dict[str, list[dict[str, Any]]]:
        self.calls.append((db_id, sample_size))
        return {name: rows[:sample_size] for name, rows in self.docs.items()}


def _manual_native_docs() -> dict[str, list[dict[str, Any]]]:
    return {
        "race_weekends_v2": [
            {
                "_id": "race:1",
                "calendar": {"race_name": "Australian GP"},
                "sessions": {
                    "race": {
                        "results_by_status": {
                            "Finished": {"count": 2, "entries": []},
                            "Accident": {"count": 1, "entries": []},
                        }
                    }
                },
                "schema_state": {"race_results": "present", "pit_stops": "missing"},
            }
        ]
    }


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


def test_baseline_suite_nlq_db_only_derives_context_and_skips_release_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    wf, log = _workflow(settings, tmp_path / "run")
    mongo = _SnapshotMongo(_manual_native_docs())
    wf.ctx.mongo = mongo
    captured: list[dict[str, Any]] = []

    monkeypatch.setattr(
        baseline_workflow,
        "load_solver_release_inputs",
        lambda *_args, **_kwargs: pytest.fail("NLQ+DB baseline must not read release inputs"),
    )

    async def fake_run_baseline_record(
        _wf: Workflow,
        spec: Any,
        record: dict[str, Any],
        schema: dict[str, Any],
        *,
        local_data: dict[str, list[dict[str, Any]]] | None = None,
        witness_k: int,
        batch_index: int | None,
    ) -> Any:
        captured.append(
            {
                "baseline_id": spec.id,
                "record": record,
                "schema": schema,
                "local_data": local_data,
                "witness_k": witness_k,
                "batch_index": batch_index,
            }
        )

        class _Result:
            def to_json(self) -> dict[str, Any]:
                return {
                    "baseline_id": spec.id,
                    "record_id": record.get("record_id"),
                    "db_id": record.get("db_id"),
                    "status": "ok",
                    "result_type": "baseline_prediction",
                    "MQL": "db.race_weekends_v2.aggregate([])",
                }

        return _Result()

    monkeypatch.setattr(baseline_workflow, "run_baseline_record", fake_run_baseline_record)

    try:
        outputs = asyncio.run(
            run_baseline_suite(
                wf,
                dataset_dir=tmp_path,
                baseline_selection="all",
                db_id="manual_formula",
                record_id=42,
                limit=999,
                witness_k=2,
                nlq="List race weekends that have a Finished result-status bucket.",
            )
        )
    finally:
        log.close()

    assert mongo.calls == [("manual_formula", 2)]
    assert [item["baseline_id"] for item in outputs] == list(BASELINE_IDS)
    assert [item["batch_index"] for item in outputs] == list(range(len(BASELINE_IDS)))
    assert len(captured) == len(BASELINE_IDS)
    for item in captured:
        assert item["record"] == {
            "db_id": "manual_formula",
            "record_id": 42,
            "nl_queries": {
                "canonical": "List race weekends that have a Finished result-status bucket."
            },
        }
        assert "MQL" not in item["record"]
        assert "shape_policy" not in item["record"]
        assert item["schema"]["collections"]["race_weekends_v2"]["schema_flex"] == "native_deep"
        assert "sessions.race.results_by_status" in (
            item["schema"]["collections"]["race_weekends_v2"]["dynamic_key_paths"]
        )
        assert item["local_data"] == _manual_native_docs()
        assert item["witness_k"] == 2


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
