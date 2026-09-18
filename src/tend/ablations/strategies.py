"""Ablation definitions for the SAG solver.

The registry holds the seven arms of the final component ablation (RESULTS.md).
``sag_full`` runs the full solver under the ablation harness and is the per-system
reference row for delta computation (``evaluation.metrics.REFERENCE_ABLATION_SYSTEM``);
each other arm removes one component. ``all`` selects every arm.

Hyperparameter sweeps (k / repair rounds / sample budget / card cap) are not arms:
pass ``--solver-option KEY=VALUE`` overrides, applied uniformly to every selected arm
via ``to_policy(overrides=...)``.
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
    # Component knobs (None / default = on, as in the full solver).
    value_grounding_override: bool | None = None
    bisection_override: bool | None = None
    card_mode: str = "lattice"
    variant_label: str = ""
    database_context_mode: str = "induced"
    use_gate_repair: bool | None = None
    limit_contract_override: bool | None = None
    synthetic_id_override: bool | None = None
    card_literal_examples: bool = True
    raw_docs_per_collection: int = 3
    raw_doc_prefix_bytes: int = 12_288

    def to_policy(self, *, overrides: dict[str, Any] | None = None) -> SAGPolicy:
        policy = SAGPolicy(
            arm=self.arm,
            value_grounding_override=self.value_grounding_override,
            bisection_override=self.bisection_override,
            card_mode=self.card_mode,
            variant_label=self.variant_label,
            database_context_mode=self.database_context_mode,
            use_gate_repair=self.use_gate_repair,
            limit_contract_override=self.limit_contract_override,
            synthetic_id_override=self.synthetic_id_override,
            card_literal_examples=self.card_literal_examples,
            raw_docs_per_collection=self.raw_docs_per_collection,
            raw_doc_prefix_bytes=self.raw_doc_prefix_bytes,
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
            "database_context_mode": policy.database_context_mode,
            "uses_value_witnesses": policy.use_value_witnesses,
            "uses_gate_repair": policy.gate_repair_enabled,
            "uses_limit_contract": policy.use_limit_contract,
            "uses_card_literal_examples": bool(policy.card_literal_examples)
            and policy.database_context_mode == "induced",
            "raw_docs_per_collection": policy.raw_docs_per_collection,
            "raw_doc_prefix_bytes": policy.raw_doc_prefix_bytes,
            "mechanism_claims": list(self.mechanism_claims),
            "disabled_vs_solver": [
                name for name in _ALL_MECHANISMS if name not in self.mechanism_claims
            ],
            "is_reference": self.id == "sag_full",
            "progress_group_prefix": progress_group_prefix,
            "progress_work_item_id": progress_work_item_id,
        }


def resolve_ablations(
    selection: str | list[str] | tuple[str, ...] | None,
) -> list[SagAblationSpec]:
    if selection is None or selection == "all":
        return list(_ABLATIONS.values())
    parts = selection if isinstance(selection, (list, tuple)) else selection.split(",")
    specs: list[SagAblationSpec] = []
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
        raise SourceError(f"unknown ablations: {unknown}; known={list(_ABLATIONS)}")
    if not specs:
        raise SourceError("ablation selection did not include any ablation ids")
    return specs


# In the order of the RESULTS.md ablation tables.
_ABLATIONS: dict[str, SagAblationSpec] = {
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
    "sag_core_generate_only": SagAblationSpec(
        id="sag_core_generate_only",
        title="SAG only Generate Query",
        description=(
            "Retain the full induced card and Value Witness in the initial prompt. "
            "Decode one candidate once. Remove every model-visible gate, feedback, "
            "repair, and result-space vote."
        ),
        arm="v2",
        limitations=(
            "single decode, no repair",
            "no A_path or A_value",
            "no limit contract",
            "no execution feedback or repair",
            "no consistency vote (k=1)",
        ),
        mechanism_claims=(
            "path_card",
            "dynamic_key_collapse",
            "value_witnesses",
        ),
        use_gate_repair=False,
        variant_label="core_generate_only",
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
    "sag_core_no_value_witness_strict": SagAblationSpec(
        id="sag_core_no_value_witness_strict",
        title="SAG minus Value Witness (strict)",
        description=(
            "Retain the induced path structure, A_path, limit contract, execution "
            "repair with plain empty feedback, synthetic-id feedback, and k=3 "
            "voting. Do not build a value index, emit witness lines, run A_value, "
            "bisect empty results, or print stored-value examples on the card."
        ),
        arm="v3",
        limitations=(
            "no value index",
            "no value witnesses",
            "no A_value",
            "plain empty feedback (no prefix bisection)",
            "no stored-value examples on the card",
        ),
        mechanism_claims=tuple(
            mechanism
            for mechanism in _ALL_MECHANISMS
            if mechanism not in ("value_witnesses", "a_value_gate", "prefix_bisection")
        ),
        value_grounding_override=False,
        bisection_override=False,
        use_gate_repair=True,
        limit_contract_override=True,
        synthetic_id_override=True,
        card_literal_examples=False,
        variant_label="core_no_value_witness_strict",
    ),
    "sag_core_no_grounding": SagAblationSpec(
        id="sag_core_no_grounding",
        title="SAG minus Grounding Induction",
        description=(
            "Bypass the complete induced grounding front end. Supply the first "
            "three bounded raw documents per collection; retain non-index-dependent "
            "execution repair and k=3 result-space voting."
        ),
        arm="v3",
        limitations=(
            "no induced path card",
            "no dynamic-key abstraction",
            "no value witnesses",
            "no A_path or A_value",
        ),
        mechanism_claims=(
            "repair_loop",
            "limit_contract",
            "prefix_bisection",
            "k_consistency",
        ),
        value_grounding_override=False,
        bisection_override=True,
        database_context_mode="raw3",
        use_gate_repair=True,
        limit_contract_override=True,
        synthetic_id_override=True,
        raw_docs_per_collection=3,
        raw_doc_prefix_bytes=12_288,
        variant_label="core_no_grounding",
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

ABLATION_IDS = tuple(_ABLATIONS)


__all__ = [
    "ABLATION_IDS",
    "SWEEP_OVERRIDE_KEYS",
    "SagAblationSpec",
    "resolve_ablations",
]
