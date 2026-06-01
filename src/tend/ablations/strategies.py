"""Ablation definitions for the SMART reference solver."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..solver.workflow import SmartSolveOptions


@dataclass(frozen=True, slots=True)
class AblationSpec:
    id: str
    title: str
    description: str
    options: SmartSolveOptions
    limitations: tuple[str, ...]


def all_ablations() -> Mapping[str, AblationSpec]:
    return _ABLATIONS


def ablation_ids() -> tuple[str, ...]:
    return tuple(_ABLATIONS)


def resolve_ablations(selection: str | list[str] | tuple[str, ...] | None) -> list[AblationSpec]:
    if selection is None or selection == "all":
        return list(_ABLATIONS.values())
    parts = selection if isinstance(selection, (list, tuple)) else selection.split(",")
    specs: list[AblationSpec] = []
    unknown: list[str] = []
    for part in parts:
        key = str(part).strip()
        if not key:
            continue
        spec = _ABLATIONS.get(key)
        if spec is None:
            unknown.append(key)
        else:
            specs.append(spec)
    if unknown:
        raise KeyError(f"unknown ablations: {unknown}; known={list(_ABLATIONS)}")
    return specs


def _options(ablation_id: str, **overrides: object) -> SmartSolveOptions:
    return SmartSolveOptions(solver_variant=ablation_id, **overrides)


_ABLATIONS: dict[str, AblationSpec] = {
    "full_smart": AblationSpec(
        id="full_smart",
        title="Full SMART",
        description="Reference four-stage SMART solver with per-stage variant-stratified feedback.",
        options=_options("full_smart"),
        limitations=(),
    ),
    "no_shape_model": AblationSpec(
        id="no_shape_model",
        title="No shape model",
        description="Bypass shape comprehension and use a flat, variant-free schema view.",
        options=_options(
            "no_shape_model",
            use_shape_comprehension=False,
            use_schema_variants=False,
            allow_local_witness_strata=False,
        ),
        limitations=("no shape probes", "schema variants removed", "no witness-inferred strata"),
    ),
    "no_schema_variants": AblationSpec(
        id="no_schema_variants",
        title="No schema variants",
        description="Keep shape probes but remove public __variants/schema_flex hints from schema.",
        options=_options("no_schema_variants", use_schema_variants=False),
        limitations=("public variant hints removed",),
    ),
    "canonical_only": AblationSpec(
        id="canonical_only",
        title="Canonical NLQ only",
        description="Do not pass the colloquial NLQ cross-check into intent formalization.",
        options=_options("canonical_only", use_colloquial_nlq=False),
        limitations=("colloquial NLQ disabled",),
    ),
    "no_intent_contracts": AblationSpec(
        id="no_intent_contracts",
        title="No intent contracts",
        description="Disable deterministic shape_policy/target_fields/clause_coverage checks.",
        options=_options("no_intent_contracts", use_intent_contracts=False),
        limitations=("intent semantic repair disabled",),
    ),
    "no_variant_handling_guard": AblationSpec(
        id="no_variant_handling_guard",
        title="No variant-handling guard",
        description="Do not require non-empty variant_handling when the shape model is flexible.",
        options=_options("no_variant_handling_guard", require_variant_handling=False),
        limitations=("variant_handling contract disabled",),
    ),
    "no_witness_digest": AblationSpec(
        id="no_witness_digest",
        title="No prompt witness",
        description="Set prompt-visible witness K to zero while leaving local execution enabled.",
        options=_options("no_witness_digest", witness_k=0),
        limitations=("prompt witness digest disabled",),
    ),
    "whole_query_execution": AblationSpec(
        id="whole_query_execution",
        title="Whole-query execution",
        description="Execute only the completed MQL instead of per-stage prefixes.",
        options=_options("whole_query_execution", execution_mode="whole_query"),
        limitations=("no prefix checkpoints", "no stage_index-localized feedback"),
    ),
    "no_per_stage_execution": AblationSpec(
        id="no_per_stage_execution",
        title="No execution feedback",
        description="Render MQL and run only static disabled-operator guards.",
        options=_options("no_per_stage_execution", execution_mode="static", r_max=0),
        limitations=("no local execution", "no execution feedback loop"),
    ),
    "no_variant_stratification": AblationSpec(
        id="no_variant_stratification",
        title="No variant stratification",
        description="Keep per-stage execution but execute unstratified prefixes only.",
        options=_options("no_variant_stratification", use_variant_stratification=False),
        limitations=("variant-stratified checkpoints disabled",),
    ),
    "no_feedback_retry": AblationSpec(
        id="no_feedback_retry",
        title="No feedback retry",
        description="Run one SMART attempt only, without self-debug feedback turns.",
        options=_options("no_feedback_retry", r_max=0),
        limitations=("R_max forced to zero",),
    ),
}

ABLATION_IDS = ablation_ids()
