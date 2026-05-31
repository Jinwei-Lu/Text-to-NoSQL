"""JSON Schema validators mirroring proposals/schemas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from tend.config import SCHEMAS_ROOT

SCHEMA_RECORD = SCHEMAS_ROOT / "record.schema.json"
SCHEMA_QUERY_PLAN = SCHEMAS_ROOT / "query_plan.schema.json"
SCHEMA_MUTATIONS = SCHEMAS_ROOT / "mutations.schema.json"
SCHEMA_NLQ = SCHEMAS_ROOT / "nlq.schema.json"
SCHEMA_WP_OUTPUT = SCHEMAS_ROOT / "wp_output.schema.json"
SCHEMA_LIBRARY = SCHEMAS_ROOT / "library.schema.json"
SCHEMA_MIGRATION_LOG = SCHEMAS_ROOT / "migration_log.schema.json"
SCHEMA_AGENT_DESIGN_RATIONALE = SCHEMAS_ROOT / "agent_design_rationale.schema.json"
SCHEMA_LEADERBOARD = SCHEMAS_ROOT / "leaderboard.schema.json"
SCHEMA_PROPERTY_VERIFICATION = SCHEMAS_ROOT / "property_verification.schema.json"
SCHEMA_ROUND_TRIP = SCHEMAS_ROOT / "round_trip_verification.schema.json"
SCHEMA_CANONICAL_FORM_SET = SCHEMAS_ROOT / "canonical_form_set.schema.json"
SCHEMA_SYNTHESIS_TRACE = SCHEMAS_ROOT / "synthesis_trace.schema.json"

SCHEMA_MAP = {
    "record": SCHEMA_RECORD,
    "query_plan": SCHEMA_QUERY_PLAN,
    "mutations": SCHEMA_MUTATIONS,
    "nlq": SCHEMA_NLQ,
    "wp_output": SCHEMA_WP_OUTPUT,
    "library": SCHEMA_LIBRARY,
    "migration_log": SCHEMA_MIGRATION_LOG,
    "agent_design_rationale": SCHEMA_AGENT_DESIGN_RATIONALE,
    "leaderboard": SCHEMA_LEADERBOARD,
    "property_verification": SCHEMA_PROPERTY_VERIFICATION,
    "round_trip_verification": SCHEMA_ROUND_TRIP,
    "canonical_form_set": SCHEMA_CANONICAL_FORM_SET,
    "synthesis_trace": SCHEMA_SYNTHESIS_TRACE,
}

_CACHE: dict[Path, Draft202012Validator] = {}


def _validator(schema_path: Path) -> Draft202012Validator:
    if schema_path not in _CACHE:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        _CACHE[schema_path] = Draft202012Validator(schema)
    return _CACHE[schema_path]


def validate(instance: Any, schema_name: str | Path) -> None:
    if isinstance(schema_name, Path):
        path = schema_name
    else:
        path = SCHEMA_MAP[schema_name]
    errors = sorted(_validator(path).iter_errors(instance), key=lambda e: e.path)
    if errors:
        raise ValueError(f"Schema validation failed for {path.name}: {errors[0].message}")
