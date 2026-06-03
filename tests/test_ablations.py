from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import tend.ablations.workflow as ablation_workflow
from tend.ablations import ABLATION_IDS, run_ablation_record, run_ablation_suite
from tend.ablations.strategies import resolve_ablations
from tend.agents import AgentContext
from tend.config import Settings
from tend.llm import LLMClient
from tend.observability import ProgressReporter, setup_logging
from tend.solver.workflow import load_solver_release_inputs
from tend.stubs import stub_fn
from tend.workflow import Workflow


def _settings() -> Settings:
    return Settings.from_env(
        overrides={"TEND_LLM_STUB": "1"},
        run_id="ablation-test",
        require_bird=False,
    )


def _workflow(settings: Settings, run_dir: Path) -> tuple[Workflow, object, ProgressReporter]:
    log = setup_logging(run_dir, console=False)
    progress = ProgressReporter(settings.run_id, log, enabled=False)
    client = LLMClient(settings, log)
    client.set_stub(stub_fn)
    ctx = AgentContext(settings=settings, llm=client, log=log, progress=progress)
    return Workflow(ctx), log, progress


class _SnapshotMongo:
    def __init__(self, docs: dict[str, list[dict[str, Any]]]) -> None:
        self.docs = docs
        self.calls: list[tuple[str, int]] = []

    def snapshot_database(self, db_id: str, sample_size: int) -> dict[str, list[dict[str, Any]]]:
        self.calls.append((db_id, sample_size))
        return {name: rows[:sample_size] for name, rows in self.docs.items()}


def _manual_native_docs() -> dict[str, list[dict[str, Any]]]:
    return {
        "card_print_dossiers": [
            {
                "_id": "card:alpha",
                "print_identity": {"name": "Alpha Bolt"},
                "legality": {
                    "by_format": {
                        "Modern": {"status": "banned", "status_presence_state": "present"},
                        "Legacy": {"status": "legal", "status_presence_state": "present"},
                    }
                },
                "schema_state": {"legalities": "present", "foreign_data": "missing"},
            }
        ]
    }


def test_ablation_registry_covers_smart_mechanisms() -> None:
    assert ABLATION_IDS == (
        "full_smart",
        "no_shape_model",
        "no_schema_variants",
        "canonical_only",
        "no_intent_contracts",
        "no_variant_handling_guard",
        "no_witness_digest",
        "whole_query_execution",
        "no_per_stage_execution",
        "no_variant_stratification",
        "no_feedback_retry",
    )
    specs = resolve_ablations("all")
    assert len(specs) == len(ABLATION_IDS)
    assert {spec.options.solver_variant for spec in specs} == set(ABLATION_IDS)


def test_ablation_suite_stub_logs_markdown_transcripts_and_progress(tmp_path: Path) -> None:
    settings = _settings()
    wf, log, progress = _workflow(settings, tmp_path / "run")
    dataset_dir = settings.paths.repo_root / "tests" / "fixtures" / "smoke_release"
    raw_record = json.loads((dataset_dir / "test.json").read_text(encoding="utf-8"))[0]
    gold_mql = raw_record["MQL"]

    try:
        outputs = asyncio.run(
            run_ablation_suite(
                wf,
                dataset_dir=dataset_dir,
                ablation_selection="all",
                db_id="financial",
                record_id=1001,
                limit=1,
            )
        )
        progress_summary = progress.summary()
    finally:
        log.close()

    assert len(outputs) == len(ABLATION_IDS)
    assert {item["ablation_id"] for item in outputs} == set(ABLATION_IDS)
    assert sorted(item["batch_index"] for item in outputs) == list(range(len(ABLATION_IDS)))
    assert all(item["work_item_id"].startswith("ablation:") for item in outputs)
    assert all(item["status"] == "ok" for item in outputs)
    assert all(item["result_type"] == "ablation_prediction" for item in outputs)
    assert all(item["disclosure"]["solver_disclosure"]["backbone"] == "deepseek-v4-flash"
               for item in outputs)

    by_id = {item["ablation_id"]: item for item in outputs}
    assert by_id["no_shape_model"]["uses_shape_model"] is False
    assert by_id["no_shape_model"]["shape_model"]["coverage_gaps"] == [
        "ablation:shape_comprehension_disabled"
    ]
    assert by_id["no_witness_digest"]["witness_k"] == 0
    assert by_id["no_witness_digest"]["prompt_witness_sample_count_by_collection"] == {}
    assert by_id["no_per_stage_execution"]["uses_static_feedback"] is True
    assert by_id["whole_query_execution"]["uses_per_stage"] is False
    assert by_id["no_variant_stratification"]["uses_variant_strata"] is False

    assert progress_summary["tasks"]["started"] == progress_summary["tasks"]["ok"]
    assert progress_summary["tasks"]["fail"] == 0
    assert progress_summary["anomaly_total"] == 0

    run_dir = tmp_path / "run"
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert any(event["event"] == "ablation_suite_start" for event in events)
    assert any(
        event["event"] == "smart_solver_static_realization"
        and event["ablation_id"] == "no_per_stage_execution"
        for event in events
    )

    llm_ok = [event for event in events if event["event"] == "llm_call_ok"]
    assert len(llm_ok) == len(ABLATION_IDS) * 2
    for event in llm_ok:
        assert event["ablation_id"] in ABLATION_IDS
        assert event["batch_index"] is not None
        transcript_ref = event["transcript_ref"]
        diagnostics_ref = event["diagnostics_ref"]
        assert transcript_ref.endswith(".md")
        assert diagnostics_ref.endswith(".diagnostics.json")
        md = (run_dir / transcript_ref).read_text(encoding="utf-8")
        diagnostics = json.loads((run_dir / diagnostics_ref).read_text(encoding="utf-8"))
        assert "## Messages" in md
        assert "## Response" in md
        assert "## Diagnostics" in md
        assert diagnostics["ablation_id"] == event["ablation_id"]
        prompt_text = "\n".join(message["content"] for message in diagnostics["messages"])
        assert "canonical_form_set" not in prompt_text
        assert "agent_design_rationale_ref" not in prompt_text
        assert gold_mql not in prompt_text

    assert by_id["full_smart"]["transcript_refs"]
    assert by_id["full_smart"]["diagnostics_refs"]
    assert not (run_dir / "anomalies.jsonl").read_text(encoding="utf-8").strip()


