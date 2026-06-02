from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import tend.cli as cli
import tend.config as config_module


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
        "TEND_MIGRATION_REF_SAMPLE_CAP",
    ):
        monkeypatch.delenv(key, raising=False)


def test_construct_default_output_is_run_dataset_unless_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = config_module._find_repo_root()
    captured: list[Path] = []

    def fake_build_runtime(settings):
        return SimpleNamespace(
            settings=settings,
            source=SimpleNamespace(db_ids=("financial",)),
        )

    async def fake_run_construct(rt, _db_ids, _phase, _records, **_kwargs):
        captured.append(rt.settings.paths.dataset_out)
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
    assert captured[-1] == repo / "runs" / "cli-default" / "dataset"

    override = tmp_path / "custom-dataset"
    monkeypatch.setenv("TEND_DATASET_OUT", str(override))
    assert cli.main([
        "construct",
        "--stub",
        "--quiet",
        "--run-id",
        "cli-override",
    ]) == 0
    assert captured[-1] == override


def test_construct_full_db_and_all_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeDb:
        query_count = 32

    def fake_run_census(_source, *, db_ids):
        captured["census_db_ids"] = db_ids
        return SimpleNamespace(databases={"financial": FakeDb()})

    def fake_build_runtime(settings):
        captured["cap"] = settings.migration_ref_sample_cap
        return SimpleNamespace(
            settings=settings,
            source=SimpleNamespace(db_ids=("financial",)),
        )

    async def fake_run_construct(_rt, db_ids, _phase, records, **kwargs):
        captured["db_ids"] = db_ids
        captured["records"] = records
        captured["structural_only"] = kwargs.get("structural_only_records")
        return 0

    monkeypatch.setattr(cli, "run_census", fake_run_census)
    monkeypatch.setattr(cli, "build_runtime", fake_build_runtime)
    monkeypatch.setattr(cli, "_run_construct", fake_run_construct)

    assert cli.main([
        "construct",
        "--stub",
        "--quiet",
        "--full-db",
        "--dbs",
        "financial",
        "--records",
        "all",
        "--run-id",
        "cli-full-financial",
    ]) == 0
    assert captured["cap"] is None
    assert captured["census_db_ids"] == ["financial"]
    assert captured["db_ids"] == ["financial"]
    assert captured["records"] == 32
    assert captured["structural_only"] is True


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


def test_artifact_diversity_runner_refills_with_unused_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tend.workflow.flows import DbArtifacts

    events: list[tuple[str, dict]] = []

    class FakeLog:
        def info(self, event: str, **fields):
            events.append((event, fields))

        def warning(self, event: str, **fields):
            events.append((event, fields))

    artifact = DbArtifacts(
        db_id="financial",
        mongodb_schema={
            "account": {
                "_id": "INT",
                "frequency": "TEXT",
                "loan": {"type": "OBJECT", "fields": {"amount": "REAL"}},
                "trans": {
                    "type": "ARRAY",
                    "items": {"type": "OBJECT", "fields": {"amount": "REAL"}},
                },
                "__variants": [
                    {"discriminator": {"loan": "present"}, "fields": {}},
                    {"discriminator": {"loan": "missing"}, "fields": {}},
                    {"discriminator": {"frequency": "present"}, "fields": {}},
                    {"discriminator": {"frequency": "missing"}, "fields": {}},
                ],
            }
        },
        mongodb_data={"account": [{"_id": 1, "frequency": "monthly"}]},
        rationale={},
        world_signature="sha256:" + "5" * 64,
        scenario_summary="financial",
        query_bearing=True,
    )
    calls: list[list[int]] = []

    async def fake_run_phase_b(_workflow, _artifacts, slots, *, seen_mql=None):
        calls.append([slot.slot_index for slot in slots])
        return [
            {"db_id": slot.db_id, "record_id": slot.record_id}
            for slot in slots
            if slot.slot_index != 0
        ]

    monkeypatch.setattr(cli, "run_phase_b", fake_run_phase_b)
    rt = SimpleNamespace(settings=SimpleNamespace(seed=0), log=FakeLog(), workflow=object())

    records, slot_count, targets, pool_sizes = __import__("asyncio").run(
        cli._run_artifact_diversity_phase_b(
            rt,
            {"financial": artifact},
            n_records=2,
            records_per_db=2,
        )
    )

    assert len(records) == 2
    assert calls[0] == [0, 1]
    assert calls[1] == [2]
    assert slot_count == 3
    assert targets == {"financial": 2}
    assert pool_sizes["financial"] >= 3
    assert any(event == "artifact_diversity_batch_done" for event, _fields in events)


def test_validate_smoke_relaxes_all_db_composition(capsys: pytest.CaptureFixture[str]) -> None:
    dataset = Path("tests/fixtures/smoke_release")

    assert cli.main(["validate", "--dataset-dir", str(dataset)]) == 1
    full = capsys.readouterr().out
    assert "TEND validate · validation INVALID · mode=full" in full
    assert "[H4] db coverage 1 != 11" in full

    assert cli.main(["validate", "--dataset-dir", str(dataset), "--smoke"]) == 0
    smoke = capsys.readouterr().out
    assert "TEND validate · validation OK · mode=smoke" in smoke


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
        rt.mongo.close()
        rt.log.close()
        return 0

    monkeypatch.setattr(cli, "BirdSource", fail_bird_source)
    monkeypatch.setattr(cli, "_run_solve", fake_run_solve)

    assert cli.main([
        "solve",
        "--dataset-dir",
        "tests/fixtures/smoke_release",
        "--stub",
        "--quiet",
        "--run-id",
        "solve-no-bird",
    ]) == 0
    assert captured["source"] is None
    assert captured["dataset_dir"] == (
        config_module._find_repo_root() / "tests" / "fixtures" / "smoke_release"
    )


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
        "full_smart,no_shape_model",
        "--stub",
        "--quiet",
        "--run-id",
        "ablation-no-bird",
    ]) == 0
    assert captured["source"] is None
    assert captured["ablations"] == "full_smart,no_shape_model"
    assert captured["dataset_dir"] == (
        config_module._find_repo_root() / "tests" / "fixtures" / "smoke_release"
    )


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

    monkeypatch.setattr(cli, "smart_solve_record", fake_solve_record)

    rc = __import__("asyncio").run(
        cli._run_solve(
            rt,
            dataset_dir=tmp_path,
            db_id=None,
            record_id=None,
            limit=1,
            r_max=0,
            witness_k=0,
        )
    )

    assert rc == 1
    assert not (settings.run_dir / "solver_predictions.jsonl").exists()
    failures = settings.run_dir / "solver_failures.jsonl"
    assert failures.exists()
    payload = json.loads(failures.read_text(encoding="utf-8").splitlines()[0])
    assert payload["result_type"] == "solver_failure"
    assert payload["error_code"] == "SOLVER_EXHAUSTED"
