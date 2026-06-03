from __future__ import annotations

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


def test_construct_passes_construction_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_build_runtime(settings):
        return SimpleNamespace(
            settings=settings,
            source=SimpleNamespace(db_ids=("financial",)),
        )

    async def fake_run_construct(_rt, _db_ids, _phase, _records, **kwargs):
        captured.append(kwargs.get("construction_mode"))
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
        "cli-legacy-mode",
    ]) == 0
    assert cli.main([
        "construct",
        "--construction-mode",
        "native",
        "--stub",
        "--quiet",
        "--dbs",
        "financial",
        "--records",
        "1",
        "--run-id",
        "cli-native-mode",
    ]) == 0

    assert captured == ["legacy", "native"]


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
    seen_skeleton_ids: list[int] = []
    seen_canonical_nl_ids: list[int] = []
    seen_nl_mql_pair_ids: list[int] = []

    async def fake_run_phase_b(
        _workflow,
        _artifacts,
        slots,
        *,
        seen_mql=None,
        seen_skeleton=None,
        seen_canonical_nl=None,
        seen_nl_mql_pair=None,
    ):
        assert isinstance(seen_mql, dict)
        assert isinstance(seen_skeleton, dict)
        assert isinstance(seen_canonical_nl, dict)
        assert isinstance(seen_nl_mql_pair, dict)
        seen_skeleton_ids.append(id(seen_skeleton))
        seen_canonical_nl_ids.append(id(seen_canonical_nl))
        seen_nl_mql_pair_ids.append(id(seen_nl_mql_pair))
        calls.append([slot.slot_index for slot in slots])
        for slot in slots:
            mql_sig = f"mql-{slot.slot_index}"
            seen_mql[(slot.db_id, mql_sig)] = slot.record_id
            seen_skeleton.setdefault((slot.db_id, "shared-skeleton"), []).append(slot.record_id)
            if slot.slot_index == 0:
                continue
            nl_sig = f"nl-{slot.slot_index}"
            seen_canonical_nl[(slot.db_id, nl_sig)] = slot.record_id
            seen_nl_mql_pair[(slot.db_id, nl_sig, mql_sig)] = slot.record_id
        return [
            {
                "db_id": slot.db_id,
                "record_id": slot.record_id,
                "MQL": f'db.account.aggregate([{{"$match":{{"slot":{slot.slot_index}}}}}])',
                "mql_signature": f"mql-{slot.slot_index}",
                "mql_skeleton_signature": "shared-skeleton",
                "nl_queries": {"canonical": f"Find slot {slot.slot_index}."},
            }
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
    assert len(set(seen_skeleton_ids)) == 1
    assert len(set(seen_canonical_nl_ids)) == 1
    assert len(set(seen_nl_mql_pair_ids)) == 1
    assert slot_count == 3
    assert targets == {"financial": 2}
    assert pool_sizes["financial"] >= 3
    assert any(event == "artifact_diversity_batch_done" for event, _fields in events)
    ledger_summary = next(
        fields for event, fields in events if event == "artifact_diversity_ledger_summary"
    )
    assert ledger_summary["total_slots"] == 3
    assert ledger_summary["built_records"] == 2
    assert ledger_summary["built_by_db"] == {"financial": 2}
    assert ledger_summary["distinct_mql"] == 2
    assert ledger_summary["distinct_mql_skeletons"] == 1
    assert ledger_summary["distinct_canonical_nl"] == 2
    assert ledger_summary["distinct_nl_mql_pairs"] == 2
    assert ledger_summary["max_mql_skeleton_family"] == 2
    assert ledger_summary["reserved_mql"] == 3
    assert ledger_summary["reserved_mql_skeletons"] == 1
    assert ledger_summary["reserved_canonical_nl"] == 2
    assert ledger_summary["reserved_nl_mql_pairs"] == 2
    assert ledger_summary["reserved_max_mql_skeleton_family"] == 3


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
    assert (subset / "mongodb_data" / "financial.json").exists()
    assert (subset / "mongodb_schema" / "financial.json").exists()
    assert (subset / "agent_design_rationale" / "financial.yaml").exists()
    assert (subset / "bird_db_catalog.json").exists()
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
        "--nlq-track",
        "colloquial",
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
        "full_smart",
        "--stub",
        "--quiet",
        "--run-id",
        "ablation-nlq-db",
    ]) == 0
    assert captured["source"] is None
    assert captured["db_id"] == "manual_cards"
    assert captured["nlq"] == "Find Modern banned card printings."
    assert captured["ablations"] == "full_smart"


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
                "agent": "smart_plan",
                "violations": ["preserve target_fields missing from plan output: ['*']"],
                "transcript_ref": "llm/smart_plan/abc.md",
                "diagnostics_ref": "llm/smart_plan/abc.diagnostics.json",
                "db_id": "financial",
                "record_id": 1001,
            },
        )

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
    failures = settings.run_dir / "solver_failures.jsonl"
    assert failures.exists()
    payload = json.loads(failures.read_text(encoding="utf-8").splitlines()[0])
    assert payload["result_type"] == "solver_failure"
    assert payload["error_code"] == "CONTRACT_VIOLATION"
    assert payload["agent"] == "smart_plan"
    assert payload["transcript_ref"] == "llm/smart_plan/abc.md"
    assert payload["diagnostics_ref"] == "llm/smart_plan/abc.diagnostics.json"


def test_run_solve_nlq_db_only_skips_release_loader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    monkeypatch.setattr(cli, "smart_solve_nlq_db", fake_solve_nlq_db)

    rc = __import__("asyncio").run(
        cli._run_solve(
            rt,
            dataset_dir=tmp_path,
            db_id="manual_formula",
            record_id=12,
            limit=1,
            r_max=0,
            witness_k=2,
            nlq="List race weekends with Finished status buckets.",
            evaluate=True,
        )
    )

    assert rc == 0
    assert captured == {
        "db_id": "manual_formula",
        "nlq": "List race weekends with Finished status buckets.",
        "record_id": 12,
        "r_max": 0,
        "witness_k": 2,
    }
    predictions = settings.run_dir / "solver_predictions.jsonl"
    assert predictions.exists()
    first_prediction = json.loads(predictions.read_text(encoding="utf-8").splitlines()[0])
    assert first_prediction["db_id"] == "manual_formula"


def test_run_baseline_nlq_db_only_skips_evaluation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    assert captured["nlq"] == "List race weekends with Finished status buckets."
    assert captured["db_id"] == "manual_formula"
    assert captured["record_id"] == 12
    predictions = settings.run_dir / "baseline_predictions.jsonl"
    assert predictions.exists()
    first_prediction = json.loads(predictions.read_text(encoding="utf-8").splitlines()[0])
    assert first_prediction["db_id"] == "manual_formula"


def test_run_ablation_nlq_db_only_skips_evaluation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
                "ablation_id": "full_smart",
                "record_id": 7,
                "db_id": "manual_cards",
                "MQL": "db.card_print_dossiers.aggregate([])",
                "status": "ok",
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
            ablations="full_smart",
            db_id="manual_cards",
            record_id=7,
            limit=1,
            r_max=1,
            witness_k=2,
            nlq="Find Modern banned card printings.",
            evaluate=True,
        )
    )

    assert rc == 0
    assert captured["nlq"] == "Find Modern banned card printings."
    assert captured["db_id"] == "manual_cards"
    assert captured["record_id"] == 7
    predictions = settings.run_dir / "ablation_predictions.jsonl"
    assert predictions.exists()
    first_prediction = json.loads(predictions.read_text(encoding="utf-8").splitlines()[0])
    assert first_prediction["db_id"] == "manual_cards"
