"""Shared fixtures for Phase B validation unit tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tend.config import FIXTURES_ROOT

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def orchestra_record() -> dict:
    return json.loads((FIXTURES_ROOT / "orchestra" / "record.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def orchestra_query_plan() -> dict:
    qps = yaml.safe_load((FIXTURES_ROOT / "orchestra" / "qps.yaml").read_text(encoding="utf-8"))
    return qps["query_plan"]


@pytest.fixture(scope="session")
def orchestra_scenario_summary() -> str:
    wp = yaml.safe_load((FIXTURES_ROOT / "orchestra" / "wp.yaml").read_text(encoding="utf-8"))
    return wp["scenario_summary"]


@pytest.fixture(scope="session")
def orchestra_witness() -> dict:
    return {
        "conductor": [
            {
                "_id": 1,
                "Name": "Alice",
                "orchestra": [
                    {
                        "performance": [
                            {"Performance_ID": 1, "Attendance": 10},
                            {"Performance_ID": 2, "Attendance": 20},
                            {"Performance_ID": 3, "Attendance": 30},
                        ]
                    }
                ],
            },
            {
                "_id": 2,
                "Name": "Bob",
                "orchestra": [
                    {
                        "performance": [
                            {"Performance_ID": 1, "Attendance": 15},
                            {"Performance_ID": 2, "Attendance": 25},
                            {"Performance_ID": 3, "Attendance": 35},
                        ]
                    }
                ],
            },
            {
                "_id": 3,
                "Name": None,
                "orchestra": [
                    {
                        "performance": [
                            {"Performance_ID": 1, "Attendance": None},
                            {"Performance_ID": 2, "Attendance": 40},
                            {"Performance_ID": 3, "Attendance": 50},
                        ]
                    }
                ],
            },
            {
                "_id": 4,
                "Name": "Dan",
                "orchestra": [
                    {
                        "performance": [
                            {"Performance_ID": 1, "Attendance": 5},
                            {"Performance_ID": 2, "Attendance": 5},
                            {"Performance_ID": 3, "Attendance": 5},
                        ]
                    }
                ],
            },
        ]
    }


@pytest.fixture(scope="session")
def mongo_available() -> bool:
    try:
        from pymongo import MongoClient

        from tend.config import MONGO_URI

        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=1500)
        client.admin.command("ping")
        client.close()
        return True
    except Exception:
        return False
