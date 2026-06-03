from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tend.solver.introspection import introspect_solver_database

_CASES = Path("tests/fixtures/solver_nlq_db/manual_native_cases.json")
_FORBIDDEN_SOLVER_INPUT_KEYS = {
    "MQL",
    "canonical_form_set",
    "shape_policy",
    "mql_signature",
    "mql_skeleton_signature",
    "mql_skeleton_summary",
}


class _SnapshotMongo:
    def __init__(self, collections: dict[str, list[dict[str, Any]]]) -> None:
        self.collections = collections

    def snapshot_database(self, _db_id: str, sample_size: int) -> dict[str, list[dict[str, Any]]]:
        return {name: docs[:sample_size] for name, docs in self.collections.items()}


def test_manual_native_solver_cases_are_nlq_db_only() -> None:
    payload = json.loads(_CASES.read_text(encoding="utf-8"))

    assert payload["cases"]
    for case in payload["cases"]:
        assert set(case["solver_input"]) == {"db_id", "nlq"}
        assert not (_FORBIDDEN_SOLVER_INPUT_KEYS & set(case))
        assert not (_FORBIDDEN_SOLVER_INPUT_KEYS & set(case["solver_input"]))
        assert case["database"]["collections"]
        snapshot = introspect_solver_database(
            _SnapshotMongo(case["database"]["collections"]),
            case["solver_input"]["db_id"],
            sample_size=3,
        )
        collection_summaries = snapshot.schema["collections"].values()
        assert any(summary["dynamic_key_paths"] for summary in collection_summaries)
        assert any(summary["presence_state_counts"] for summary in collection_summaries)
