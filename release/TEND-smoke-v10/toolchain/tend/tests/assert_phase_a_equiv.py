"""Semantic equivalence check for Phase A rebuild vs fixtures."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from tend.config import FIXTURES_ROOT, REPO_ROOT
from tend.schemas.validators import validate


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _field_paths_from_schema(node: Any, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if isinstance(node, str):
        if prefix:
            paths.add(prefix)
        return paths
    if isinstance(node, dict):
        if node.get("type") == "ARRAY" and "items" in node:
            child_prefix = f"{prefix}[]" if prefix else "[]"
            paths |= _field_paths_from_schema(node["items"], child_prefix)
            return paths
        if node.get("type") == "OBJECT" and "fields" in node:
            for name, child in node["fields"].items():
                child_prefix = f"{prefix}.{name}" if prefix else name
                paths |= _field_paths_from_schema(child, child_prefix)
            return paths
        # collectionSchema: top-level field declarations
        for name, child in node.items():
            if name.startswith("__"):
                continue
            child_prefix = f"{prefix}.{name}" if prefix else name
            paths |= _field_paths_from_schema(child, child_prefix)
    return paths


def _data_field_paths(doc: Any, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if isinstance(doc, dict):
        for key, value in doc.items():
            if key == "_id":
                continue
            child_prefix = f"{prefix}.{key}" if prefix else key
            paths.add(child_prefix)
            paths |= _data_field_paths(value, child_prefix)
    elif isinstance(doc, list):
        for item in doc:
            paths |= _data_field_paths(item, prefix)
    return paths


def _attendance_count(data: dict[str, Any]) -> int:
    count = 0
    for doc in data.get("conductor", []):
        for orch in doc.get("orchestra", []):
            for perf in orch.get("performance", []):
                if "Attendance" in perf:
                    count += 1
    return count


def assert_equivalent(db_id: str, out_root: Path | None = None) -> list[str]:
    """Return list of equivalence errors (empty if OK)."""
    out_root = out_root or REPO_ROOT / "out" / "TEND"
    fixture_dir = FIXTURES_ROOT / db_id
    errors: list[str] = []

    schema_path = out_root / "mongodb_schema" / f"{db_id}.json"
    data_path = out_root / "mongodb_data" / f"{db_id}.json"
    rationale_path = out_root / "agent_design_rationale" / f"{db_id}.yaml"

    for path in (schema_path, data_path, rationale_path):
        if not path.exists():
            errors.append(f"missing artifact: {path}")
            return errors

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    data = json.loads(data_path.read_text(encoding="utf-8"))
    rationale = _load_yaml(rationale_path)
    fixture_sra = _load_yaml(fixture_dir / "sra.yaml") if (fixture_dir / "sra.yaml").exists() else {}

    try:
        validate(rationale, "agent_design_rationale")
    except ValueError as exc:
        errors.append(str(exc))

    if db_id == "orchestra":
        if set(schema.keys()) != {"conductor"}:
            errors.append(f"expected single conductor collection, got {set(schema.keys())}")

        schema_paths = set()
        for coll, tree in schema.items():
            schema_paths |= _field_paths_from_schema(tree)

        if "orchestra[].performance[].Attendance" not in schema_paths:
            errors.append("schema missing denormalized Attendance on performance path")

        docs = data.get("conductor", [])
        if len(docs) != 12:
            errors.append(f"expected 12 conductor documents, got {len(docs)}")

        attendance = _attendance_count(data)
        if attendance < 1:
            errors.append("expected Attendance denormalized on at least one performance")

        expected_patterns = set(fixture_sra.get("patterns_applied", ["embed", "mixed"]))
        actual_patterns = set(rationale.get("patterns_applied", []))
        if not expected_patterns.issubset(actual_patterns):
            errors.append(f"patterns_applied mismatch: expected superset of {expected_patterns}, got {actual_patterns}")

        for decision in fixture_sra.get("decisions", []):
            matching = [
                d
                for d in rationale.get("decisions", [])
                if d.get("id") == decision.get("id") and d.get("type") == decision.get("type")
            ]
            if not matching:
                errors.append(f"missing rationale decision {decision.get('id')} type {decision.get('type')}")

        data_paths = set()
        for doc in docs:
            data_paths |= _data_field_paths(doc)
        if not any("Attendance" in path for path in data_paths):
            errors.append("data missing Attendance field path")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assert Phase A semantic equivalence to fixtures.")
    parser.add_argument("db_id", help="db_id to check (e.g. orchestra)")
    parser.add_argument("--out", default="out/TEND", help="Built artifact root")
    args = parser.parse_args(argv)

    errors = assert_equivalent(args.db_id, Path(args.out))
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1

    print(f"Phase A equivalence OK for {args.db_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
