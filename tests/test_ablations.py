from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import tend.ablations.workflow as ablation_workflow
from tend.ablations import ABLATION_IDS, run_ablation_record, run_ablation_suite
from tend.ablations.strategies import resolve_ablations
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
from tend.solver.contracts import SolverDisclosure
from tend.solver.workflow import SolverFailure, SmartSolveOptions, load_solver_release_inputs
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
    # [CF3] registry includes both no_shape_model and no_witness_strata as separate entries
    assert ABLATION_IDS == (
        "full_smart",
        "no_shape_model",
        "no_schema_variants",
        "no_witness_strata",
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

    # [CF3] no_shape_model toggles ONLY use_shape_comprehension=False; everything else is default
    full_opts = SmartSolveOptions()
    no_shape_spec = resolve_ablations("no_shape_model")[0]
    no_shape_opts = no_shape_spec.options
    assert no_shape_opts.use_shape_comprehension is False, "no_shape_model must disable shape comprehension"
    assert no_shape_opts.allow_local_witness_strata == full_opts.allow_local_witness_strata, (
        "no_shape_model must NOT touch allow_local_witness_strata (single-variable isolation)"
    )

    # [CF3] no_witness_strata toggles ONLY allow_local_witness_strata=False; shape comprehension untouched
    no_strata_spec = resolve_ablations("no_witness_strata")[0]
    no_strata_opts = no_strata_spec.options
    assert no_strata_opts.allow_local_witness_strata is False, "no_witness_strata must disable witness strata"
    assert no_strata_opts.use_shape_comprehension == full_opts.use_shape_comprehension, (
        "no_witness_strata must NOT touch use_shape_comprehension (single-variable isolation)"
    )


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
    # [ablations F5] disclosure exposes top-level backbone (flattened keys, not only nested solver_disclosure)
    assert all(item["disclosure"]["backbone"] == "deepseek-v4-flash" for item in outputs)
    # nested solver_disclosure still present for full access
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
    spec = resolve_ablations("full_smart")[0]
    options = SmartSolveOptions()
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
    spec = resolve_ablations("full_smart")[0]
    options = SmartSolveOptions()
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
    """_disclosure must hoist backbone/disjointness_ok/s_solver/r_max/witness_k/no_training
    to the top level so ablation disclosures align with baseline disclosure shape."""
    spec = resolve_ablations("full_smart")[0]
    options = SmartSolveOptions()
    solver_disclosure = {
        "backbone": "deepseek-v4-flash",
        "disjointness_ok": True,
        "s_solver": ["smart_intent", "smart_plan", "smart_ms"],
        "r_max": 2,
        "witness_k": 3,
        "no_training": True,
    }
    d = _disclosure(spec, options, solver_disclosure)

    # Top-level keys must be present and match solver_disclosure values
    assert d["backbone"] == "deepseek-v4-flash"
    assert d["disjointness_ok"] is True
    assert d["s_solver"] == ["smart_intent", "smart_plan", "smart_ms"]
    assert d["r_max"] == 2
    assert d["witness_k"] == 3
    assert d["no_training"] is True

    # solver_disclosure still available nested
    assert d["solver_disclosure"] == solver_disclosure


def test_disclosure_top_level_none_when_solver_disclosure_empty() -> None:
    """When solver_disclosure is empty, top-level comparable keys are None (not KeyError)."""
    spec = resolve_ablations("full_smart")[0]
    options = SmartSolveOptions()
    d = _disclosure(spec, options, {})

    for key in ("backbone", "disjointness_ok", "s_solver", "r_max", "witness_k", "no_training"):
        assert key in d, f"top-level key '{key}' missing from disclosure"
        assert d[key] is None, f"expected None for '{key}' when solver_disclosure empty, got {d[key]!r}"


# ---------------------------------------------------------------------------
# [CF9] isinstance dispatch routes SolverFailure correctly through run_ablation_record
# ---------------------------------------------------------------------------

def test_isinstance_dispatch_routes_solver_failure(tmp_path: Path) -> None:
    """When smart_solve_record returns a SolverFailure, run_ablation_record must produce
    an AblationFailure (not AblationPrediction), confirming the isinstance branch fires."""
    from tend.ablations.workflow import AblationFailure, AblationPrediction

    settings = _settings()
    wf, log, _ = _workflow(settings, tmp_path / "run")
    dataset_dir = settings.paths.repo_root / "tests" / "fixtures" / "smoke_release"
    record, schema, data = load_solver_release_inputs(
        dataset_dir, db_id="financial", record_id=1001, limit=1
    )[0]
    spec = resolve_ablations("full_smart")[0]

    # Craft a SolverFailure with a recognizable MQL so static_feedback is non-empty
    canned_failure = SolverFailure(
        record_id=record["record_id"],
        db_id="financial",
        error_code="rtv_failed",
        message="stub: round-trip failed",
        disclosure=SolverDisclosure(
            s_solver=["smart_intent", "smart_plan", "smart_ms"],
            backbone="deepseek-v4-flash",
            r_max=2,
            witness_k=3,
        ),
        shape_model={},
        logical_spec={},
        physical_plan={},
        feedback=[{"attempt": 0, "error": "rtv_failed"}],
    )

    try:
        with patch.object(
            ablation_workflow,
            "smart_solve_record",
            new=AsyncMock(return_value=canned_failure),
        ):
            result = asyncio.run(
                run_ablation_record(wf, spec, record, schema, local_data=data)
            )
    finally:
        log.close()

    payload = result.to_json()

    # [CF9] dispatch must route to AblationFailure
    assert isinstance(result, AblationFailure), (
        f"expected AblationFailure from SolverFailure dispatch, got {type(result).__name__}"
    )
    assert not isinstance(result, AblationPrediction)
    assert payload["result_type"] == "ablation_failure"
    assert payload["status"] == "failed"
    assert payload["error_code"] == "rtv_failed"

    # [H6] feedback had one failed entry -> attempts == 2
    assert payload["attempts"] == 2, (
        f"expected attempts=2 (1 failed + 1 final), got {payload['attempts']}"
    )
