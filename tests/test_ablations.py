from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import tend.ablations.workflow as ablation_workflow
from tend.ablations import ABLATION_IDS, run_ablation_record, run_ablation_suite
from tend.ablations.strategies import (
    BUDGET_PROFILES,
    SmartEGAblationSpec,
    resolve_ablations,
)
from tend.errors import SourceError
from tend.ablations.workflow import (
    _attempt_count,
    _disclosure,
    _failure_from_solver_payload,
    _llm_refs_for,
    _prediction_from_solver_payload,
    _runtime_options,
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
    assert full_options["use_probe_scheduler"] is False
    assert full_options["probe_scheduler_status"] == "unsupported"
    assert "probe_scheduler" not in full_options["mechanism_claims"]
    assert full_options["budget_profile"] == "full"
    assert full_options["effective_budget_profile"] == "full"
    assert full_options["effective_tool_turn_count"] == 48
    assert full_options["budget_disclosure"] == {
        "profile": "full",
        "profile_max_tool_turns": 48,
        "profile_max_revisits": 2,
        "profile_cost_budget_usd": 1.0,
        "effective_max_tool_turns": 48,
        "effective_max_revisits": 2,
        "effective_cost_budget_usd": 1.0,
        "mechanism_overrides": [],
        "runtime_overrides_applied": [],
        "budget_profile_locked": False,
        "source": "ablation_spec",
    }

    toggles = {
        "smart_eg_no_evidence_gate": "use_evidence_gate",
        "smart_eg_no_counterexample": "use_counterexample",
        "smart_eg_no_value_grounding": "use_value_grounding",
        "smart_eg_no_relationship_probe": "use_relationship_probe",
        "smart_eg_no_prefix_execution": "use_prefix_execution",
        "smart_eg_no_revisit": "use_revisit",
    }
    for ablation_id, disabled_key in toggles.items():
        options = resolve_ablations(ablation_id)[0].to_runtime_options()
        assert options[disabled_key] is False
        assert options["budget_profile"] == "reference"
        assert options["max_tool_turns"] == 48
        assert options["cost_budget_usd"] == 1.0

    assert resolve_ablations("smart_eg_budget_low")[0].to_runtime_options() == {
        **full_options,
        "ablation_id": "smart_eg_budget_low",
        "solver_variant": "smart_eg_budget_low",
        "budget_profile": "low",
        "effective_budget_profile": "low",
        "effective_tool_turn_count": 8,
        "budget_disclosure": {
            "profile": "low",
            "profile_max_tool_turns": 8,
            "profile_max_revisits": 0,
            "profile_cost_budget_usd": 0.25,
            "effective_max_tool_turns": 8,
            "effective_max_revisits": 0,
            "effective_cost_budget_usd": 0.25,
            "mechanism_overrides": [],
            "runtime_overrides_applied": [],
            "budget_profile_locked": True,
            "source": "ablation_spec",
        },
        "max_tool_turns": 8,
        "max_revisits": 0,
        "cost_budget_usd": 0.25,
    }
    assert resolve_ablations("smart_eg_budget_medium")[0].to_runtime_options() == {
        **full_options,
        "ablation_id": "smart_eg_budget_medium",
        "solver_variant": "smart_eg_budget_medium",
        "budget_profile": "medium",
        "effective_budget_profile": "medium",
        "effective_tool_turn_count": 24,
        "budget_disclosure": {
            "profile": "medium",
            "profile_max_tool_turns": 24,
            "profile_max_revisits": 2,
            "profile_cost_budget_usd": 1.0,
            "effective_max_tool_turns": 24,
            "effective_max_revisits": 2,
            "effective_cost_budget_usd": 1.0,
            "mechanism_overrides": [],
            "runtime_overrides_applied": [],
            "budget_profile_locked": True,
            "source": "ablation_spec",
        },
        "max_tool_turns": 24,
    }
    assert resolve_ablations("smart_eg_budget_high")[0].to_runtime_options() == {
        **full_options,
        "ablation_id": "smart_eg_budget_high",
        "solver_variant": "smart_eg_budget_high",
        "budget_profile": "high",
        "effective_budget_profile": "high",
        "effective_tool_turn_count": 72,
        "budget_disclosure": {
            "profile": "high",
            "profile_max_tool_turns": 72,
            "profile_max_revisits": 4,
            "profile_cost_budget_usd": 3.0,
            "effective_max_tool_turns": 72,
            "effective_max_revisits": 4,
            "effective_cost_budget_usd": 3.0,
            "mechanism_overrides": [],
            "runtime_overrides_applied": [],
            "budget_profile_locked": True,
            "source": "ablation_spec",
        },
        "max_tool_turns": 72,
        "max_revisits": 4,
        "cost_budget_usd": 3.0,
    }


def test_registry_rejects_unsupported_probe_scheduler_ablation() -> None:
    with pytest.raises(SourceError, match="smart_eg_no_probe_scheduler"):
        resolve_ablations("smart_eg_no_probe_scheduler")


def test_resolve_ablations_rejects_empty_and_unknown_with_source_error() -> None:
    with pytest.raises(SourceError, match="did not include"):
        resolve_ablations("")
    with pytest.raises(SourceError, match="unknown ablations"):
        resolve_ablations("smart_eg_full,missing_ablation")


def test_budget_variants_are_profiles_not_mechanism_isolation() -> None:
    assert set(BUDGET_PROFILES) == {"full", "reference", "low", "medium", "high"}
    assert BUDGET_PROFILES["full"].max_tool_turns == 48
    assert BUDGET_PROFILES["reference"].max_tool_turns == 48
    assert BUDGET_PROFILES["low"].max_tool_turns == 8
    assert BUDGET_PROFILES["medium"].max_tool_turns == 24
    assert BUDGET_PROFILES["high"].max_tool_turns == 72

    for profile in ("low", "medium", "high"):
        spec = resolve_ablations(f"smart_eg_budget_{profile}")[0]
        options = spec.to_runtime_options()
        assert spec.title == f"{profile.title()} budget profile"
        assert "budget profile" in spec.description
        assert "cost budget" not in spec.description.lower()
        assert all("cost budget" not in item.lower() for item in spec.limitations)
        assert options["budget_profile"] == profile
        assert options["cost_budget_usd_source"] == "provider_cost_usd_if_available"
        assert options["cost_budget_usd_unpriced_behavior"] == "advisory_when_unpriced"

    full = resolve_ablations("smart_eg_full")[0]
    assert full.to_runtime_options()["budget_profile"] == "full"


def test_all_ablation_options_expose_policy_budget_and_transition_intent() -> None:
    expected_effective_budgets = {
        "smart_eg_full": ("full", 13, 1, 0.75),
        "smart_eg_no_evidence_gate": ("reference", 13, 1, 0.75),
        "smart_eg_no_counterexample": ("reference", 13, 1, 0.75),
        "smart_eg_no_value_grounding": ("reference", 13, 1, 0.75),
        "smart_eg_no_relationship_probe": ("reference", 13, 1, 0.75),
        "smart_eg_no_prefix_execution": ("reference", 13, 1, 0.75),
        "smart_eg_no_revisit": ("reference", 13, 0, 0.75),
        "smart_eg_budget_low": ("low", 8, 0, 0.25),
        "smart_eg_budget_medium": ("medium", 24, 2, 1.0),
        "smart_eg_budget_high": ("high", 72, 4, 3.0),
    }
    by_id: dict[str, dict[str, Any]] = {}
    for spec in resolve_ablations("all"):
        options = _runtime_options(
            spec,
            max_tool_turns=13,
            max_revisits=1,
            cost_budget_usd=0.75,
            db_id="financial",
            record_id=1001,
        )
        by_id[spec.id] = options

        profile_name, max_turns, max_revisits, cost_budget = expected_effective_budgets[
            spec.id
        ]
        profile = BUDGET_PROFILES[profile_name]
        budget = options["budget_disclosure"]
        assert options["budget_profile"] == profile_name
        assert options["effective_budget_profile"] == profile_name
        assert options["max_tool_turns"] == max_turns
        assert options["max_revisits"] == max_revisits
        assert options["cost_budget_usd"] == cost_budget
        assert budget["profile"] == profile_name
        assert budget["profile_max_tool_turns"] == profile.max_tool_turns
        assert budget["profile_max_revisits"] == profile.max_revisits
        assert budget["profile_cost_budget_usd"] == profile.cost_budget_usd
        assert budget["effective_max_tool_turns"] == max_turns
        assert budget["effective_max_revisits"] == max_revisits
        assert budget["effective_cost_budget_usd"] == cost_budget
        if spec.id.startswith("smart_eg_budget_"):
            assert budget["budget_profile_locked"] is True
            assert budget["runtime_overrides_applied"] == []
        else:
            assert budget["budget_profile_locked"] is False
            expected_runtime_overrides = ["max_tool_turns", "cost_budget_usd"]
            if spec.id != "smart_eg_no_revisit":
                expected_runtime_overrides.insert(1, "max_revisits")
            assert budget["runtime_overrides_applied"] == expected_runtime_overrides
        assert options["tool_exposure_intent"]["probe_scheduler"] == "unsupported"
        assert options["probe_scheduler_status"] == "unsupported"
        assert options["use_probe_scheduler"] is False
        assert "probe_scheduler" not in options["mechanism_claims"]

    assert by_id["smart_eg_no_evidence_gate"]["gate_flags"] == {
        "evidence_gate": False,
        "evidence_debt_blocking": False,
        "counterexample_gate": True,
        "value_grounding_gate": True,
    }
    assert by_id["smart_eg_no_evidence_gate"]["policy_options"][
        "block_evidence_debt"
    ] is False

    no_value = by_id["smart_eg_no_value_grounding"]
    assert no_value["tool_exposure_intent"]["value_grounding"] == "disabled"
    assert no_value["prompt_intent"]["value_grounding"] == "disabled"
    assert no_value["gate_flags"]["value_grounding_gate"] is False
    assert no_value["policy_options"]["expose_value_grounding_tools"] is False
    assert no_value["policy_options"]["include_value_grounding_prompt"] is False
    assert no_value["policy_options"]["block_value_grounding_debt"] is False

    no_prefix = by_id["smart_eg_no_prefix_execution"]
    assert no_prefix["tool_exposure_intent"]["prefix_execution"] == "disabled"
    assert no_prefix["policy_options"]["expose_prefix_execution_tools"] is False

    no_revisit = by_id["smart_eg_no_revisit"]
    assert no_revisit["state_transition_intent"] == {
        "revisit": "disabled",
        "backward_mode_shift": "rejected_by_policy",
    }
    assert no_revisit["policy_options"]["allow_revisit"] is False
    assert no_revisit["policy_options"]["allow_backward_mode_shift"] is False
    assert no_revisit["budget_disclosure"]["mechanism_overrides"] == ["max_revisits"]


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
        input_mode: str,
        nlq_track: str,
        nlq_hash: str | None,
        witness_k: int,
        evaluation_skip_reason: str | None,
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
                "input_mode": input_mode,
                "nlq_track": nlq_track,
                "nlq_hash": nlq_hash,
                "witness_k": witness_k,
                "evaluation_skip_reason": evaluation_skip_reason,
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
    assert by_id["smart_eg_full"]["budget_profile"] == "full"
    assert by_id["smart_eg_full"]["budget_disclosure"]["runtime_overrides_applied"] == [
        "max_tool_turns",
        "max_revisits",
        "cost_budget_usd",
    ]
    assert by_id["smart_eg_budget_low"]["max_tool_turns"] == 8
    assert by_id["smart_eg_budget_low"]["max_revisits"] == 0
    assert by_id["smart_eg_budget_low"]["cost_budget_usd"] == 0.25
    assert by_id["smart_eg_budget_low"]["budget_profile"] == "low"
    assert by_id["smart_eg_budget_medium"]["max_tool_turns"] == 24
    assert by_id["smart_eg_budget_medium"]["max_revisits"] == 2
    assert by_id["smart_eg_budget_medium"]["cost_budget_usd"] == 1.0
    assert by_id["smart_eg_budget_medium"]["budget_profile"] == "medium"
    assert by_id["smart_eg_budget_high"]["max_tool_turns"] == 72
    assert by_id["smart_eg_budget_high"]["max_revisits"] == 4
    assert by_id["smart_eg_budget_high"]["cost_budget_usd"] == 3.0
    assert by_id["smart_eg_budget_high"]["budget_profile"] == "high"
    assert all(item["witness_preloaded"] is False for item in captured)
    assert all(item["input_mode"] == "release" for item in captured)
    assert all(item["nlq_track"] == "record" for item in captured)
    assert all(item["nlq_hash"] is None for item in captured)
    assert all(item["witness_k"] == 3 for item in captured)
    assert all(item["evaluation_skip_reason"] is None for item in captured)

    assert progress_summary["tasks"]["started"] == progress_summary["tasks"]["ok"]
    assert progress_summary["tasks"]["fail"] == 0
    assert progress_summary["anomaly_total"] == 0

    run_dir = tmp_path / "run"
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert any(event["event"] == "ablation_suite_start" for event in events)
    assert not (run_dir / "anomalies.jsonl").read_text(encoding="utf-8").strip()


def test_ablation_suite_serialization_failure_becomes_typed_row(tmp_path: Path) -> None:
    settings = _settings()
    wf, log, _progress = _workflow(settings, tmp_path / "run")
    dataset_dir = settings.paths.repo_root / "tests" / "fixtures" / "smoke_release"

    class _BadResult:
        def to_json(self) -> dict[str, Any]:
            raise TypeError("not JSON serializable")

    async def fake_run_ablation_record(
        *_args: Any,
        **_kwargs: Any,
    ) -> Any:
        return _BadResult()

    try:
        with patch.object(ablation_workflow, "run_ablation_record", new=fake_run_ablation_record):
            outputs = asyncio.run(
                run_ablation_suite(
                    wf,
                    dataset_dir=dataset_dir,
                    ablation_selection="smart_eg_full",
                    db_id="financial",
                    record_id=1001,
                    limit=1,
                    workers=2,
                )
            )
    finally:
        log.close()

    assert len(outputs) == 1
    failure = outputs[0]
    assert failure["result_type"] == "ablation_failure"
    assert failure["status"] == "failed"
    assert failure["ablation_id"] == "smart_eg_full"
    assert failure["db_id"] == "financial"
    assert failure["record_id"] == 1001
    assert failure["error_code"] == "internal"
    assert "not JSON serializable" in failure["message"]

    run_dir = tmp_path / "run"
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    start_event = next(event for event in events if event["event"] == "ablation_suite_start")
    assert start_event["workers"] == 2
    anomalies = [
        json.loads(line)
        for line in (run_dir / "anomalies.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert anomalies[0]["stage"] == "ablation_worker"


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
        input_mode: str,
        nlq_track: str,
        nlq_hash: str | None,
        witness_k: int,
        evaluation_skip_reason: str | None,
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
                "input_mode": input_mode,
                "nlq_track": nlq_track,
                "nlq_hash": nlq_hash,
                "witness_k": witness_k,
                "evaluation_skip_reason": evaluation_skip_reason,
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
        assert item["input_mode"] == "nlq_db"
        assert item["nlq_track"] == "canonical"
        assert item["nlq_hash"] == "sha256:" + hashlib.sha256(
            "Find Modern banned card printings.".encode("utf-8")
        ).hexdigest()
        assert item["witness_k"] == 3
        assert item["evaluation_skip_reason"] == "no_release_dataset"


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

    # Top-level solver identity keys must be present and match solver_disclosure values.
    assert d["backbone"] == "deepseek-v4-flash"
    assert d["disjointness_ok"] is True
    assert d["s_solver"] == ["smart_eg_agent", "smart_eg_tools"]
    assert d["max_tool_turns"] == options["max_tool_turns"]
    assert d["max_revisits"] == options["max_revisits"]
    assert d["cost_budget_usd"] == 1.0
    assert d["budget_profile"] == "full"
    assert d["solver_reported_max_tool_turns"] == 24
    assert d["solver_reported_max_revisits"] == 2
    assert d["solver_reported_cost_budget_usd"] == 1.0
    assert d["cost_budget_usd_source"] == "provider_cost_usd_if_available"
    assert d["cost_budget_usd_unpriced_behavior"] == "advisory_when_unpriced"
    assert d["no_training"] is True
    assert d["effective_budget_profile"] == "full"
    assert d["effective_tool_turn_count"] == 48
    assert d["tool_exposure_intent"]["probe_scheduler"] == "unsupported"
    assert "probe_scheduler" not in d["mechanism_claims"]

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
        "budget_profile",
        "cost_budget_usd_source",
        "cost_budget_usd_unpriced_behavior",
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
    assert d["budget_profile"] == options["budget_profile"]
    assert d["cost_budget_usd_source"] == options["cost_budget_usd_source"]
    assert d["cost_budget_usd_unpriced_behavior"] == options[
        "cost_budget_usd_unpriced_behavior"
    ]


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


def test_llm_refs_for_filters_by_ablation_db_and_record(tmp_path: Path) -> None:
    settings = _settings()
    wf, log, _ = _workflow(settings, tmp_path / "run")
    events_path = tmp_path / "run" / "events.jsonl"
    events = [
        {
            "event": "llm_response",
            "ablation_id": "smart_eg_full",
            "db_id": "financial",
            "record_id": 1001,
            "transcript_ref": "llm/match.md",
            "diagnostics_ref": "llm/match.diagnostics.json",
        },
        {
            "event": "llm_response",
            "ablation_id": "smart_eg_full",
            "db_id": "financial",
            "record_id": "1001",
            "transcript_ref": "llm/match-string-record.md",
            "diagnostics_ref": "llm/match-string-record.diagnostics.json",
        },
        {
            "event": "llm_response",
            "ablation_id": "smart_eg_no_revisit",
            "db_id": "financial",
            "record_id": 1001,
            "transcript_ref": "llm/wrong-ablation.md",
            "diagnostics_ref": "llm/wrong-ablation.diagnostics.json",
        },
        {
            "event": "llm_response",
            "ablation_id": "smart_eg_full",
            "db_id": "cards",
            "record_id": 1001,
            "transcript_ref": "llm/wrong-db.md",
            "diagnostics_ref": "llm/wrong-db.diagnostics.json",
        },
        {
            "event": "llm_response",
            "ablation_id": "smart_eg_full",
            "db_id": "financial",
            "record_id": 2002,
            "transcript_ref": "llm/wrong-record.md",
            "diagnostics_ref": "llm/wrong-record.diagnostics.json",
        },
        {
            "event": "llm_response",
            "ablation_id": "smart_eg_full",
            "db_id": "financial",
            "record_id": 1001,
            "transcript_ref": "llm/match.md",
            "diagnostics_ref": "llm/match.diagnostics.json",
        },
    ]
    try:
        events_path.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
        refs = _llm_refs_for(wf, "smart_eg_full", "financial", 1001)
    finally:
        log.close()

    assert refs == {
        "transcript_refs": ["llm/match.md", "llm/match-string-record.md"],
        "diagnostics_refs": [
            "llm/match.diagnostics.json",
            "llm/match-string-record.diagnostics.json",
        ],
    }


def test_prediction_and_failure_rows_preserve_traceability_refs_and_options(
    tmp_path: Path,
) -> None:
    wf, log = _make_wf(tmp_path)
    spec = resolve_ablations("smart_eg_budget_low")[0]
    options = _runtime_options(
        spec,
        max_tool_turns=12,
        max_revisits=1,
        cost_budget_usd=0.5,
        batch_index=3,
        db_id="financial",
        record_id=1001,
        input_mode="nlq_db",
        nlq_track="manual",
        nlq_hash="abc123",
        witness_k=5,
        evaluation_skip_reason="no_release_dataset",
    )
    prediction_payload = {
        "record_id": 1001,
        "db_id": "financial",
        "MQL": "db.account.aggregate([])",
        "feedback": [],
        "disclosure": {"budget_profile": "low"},
    }
    failure_payload = {
        **prediction_payload,
        "result_type": "solver_failure",
        "error_code": "EXECUTION_UNRESOLVED",
        "message": "failed",
        "error_refs": ["solve/sessions/session/errors.jsonl#1"],
    }

    try:
        prediction = _prediction_from_solver_payload(
            wf,
            spec,
            options,
            prediction_payload,
            local_data=None,
            transcript_refs=["llm/prediction.md"],
            diagnostics_refs=["llm/prediction.diagnostics.json"],
        )
        failure = _failure_from_solver_payload(
            wf,
            spec,
            options,
            failure_payload,
            local_data=None,
            transcript_refs=["llm/failure.md"],
            diagnostics_refs=["llm/failure.diagnostics.json"],
        )
    finally:
        log.close()

    prediction_row = prediction.to_json()
    failure_row = failure.to_json()
    for row in (prediction_row, failure_row):
        assert row["session_id"] == options["session_id"]
        assert row["ablation_id"] == "smart_eg_budget_low"
        assert row["batch_index"] == 3
        assert row["work_item_id"] == "ablation:3:smart_eg_budget_low:financial:1001"
        assert row["max_tool_turns"] == 8
        assert row["max_revisits"] == 0
        assert row["cost_budget_usd"] == 0.25
        assert row["input_mode"] == "nlq_db"
        assert row["nlq_track"] == "manual"
        assert row["nlq_hash"] == "abc123"
        assert row["witness_k"] == 5
        assert row["evaluation_skip_reason"] == "no_release_dataset"
        assert "uses_probe_scheduler" not in row
        assert row["disclosure"]["options"]["budget_profile"] == "low"
        assert row["disclosure"]["budget_profile"] == "low"
        assert row["disclosure"]["effective_budget_profile"] == "low"
        assert row["disclosure"]["effective_tool_turn_count"] == 8
        assert row["disclosure"]["input_mode"] == "nlq_db"
        assert row["disclosure"]["nlq_track"] == "manual"
        assert row["disclosure"]["nlq_hash"] == "abc123"
        assert row["disclosure"]["witness_k"] == 5
        assert row["disclosure"]["evaluation_skip_reason"] == (
            "no_release_dataset"
        )
        assert row["disclosure"]["tool_exposure_intent"]["probe_scheduler"] == (
            "unsupported"
        )
        assert row["disclosure"]["cost_budget_usd_unpriced_behavior"] == (
            "advisory_when_unpriced"
        )

    assert prediction_row["transcript_refs"] == ["llm/prediction.md"]
    assert prediction_row["diagnostics_refs"] == ["llm/prediction.diagnostics.json"]
    assert failure_row["transcript_refs"] == ["llm/failure.md"]
    assert failure_row["diagnostics_refs"] == ["llm/failure.diagnostics.json"]
    assert failure_row["error_refs"] == ["solve/sessions/session/errors.jsonl#1"]
