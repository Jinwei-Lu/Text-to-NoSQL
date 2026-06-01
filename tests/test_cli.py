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

    async def fake_run_construct(rt, _db_ids, _phase, _records):
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
