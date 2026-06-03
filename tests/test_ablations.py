from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import tend.ablations.workflow as ablation_workflow
from tend.ablations import ABLATION_IDS, run_ablation_record, run_ablation_suite
from tend.ablations.strategies import SmartEGAblationSpec, resolve_ablations
from tend.ablations.workflow import (
    _attempt_count,
    _disclosure,
    _failure_from_solver_payload,
    _prediction_from_solver_payload,
)
from tend.agents import AgentContext
from tend.config import Settings
from tend.llm import LLMClient
from tend.observability import ProgressReporter, setup_logging
from tend.solver.inputs import load_solver_release_inputs
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


def test_ablation_registry_covers_smart_eg_mechanisms() -> None:
    assert ABLATION_IDS == (
        "smart_eg_full",
        "smart_eg_no_evidence_gate",
        "smart_eg_no_counterexample",
        "smart_eg_no_value_grounding",
        "smart_eg_no_relationship_probe",
        "smart_eg_no_prefix_execution",
        "smart_eg_no_revisit",
        "smart_eg_no_probe_scheduler",
        "smart_eg_budget_low",
        "smart_eg_budget_medium",
        "smart_eg_budget_high",
    )
    specs = resolve_ablations("all")
    assert len(specs) == len(ABLATION_IDS)
    assert all(isinstance(spec, SmartEGAblationSpec) for spec in specs)
    assert {spec.id for spec in specs} == set(ABLATION_IDS)
    assert not hasattr(specs[0], "options"), "old SmartSolveOptions registry must be retired"

    full = resolve_ablations("smart_eg_full")[0]
    full_options = full.to_runtime_options()
    assert full_options["solver_variant"] == "smart_eg_full"
    assert full_options["use_evidence_gate"] is True
    assert full_options["use_counterexample"] is True
    assert full_options["use_value_grounding"] is True
    assert full_options["use_relationship_probe"] is True
    assert full_options["use_prefix_execution"] is True
    assert full_options["use_revisit"] is True
    assert full_options["use_probe_scheduler"] is True

    toggles = {
        "smart_eg_no_evidence_gate": "use_evidence_gate",
        "smart_eg_no_counterexample": "use_counterexample",
        "smart_eg_no_value_grounding": "use_value_grounding",
        "smart_eg_no_relationship_probe": "use_relationship_probe",
        "smart_eg_no_prefix_execution": "use_prefix_execution",
        "smart_eg_no_revisit": "use_revisit",
        "smart_eg_no_probe_scheduler": "use_probe_scheduler",
    }
    for ablation_id, disabled_key in toggles.items():
        options = resolve_ablations(ablation_id)[0].to_runtime_options()
        assert options[disabled_key] is False
        for key, value in full_options.items():
            if key in {"ablation_id", "solver_variant", disabled_key}:
                continue
            if ablation_id == "smart_eg_no_revisit" and key == "max_revisits":
                assert options[key] == 0
                continue
            assert options[key] == value, f"{ablation_id} changed unrelated option {key}"

    assert resolve_ablations("smart_eg_budget_low")[0].to_runtime_options() == {
        **full_options,
        "ablation_id": "smart_eg_budget_low",
        "solver_variant": "smart_eg_budget_low",
        "max_tool_turns": 8,
        "max_revisits": 0,
        "cost_budget_usd": 0.25,
    }
    assert resolve_ablations("smart_eg_budget_medium")[0].to_runtime_options() == {
        **full_options,
        "ablation_id": "smart_eg_budget_medium",
        "solver_variant": "smart_eg_budget_medium",
        "max_tool_turns": 24,
    }
    assert resolve_ablations("smart_eg_budget_high")[0].to_runtime_options() == {
        **full_options,
        "ablation_id": "smart_eg_budget_high",
        "solver_variant": "smart_eg_budget_high",
        "max_tool_turns": 48,
        "max_revisits": 4,
        "cost_budget_usd": 3.0,
    }


