"""Unit tests for orchestrator modules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tend.errors import SplitError
from tend.orchestrate.coverage import CoverageController, SIX_AXES
from tend.orchestrate.publish import bootstrap_fixtures_snapshot, check_c1_c9, publish_dataset
from tend.orchestrate.seed import db_seed, global_seed, record_seed, split_seed
from tend.orchestrate.split import cross_domain_split


@pytest.fixture()
def snapshot_root(tmp_path: Path) -> Path:
    return bootstrap_fixtures_snapshot(tmp_path / "fixtures-snapshot")


@pytest.fixture()
def catalog(snapshot_root: Path) -> dict:
    return json.loads((snapshot_root / "spider_db_catalog.json").read_text(encoding="utf-8"))


@pytest.fixture()
def records(snapshot_root: Path) -> list[dict]:
    return json.loads((snapshot_root / "records.json").read_text(encoding="utf-8"))


def test_seed_deterministic() -> None:
    assert global_seed() == 42
    assert db_seed("orchestra") == db_seed("orchestra")
    assert record_seed("orchestra", 1001) == record_seed("orchestra", 1001)
    assert split_seed() == split_seed()


def test_coverage_controller_strong_pull(records: list[dict]) -> None:
    ctrl = CoverageController.with_defaults(target_records=len(records))
    ctrl.min_quota[("difficulty_tier", "L4")] = 1
    record = next(r for r in records if r["difficulty"] == "L4")
    ok, reason = ctrl.should_accept(record)
    assert ok is True
    assert reason == "strong-pull"


def test_cross_domain_split_satisfies_h5_h9(records: list[dict], catalog: dict) -> None:
    train, test, meta = cross_domain_split(catalog, records, seed=split_seed())
    assert train and test
    assert meta["supply_relax_active"] is False or meta["supply_relax_active"] is True
    n_test = len(test)
    assert sum(1 for r in test if r["difficulty"] == "L4") / n_test >= 0.30
    assert sum(1 for r in test if r.get("schema_flex", "none") != "none") / n_test >= meta["h7_min"]
    assert sum(1 for r in test if r["difficulty"] == "L0") / n_test <= 0.05


def test_cross_domain_split_rejects_empty_pool(catalog: dict) -> None:
    with pytest.raises(SplitError):
        cross_domain_split(catalog, [])


def test_publish_dataset_writes_outputs(snapshot_root: Path, tmp_path: Path) -> None:
    out_root = tmp_path / "TEND"
    result = publish_dataset(snapshot_root, out_root, seed=split_seed())
    assert (out_root / "train.json").exists()
    assert (out_root / "test.json").exists()
    assert (out_root / "TEND.json").exists()
    assert (out_root / "spider_db_catalog.json").exists()
    assert len(result["TEND"]) == len(result["train"]) + len(result["test"])

    tend = json.loads((out_root / "TEND.json").read_text(encoding="utf-8"))
    ids = [r["record_id"] for r in tend]
    assert ids == sorted(ids)
    assert check_c1_c9(tend, out_root) == []


def test_six_axes_cover_fixture_records(records: list[dict]) -> None:
    for record in records:
        for axis in SIX_AXES:
            assert SIX_AXES[axis](record)
