"""Shared fixtures for core operator tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from tend.config import FIXTURES_ROOT
from tend.core.io import load_json
from tend.phase_a.dm import migrate

FIXTURE_RECORD = FIXTURES_ROOT / "orchestra" / "record.json"
FIXTURE_MUTATIONS = FIXTURES_ROOT / "orchestra" / "mutations.json"


@pytest.fixture(scope="session")
def orchestra_record() -> dict:
    return load_json(FIXTURE_RECORD)


@pytest.fixture(scope="session")
def orchestra_mutations() -> list[dict]:
    payload = load_json(FIXTURE_MUTATIONS)
    return payload["mutations"]


@pytest.fixture(scope="session")
def orchestra_snapshot() -> dict:
    data_path = Path("out/TEND/mongodb_data/orchestra.json")
    if data_path.exists():
        return load_json(data_path)
    data, _ = migrate("orchestra", {})
    return data