def test_ablation_suite_stub_logs_markdown_transcripts_and_progress(tmp_path: Path) -> None:
    settings = _settings()
    wf, log, progress = _workflow(settings, tmp_path / "run")
    dataset_dir = settings.paths.repo_root / "tests" / "fixtures" / "smoke_release"
    captured: list[dict[str, Any]] = []

    async def fake_run_ablation_record(
        _wf: Workflow,
        spec: SmartEGAblationSpec,
        record: dict[str, Any],
        schema: dict[str, Any],
        *,
        local_data: dict[str, list[dict[str, Any]]] | None = None,
        max_tool_turns: int,
        max_revisits: int,
        cost_budget_usd: float,
        batch_index: int | None,
        witness_preloaded: bool,
    ) -> Any:
        options = spec.to_runtime_options(
            max_tool_turns=max_tool_turns,
            max_revisits=max_revisits,
            cost_budget_usd=cost_budget_usd,
            progress_work_item_id=f"{spec.id}:{batch_index}",
        )
        captured.append(
            {
                "ablation_id": spec.id,
                "record_id": record.get("record_id"),
                "db_id": record.get("db_id"),
                "schema": schema,
                "local_data": local_data,
                "options": options,
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
                    "MQL": "db.account.aggregate([])",
                }

        return _Result()

    try:
        with patch.object(ablation_workflow, "run_ablation_record", new=fake_run_ablation_record):
            outputs = asyncio.run(
                run_ablation_suite(
                    wf,
                    dataset_dir=dataset_dir,
                    ablation_selection="all",
                    db_id="financial",
                    record_id=1001,
                    limit=1,
                    max_tool_turns=12,
                    max_revisits=1,
                    cost_budget_usd=0.5,
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
    assert [item["ablation_id"] for item in captured] == list(ABLATION_IDS)
    by_id = {item["ablation_id"]: item["options"] for item in captured}
    assert by_id["smart_eg_full"]["max_tool_turns"] == 12
    assert by_id["smart_eg_full"]["max_revisits"] == 1
    assert by_id["smart_eg_full"]["cost_budget_usd"] == 0.5
    assert by_id["smart_eg_budget_low"]["max_tool_turns"] == 8
    assert by_id["smart_eg_budget_low"]["max_revisits"] == 0
    assert by_id["smart_eg_budget_low"]["cost_budget_usd"] == 0.25
    assert by_id["smart_eg_budget_medium"]["max_tool_turns"] == 24
    assert by_id["smart_eg_budget_medium"]["max_revisits"] == 2
    assert by_id["smart_eg_budget_medium"]["cost_budget_usd"] == 1.0
    assert by_id["smart_eg_budget_high"]["max_tool_turns"] == 48
    assert by_id["smart_eg_budget_high"]["max_revisits"] == 4
    assert by_id["smart_eg_budget_high"]["cost_budget_usd"] == 3.0
    assert all(item["witness_preloaded"] is False for item in captured)

    assert progress_summary["tasks"]["started"] == progress_summary["tasks"]["ok"]
    assert progress_summary["tasks"]["fail"] == 0
    assert progress_summary["anomaly_total"] == 0

    run_dir = tmp_path / "run"
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert any(event["event"] == "ablation_suite_start" for event in events)
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
        spec: SmartEGAblationSpec,
        record: dict[str, Any],
        schema: dict[str, Any],
        *,
        local_data: dict[str, list[dict[str, Any]]] | None = None,
        max_tool_turns: int,
        max_revisits: int,
        cost_budget_usd: float,
        batch_index: int | None,
        witness_preloaded: bool,
    ) -> Any:
        captured.append(
            {
                "ablation_id": spec.id,
                "record": record,
                "schema": schema,
                "local_data": local_data,
                "max_tool_turns": max_tool_turns,
                "max_revisits": max_revisits,
                "cost_budget_usd": cost_budget_usd,
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
                max_tool_turns=13,
                max_revisits=1,
                cost_budget_usd=0.75,
                nlq="Find Modern banned card printings.",
            )
        )
    finally:
        log.close()

    assert mongo.calls == [("manual_cards", 3)]
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
        assert item["max_tool_turns"] == 13
        assert item["max_revisits"] == 1
        assert item["cost_budget_usd"] == 0.75
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
    spec = resolve_ablations("smart_eg_full")[0]

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
    assert anomalies[0]["ablation_id"] == "smart_eg_full"
    assert anomalies[0]["record_id"] == 1001


# ---------------------------------------------------------------------------
# [H6] _attempt_count returns correct total for multi-attempt success
# ---------------------------------------------------------------------------

def test_attempt_count_no_failures_is_one() -> None:
    """Empty feedback_log means one attempt (the successful one)."""
    assert _attempt_count([]) == 1


def test_attempt_count_one_failed_attempt_is_two() -> None:
    """One failed entry in feedback_log means two attempts total (failed + success)."""
    assert _attempt_count([{"attempt": 0, "error": "something"}]) == 2


def test_attempt_count_two_failed_attempts_is_three() -> None:
    """Two failed entries in feedback_log means three attempts total."""
    assert _attempt_count([{"attempt": 0}, {"attempt": 1}]) == 3


def test_attempt_count_n_failed_entries_is_n_plus_one() -> None:
    """General property: N failed entries -> N+1 total attempts."""
    for n in range(1, 6):
        feedback = [{"attempt": i, "error": f"err_{i}"} for i in range(n)]
        assert _attempt_count(feedback) == n + 1, f"expected {n + 1} for {n} failed entries"


# ---------------------------------------------------------------------------
# [CF5] AblationFailure.static_feedback is non-empty when payload has a valid MQL
# ---------------------------------------------------------------------------

def _make_wf(tmp_path: Path) -> tuple[object, object]:
    settings = _settings()
    wf, log, _ = _workflow(settings, tmp_path / "run")
    return wf, log


def test_failure_static_feedback_non_empty_for_valid_mql(tmp_path: Path) -> None:
    """_failure_from_solver_payload sets non-empty static_feedback when the payload MQL is valid."""
    wf, log = _make_wf(tmp_path)
    spec = resolve_ablations("smart_eg_full")[0]
    options = spec.to_runtime_options()
    payload = {
        "record_id": 42,
        "db_id": "financial",
        "error_code": "rtv_failed",
        "message": "round-trip verification failed",
        "MQL": 'db.account.aggregate([{"$match": {"status": "A"}}])',
        "feedback": [],
        "shape_model": {},
        "logical_spec": {},
        "physical_plan": {},
        "disclosure": {},
    }
    try:
        failure = _failure_from_solver_payload(
            wf, spec, options, payload,
            local_data=None,
            transcript_refs=[],
            diagnostics_refs=[],
        )
    finally:
        log.close()

    # [CF5] static_feedback must not be [] when payload has a non-empty MQL
    assert failure.static_feedback, "static_feedback must be non-empty when MQL is present"
    codes = {item["code"] for item in failure.static_feedback}
    # A valid aggregate pipeline produces at least PARSE_OK
    assert "PARSE_OK" in codes, f"expected PARSE_OK in static_feedback codes, got {codes}"


def test_failure_static_feedback_has_error_for_blank_mql(tmp_path: Path) -> None:
    """_failure_from_solver_payload reports EMPTY_MQL when the payload carries no MQL."""
    wf, log = _make_wf(tmp_path)
    spec = resolve_ablations("smart_eg_full")[0]
    options = spec.to_runtime_options()
    payload = {
        "record_id": 42,
        "db_id": "financial",
        "error_code": "some_error",
        "message": "failed before MQL was generated",
        "MQL": "",
        "feedback": [],
        "shape_model": {},
        "logical_spec": {},
        "physical_plan": {},
        "disclosure": {},
    }
    try:
        failure = _failure_from_solver_payload(
            wf, spec, options, payload,
            local_data=None,
            transcript_refs=[],
            diagnostics_refs=[],
        )
    finally:
        log.close()

    assert failure.static_feedback
    codes = {item["code"] for item in failure.static_feedback}
    assert "EMPTY_MQL" in codes


# ---------------------------------------------------------------------------
# [ablations F5] disclosure dict exposes top-level comparable keys
# ---------------------------------------------------------------------------

def test_disclosure_exposes_top_level_comparable_keys() -> None:
    """_disclosure must hoist comparable runtime and provider keys
    to the top level so ablation disclosures align with baseline disclosure shape."""
    spec = resolve_ablations("smart_eg_full")[0]
    options = spec.to_runtime_options()
    solver_disclosure = {
        "backbone": "deepseek-v4-flash",
        "disjointness_ok": True,
        "s_solver": ["smart_eg_agent", "smart_eg_tools"],
        "max_tool_turns": 24,
        "max_revisits": 2,
        "cost_budget_usd": 1.0,
        "no_training": True,
    }
    d = _disclosure(spec, options, solver_disclosure)

    # Top-level keys must be present and match solver_disclosure values
    assert d["backbone"] == "deepseek-v4-flash"
    assert d["disjointness_ok"] is True
    assert d["s_solver"] == ["smart_eg_agent", "smart_eg_tools"]
    assert d["max_tool_turns"] == 24
    assert d["max_revisits"] == 2
    assert d["cost_budget_usd"] == 1.0
    assert d["no_training"] is True

    # solver_disclosure still available nested
    assert d["solver_disclosure"] == solver_disclosure


def test_disclosure_top_level_none_when_solver_disclosure_empty() -> None:
    """When solver_disclosure is empty, top-level comparable keys are None (not KeyError)."""
    spec = resolve_ablations("smart_eg_full")[0]
    options = spec.to_runtime_options()
    d = _disclosure(spec, options, {})

    for key in (
        "backbone",
        "disjointness_ok",
        "s_solver",
        "max_tool_turns",
        "max_revisits",
        "cost_budget_usd",
        "no_training",
    ):
        assert key in d, f"top-level key '{key}' missing from disclosure"
    assert d["backbone"] is None
    assert d["disjointness_ok"] is None
    assert d["s_solver"] is None
    assert d["no_training"] is None
    assert d["max_tool_turns"] == options["max_tool_turns"]
    assert d["max_revisits"] == options["max_revisits"]
    assert d["cost_budget_usd"] == options["cost_budget_usd"]


# ---------------------------------------------------------------------------
# [CF9] result_type dispatch routes EG solver failures correctly through run_ablation_record
# ---------------------------------------------------------------------------

def test_result_type_dispatch_routes_solver_failure(tmp_path: Path) -> None:
    """When SMART-EG returns a solver_failure payload, run_ablation_record must produce
    an AblationFailure (not AblationPrediction)."""
    from tend.ablations.workflow import AblationFailure, AblationPrediction

    settings = _settings()
    wf, log, _ = _workflow(settings, tmp_path / "run")
    dataset_dir = settings.paths.repo_root / "tests" / "fixtures" / "smoke_release"
    record, schema, data = load_solver_release_inputs(
        dataset_dir, db_id="financial", record_id=1001, limit=1
    )[0]
    spec = resolve_ablations("smart_eg_full")[0]

    class _CannedFailure:
        def to_json(self) -> dict[str, Any]:
            return {
                "result_type": "solver_failure",
                "record_id": record["record_id"],
                "db_id": "financial",
                "error_code": "EXECUTION_UNRESOLVED",
                "message": "stub: round-trip failed",
                "MQL": 'db.account.aggregate([{"$match": {"status": "A"}}])',
                "feedback": [{"attempt": 0, "error": "rtv_failed"}],
                "disclosure": {
                    "s_solver": ["smart_eg_agent", "smart_eg_tools"],
                    "backbone": "deepseek-v4-flash",
                    "max_tool_turns": 24,
                    "max_revisits": 2,
                    "cost_budget_usd": 1.0,
                },
            }

    try:
        with patch.object(
            ablation_workflow,
            "smart_solve_record_eg",
            new=AsyncMock(return_value=_CannedFailure()),
        ):
            result = asyncio.run(
                run_ablation_record(wf, spec, record, schema, local_data=data)
            )
    finally:
        log.close()

    payload = result.to_json()

    assert isinstance(result, AblationFailure), (
        f"expected AblationFailure from SolverFailure dispatch, got {type(result).__name__}"
    )
    assert not isinstance(result, AblationPrediction)
    assert payload["result_type"] == "ablation_failure"
    assert payload["status"] == "failed"
    assert payload["error_code"] == "EXECUTION_UNRESOLVED"

    # [H6] feedback had one failed entry -> attempts == 2
    assert payload["attempts"] == 2, (
        f"expected attempts=2 (1 failed + 1 final), got {payload['attempts']}"
    )
