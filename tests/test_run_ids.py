from __future__ import annotations

import re
from types import SimpleNamespace

import tend.cli as cli
import tend.config as config_module
from tend.config import Settings
from tend.observability import new_run_id


_TIMESTAMP_RUN_ID = re.compile(r"^run-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-[0-9a-f]{4}$")
_TAGGED_RUN_ID = re.compile(
    r"^run-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-logging-solve-smoke-[0-9a-f]{4}$"
)


def test_new_run_id_is_timestamp_named() -> None:
    assert _TIMESTAMP_RUN_ID.match(new_run_id())


def test_settings_default_run_id_uses_timestamp_directory(monkeypatch) -> None:
    monkeypatch.setattr("tend.config.load_dotenv", lambda _path: {})
    monkeypatch.setenv("TEND_LLM_STUB", "1")

    settings = Settings.from_env(require_bird=False, require_llm=False)

    assert _TIMESTAMP_RUN_ID.match(settings.run_id)
    assert settings.run_dir == settings.paths.runs / settings.run_id
    assert settings.paths.dataset_out == settings.run_dir / "dataset"
    assert settings.run_id != "dev"


def test_solver_baseline_and_ablation_cli_defaults_use_timestamp_run_dirs(monkeypatch) -> None:
    monkeypatch.setattr(config_module, "load_dotenv", lambda _path: {})
    monkeypatch.setenv("TEND_LLM_STUB", "1")
    captured: list[tuple[str, object]] = []

    def fake_build_solver_runtime(settings, *, run_kind="solver"):
        captured.append((run_kind, settings))
        return SimpleNamespace(settings=settings)

    async def fake_solver(_rt, **_kwargs):
        return 0

    async def fake_baseline(_rt, **_kwargs):
        return 0

    async def fake_ablation(_rt, **_kwargs):
        return 0

    monkeypatch.setattr(cli, "build_solver_runtime", fake_build_solver_runtime)
    monkeypatch.setattr(cli, "_run_solve", fake_solver)
    monkeypatch.setattr(cli, "_run_baseline", fake_baseline)
    monkeypatch.setattr(cli, "_run_ablation", fake_ablation)

    assert cli.main(["solve", "--stub", "--quiet", "--nlq", "x", "--db-id", "financial"]) == 0
    assert cli.main(["baseline", "--stub", "--quiet", "--nlq", "x", "--db-id", "financial"]) == 0
    assert cli.main(["ablation", "--stub", "--quiet", "--nlq", "x", "--db-id", "financial"]) == 0

    assert [kind for kind, _settings in captured] == ["solver", "baseline", "ablation"]
    for _kind, settings in captured:
        assert _TIMESTAMP_RUN_ID.match(settings.run_id)
        assert settings.run_dir == settings.paths.runs / settings.run_id


def test_cli_run_id_argument_is_a_timestamped_tag(monkeypatch) -> None:
    monkeypatch.setattr(config_module, "load_dotenv", lambda _path: {})
    monkeypatch.setenv("TEND_LLM_STUB", "1")
    captured: list[Settings] = []

    def fake_build_solver_runtime(settings, *, run_kind="solver"):
        captured.append(settings)
        return SimpleNamespace(settings=settings)

    async def fake_solver(_rt, **_kwargs):
        return 0

    monkeypatch.setattr(cli, "build_solver_runtime", fake_build_solver_runtime)
    monkeypatch.setattr(cli, "_run_solve", fake_solver)

    assert cli.main([
        "solve",
        "--stub",
        "--quiet",
        "--nlq",
        "x",
        "--db-id",
        "financial",
        "--run-id",
        "logging-solve-smoke",
    ]) == 0

    settings = captured[-1]
    assert settings.run_id != "logging-solve-smoke"
    assert _TAGGED_RUN_ID.match(settings.run_id)
    assert settings.run_dir == settings.paths.runs / settings.run_id
