"""Ablation definitions for the SMART-EG solver."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_TOOL_TURNS = 48
MEDIUM_BUDGET_MAX_TOOL_TURNS = 24
DEFAULT_MAX_REVISITS = 2
DEFAULT_COST_BUDGET_USD = 1.0
COST_BUDGET_USD_SOURCE = "provider_cost_usd_if_available"
COST_BUDGET_USD_UNPRICED_BEHAVIOR = "advisory_when_unpriced"


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
    use_probe_scheduler: bool = True
    max_tool_turns: int | None = None
    max_revisits: int | None = None
    cost_budget_usd: float | None = None
    budget_profile: str = "medium"

    def to_runtime_options(
        self,
        *,
        max_tool_turns: int | None = None,
        max_revisits: int | None = None,
        cost_budget_usd: float | None = None,
        progress_group_prefix: str = "solve",
        progress_work_item_id: str | None = None,
    ) -> dict[str, Any]:
        effective_tool_turns = (
            self.max_tool_turns
            if self.max_tool_turns is not None
            else max_tool_turns
            if max_tool_turns is not None
            else DEFAULT_MAX_TOOL_TURNS
        )
        effective_revisits = (
            self.max_revisits
            if self.max_revisits is not None
            else max_revisits
            if max_revisits is not None
            else DEFAULT_MAX_REVISITS
        )
        effective_cost = (
            self.cost_budget_usd
            if self.cost_budget_usd is not None
            else cost_budget_usd
            if cost_budget_usd is not None
            else DEFAULT_COST_BUDGET_USD
        )
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
            "max_tool_turns": max(1, int(effective_tool_turns)),
            "max_revisits": max(0, int(effective_revisits)),
            "cost_budget_usd": max(0.0, float(effective_cost)),
            "budget_profile": self.budget_profile,
            "cost_budget_usd_source": COST_BUDGET_USD_SOURCE,
            "cost_budget_usd_unpriced_behavior": COST_BUDGET_USD_UNPRICED_BEHAVIOR,
            "progress_group_prefix": progress_group_prefix,
            "progress_work_item_id": progress_work_item_id,
        }


AblationSpec = SmartEGAblationSpec


def ablation_ids() -> tuple[str, ...]:
    return tuple(_ABLATIONS)


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
        raise KeyError(f"unknown ablations: {unknown}; known={list(_ABLATIONS)}")
    return specs


_ABLATIONS: dict[str, SmartEGAblationSpec] = {
    "smart_eg_full": SmartEGAblationSpec(
        id="smart_eg_full",
        title="SMART-EG full",
        description=(
            "Provider-native SMART-EG solver with evidence gates, probes, revisits, "
            "and prefix execution."
        ),
    ),
    "smart_eg_no_evidence_gate": SmartEGAblationSpec(
        id="smart_eg_no_evidence_gate",
        title="No evidence gate",
        description="Disable deterministic submit-gate evidence requirements.",
        limitations=("submit gates do not block insufficient evidence",),
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
        description="Disable NLQ constant/entity grounding probes.",
        limitations=("value grounding disabled",),
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
        description="Disable prefix-execution tool exposure while retaining final execution.",
        limitations=("prefix execution tool exposure disabled",),
        use_prefix_execution=False,
    ),
    "smart_eg_no_revisit": SmartEGAblationSpec(
        id="smart_eg_no_revisit",
        title="No revisit",
        description="Disable explicit milestone revisits and stale propagation.",
        limitations=("revisit actions disabled",),
        use_revisit=False,
        max_revisits=0,
    ),
    "smart_eg_budget_low": SmartEGAblationSpec(
        id="smart_eg_budget_low",
        title="Low budget profile",
        description=(
            "Run SMART-EG under a low tool-turn/revisit budget profile; this "
            "is not a single-mechanism isolation ablation."
        ),
        limitations=(
            "low tool-turn profile",
            "zero revisit profile",
            "cost ceiling applies only when provider cost_usd is reported",
        ),
        max_tool_turns=8,
        max_revisits=0,
        cost_budget_usd=0.25,
        budget_profile="low",
    ),
    "smart_eg_budget_medium": SmartEGAblationSpec(
        id="smart_eg_budget_medium",
        title="Medium budget profile",
        description=(
            "Run SMART-EG under the reference tool-turn/revisit budget profile; "
            "this is not a single-mechanism isolation ablation."
        ),
        limitations=(
            "reference tool-turn profile",
            "cost ceiling applies only when provider cost_usd is reported",
        ),
        max_tool_turns=MEDIUM_BUDGET_MAX_TOOL_TURNS,
        max_revisits=DEFAULT_MAX_REVISITS,
        cost_budget_usd=DEFAULT_COST_BUDGET_USD,
        budget_profile="medium",
    ),
    "smart_eg_budget_high": SmartEGAblationSpec(
        id="smart_eg_budget_high",
        title="High budget profile",
        description=(
            "Run SMART-EG under an expanded tool-turn/revisit budget profile; "
            "this is not a single-mechanism isolation ablation."
        ),
        limitations=(
            "high tool-turn profile",
            "high revisit profile",
            "cost ceiling applies only when provider cost_usd is reported",
        ),
        max_tool_turns=48,
        max_revisits=4,
        cost_budget_usd=3.0,
        budget_profile="high",
    ),
}

ABLATION_IDS = ablation_ids()


__all__ = [
    "ABLATION_IDS",
    "AblationSpec",
    "SmartEGAblationSpec",
    "ablation_ids",
    "resolve_ablations",
]
