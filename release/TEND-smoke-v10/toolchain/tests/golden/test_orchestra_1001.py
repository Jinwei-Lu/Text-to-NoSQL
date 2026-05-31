"""Golden integration test for orchestra/1001 MVP gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from tend.config import FIXTURES_ROOT
from tend.core.ex_verdict import EX_verdict
from tend.core.io import load_json
from tend.phase_a.dm import migrate


@pytest.fixture(scope="module")
def witness():
    path = Path("out/TEND/mongodb_data/orchestra.json")
    if path.exists():
        data = load_json(path)
        if data.get("conductor"):
            return data
    from tend.phase_a.sra import ORCHESTRA_RATIONALE, ORCHESTRA_SCHEMA

    data, _ = migrate("orchestra", ORCHESTRA_SCHEMA, ORCHESTRA_RATIONALE)
    return data


@pytest.fixture(scope="module")
def record():
    return load_json(FIXTURES_ROOT / "orchestra" / "record.json")


def test_phase_a_schema_exists():
    schema_path = Path("out/TEND/mongodb_schema/orchestra.json")
    if not schema_path.exists():
        pytest.skip("Run build_phase_a first")
    schema = load_json(schema_path)
    assert "conductor" in schema


def test_record_ex_verdict(record, witness):
    assert EX_verdict(record["MQL"], record, witness) is True


def test_mutations_all_fail(record, witness):
    mutations = load_json(FIXTURES_ROOT / "orchestra" / "mutations.json")["mutations"]
    for mut in mutations:
        assert EX_verdict(mut["MQL"], record, witness) is False


def test_nnc_fixture_labels(record):
    assert record["difficulty"] == "L4"
    assert record["sql_infeasibility_class"] == "structural_pipeline"


def test_phase_b_valid_summary():
    summary_glob = list(Path("out/runs").glob("**/audit/orchestra/1001/phase_b_valid_summary.json"))
    if not summary_glob:
        pytest.skip("Run build_phase_b_valid first")
    summary = load_json(summary_glob[-1])
    assert summary.get("ok") is True or summary.get("gate_pass") is True
