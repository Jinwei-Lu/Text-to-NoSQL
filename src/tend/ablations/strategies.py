"""Ablation definitions for the SAG solver mechanism.

Two registries:

- **Canonical arms** (``ablation_ids()``, the default ``all`` selection): the measured
  cumulative mechanism ladder (financial /110: card1 56 → gate 59 → v2 58 → v3 61).
  The arms are CANONICAL fixed configurations, not tunable budgets — that is what
  makes them honest single-mechanism isolations. ``sag_full`` runs the identical full
  v3 mechanism under the ablation harness and exists purely as the per-system
  reference row for delta computation (``evaluation.metrics.REFERENCE_ABLATION_SYSTEM``).

- **Extended component-knockout arms** (``extended_ablation_ids()``, selected
  explicitly or via the ``extended`` keyword): each is full v3 minus exactly ONE
  component (docs/experiment_design_2026-06.md §4.2), decoupled from the ladder
  order. They are panel-scale ablations, deliberately NOT part of ``all``.

Hyperparameter sweeps (k / repair rounds / sample budget — §4.2 A10–A12) are not
arms: pass ``--solver-option KEY=VALUE`` overrides, applied uniformly to every
selected arm via ``to_policy(overrides=...)``.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ..errors import SourceError
from ..solver.sag import SAGPolicy

_ALL_MECHANISMS = (
    "path_card",
    "dynamic_key_collapse",
    "a_path_gate",
    "repair_loop",
    "value_witnesses",
    "a_value_gate",
    "limit_contract",
    "prefix_bisection",
    "k_consistency",
)

# SAGPolicy numeric fields a CLI sweep may override uniformly across arms.
SWEEP_OVERRIDE_KEYS = ("k_consistency", "max_repair_rounds", "sample_docs", "card_cap")


@dataclass(frozen=True, slots=True)
class SagAblationSpec:
    id: str
    title: str
    description: str
    arm: str  # SAGPolicy.arm
    limitations: tuple[str, ...] = ()
    mechanism_claims: tuple[str, ...] = ()
    # Component-knockout knobs (None / default = arm-derived; canonical arms never set them).
    gate_override: bool | None = None
    value_grounding_override: bool | None = None
    bisection_override: bool | None = None
    card_mode: str = "lattice"
    variant_label: str = ""

    def to_policy(self, *, overrides: dict[str, Any] | None = None) -> SAGPolicy:
        policy = SAGPolicy(
            arm=self.arm,
            gate_override=self.gate_override,
            value_grounding_override=self.value_grounding_override,
            bisection_override=self.bisection_override,
            card_mode=self.card_mode,
            variant_label=self.variant_label,
        )
        if overrides:
            unknown = sorted(set(overrides) - set(SWEEP_OVERRIDE_KEYS))
            if unknown:
                raise SourceError(
                    f"unknown ablation policy overrides: {unknown}; "
                    f"allowed: {list(SWEEP_OVERRIDE_KEYS)}"
                )
            policy = replace(policy, **{k: int(v) for k, v in overrides.items()})
        policy.validate()
        return policy

    def to_runtime_options(
        self,
        *,
        progress_group_prefix: str = "solve",
        progress_work_item_id: str | None = None,
        policy: SAGPolicy | None = None,
    ) -> dict[str, Any]:
        policy = policy or self.to_policy()
        return {
            "ablation_id": self.id,
            "solver_variant": self.id,
            "arm": self.arm,
            "k_consistency": policy.effective_k,
            "max_repair_rounds": policy.max_repair_rounds if policy.use_repair else 1,
            "card_mode": policy.card_mode,
            "sample_docs": policy.sample_docs,
            "card_cap": policy.card_cap,
            "mechanism_claims": list(self.mechanism_claims),
            "disabled_vs_solver": [
                name for name in _ALL_MECHANISMS if name not in self.mechanism_claims
            ],
            "is_reference": self.id == "sag_full",
            "progress_group_prefix": progress_group_prefix,
            "progress_work_item_id": progress_work_item_id,
        }


AblationSpec = SagAblationSpec


def ablation_ids() -> tuple[str, ...]:
    return tuple(_ABLATIONS)


def extended_ablation_ids() -> tuple[str, ...]:
    return tuple(_EXTENDED_ABLATIONS)


def resolve_ablations(
    selection: str | list[str] | tuple[str, ...] | None,
) -> list[SagAblationSpec]:
    if selection is None or selection == "all":
        return list(_ABLATIONS.values())
    if selection == "extended":
        # The knockouts plus the reference row the per-system deltas need.
        return [_ABLATIONS["sag_full"], *_EXTENDED_ABLATIONS.values()]
    parts = selection if isinstance(selection, (list, tuple)) else selection.split(",")
    specs: list[SagAblationSpec] = []
    unknown: list[str] = []
    for part in parts:
        key = str(part).strip()
        if not key:
            continue
        spec = _ABLATIONS.get(key) or _EXTENDED_ABLATIONS.get(key)
        if spec is None:
            unknown.append(key)
        else:
            specs.append(spec)
    if unknown:
        raise SourceError(
            f"unknown ablations: {unknown}; "
            f"known={list(_ABLATIONS) + list(_EXTENDED_ABLATIONS)}"
        )
    if not specs:
        raise SourceError("ablation selection did not include any ablation ids")
    return specs


_ABLATIONS: dict[str, SagAblationSpec] = {
    "sag_card1": SagAblationSpec(
        id="sag_card1",
        title="Path card only",
        description=(
            "Full induced lattice path card as the decoding hypothesis space, single "
            "shot: no alignment gate, no repair loop, no consistency vote (k=1)."
        ),
        arm="card1",
        limitations=(
            "single decode, no repair",
            "no alignment gate",
            "no value witnesses",
            "no execution feedback",
        ),
        mechanism_claims=("path_card", "dynamic_key_collapse"),
    ),
    "sag_gate": SagAblationSpec(
        id="sag_gate",
        title="Card + A_path gate + execution repair",
        description=(
            "Path card plus the A_path gate (collection enum, witnessed-edge $lookup "
            "admissibility) and the execution repair loop with plain empty-result "
            "feedback; no value witnesses, no bisection gradient, k=1."
        ),
        arm="gate",
        limitations=(
            "no value witnesses",
            "no A_value gate",
            "no limit contract",
            "plain empty feedback (no prefix bisection)",
            "no consistency vote",
        ),
        mechanism_claims=(
            "path_card",
            "dynamic_key_collapse",
            "a_path_gate",
            "repair_loop",
        ),
    ),
    "sag_v2": SagAblationSpec(
        id="sag_v2",
        title="Full mechanism minus consistency",
        description=(
            "Everything the solver runs — value witnesses, both gates, the limit "
            "contract, prefix-bisection repair — with a single sample (k=1)."
        ),
        arm="v2",
        limitations=("no consistency vote (k=1)",),
        mechanism_claims=(
            "path_card",
            "dynamic_key_collapse",
            "a_path_gate",
            "repair_loop",
            "value_witnesses",
            "a_value_gate",
            "limit_contract",
            "prefix_bisection",
        ),
    ),
    "sag_full": SagAblationSpec(
        id="sag_full",
        title="Full SAG (reference row)",
        description=(
            "The identical full v3 mechanism (k=3 result-consistency clustering) run "
            "under the ablation harness — the delta reference, not a mechanism variant."
        ),
        arm="v3",
        limitations=(),
        mechanism_claims=_ALL_MECHANISMS,
    ),
}

# Component knockouts: full v3 minus exactly one mechanism each (panel-scale arms;
# the per-mechanism contribution = sag_full − knockout, order-independent — the
# complement of the cumulative ladder above).
_EXTENDED_ABLATIONS: dict[str, SagAblationSpec] = {
    "sag_v3_no_value": SagAblationSpec(
        id="sag_v3_no_value",
        title="v3 minus value grounding",
        description=(
            "Full v3 with value witnesses, the A_value gate, and the limit contract "
            "disabled (bisection follows value grounding off); card, A_path gate, "
            "repair, and k=3 consistency stay on."
        ),
        arm="v3",
        limitations=("no value witnesses", "no A_value gate", "no limit contract",
                     "plain empty feedback (no prefix bisection)"),
        mechanism_claims=(
            "path_card",
            "dynamic_key_collapse",
            "a_path_gate",
            "repair_loop",
            "k_consistency",
        ),
        value_grounding_override=False,
        variant_label="no_value",
    ),
    "sag_v3_no_gate": SagAblationSpec(
        id="sag_v3_no_gate",
        title="v3 minus A_path gate",
        description=(
            "Full v3 with the A_path admissibility gate disabled; value grounding, "
            "repair, and k=3 consistency stay on."
        ),
        arm="v3",
        limitations=("no A_path gate",),
        mechanism_claims=tuple(m for m in _ALL_MECHANISMS if m != "a_path_gate"),
        gate_override=False,
        variant_label="no_gate",
    ),
    "sag_v3_plain_empty": SagAblationSpec(
        id="sag_v3_plain_empty",
        title="v3 minus bisection feedback",
        description=(
            "Full v3 with prefix-bisection feedback replaced by a plain 'returns 0 "
            "rows' message — isolates feedback CONTENT from retry count."
        ),
        arm="v3",
        limitations=("plain empty feedback (no prefix bisection)",),
        mechanism_claims=tuple(m for m in _ALL_MECHANISMS if m != "prefix_bisection"),
        bisection_override=False,
        variant_label="plain_empty",
    ),
    "sag_v3_top_card": SagAblationSpec(
        id="sag_v3_top_card",
        title="v3 minus card completeness",
        description=(
            "Full v3 with the card truncated to top-level fields only (the early "
            "prototype's grounding); gates and witnesses still use the full lattice."
        ),
        arm="v3",
        limitations=("card shows top-level fields only",),
        mechanism_claims=tuple(
            m for m in _ALL_MECHANISMS if m not in ("path_card", "dynamic_key_collapse")
        ),
        card_mode="toplevel",
        variant_label="top_card",
    ),
    "sag_v3_no_collapse": SagAblationSpec(
        id="sag_v3_no_collapse",
        title="v3 minus dynamic-key collapse",
        description=(
            "Full v3 with the card rendered without `<*>` collapse or dynamic-key "
            "affordances: concrete data keys verbatim, blowup absorbed by the cap."
        ),
        arm="v3",
        limitations=("no dynamic-key collapse in the card",),
        mechanism_claims=tuple(
            m for m in _ALL_MECHANISMS if m != "dynamic_key_collapse"
        ),
        card_mode="nocollapse",
        variant_label="no_collapse",
    ),
}

ABLATION_IDS = ablation_ids()
EXTENDED_ABLATION_IDS = extended_ablation_ids()


__all__ = [
    "ABLATION_IDS",
    "EXTENDED_ABLATION_IDS",
    "SWEEP_OVERRIDE_KEYS",
    "AblationSpec",
    "SagAblationSpec",
    "ablation_ids",
    "extended_ablation_ids",
    "resolve_ablations",
]
