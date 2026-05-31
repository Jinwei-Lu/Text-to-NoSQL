"""Tests for gate assertion helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tend.orchestrate.publish import bootstrap_fixtures_snapshot, publish_dataset
from tend.orchestrate.record_expand import expand_records
from tend.orchestrate.seed import split_seed
from tend.tests.assert_gate_p import assert_gate_p


@pytest.fixture()
def pilot_tree(tmp_path: Path) -> Path:
    snapshot = bootstrap_fixtures_snapshot(tmp_path / "snap")
    records = json.loads((snapshot / "records.json").read_text(encoding="utf-8"))
    expanded = expand_records(records, target_total=210)
    pub_dir = tmp_path / "snap" / "pilot"
    pub_dir.mkdir(parents=True, exist_ok=True)
    (pub_dir / "records.json").write_text(json.dumps(expanded, indent=2), encoding="utf-8")
    (pub_dir / "spider_db_catalog.json").write_text(
        (snapshot / "spider_db_catalog.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    for sub in ("mongodb_schema", "mongodb_data", "agent_design_rationale", "fixtures"):
        src = snapshot / sub
        dst = pub_dir / sub
        if src.exists():
            import shutil

            shutil.copytree(src, dst)
    out_root = tmp_path / "TEND" / "pilot"
    publish_dataset(pub_dir, out_root, seed=split_seed())
    (out_root / "_meta.json").write_text(
        json.dumps({"stage": "pilot-b", "llm_stub": False, "use_fixtures": False}),
        encoding="utf-8",
    )
    return out_root


def test_expand_records_reaches_target() -> None:
    seeds = [{"record_id": 1, "db_id": "a"}, {"record_id": 2, "db_id": "b"}]
    expanded = expand_records(seeds, target_total=10)
    assert len(expanded) == 10
    assert len({r["record_id"] for r in expanded}) == 10


def test_assert_gate_p_passes_on_pilot_tree(pilot_tree: Path) -> None:
    errors = assert_gate_p(pilot_tree, require_live_llm=False)
    assert errors == []
