"""Ablation definitions for the SMART-EG solver."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import SourceError

DEFAULT_MAX_TURNS = 100
MEDIUM_BUDGET_MAX_TURNS = 24
DEFAULT_MAX_REVISITS = 2
DEFAULT_COST_BUDGET_USD = 1.0
COST_BUDGET_USD_SOURCE = "provider_cost_usd_if_available"
COST_BUDGET_USD_UNPRICED_BEHAVIOR = "advisory_when_unpriced"
PROBE_SCHEDULER_STATUS = "unsupported"


@dataclass(frozen=True, slots=True)
class BudgetProfile:
    name: str
    max_turns: int
    max_revisits: int
    cost_budget_usd: float


BUDGET_PROFILES: dict[str, BudgetProfile] = {
    "full": BudgetProfile(
        "full", DEFAULT_MAX_TURNS, DEFAULT_MAX_REVISITS, DEFAULT_COST_BUDGET_USD
    ),
    "reference": BudgetProfile(
        "reference",
        DEFAULT_MAX_TURNS,
        DEFAULT_MAX_REVISITS,
        DEFAULT_COST_BUDGET_USD,
    ),
    "low": BudgetProfile("low", 8, 0, 0.25),
    "medium": BudgetProfile(
        "medium",
        MEDIUM_BUDGET_MAX_TURNS,
        DEFAULT_MAX_REVISITS,
        DEFAULT_COST_BUDGET_USD,
    ),
    "high": BudgetProfile("high", 72, 4, 3.0),
}


@dataclass(frozen=True, slots=True)
class SmartEGAblationSpec:
    id: str
    title: str
    description: str
    limitations: tuple[str, ...] = ()
    use_evidence_gate: bool = True
    use_counterexample: bool = True
    use_value_grounding: bool = True
    use_relationship_probe: bool = True
    use_prefix_execution: bool = True
    use_revisit: bool = True
    use_probe_scheduler: bool = False
    max_turns: int | None = None
    max_revisits: int | None = None
    cost_budget_usd: float | None = None
    budget_profile: str = "reference"

    def to_runtime_options(
        self,
        *,
        max_turns: int | None = None,
        max_revisits: int | None = None,
        cost_budget_usd: float | None = None,
        progress_group_prefix: str = "solve",
        progress_work_item_id: str | None = None,
    ) -> dict[str, Any]:
        profile = BUDGET_PROFILES.get(self.budget_profile)
        profile_locks_budget = _profile_locks_budget(self)
        effective_turns = (
            self.max_turns
            if self.max_turns is not None
            else max_turns
            if max_turns is not None and not profile_locks_budget
            else profile.max_turns
            if profile is not None
            else max_turns
            if max_turns is not None
            else DEFAULT_MAX_TURNS
        )
        effective_revisits = (
            self.max_revisits
            if self.max_revisits is not None
            else max_revisits
            if max_revisits is not None and not profile_locks_budget
            else profile.max_revisits
            if profile is not None
            else max_revisits
            if max_revisits is not None
            else DEFAULT_MAX_REVISITS
        )
        effective_cost = (
            self.cost_budget_usd
            if self.cost_budget_usd is not None
            else cost_budget_usd
            if cost_budget_usd is not None and not profile_locks_budget
            else profile.cost_budget_usd
            if profile is not None
            else cost_budget_usd
            if cost_budget_usd is not None
            else DEFAULT_COST_BUDGET_USD
        )
        turn_budget = max(1, int(effective_turns))
        max_revisits_value = max(0, int(effective_revisits))
        cost_budget = max(0.0, float(effective_cost))
        profile_budget = profile or BudgetProfile(
            self.budget_profile,
            turn_budget,
            max_revisits_value,
            cost_budget,
        )
        tool_exposure_intent = {
            "counterexample": _enabled(self.use_counterexample),
            "value_grounding": _enabled(self.use_value_grounding),
            "relationship_probe": _enabled(self.use_relationship_probe),
            "prefix_execution": _enabled(self.use_prefix_execution),
            "probe_scheduler": PROBE_SCHEDULER_STATUS,
        }
        prompt_intent = {
            "value_grounding": _enabled(self.use_value_grounding),
        }
        gate_flags = {
            "evidence_gate": self.use_evidence_gate,
            "evidence_debt_blocking": self.use_evidence_gate,
            "counterexample_gate": self.use_counterexample,
            "value_grounding_gate": self.use_value_grounding,
        }
        state_transition_intent = {
            "revisit": _enabled(self.use_revisit),
            "backward_mode_shift": "allowed" if self.use_revisit else "rejected_by_policy",
        }
        return {
            "ablation_id": self.id,
            "solver_variant": self.id,
            "use_evidence_gate": self.use_evidence_gate,
            "use_counterexample": self.use_counterexample,
            "use_value_grounding": self.use_value_grounding,
            "use_relationship_probe": self.use_relationship_probe,
            "use_prefix_execution": self.use_prefix_execution,
            "use_revisit": self.use_revisit,
            "use_probe_scheduler": self.use_probe_scheduler,
            "probe_scheduler_status": PROBE_SCHEDULER_STATUS,
            "max_turns": turn_budget,
            "max_revisits": max_revisits_value,
            "cost_budget_usd": cost_budget,
            "budget_profile": self.budget_profile,
            "effective_budget_profile": self.budget_profile,
            "effective_agent_turn_count": turn_budget,
            "budget_disclosure": {
                "profile": self.budget_profile,
                "profile_max_turns": profile_budget.max_turns,
                "profile_max_revisits": profile_budget.max_revisits,
                "profile_cost_budget_usd": profile_budget.cost_budget_usd,
                "effective_max_turns": turn_budget,
                "effective_max_revisits": max_revisits_value,
                "effective_cost_budget_usd": cost_budget,
                "mechanism_overrides": [
                    name
                    for name, enabled in (
                        ("max_turns", self.max_turns is not None),
                        ("max_revisits", self.max_revisits is not None),
                        ("cost_budget_usd", self.cost_budget_usd is not None),
                    )
                    if enabled
                ],
                "runtime_overrides_applied": [
                    name
                    for name, enabled in (
                        (
                            "max_turns",
                            max_turns is not None
                            and self.max_turns is None
                            and not profile_locks_budget,
                        ),
                        (
                            "max_revisits",
                            max_revisits is not None
                            and self.max_revisits is None
                            and not profile_locks_budget,
                        ),
                        (
                            "cost_budget_usd",
                            cost_budget_usd is not None
                            and self.cost_budget_usd is None
                            and not profile_locks_budget,
                        ),
                    )
                    if enabled
                ],
                "budget_profile_locked": profile_locks_budget,
                "source": "ablation_spec",
            },
            "cost_budget_usd_source": COST_BUDGET_USD_SOURCE,
            "cost_budget_usd_unpriced_behavior": COST_BUDGET_USD_UNPRICED_BEHAVIOR,
            "tool_exposure_intent": tool_exposure_intent,
            "prompt_intent": prompt_intent,
            "gate_flags": gate_flags,
            "state_transition_intent": state_transition_intent,
            "policy_options": {
                "expose_counterexample_tools": self.use_counterexample,
                "expose_value_grounding_tools": self.use_value_grounding,
                "include_value_grounding_prompt": self.use_value_grounding,
                "block_value_grounding_debt": self.use_value_grounding,
                "expose_relationship_probe_tools": self.use_relationship_probe,
                "expose_prefix_execution_tools": self.use_prefix_execution,
                "enforce_evidence_submit_gate": self.use_evidence_gate,
                "block_evidence_debt": self.use_evidence_gate,
                "allow_revisit": self.use_revisit,
                "allow_backward_mode_shift": self.use_revisit,
                "use_probe_scheduler": False,
            },
            "mechanism_claims": _mechanism_claims(self),
            "progress_group_prefix": progress_group_prefix,
            "progress_work_item_id": progress_work_item_id,
        }


AblationSpec = SmartEGAblationSpec


def ablation_ids() -> tuple[str, ...]:
    return tuple(_ABLATIONS)


def _enabled(value: bool) -> str:
    return "enabled" if value else "disabled"


def _profile_locks_budget(spec: SmartEGAblationSpec) -> bool:
    return spec.id.startswith("smart_eg_budget_")


def _mechanism_claims(spec: SmartEGAblationSpec) -> list[str]:
    claims: list[str] = []
    if spec.use_evidence_gate:
        claims.append("evidence_gate")
    if spec.use_counterexample:
        claims.append("counterexample")
    if spec.use_value_grounding:
        claims.append("value_grounding")
    if spec.use_relationship_probe:
        claims.append("relationship_probe")
    if spec.use_prefix_execution:
        claims.append("prefix_execution")
    if spec.use_revisit:
        claims.append("revisit")
    return claims


def resolve_ablations(
    selection: str | list[str] | tuple[str, ...] | None,
) -> list[SmartEGAblationSpec]:
    if selection is None or selection == "all":
        return list(_ABLATIONS.values())
    parts = selection if isinstance(selection, (list, tuple)) else selection.split(",")
    specs: list[SmartEGAblationSpec] = []
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


_ABLATIONS: dict[str, SmartEGAblationSpec] = {
    "smart_eg_full": SmartEGAblationSpec(
        id="smart_eg_full",
        title="SMART-EG full",
        description=(
            "Provider-native SMART-EG solver with evidence gates, probes, revisits, "
            "prefix execution, and the full reference budget profile."
        ),
        budget_profile="full",
    ),
    "smart_eg_no_evidence_gate": SmartEGAblationSpec(
        id="smart_eg_no_evidence_gate",
        title="No evidence gate",
        description=(
            "Disable deterministic evidence submit gates and evidence-debt blocking."
        ),
        limitations=(
            "evidence collection may still be logged",
            "evidence-related debts do not block submission when policy options are honored",
        ),
        use_evidence_gate=False,
    ),
    "smart_eg_no_counterexample": SmartEGAblationSpec(
        id="smart_eg_no_counterexample",
        title="No counterexample mining",
        description="Disable automatic and agent-callable counterexample probes.",
        limitations=("counterexample mining disabled",),
        use_counterexample=False,
    ),
    "smart_eg_no_value_grounding": SmartEGAblationSpec(
        id="smart_eg_no_value_grounding",
        title="No value grounding",
        description=(
            "Disable NLQ constant/entity grounding tools, prompt cues, and gates."
        ),
        limitations=("value-grounding tools, prompt cues, and gates disabled",),
        use_value_grounding=False,
    ),
    "smart_eg_no_relationship_probe": SmartEGAblationSpec(
        id="smart_eg_no_relationship_probe",
        title="No relationship probe",
        description="Disable relationship-candidate discovery and validation probes.",
        limitations=("relationship probes disabled",),
        use_relationship_probe=False,
    ),
    "smart_eg_no_prefix_execution": SmartEGAblationSpec(
        id="smart_eg_no_prefix_execution",
        title="No prefix execution",
        description=(
            "Disable real prefix-execution tool exposure while retaining final execution."
        ),
        limitations=("real prefix execution tool exposure disabled",),
        use_prefix_execution=False,
    ),
    "smart_eg_no_revisit": SmartEGAblationSpec(
        id="smart_eg_no_revisit",
        title="No revisit",
        description=(
            "Disable explicit milestone revisits and reject backward mode shifts."
        ),
        limitations=("revisit actions disabled", "backward mode shifts rejected by policy"),
        use_revisit=False,
        max_revisits=0,
    ),
    "smart_eg_budget_low": SmartEGAblationSpec(
        id="smart_eg_budget_low",
        title="Low budget profile",
        description=(
            "Run SMART-EG under a low agent-turn/revisit budget profile; this "
            "is not a single-mechanism isolation ablation."
        ),
        limitations=(
            "low agent-turn profile",
            "zero revisit profile",
            "cost ceiling applies only when provider cost_usd is reported",
        ),
        budget_profile="low",
    ),
    "smart_eg_budget_medium": SmartEGAblationSpec(
        id="smart_eg_budget_medium",
        title="Medium budget profile",
        description=(
            "Run SMART-EG under the medium agent-turn/revisit budget profile; "
            "this is not a single-mechanism isolation ablation."
        ),
        limitations=(
            "medium agent-turn profile",
            "cost ceiling applies only when provider cost_usd is reported",
        ),
        budget_profile="medium",
    ),
    "smart_eg_budget_high": SmartEGAblationSpec(
        id="smart_eg_budget_high",
        title="High budget profile",
        description=(
            "Run SMART-EG under an expanded agent-turn/revisit budget profile; "
            "this is not a single-mechanism isolation ablation."
        ),
        limitations=(
            "high agent-turn profile",
            "high revisit profile",
            "cost ceiling applies only when provider cost_usd is reported",
        ),
        budget_profile="high",
    ),
}

ABLATION_IDS = ablation_ids()


__all__ = [
    "ABLATION_IDS",
    "AblationSpec",
    "BUDGET_PROFILES",
    "BudgetProfile",
    "SmartEGAblationSpec",
    "ablation_ids",
    "resolve_ablations",
]
