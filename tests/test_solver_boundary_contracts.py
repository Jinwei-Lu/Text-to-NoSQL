from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from tend.solver.agents import SmartNosqlPlanner
from tend.solver.guards import build_disclosure, check_disjointness


def _settings(model: str, *, stub: bool = False):
    llm = SimpleNamespace(model=model, agent_models={})
    return SimpleNamespace(llm=llm, stub=stub)


def _allow_list(
    *,
    construction_model_ids: list[str] | None = None,
    frozen_panels: dict[str, list[str]] | None = None,
) -> dict:
    construction = (
        construction_model_ids
        if construction_model_ids is not None
        else ["construction-model-a"]
    )
    panels = frozen_panels if frozen_panels is not None else {
        "small": ["small-model-a"],
        "medium": ["medium-model-a"],
        "large": ["large-model-a"],
        "frontier": ["frontier-model-a"],
    }
    return {
        "four_party_disjointness": {"construction_model_ids": construction},
        "frozen_panels": panels,
    }


@pytest.mark.parametrize(
    ("model", "allow_list", "hit_key", "expected_hit"),
    [
        (
            "deepseek-v4-flash",
            _allow_list(construction_model_ids=["deepseek-v4-flash"]),
            "construction_pool_hits",
            "deepseek-v4-flash",
        ),
        (
            "claude-4-opus",
            _allow_list(frozen_panels={
                "small": ["small-model-a"],
                "medium": ["medium-model-a"],
                "large": ["large-model-a"],
                "frontier": ["claude-4-opus"],
            }),
            "frozen_panel_hits",
            "claude-4-opus",
        ),
    ],
)
def test_reused_construction_or_frozen_model_marks_disjointness_not_ok(
    model: str,
    allow_list: dict,
    hit_key: str,
    expected_hit: str,
) -> None:
    disclosure = build_disclosure(_settings(model), allow_list, r_max=2, witness_k=3)

    assert disclosure.disjointness_ok is False
    assert disclosure.disjointness_detail[hit_key] == [expected_hit]
    assert disclosure.disjointness_detail["manifest_errors"] == []


@pytest.mark.parametrize(
    "allow_list",
    [
        {"frozen_panels": _allow_list()["frozen_panels"]},
        _allow_list(construction_model_ids=[]),
    ],
)
def test_non_stub_disjointness_fails_closed_for_missing_or_empty_construction_manifest(
    allow_list: dict,
) -> None:
    detail = check_disjointness(["solver-model-a"], allow_list, require_manifests=True)

    assert detail["ok"] is False
    assert any("construction_model_ids" in error for error in detail["manifest_errors"])


@pytest.mark.parametrize(
    "frozen_panels",
    [
        None,
        {},
        {
            "small": ["small-model-a"],
            "medium": [],
            "large": ["large-model-a"],
            "frontier": ["frontier-model-a"],
        },
    ],
)
def test_non_stub_disjointness_fails_closed_for_missing_or_empty_frozen_panels(
    frozen_panels: dict[str, list[str]] | None,
) -> None:
    allow_list = _allow_list(frozen_panels=frozen_panels)
    if frozen_panels is None:
        allow_list.pop("frozen_panels")

    detail = check_disjointness(["solver-model-a"], allow_list, require_manifests=True)

    assert detail["ok"] is False
    assert any("frozen_panels" in error for error in detail["manifest_errors"])


def test_non_stub_disjointness_rejects_construction_role_labels_as_manifest() -> None:
    detail = check_disjointness(
        ["solver-model-a"],
        _allow_list(construction_model_ids=["QPS", "MS"]),
        require_manifests=True,
    )

    assert detail["ok"] is False
    assert any("role labels" in error for error in detail["manifest_errors"])


def test_planner_schema_and_contract_require_stage_diagnostics() -> None:
    output = {
        "collection": "account",
        "stages": [{"op": "$match", "stage": {"$match": {"status": "active"}}}],
        "variant_handling": [],
    }

    schema_errors = list(
        Draft202012Validator(SmartNosqlPlanner.output_schema).iter_errors(output)
    )
    violations = SmartNosqlPlanner().check_contract(
        None,
        {"logical_spec": {"shape_policy": "reshape"}, "shape_model": {}},
        output,
    )

    assert schema_errors
    assert any("stage 0 must include" in violation for violation in violations)


def test_planner_contract_accepts_structured_stage_rationale() -> None:
    output = {
        "collection": "account",
        "stages": [
            {
                "op": "$match",
                "stage": {"$match": {"status": "active"}},
                "rationale": {"variant_branch": "active account subset"},
            }
        ],
        "variant_handling": [],
    }

    schema_errors = list(
        Draft202012Validator(SmartNosqlPlanner.output_schema).iter_errors(output)
    )
    violations = SmartNosqlPlanner().check_contract(
        None,
        {"logical_spec": {"shape_policy": "reshape"}, "shape_model": {}},
        output,
    )

    assert schema_errors == []
    assert not any("must include" in violation for violation in violations)


def test_smart_prompts_name_allowed_inputs_without_only_shape_contradictions() -> None:
    prompt_dir = Path(__file__).resolve().parents[1] / "proposals" / "agent_prompts"
    intent = (prompt_dir / "smart_intent_formalizer.md").read_text(encoding="utf-8")
    planner = (prompt_dir / "smart_nosql_planner.md").read_text(encoding="utf-8")

    assert "canonical/colloquial NLQ" in intent
    assert "shape model" in intent
    assert "bounded checkpoint" in intent
    assert "shape model only" not in intent

    assert "NLQ-derived logical spec" in planner
    assert "shape model" in planner
    assert "bounded checkpoint feedback" in planner
    assert "disclosed witness digest" in planner
    assert "Use only the logical spec and shape model" not in planner
