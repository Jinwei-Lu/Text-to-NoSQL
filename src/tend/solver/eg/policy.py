"""SMART-EG runtime policy and convergence checks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import SmartEGBudgets, SmartEGState


class SmartEGPolicy:
    """Runtime knobs for SMART-EG.

    Accepts either a concrete ``SmartEGBudgets`` object or scalar knobs used by integration
    call sites. The concrete budgets object is what the runtime state carries.
    """

    def __init__(
        self,
        budgets: SmartEGBudgets | None = None,
        *,
        max_turns: int | None = None,
        max_revisits: int = 4,
        evidence_gate: bool = True,
        counterexample_gate: bool = True,
        value_grounding: bool = True,
        relationship_probe: bool = True,
        prefix_execution: bool = True,
        revisit: bool = True,
        probe_scheduler: bool = True,
        budget_profile: str = "medium",
        cost_budget_usd: float | None = None,
        token_budget: int | None = None,
        stream: bool = True,
        force_tool_choice: bool = False,
        enable_final_sanity_execution: bool = True,
    ) -> None:
        self.budgets = budgets or SmartEGBudgets(
            max_turns=max(1, int(max_turns if max_turns is not None else 100)),
            max_revisits=max(0, max_revisits),
            max_tokens=token_budget,
            max_cost_usd=cost_budget_usd,
        )
        self.evidence_gate = evidence_gate
        self.counterexample_gate = counterexample_gate
        self.enable_counterexamples = counterexample_gate
        self.value_grounding = value_grounding
        self.relationship_probe = relationship_probe
        self.prefix_execution = prefix_execution
        self.revisit = revisit
        self.probe_scheduler = probe_scheduler
        self.budget_profile = budget_profile
        self.stream = stream
        self.force_tool_choice = force_tool_choice
        self.enable_final_sanity_execution = enable_final_sanity_execution

    @property
    def first_token_timeout_s(self) -> float:
        """Fixed provider-health timeout for the first streamed token."""
        return 6.0

    def to_json(self) -> dict[str, Any]:
        return {
            "budgets": self.budgets.to_json(),
            "evidence_gate": self.evidence_gate,
            "counterexample_gate": self.counterexample_gate,
            "value_grounding": self.value_grounding,
            "relationship_probe": self.relationship_probe,
            "prefix_execution": self.prefix_execution,
            "revisit": self.revisit,
            "probe_scheduler": self.probe_scheduler,
            "budget_profile": self.budget_profile,
            "first_token_timeout_s": 6.0,
            "stream": self.stream,
            "force_tool_choice": self.force_tool_choice,
            "enable_final_sanity_execution": self.enable_final_sanity_execution,
        }


@dataclass(frozen=True)
class ConvergenceResult:
    hard_stop: bool = False
    terminal_only: bool = False
    reason: str | None = None


class SmartEGConvergenceChecker:
    def __init__(self, policy: SmartEGPolicy) -> None:
        self.policy = policy

    def check(self, state: SmartEGState) -> ConvergenceResult:
        budgets = state.budgets
        counters = state.counters
        if counters.llm_turns >= budgets.max_turns:
            return ConvergenceResult(hard_stop=True, reason="max_turns")
        if budgets.max_tokens is not None and counters.tokens >= budgets.max_tokens:
            return ConvergenceResult(hard_stop=True, reason="max_tokens")
        if budgets.max_cost_usd is not None and counters.cost_usd >= budgets.max_cost_usd:
            return ConvergenceResult(hard_stop=True, reason="max_cost_usd")
        if counters.protocol_violations >= budgets.max_repeated_protocol_violations:
            return ConvergenceResult(terminal_only=True, reason="protocol_invalid_repeated")
        if counters.submit_rejections >= budgets.max_repeated_submit_rejections:
            return ConvergenceResult(terminal_only=True, reason="submit_rejected_repeated")
        remaining = budgets.max_turns - counters.llm_turns
        if remaining <= budgets.terminal_turn_window:
            return ConvergenceResult(terminal_only=True, reason="final_turn_window")
        return ConvergenceResult()