def test_ablation_suite_nlq_db_only_derives_context_and_skips_release_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    wf, log, _progress = _workflow(settings, tmp_path / "run")
    mongo = _SnapshotMongo(_manual_native_docs())
    wf.ctx.mongo = mongo
    captured: list[dict[str, Any]] = []

    monkeypatch.setattr(
        ablation_workflow,
        "load_solver_release_inputs",
        lambda *_args, **_kwargs: pytest.fail("NLQ+DB ablation must not read release inputs"),
    )

    async def fake_run_ablation_record(
        _wf: Workflow,
        spec: Any,
        record: dict[str, Any],
        schema: dict[str, Any],
        *,
        local_data: dict[str, list[dict[str, Any]]] | None = None,
        r_max: int,
        witness_k: int,
        batch_index: int | None,
        witness_preloaded: bool,
    ) -> Any:
        captured.append(
            {
                "ablation_id": spec.id,
                "record": record,
                "schema": schema,
                "local_data": local_data,
                "r_max": r_max,
                "witness_k": witness_k,
                "batch_index": batch_index,
                "witness_preloaded": witness_preloaded,
            }
        )

        class _Result:
            def to_json(self) -> dict[str, Any]:
                return {
                    "ablation_id": spec.id,
                    "record_id": record.get("record_id"),
                    "db_id": record.get("db_id"),
                    "status": "ok",
                    "result_type": "ablation_prediction",
                    "MQL": "db.card_print_dossiers.aggregate([])",
                }

        return _Result()

    monkeypatch.setattr(ablation_workflow, "run_ablation_record", fake_run_ablation_record)

    try:
        outputs = asyncio.run(
            run_ablation_suite(
                wf,
                dataset_dir=tmp_path,
                ablation_selection="all",
                db_id="manual_cards",
                record_id=7,
                limit=999,
                r_max=1,
                witness_k=2,
                nlq="Find Modern banned card printings.",
            )
        )
    finally:
        log.close()

    assert mongo.calls == [("manual_cards", 2)]
    assert [item["ablation_id"] for item in outputs] == list(ABLATION_IDS)
    assert [item["batch_index"] for item in outputs] == list(range(len(ABLATION_IDS)))
    assert len(captured) == len(ABLATION_IDS)
    for item in captured:
        assert item["record"] == {
            "db_id": "manual_cards",
            "record_id": 7,
            "nl_queries": {"canonical": "Find Modern banned card printings."},
        }
        assert "MQL" not in item["record"]
        assert "shape_policy" not in item["record"]
        assert item["schema"]["collections"]["card_print_dossiers"]["schema_flex"] == (
            "native_deep"
        )
        assert "legality.by_format" in (
            item["schema"]["collections"]["card_print_dossiers"]["dynamic_key_paths"]
        )
        assert item["local_data"] == _manual_native_docs()
        assert item["r_max"] == 1
        assert item["witness_k"] == 2
        assert item["witness_preloaded"] is True


def test_ablation_record_missing_nlq_is_prompt_anomaly(tmp_path: Path) -> None:
    settings = _settings()
    wf, log, _progress = _workflow(settings, tmp_path / "run")
    dataset_dir = settings.paths.repo_root / "tests" / "fixtures" / "smoke_release"
    record, schema, data = load_solver_release_inputs(
        dataset_dir,
        db_id="financial",
        record_id=1001,
        limit=1,
    )[0]
    record = {"record_id": record["record_id"], "db_id": record["db_id"]}
    spec = resolve_ablations("full_smart")[0]

    try:
        result = asyncio.run(run_ablation_record(wf, spec, record, schema, local_data=data))
    finally:
        log.close()

    payload = result.to_json()
    assert payload["result_type"] == "ablation_failure"
    assert payload["status"] == "failed"
    assert payload["error_code"] == "prompt_malformed"
    anomalies = [
        json.loads(line)
        for line in (tmp_path / "run" / "anomalies.jsonl").read_text().splitlines()
    ]
    assert anomalies[0]["anomaly"] == "prompt_malformed"
    assert anomalies[0]["ablation_id"] == "full_smart"
    assert anomalies[0]["record_id"] == 1001
