from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import tend.cli as cli
import tend.config as config_module
from tend.errors import ContractViolationError


@pytest.fixture(autouse=True)
def clean_cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "load_dotenv", lambda _path: {})
    for key in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "TEND_BIRD_ROOT",
        "TEND_DATASET_OUT",
        "TEND_LLM_STUB",
        "TEND_QUIET",
    ):
        monkeypatch.delenv(key, raising=False)


def test_print_evaluation_block_distinguishes_skip_reasons(capsys: pytest.CaptureFixture[str]) -> None:
    cli._print_evaluation_block(None, evaluate=False, skip_reason="disabled")
    cli._print_evaluation_block(None, evaluate=True, skip_reason="no_release_dataset")
    cli._print_evaluation_block(None, evaluate=True, skip_reason="no_predictions")

    output = capsys.readouterr().out

    assert "evaluation : disabled (--no-eval)" in output
    assert "evaluation : skipped (NLQ+DB mode has no release evaluation dataset)" in output
    assert "evaluation : skipped (no predictions)" in output


def test_print_evaluation_block_uses_ablation_per_system_headline(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    evaluation = SimpleNamespace(
        status="partial",
        report={
            "scores": {"EX": 0.5, "EFM": 0.5, "EVM": 0.5},
            "headline": {
                "mode": "per_system",
                "systems": {
                    "smart_eg_full": {
                        "scores": {"EX": 1.0, "EFM": 1.0, "EVM": 1.0},
                        "delta_vs_smart_eg_full": {"EX": 0.0},
                    },
                    "smart_eg_no_evidence_gate": {
                        "scores": {"EX": 0.0, "EFM": 0.0, "EVM": 0.0},
                        "delta_vs_smart_eg_full": {"EX": -1.0},
                    },
                },
            },
        },
        paths=SimpleNamespace(report_md=tmp_path / "report.md"),
    )

    cli._print_evaluation_block(evaluation)
    output = capsys.readouterr().out

    assert "evaluation : partial per-system EX" in output
    assert "smart_eg_full: EX=1.0" in output
    assert "smart_eg_no_evidence_gate: EX=0.0" in output
    assert "evaluation : partial EX=0.5" not in output


def test_solve_summary_prints_primary_session_refs_without_llm_as_main_log(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    rt = SimpleNamespace(
        settings=SimpleNamespace(
            run_id="run-2026-06-04_12-00-00-abcd",
            stub=True,
            llm=SimpleNamespace(model="stub"),
            run_dir=tmp_path,
        )
    )

    cli._print_solve_summary(
        rt,
        [
            {
                "record_id": 1,
                "db_id": "financial",
                "MQL": "db.account.aggregate([])",
                "agent_session_ref": "agent/solve-smart-eg-session.md",
                "transcript_ref": "llm/smart_eg/call.md",
            }
        ],
        [],
        {},
        tmp_path / "solve" / "solver_predictions.jsonl",
        tmp_path / "solve" / "solver_failures.jsonl",
        evaluate=False,
        skip_reason="disabled",
    )

    output = capsys.readouterr().out

    assert f"logs   : {tmp_path}/events.jsonl | anomalies.jsonl | progress.jsonl" in output
    assert "session refs : agent/solve-smart-eg-session.md" in output
    assert "llm/" not in output


def test_construct_default_output_is_run_dataset_unless_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = config_module._find_repo_root()
    captured: list[tuple[str, Path]] = []

    def fake_build_runtime(settings):
        return SimpleNamespace(
            settings=settings,
            source=SimpleNamespace(db_ids=("financial",)),
        )

    async def fake_run_construct(rt, _db_ids, _phase, _records, **_kwargs):
        captured.append((rt.settings.run_id, rt.settings.paths.dataset_out))
        return 0

    monkeypatch.setattr(cli, "build_runtime", fake_build_runtime)
    monkeypatch.setattr(cli, "_run_construct", fake_run_construct)

    assert cli.main([
        "construct",
        "--stub",
        "--quiet",
        "--run-id",
        "cli-default",
    ]) == 0
    run_id, dataset_out = captured[-1]
    assert run_id != "cli-default"
    assert "cli-default" in run_id
    assert dataset_out == repo / "runs" / run_id / "dataset"

    override = tmp_path / "custom-dataset"
    monkeypatch.setenv("TEND_DATASET_OUT", str(override))
    assert cli.main([
        "construct",
        "--stub",
        "--quiet",
        "--run-id",
        "cli-override",
    ]) == 0
    run_id, dataset_out = captured[-1]
    assert run_id != "cli-override"
    assert "cli-override" in run_id
    assert dataset_out == override


def test_construct_is_native_only_and_rejects_legacy_mode_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    def fake_build_runtime(settings):
        return SimpleNamespace(
            settings=settings,
            source=SimpleNamespace(db_ids=("financial",)),
        )

    async def fake_run_construct(_rt, _db_ids, _phase, _records, **kwargs):
        captured.append(kwargs)
        return 0

    monkeypatch.setattr(cli, "build_runtime", fake_build_runtime)
    monkeypatch.setattr(cli, "_run_construct", fake_run_construct)

    assert cli.main([
        "construct",
        "--stub",
        "--quiet",
        "--dbs",
        "financial",
        "--records",
        "1",
        "--run-id",
        "cli-native-only",
    ]) == 0

    assert "construction_mode" not in captured[-1]

    with pytest.raises(SystemExit) as old_mode:
        cli.main(["construct", "--construction-mode", "legacy", "--stub", "--quiet"])
    assert old_mode.value.code == 2


def test_construct_all_records_uses_source_workload_and_rejects_legacy_knobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeDb:
        query_count = 32

    def fake_run_census(_source, *, db_ids):
        captured["census_db_ids"] = db_ids
        return SimpleNamespace(databases={"financial": FakeDb()})

    def fake_build_runtime(settings):
        return SimpleNamespace(
            settings=settings,
            source=SimpleNamespace(db_ids=("financial",)),
        )

    async def fake_run_construct(_rt, db_ids, _phase, records, **kwargs):
        captured["db_ids"] = db_ids
        captured["records"] = records
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(cli, "run_census", fake_run_census)
    monkeypatch.setattr(cli, "build_runtime", fake_build_runtime)
    monkeypatch.setattr(cli, "_run_construct", fake_run_construct)

    assert cli.main([
        "construct",
        "--stub",
        "--quiet",
        "--dbs",
        "financial",
        "--records",
        "all",
        "--run-id",
        "cli-full-financial",
    ]) == 0
    assert captured["census_db_ids"] == ["financial"]
    assert captured["db_ids"] == ["financial"]
    assert captured["records"] == 32
    assert "structural_only_records" not in captured["kwargs"]

    with pytest.raises(SystemExit) as full_db:
        cli.main(["construct", "--full-db", "--stub", "--quiet"])
    assert full_db.value.code == 2

    with pytest.raises(SystemExit) as structural_fraction:
        cli.main(["construct", "--structural-fraction", "0.2", "--stub", "--quiet"])
    assert structural_fraction.value.code == 2


def test_construct_records_per_db_expands_total_and_passes_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_build_runtime(settings):
        return SimpleNamespace(
            settings=settings,
            source=SimpleNamespace(db_ids=("financial", "toxicology")),
        )

    async def fake_run_construct(_rt, db_ids, _phase, records, **kwargs):
        captured["db_ids"] = db_ids
        captured["records"] = records
        captured["records_per_db"] = kwargs.get("records_per_db")
        return 0

    monkeypatch.setattr(cli, "build_runtime", fake_build_runtime)
    monkeypatch.setattr(cli, "_run_construct", fake_run_construct)

    assert cli.main([
        "construct",
        "--stub",
        "--quiet",
        "--dbs",
        "financial,toxicology",
        "--records",
        "1",
        "--records-per-db",
        "100",
        "--run-id",
        "cli-per-db",
    ]) == 0
    assert captured["db_ids"] == ["financial", "toxicology"]
    assert captured["records"] == 200
    assert captured["records_per_db"] == 100


def test_validate_smoke_relaxes_all_db_composition(capsys: pytest.CaptureFixture[str]) -> None:
    dataset = Path("tests/fixtures/smoke_release")

    assert cli.main(["validate", "--dataset-dir", str(dataset)]) == 1
    full = capsys.readouterr().out
    assert "TEND validate · validation INVALID · mode=full" in full
    assert "[H4] db coverage 1 != 11" in full

    assert cli.main(["validate", "--dataset-dir", str(dataset), "--smoke"]) == 0
    smoke = capsys.readouterr().out
    assert "TEND validate · validation OK · mode=smoke" in smoke
    assert "diversity: mql=" in smoke and "pairs=" in smoke


def test_publish_refuses_invalid_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalid = tmp_path / "invalid"
    invalid.mkdir()
    (invalid / "test.json").write_text(json.dumps([]), encoding="utf-8")
    (invalid / "TEND.json").write_text(json.dumps([]), encoding="utf-8")
    out = tmp_path / "release" / "TEND-dataset"

    assert cli.main([
        "publish",
        "--dataset-dir",
        str(invalid),
        "--out",
        str(out),
    ]) == 1

    printed = capsys.readouterr().out
    assert "TEND publish · validation INVALID · mode=full" in printed
    assert "publish refused" in printed
    assert not out.exists()


def test_materialize_evaluation_dataset_subset_keeps_only_selected_records(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "mongodb_data").mkdir(parents=True)
    (source / "mongodb_schema").mkdir()
    (source / "agent_design_rationale").mkdir()
    records = [
        {"record_id": 1, "db_id": "financial", "MQL": "db.account.aggregate([])"},
        {"record_id": 2, "db_id": "superhero", "MQL": "db.hero.aggregate([])"},
    ]
    (source / "test.json").write_text(json.dumps(records), encoding="utf-8")
    (source / "TEND.json").write_text(json.dumps(records), encoding="utf-8")
    (source / "bird_db_catalog.json").write_text(json.dumps({"dbs": ["financial"]}), encoding="utf-8")
    (source / "mongodb_data" / "financial.json").write_text(json.dumps({"account": []}), encoding="utf-8")
    (source / "mongodb_schema" / "financial.json").write_text(json.dumps({"account": {}}), encoding="utf-8")
    (source / "mongodb_data" / "superhero.json").write_text(json.dumps({"hero": []}), encoding="utf-8")
    (source / "mongodb_schema" / "superhero.json").write_text(json.dumps({"hero": {}}), encoding="utf-8")
    (source / "agent_design_rationale" / "financial.yaml").write_text("financial: true\n", encoding="utf-8")
    (source / "agent_design_rationale" / "superhero.yaml").write_text("superhero: true\n", encoding="utf-8")

    subset = cli._materialize_evaluation_dataset_subset(
        source,
        [records[0]],
        tmp_path / "subset",
    )

    assert json.loads((subset / "test.json").read_text(encoding="utf-8")) == [records[0]]
    assert json.loads((subset / "TEND.json").read_text(encoding="utf-8")) == [records[0]]
    manifest = json.loads((subset / "evaluation_selection.json").read_text(encoding="utf-8"))
    assert manifest["selected_record_count"] == 1
    assert manifest["selected_records"] == [{"db_id": "financial", "record_id": 1}]
    assert (subset / "mongodb_data" / "financial.json").exists()
    assert (subset / "mongodb_schema" / "financial.json").exists()
    assert (subset / "agent_design_rationale" / "financial.yaml").exists()
    assert (subset / "bird_db_catalog.json").exists()
    assert not (subset / "mongodb_data" / "superhero.json").exists()
    assert not (subset / "mongodb_schema" / "superhero.json").exists()


def test_materialize_evaluation_dataset_subset_supports_release_package_layout(
    tmp_path: Path,
) -> None:
    source = tmp_path / "release" / "tend-native-mongodb-v1"
    (source / "data").mkdir(parents=True)
    (source / "schema" / "mongodb_schema").mkdir(parents=True)
    (source / "mongodb_data").mkdir()
    (source / "metadata" / "agent_design_rationale").mkdir(parents=True)
    records = [
        {"record_id": 1, "db_id": "financial", "MQL": "db.account.aggregate([])"},
        {"record_id": 2, "db_id": "superhero", "MQL": "db.hero.aggregate([])"},
    ]
    (source / "data" / "test.json").write_text(json.dumps(records), encoding="utf-8")
    (source / "data" / "TEND.json").write_text(json.dumps(records), encoding="utf-8")
    (source / "data" / "bird_db_catalog.json").write_text(
        json.dumps({"dbs": ["financial"]}),
        encoding="utf-8",
    )
    (source / "mongodb_data" / "financial.json").write_text(
        json.dumps({"account": []}),
        encoding="utf-8",
    )
    (source / "schema" / "mongodb_schema" / "financial.json").write_text(
        json.dumps({"account": {}}),
        encoding="utf-8",
    )
    (source / "mongodb_data" / "superhero.json").write_text(
        json.dumps({"hero": []}),
        encoding="utf-8",
    )
    (source / "schema" / "mongodb_schema" / "superhero.json").write_text(
        json.dumps({"hero": {}}),
        encoding="utf-8",
    )
    (source / "metadata" / "agent_design_rationale" / "financial.yaml").write_text(
        "financial: true\n",
        encoding="utf-8",
    )

    subset = cli._materialize_evaluation_dataset_subset(
        source,
        [records[0]],
        tmp_path / "subset",
    )

    assert json.loads((subset / "test.json").read_text(encoding="utf-8")) == [records[0]]
    assert json.loads((subset / "TEND.json").read_text(encoding="utf-8")) == [records[0]]
    manifest = json.loads((subset / "evaluation_selection.json").read_text(encoding="utf-8"))
    assert manifest["selected_record_count"] == 1
    assert manifest["source_dataset_dir"] == str(source)
    assert (subset / "bird_db_catalog.json").exists()
    assert (subset / "mongodb_data" / "financial.json").exists()
    assert (subset / "mongodb_schema" / "financial.json").exists()
    assert (subset / "agent_design_rationale" / "financial.yaml").exists()
    assert not (subset / "mongodb_data" / "superhero.json").exists()
    assert not (subset / "mongodb_schema" / "superhero.json").exists()


def test_solve_with_dataset_dir_does_not_require_bird(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("TEND_BIRD_ROOT", str(tmp_path / "missing-bird"))

    def fail_bird_source(*_args, **_kwargs):
        raise AssertionError("solve should not construct BirdSource")

    async def fake_run_solve(rt, **kwargs):
        captured["source"] = rt.source
        captured["dataset_dir"] = kwargs["dataset_dir"]
        captured["nlq_track"] = kwargs["nlq_track"]
        rt.mongo.close()
        rt.log.close()
        return 0

    monkeypatch.setattr(cli, "BirdSource", fail_bird_source)
    monkeypatch.setattr(cli, "_run_solve", fake_run_solve)

    assert cli.main([
        "solve",
        "--dataset-dir",
        "tests/fixtures/smoke_release",
        "--nlq-track",
        "colloquial",
        "--stub",
        "--quiet",
        "--run-id",
        "solve-no-bird",
    ]) == 0
    assert captured["source"] is None
    assert captured["dataset_dir"] == (
        config_module._find_repo_root() / "tests" / "fixtures" / "smoke_release"
    )
    assert captured["nlq_track"] == "colloquial"


def test_baseline_with_dataset_dir_does_not_require_bird(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("TEND_BIRD_ROOT", str(tmp_path / "missing-bird"))

    def fail_bird_source(*_args, **_kwargs):
        raise AssertionError("baseline should not construct BirdSource")

    async def fake_run_baseline(rt, **kwargs):
        captured["source"] = rt.source
        captured["dataset_dir"] = kwargs["dataset_dir"]
        captured["baselines"] = kwargs["baselines"]
        captured["nlq_track"] = kwargs["nlq_track"]
        rt.mongo.close()
        rt.log.close()
        return 0

    monkeypatch.setattr(cli, "BirdSource", fail_bird_source)
    monkeypatch.setattr(cli, "_run_baseline", fake_run_baseline)

    assert cli.main([
        "baseline",
        "--dataset-dir",
        "tests/fixtures/smoke_release",
        "--baselines",
        "direct,schema_direct",
        "--nlq-track",
        "colloquial",
        "--stub",
        "--quiet",
        "--run-id",
        "baseline-no-bird",
    ]) == 0
    assert captured["source"] is None
    assert captured["baselines"] == "direct,schema_direct"
    assert captured["dataset_dir"] == (
        config_module._find_repo_root() / "tests" / "fixtures" / "smoke_release"
    )
    assert captured["nlq_track"] == "colloquial"


def test_ablation_with_dataset_dir_does_not_require_bird(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("TEND_BIRD_ROOT", str(tmp_path / "missing-bird"))

    def fail_bird_source(*_args, **_kwargs):
        raise AssertionError("ablation should not construct BirdSource")

    async def fake_run_ablation(rt, **kwargs):
        captured["source"] = rt.source
        captured["dataset_dir"] = kwargs["dataset_dir"]
        captured["ablations"] = kwargs["ablations"]
        captured["nlq_track"] = kwargs["nlq_track"]
        captured["workers"] = kwargs["workers"]
        rt.mongo.close()
        rt.log.close()
        return 0

    monkeypatch.setattr(cli, "BirdSource", fail_bird_source)
    monkeypatch.setattr(cli, "_run_ablation", fake_run_ablation)

    assert cli.main([
        "ablation",
        "--dataset-dir",
        "tests/fixtures/smoke_release",
        "--ablations",
        "smart_eg_full,smart_eg_no_evidence_gate",
        "--workers",
        "3",
        "--nlq-track",
        "colloquial",
        "--stub",
        "--quiet",
        "--run-id",
        "ablation-no-bird",
    ]) == 0
    assert captured["source"] is None
    assert captured["ablations"] == "smart_eg_full,smart_eg_no_evidence_gate"
    assert captured["workers"] == 3
    assert captured["dataset_dir"] == (
        config_module._find_repo_root() / "tests" / "fixtures" / "smoke_release"
    )
    assert captured["nlq_track"] == "colloquial"


def test_baseline_cli_accepts_nlq_db_only_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("TEND_BIRD_ROOT", str(tmp_path / "missing-bird"))

    def fail_bird_source(*_args, **_kwargs):
        raise AssertionError("baseline should not construct BirdSource")

    async def fake_run_baseline(rt, **kwargs):
        captured["source"] = rt.source
        captured["db_id"] = kwargs["db_id"]
        captured["nlq"] = kwargs["nlq"]
        captured["baselines"] = kwargs["baselines"]
        rt.mongo.close()
        rt.log.close()
        return 0

    monkeypatch.setattr(cli, "BirdSource", fail_bird_source)
    monkeypatch.setattr(cli, "_run_baseline", fake_run_baseline)

    assert cli.main([
        "baseline",
        "--db-id",
        "manual_formula",
        "--nlq",
        "List race weekends with Finished status buckets.",
        "--baselines",
        "direct",
        "--stub",
        "--quiet",
        "--run-id",
        "baseline-nlq-db",
    ]) == 0
    assert captured["source"] is None
    assert captured["db_id"] == "manual_formula"
    assert captured["nlq"] == "List race weekends with Finished status buckets."
    assert captured["baselines"] == "direct"


def test_ablation_cli_accepts_nlq_db_only_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("TEND_BIRD_ROOT", str(tmp_path / "missing-bird"))

    def fail_bird_source(*_args, **_kwargs):
        raise AssertionError("ablation should not construct BirdSource")

    async def fake_run_ablation(rt, **kwargs):
        captured["source"] = rt.source
        captured["db_id"] = kwargs["db_id"]
        captured["nlq"] = kwargs["nlq"]
        captured["ablations"] = kwargs["ablations"]
        rt.mongo.close()
        rt.log.close()
        return 0

    monkeypatch.setattr(cli, "BirdSource", fail_bird_source)
    monkeypatch.setattr(cli, "_run_ablation", fake_run_ablation)

    assert cli.main([
        "ablation",
        "--db-id",
        "manual_cards",
        "--nlq",
        "Find Modern banned card printings.",
        "--ablations",
        "smart_eg_full",
        "--stub",
        "--quiet",
        "--run-id",
        "ablation-nlq-db",
    ]) == 0
    assert captured["source"] is None
    assert captured["db_id"] == "manual_cards"
    assert captured["nlq"] == "Find Modern banned card printings."
    assert captured["ablations"] == "smart_eg_full"


def test_solve_cli_accepts_smart_eg_budget_flags_and_rejects_old_knobs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("TEND_BIRD_ROOT", str(tmp_path / "missing-bird"))

    def fake_build_solver_runtime(settings, *, run_kind: str = "solver"):
        return SimpleNamespace(
            settings=settings,
            source=None,
            mongo=SimpleNamespace(close=lambda: None),
            log=SimpleNamespace(close=lambda: None),
        )

    async def fake_run_solve(rt, **kwargs):
        captured["source"] = rt.source
        captured.update(kwargs)
        rt.mongo.close()
        rt.log.close()
        return 0

    monkeypatch.setattr(cli, "build_solver_runtime", fake_build_solver_runtime)
    monkeypatch.setattr(cli, "_run_solve", fake_run_solve)

    assert cli.main([
        "solve",
        "--dataset-dir",
        "tests/fixtures/smoke_release",
        "--max-tool-turns",
        "17",
        "--max-revisits",
        "3",
        "--cost-budget-usd",
        "2.5",
        "--solver-option",
        "use_counterexample=false",
        "--solver-option",
        "use_value_grounding=0",
        "--stub",
        "--quiet",
        "--run-id",
        "solve-smart-eg-flags",
    ]) == 0

    assert captured["source"] is None
    assert captured["max_tool_turns"] == 17
    assert captured["max_revisits"] == 3
    assert captured["cost_budget_usd"] == 2.5
    assert captured["solver_options"] == {
        "use_counterexample": False,
        "use_value_grounding": False,
    }
    assert "r_max" not in captured
    assert "witness_k" not in captured

    with pytest.raises(SystemExit) as old_r_max:
        cli.main(["solve", "--r-max", "1", "--stub", "--quiet"])
    assert old_r_max.value.code == 2

    with pytest.raises(SystemExit) as old_witness_k:
        cli.main(["solve", "--witness-k", "2", "--stub", "--quiet"])
    assert old_witness_k.value.code == 2


def test_smart_solve_record_forwards_solver_options(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_solve_nlq_db(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return SimpleNamespace(to_json=lambda: {"result_type": "solver_prediction"})

    monkeypatch.setattr(cli, "smart_solve_nlq_db_eg", fake_solve_nlq_db)

    result = asyncio.run(
        cli.smart_solve_record_eg(
            "wf",
            {
                "db_id": "financial",
                "record_id": 31131,
                "NLQ": "canonical question",
            },
            {},
            max_tool_turns=13,
            options={"use_counterexample": False, "use_value_grounding": False},
        )
    )

    assert result.to_json()["result_type"] == "solver_prediction"
    assert captured["db_id"] == "financial"
    assert captured["nlq"] == "canonical question"
    assert captured["max_tool_turns"] == 13
    assert captured["options"] == {"use_counterexample": False, "use_value_grounding": False}


def test_run_solve_writes_failures_separately(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from tend.agents import AgentContext
    from tend.config import Settings
    from tend.observability import make_reporter, setup_logging
    from tend.workflow import Workflow

    settings = Settings.from_env(
        run_id="solver-failure-cli-test",
        overrides={"TEND_LLM_STUB": "1"},
        require_bird=False,
    )
    settings = replace(settings, paths=replace(settings.paths, runs=tmp_path / "runs"))
    log = setup_logging(tmp_path / "run", console=False)
    progress = make_reporter(settings.run_id, log, enabled=False)
    ctx = AgentContext(settings=settings, llm=None, log=log, progress=progress, mongo=None)
    rt = cli.Runtime(
        settings,
        ctx,
        Workflow(ctx),
        progress,
        log,
        None,
        SimpleNamespace(close=lambda: None),
    )

    class FakeFailure:
        def to_json(self) -> dict:
            return {
                "result_type": "solver_failure",
                "record_id": 1001,
                "db_id": "financial",
                "error_code": "SOLVER_EXHAUSTED",
                "message": "forced failure",
            }

    monkeypatch.setattr(
        cli,
        "load_solver_release_inputs",
        lambda *_args, **_kwargs: [({"record_id": 1001, "db_id": "financial"}, {}, None)],
    )

    async def fake_solve_record(*_args, **_kwargs):
        return FakeFailure()

    monkeypatch.setattr(cli, "smart_solve_record_eg", fake_solve_record)

    rc = __import__("asyncio").run(
        cli._run_solve(
            rt,
            dataset_dir=tmp_path,
            db_id=None,
            record_id=None,
            limit=1,
            max_tool_turns=1,
            max_revisits=0,
            cost_budget_usd=0.1,
        )
    )

    assert rc == 1
    predictions = settings.run_dir / "solve" / "solver_predictions.jsonl"
    assert predictions.exists()
    assert predictions.read_text(encoding="utf-8") == ""
    failures = settings.run_dir / "solve" / "solver_failures.jsonl"
    assert failures.exists()
    payload = json.loads(failures.read_text(encoding="utf-8").splitlines()[0])
    assert payload["result_type"] == "solver_failure"
    assert payload["error_code"] == "SOLVER_EXHAUSTED"
    assert not (settings.run_dir / "solver_predictions.jsonl").exists()
    assert not (settings.run_dir / "solver_failures.jsonl").exists()


def test_run_baseline_evaluates_against_selected_dataset_subset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from tend.agents import AgentContext
    from tend.config import Settings
    from tend.observability import make_reporter, setup_logging
    from tend.workflow import Workflow

    dataset = tmp_path / "dataset"
    (dataset / "mongodb_data").mkdir(parents=True)
    (dataset / "mongodb_schema").mkdir()
    records = [
        {
            "record_id": 1,
            "db_id": "financial",
            "MQL": "db.account.aggregate([])",
            "nl_queries": {"canonical": "List accounts.", "colloquial": "Show accounts."},
        },
        {
            "record_id": 2,
            "db_id": "superhero",
            "MQL": "db.hero.aggregate([])",
            "nl_queries": {"canonical": "List heroes.", "colloquial": "Show heroes."},
        },
    ]
    (dataset / "test.json").write_text(json.dumps(records), encoding="utf-8")
    (dataset / "TEND.json").write_text(json.dumps(records), encoding="utf-8")
    (dataset / "mongodb_data" / "financial.json").write_text(json.dumps({"account": []}), encoding="utf-8")
    (dataset / "mongodb_schema" / "financial.json").write_text(json.dumps({"account": {}}), encoding="utf-8")
    (dataset / "mongodb_data" / "superhero.json").write_text(json.dumps({"hero": []}), encoding="utf-8")
    (dataset / "mongodb_schema" / "superhero.json").write_text(json.dumps({"hero": {}}), encoding="utf-8")

    settings = Settings.from_env(
        run_id="baseline-subset-eval-test",
        overrides={"TEND_LLM_STUB": "1"},
        require_bird=False,
    )
    settings = replace(settings, paths=replace(settings.paths, runs=tmp_path / "runs"))
    log = setup_logging(tmp_path / "run", console=False)
    progress = make_reporter(settings.run_id, log, enabled=False)
    ctx = AgentContext(settings=settings, llm=None, log=log, progress=progress, mongo=None)
    rt = cli.Runtime(
        settings,
        ctx,
        Workflow(ctx),
        progress,
        log,
        None,
        SimpleNamespace(close=lambda: None),
    )

    async def fake_run_baseline_suite(*_args, **_kwargs):
        return [{
            "baseline_id": "direct",
            "record_id": 1,
            "db_id": "financial",
            "MQL": "db.account.aggregate([])",
            "status": "ok",
        }]

    captured: dict[str, Path] = {}

    async def fake_maybe_evaluate(_rt, **kwargs):
        captured["dataset_dir"] = kwargs["dataset_dir"]
        captured["predictions_path"] = kwargs["predictions_path"]
        return SimpleNamespace(
            ok=True,
            status="ok",
            report={"scores": {"EX": 1.0, "EFM": 1.0, "EVM": 1.0}},
            paths=SimpleNamespace(report_md=tmp_path / "report.md"),
        )

    monkeypatch.setattr(cli, "run_baseline_suite", fake_run_baseline_suite)
    monkeypatch.setattr(cli, "_maybe_evaluate", fake_maybe_evaluate)

    rc = __import__("asyncio").run(
        cli._run_baseline(
            rt,
            dataset_dir=dataset,
            baselines="direct",
            db_id="financial",
            record_id=None,
            limit=1,
            witness_k=0,
            nlq_track="canonical",
            evaluate=True,
        )
    )

    assert rc == 0
    selected = json.loads((captured["dataset_dir"] / "test.json").read_text(encoding="utf-8"))
    assert [row["record_id"] for row in selected] == [1]
    assert captured["dataset_dir"] != dataset
    assert captured["dataset_dir"] == settings.run_dir / "baseline" / "evaluation_dataset"
    assert not (settings.run_dir / "evaluation_dataset").exists()
    assert captured["predictions_path"].parent == settings.run_dir / "baseline"
    assert captured["predictions_path"].name == "baseline_evaluation_inputs.jsonl"


def test_run_solve_writes_failure_artifact_for_solver_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from tend.agents import AgentContext
    from tend.config import Settings
    from tend.observability import make_reporter, setup_logging
    from tend.workflow import Workflow

    settings = Settings.from_env(
        run_id="solver-exception-cli-test",
        overrides={"TEND_LLM_STUB": "1"},
        require_bird=False,
    )
    settings = replace(settings, paths=replace(settings.paths, runs=tmp_path / "runs"))
    log = setup_logging(tmp_path / "run", console=False)
    progress = make_reporter(settings.run_id, log, enabled=False)
    ctx = AgentContext(settings=settings, llm=None, log=log, progress=progress, mongo=None)
    rt = cli.Runtime(
        settings,
        ctx,
        Workflow(ctx),
        progress,
        log,
        None,
        SimpleNamespace(close=lambda: None),
    )

    monkeypatch.setattr(
        cli,
        "load_solver_release_inputs",
        lambda *_args, **_kwargs: [({"record_id": 1001, "db_id": "financial"}, {}, None)],
    )

    async def fake_solve_record(*_args, **_kwargs):
        raise ContractViolationError(
            "agent output failed semantic contract",
            context={
                "agent": "smart_eg",
                "violations": ["preserve target_fields missing from plan output: ['*']"],
                "transcript_ref": "llm/smart_eg/abc.md",
                "diagnostics_ref": "llm/smart_eg/abc.diagnostics.json",
                "db_id": "financial",
                "record_id": 1001,
            },
        )

    monkeypatch.setattr(cli, "smart_solve_record_eg", fake_solve_record)

    rc = __import__("asyncio").run(
        cli._run_solve(
            rt,
            dataset_dir=tmp_path,
            db_id=None,
            record_id=None,
            limit=1,
            max_tool_turns=1,
            max_revisits=0,
            cost_budget_usd=0.1,
        )
    )

    assert rc == 1
    failures = settings.run_dir / "solve" / "solver_failures.jsonl"
    assert failures.exists()
    payload = json.loads(failures.read_text(encoding="utf-8").splitlines()[0])
    assert payload["result_type"] == "solver_failure"
    assert payload["error_code"] == "CONTRACT_VIOLATION"
    assert payload["agent"] == "smart_eg"
    assert payload["transcript_ref"] == "llm/smart_eg/abc.md"
    assert payload["diagnostics_ref"] == "llm/smart_eg/abc.diagnostics.json"


def test_run_solve_nlq_db_only_skips_release_loader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tend.agents import AgentContext
    from tend.config import Settings
    from tend.observability import make_reporter, setup_logging
    from tend.workflow import Workflow

    settings = Settings.from_env(
        run_id="solver-nlq-db-cli-test",
        overrides={"TEND_LLM_STUB": "1"},
        require_bird=False,
    )
    settings = replace(settings, paths=replace(settings.paths, runs=tmp_path / "runs"))
    log = setup_logging(tmp_path / "run", console=False)
    progress = make_reporter(settings.run_id, log, enabled=False)
    ctx = AgentContext(settings=settings, llm=None, log=log, progress=progress, mongo=None)
    rt = cli.Runtime(
        settings,
        ctx,
        Workflow(ctx),
        progress,
        log,
        None,
        SimpleNamespace(close=lambda: None),
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        cli,
        "load_solver_release_inputs",
        lambda *_args, **_kwargs: pytest.fail("NLQ+DB solve must not read release inputs"),
    )

    async def fake_solve_nlq_db(*_args, **kwargs):
        captured.update(kwargs)

        class FakePrediction:
            def to_json(self) -> dict:
                return {
                    "record_id": 12,
                    "db_id": "manual_formula",
                    "MQL": "db.race_weekends_v2.aggregate([])",
                }

        return FakePrediction()

    monkeypatch.setattr(cli, "smart_solve_nlq_db_eg", fake_solve_nlq_db)

    rc = __import__("asyncio").run(
        cli._run_solve(
            rt,
            dataset_dir=tmp_path,
            db_id="manual_formula",
            record_id=12,
            limit=1,
            max_tool_turns=9,
            max_revisits=1,
            cost_budget_usd=0.25,
            nlq="List race weekends with Finished status buckets.",
            evaluate=True,
        )
    )

    assert rc == 0
    stdout = capsys.readouterr().out
    assert "evaluation : skipped (NLQ+DB mode has no release evaluation dataset)" in stdout
    assert "evaluation : disabled (--no-eval)" not in stdout
    assert captured == {
        "db_id": "manual_formula",
        "nlq": "List race weekends with Finished status buckets.",
        "record_id": 12,
        "max_tool_turns": 9,
        "max_revisits": 1,
        "cost_budget_usd": 0.25,
    }
    predictions = settings.run_dir / "solve" / "solver_predictions.jsonl"
    assert predictions.exists()
    first_prediction = json.loads(predictions.read_text(encoding="utf-8").splitlines()[0])
    assert first_prediction["db_id"] == "manual_formula"
    failures = settings.run_dir / "solve" / "solver_failures.jsonl"
    assert failures.exists()
    assert failures.read_text(encoding="utf-8") == ""
    assert not (settings.run_dir / "solver_predictions.jsonl").exists()
    assert not (settings.run_dir / "solver_failures.jsonl").exists()


def test_run_baseline_nlq_db_only_skips_evaluation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tend.agents import AgentContext
    from tend.config import Settings
    from tend.observability import make_reporter, setup_logging
    from tend.workflow import Workflow

    settings = Settings.from_env(
        run_id="baseline-nlq-db-cli-test",
        overrides={"TEND_LLM_STUB": "1"},
        require_bird=False,
    )
    settings = replace(settings, paths=replace(settings.paths, runs=tmp_path / "runs"))
    log = setup_logging(tmp_path / "run", console=False)
    progress = make_reporter(settings.run_id, log, enabled=False)
    ctx = AgentContext(settings=settings, llm=None, log=log, progress=progress, mongo=None)
    rt = cli.Runtime(
        settings,
        ctx,
        Workflow(ctx),
        progress,
        log,
        None,
        SimpleNamespace(close=lambda: None),
    )
    captured: dict[str, object] = {}

    async def fake_run_baseline_suite(*_args, **kwargs):
        captured.update(kwargs)
        return [
            {
                "baseline_id": "direct",
                "record_id": 12,
                "db_id": "manual_formula",
                "MQL": "db.race_weekends_v2.aggregate([])",
                "status": "ok",
                "evaluation_skip_reason": "no_release_dataset",
            }
        ]

    monkeypatch.setattr(cli, "run_baseline_suite", fake_run_baseline_suite)
    monkeypatch.setattr(
        cli,
        "_maybe_evaluate",
        lambda *_args, **_kwargs: pytest.fail("NLQ+DB baseline must not auto-evaluate"),
    )

    rc = __import__("asyncio").run(
        cli._run_baseline(
            rt,
            dataset_dir=tmp_path,
            baselines="direct",
            db_id="manual_formula",
            record_id=12,
            limit=1,
            witness_k=2,
            nlq="List race weekends with Finished status buckets.",
            evaluate=True,
        )
    )

    assert rc == 0
    stdout = capsys.readouterr().out
    assert "evaluation : skipped (NLQ+DB mode has no release evaluation dataset)" in stdout
    assert "evaluation : disabled (--no-eval)" not in stdout
    assert captured["nlq"] == "List race weekends with Finished status buckets."
    assert captured["db_id"] == "manual_formula"
    assert captured["record_id"] == 12
    predictions = settings.run_dir / "baseline" / "baseline_predictions.jsonl"
    assert predictions.exists()
    first_prediction = json.loads(predictions.read_text(encoding="utf-8").splitlines()[0])
    assert first_prediction["db_id"] == "manual_formula"
    assert first_prediction["evaluation_skip_reason"] == "no_release_dataset"
    failures = settings.run_dir / "baseline" / "baseline_failures.jsonl"
    assert failures.exists()
    assert failures.read_text(encoding="utf-8") == ""
    assert not (settings.run_dir / "baseline_predictions.jsonl").exists()
    assert not (settings.run_dir / "baseline_failures.jsonl").exists()


def test_run_ablation_nlq_db_only_skips_evaluation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tend.agents import AgentContext
    from tend.config import Settings
    from tend.observability import make_reporter, setup_logging
    from tend.workflow import Workflow

    settings = Settings.from_env(
        run_id="ablation-nlq-db-cli-test",
        overrides={"TEND_LLM_STUB": "1"},
        require_bird=False,
    )
    settings = replace(settings, paths=replace(settings.paths, runs=tmp_path / "runs"))
    log = setup_logging(tmp_path / "run", console=False)
    progress = make_reporter(settings.run_id, log, enabled=False)
    ctx = AgentContext(settings=settings, llm=None, log=log, progress=progress, mongo=None)
    rt = cli.Runtime(
        settings,
        ctx,
        Workflow(ctx),
        progress,
        log,
        None,
        SimpleNamespace(close=lambda: None),
    )
    captured: dict[str, object] = {}

    async def fake_run_ablation_suite(*_args, **kwargs):
        captured.update(kwargs)
        return [
            {
                "ablation_id": "smart_eg_full",
                "record_id": 7,
                "db_id": "manual_cards",
                "MQL": "db.card_print_dossiers.aggregate([])",
                "status": "ok",
                "evaluation_skip_reason": "no_release_dataset",
            }
        ]

    monkeypatch.setattr(cli, "run_ablation_suite", fake_run_ablation_suite)
    monkeypatch.setattr(
        cli,
        "_maybe_evaluate",
        lambda *_args, **_kwargs: pytest.fail("NLQ+DB ablation must not auto-evaluate"),
    )

    rc = __import__("asyncio").run(
        cli._run_ablation(
            rt,
            dataset_dir=tmp_path,
            ablations="smart_eg_full",
            db_id="manual_cards",
            record_id=7,
            limit=1,
            max_tool_turns=13,
            max_revisits=1,
            cost_budget_usd=0.75,
            nlq="Find Modern banned card printings.",
            evaluate=True,
        )
    )

    assert rc == 0
    stdout = capsys.readouterr().out
    assert "evaluation : skipped (NLQ+DB mode has no release evaluation dataset)" in stdout
    assert "evaluation : disabled (--no-eval)" not in stdout
    assert captured["nlq"] == "Find Modern banned card printings."
    assert captured["db_id"] == "manual_cards"
    assert captured["record_id"] == 7
    assert captured["max_tool_turns"] == 13
    assert captured["max_revisits"] == 1
    assert captured["cost_budget_usd"] == 0.75
    assert captured["workers"] == 1
    predictions = settings.run_dir / "ablation" / "ablation_predictions.jsonl"
    assert predictions.exists()
    first_prediction = json.loads(predictions.read_text(encoding="utf-8").splitlines()[0])
    assert first_prediction["db_id"] == "manual_cards"
    assert first_prediction["evaluation_skip_reason"] == "no_release_dataset"
    failures = settings.run_dir / "ablation" / "ablation_failures.jsonl"
    assert failures.exists()
    assert failures.read_text(encoding="utf-8") == ""
    assert not (settings.run_dir / "ablation_predictions.jsonl").exists()
    assert not (settings.run_dir / "ablation_failures.jsonl").exists()


def test_run_ablation_variant_failure_does_not_fail_when_evaluation_is_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from tend.agents import AgentContext
    from tend.config import Settings
    from tend.observability import make_reporter, setup_logging
    from tend.workflow import Workflow

    dataset = tmp_path / "dataset"
    (dataset / "mongodb_data").mkdir(parents=True)
    (dataset / "mongodb_schema").mkdir()
    record = {
        "record_id": 1,
        "db_id": "financial",
        "MQL": "db.account.aggregate([])",
        "nl_queries": {"canonical": "List accounts."},
    }
    (dataset / "test.json").write_text(json.dumps([record]), encoding="utf-8")
    (dataset / "TEND.json").write_text(json.dumps([record]), encoding="utf-8")
    (dataset / "mongodb_data" / "financial.json").write_text(json.dumps({"account": []}), encoding="utf-8")
    (dataset / "mongodb_schema" / "financial.json").write_text(json.dumps({"account": {}}), encoding="utf-8")

    settings = Settings.from_env(
        run_id="ablation-variant-failure-cli-test",
        overrides={"TEND_LLM_STUB": "1"},
        require_bird=False,
    )
    settings = replace(settings, paths=replace(settings.paths, runs=tmp_path / "runs"))
    log = setup_logging(tmp_path / "run", console=False)
    progress = make_reporter(settings.run_id, log, enabled=False)
    ctx = AgentContext(settings=settings, llm=None, log=log, progress=progress, mongo=None)
    rt = cli.Runtime(
        settings,
        ctx,
        Workflow(ctx),
        progress,
        log,
        None,
        SimpleNamespace(close=lambda: None),
    )

    async def fake_run_ablation_suite(*_args, **_kwargs):
        return [
            {
                "result_type": "ablation_prediction",
                "ablation_id": "smart_eg_full",
                "record_id": 1,
                "db_id": "financial",
                "MQL": "db.account.aggregate([])",
                "status": "ok",
            },
            {
                "result_type": "ablation_failure",
                "ablation_id": "smart_eg_no_evidence_gate",
                "record_id": 1,
                "db_id": "financial",
                "status": "failed",
                "error_code": "CONTRACT_VIOLATION",
                "message": "variant violated semantic contract",
            },
        ]

    captured: dict[str, object] = {}

    async def fake_maybe_evaluate(_rt, **kwargs):
        captured["predictions"] = kwargs["predictions"]
        captured["predictions_path"] = kwargs["predictions_path"]
        return SimpleNamespace(
            ok=True,
            status="ok",
            report={"scores": {"EX": 0.5, "EFM": 0.5, "EVM": 0.5}},
            paths=SimpleNamespace(report_md=tmp_path / "report.md"),
        )

    monkeypatch.setattr(cli, "run_ablation_suite", fake_run_ablation_suite)
    monkeypatch.setattr(cli, "_maybe_evaluate", fake_maybe_evaluate)

    rc = __import__("asyncio").run(
        cli._run_ablation(
            rt,
            dataset_dir=dataset,
            ablations="smart_eg_full,smart_eg_no_evidence_gate",
            db_id="financial",
            record_id=None,
            limit=1,
            max_tool_turns=1,
            max_revisits=0,
            cost_budget_usd=0.1,
            evaluate=True,
        )
    )

    assert rc == 0
    assert [item["result_type"] for item in captured["predictions"]] == [
        "ablation_prediction",
        "ablation_failure",
    ]
    assert captured["predictions_path"].parent == settings.run_dir / "ablation"
    assert captured["predictions_path"].name == "ablation_evaluation_inputs.jsonl"
    summary = json.loads(
        (settings.run_dir / "ablation" / "ablation_summary.json").read_text(encoding="utf-8")
    )
    assert summary["status"] == "ok"
    assert summary["experiment_status"] == "ok"
    assert summary["outcome_status"] == "partial_variant_failures"
    assert summary["all_variants_failed"] is False
    assert summary["variant_failures_are_scored_outcomes"] is True
    assert summary["predictions"] == 1
    assert summary["failures"] == 1


def test_run_ablation_all_variant_failures_are_experiment_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from tend.agents import AgentContext
    from tend.config import Settings
    from tend.observability import make_reporter, setup_logging
    from tend.workflow import Workflow

    dataset = tmp_path / "dataset"
    (dataset / "mongodb_data").mkdir(parents=True)
    (dataset / "mongodb_schema").mkdir()
    record = {
        "record_id": 1,
        "db_id": "financial",
        "MQL": "db.account.aggregate([])",
        "nl_queries": {"canonical": "List accounts."},
    }
    (dataset / "test.json").write_text(json.dumps([record]), encoding="utf-8")
    (dataset / "TEND.json").write_text(json.dumps([record]), encoding="utf-8")
    (dataset / "mongodb_data" / "financial.json").write_text(json.dumps({"account": []}), encoding="utf-8")
    (dataset / "mongodb_schema" / "financial.json").write_text(json.dumps({"account": {}}), encoding="utf-8")

    settings = Settings.from_env(
        run_id="ablation-all-variant-failures-cli-test",
        overrides={"TEND_LLM_STUB": "1"},
        require_bird=False,
    )
    settings = replace(settings, paths=replace(settings.paths, runs=tmp_path / "runs"))
    log = setup_logging(tmp_path / "run", console=False)
    progress = make_reporter(settings.run_id, log, enabled=False)
    ctx = AgentContext(settings=settings, llm=None, log=log, progress=progress, mongo=None)
    rt = cli.Runtime(
        settings,
        ctx,
        Workflow(ctx),
        progress,
        log,
        None,
        SimpleNamespace(close=lambda: None),
    )

    async def fake_run_ablation_suite(*_args, **_kwargs):
        return [
            {
                "result_type": "ablation_failure",
                "ablation_id": "smart_eg_no_prefix_execution",
                "record_id": 1,
                "db_id": "financial",
                "status": "failed",
                "error_code": "NO_VALID_QUERY_FOUND",
                "message": "variant stopped after gate feedback",
            },
        ]

    captured: dict[str, object] = {}

    async def fake_maybe_evaluate(_rt, **kwargs):
        captured["predictions"] = kwargs["predictions"]
        return SimpleNamespace(
            ok=False,
            status="partial",
            report={"status": "partial", "scores": {"EX": 0.0, "EFM": 0.0, "EVM": 0.0}},
            paths=SimpleNamespace(report_md=tmp_path / "report.md"),
        )

    monkeypatch.setattr(cli, "run_ablation_suite", fake_run_ablation_suite)
    monkeypatch.setattr(cli, "_maybe_evaluate", fake_maybe_evaluate)

    rc = __import__("asyncio").run(
        cli._run_ablation(
            rt,
            dataset_dir=dataset,
            ablations="smart_eg_no_prefix_execution",
            db_id="financial",
            record_id=None,
            limit=1,
            max_tool_turns=1,
            max_revisits=0,
            cost_budget_usd=0.1,
            evaluate=True,
        )
    )

    assert rc == 0
    assert captured["predictions"] == [
        {
            "result_type": "ablation_failure",
            "ablation_id": "smart_eg_no_prefix_execution",
            "record_id": 1,
            "db_id": "financial",
            "status": "failed",
            "error_code": "NO_VALID_QUERY_FOUND",
            "message": "variant stopped after gate feedback",
        }
    ]
    summary = json.loads(
        (settings.run_dir / "ablation" / "ablation_summary.json").read_text(encoding="utf-8")
    )
    assert summary["status"] == "ok"
    assert summary["experiment_status"] == "ok"
    assert summary["outcome_status"] == "all_variants_failed"
    assert summary["all_variants_failed"] is True
    assert summary["variant_failures_are_scored_outcomes"] is True
    assert summary["predictions"] == 0
    assert summary["failures"] == 1
    assert summary["evaluation"]["status"] == "partial"
