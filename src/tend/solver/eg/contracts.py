"""SMART-EG runtime contracts."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .evidence import EvidenceClaim, EvidenceDebt, EvidenceLedger, EvidenceRecord

SmartEGMode = Literal["environment", "intent", "planning", "execution", "repair"]
Milestone = Literal["environment", "intent", "plan", "final"]
NextAction = Literal["continue", "revisit", "terminal_only", "abandon", "finalized"]


@dataclass
class SmartEGBudgets:
    max_tool_turns: int = 48
    max_revisits: int = 3
    max_repeated_submit_rejections: int = 8
    max_repeated_protocol_violations: int = 2
    terminal_turn_window: int = 2
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    history_max_messages: int = 40

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SmartEGCounters:
    llm_turns: int = 0
    tool_turns: int = 0
    revisits: int = 0
    submit_rejections: int = 0
    protocol_violations: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    repeated_execution_failures: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QueryCandidate:
    candidate_id: str
    collection: str
    pipeline: list[dict[str, Any]]
    mql: str
    evidence_refs: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionTrace:
    prefix_runs: list[dict[str, Any]] = field(default_factory=list)
    checkpoint_results: list[dict[str, Any]] = field(default_factory=list)
    final_sanity_runs: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    repaired_candidate_ids: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SmartEGState:
    nlq: str
    db_id: str
    record_id: str | int | None = None
    mode: SmartEGMode = "environment"
    environment: dict[str, Any] | None = None
    intent: dict[str, Any] | None = None
    query_plan: dict[str, Any] | None = None
    execution_trace: ExecutionTrace = field(default_factory=ExecutionTrace)
    evidence_ledger: EvidenceLedger = field(default_factory=EvidenceLedger)
    debt_queue: list[EvidenceDebt] = field(default_factory=list)
    candidates: list[QueryCandidate] = field(default_factory=list)
    best_candidate_id: str | None = None
    budgets: SmartEGBudgets = field(default_factory=SmartEGBudgets)
    counters: SmartEGCounters = field(default_factory=SmartEGCounters)
    last_submit_rejection_evidence_count: int = 0
    stale_milestones: set[str] = field(default_factory=set)
    terminal: bool = False
    terminal_only: bool = False
    terminal_reason: str | None = None
    result: Any | None = None
    revisit_signatures: set[str] = field(default_factory=set)
    submit_gate_refs: list[str] = field(default_factory=list)
    session_id: str | None = None

    def refresh_debt_queue(self) -> None:
        self.debt_queue = self.evidence_ledger.blocking_debts()

    def summary(self) -> dict[str, Any]:
        self.refresh_debt_queue()
        return {
            "nlq": self.nlq,
            "db_id": self.db_id,
            "record_id": self.record_id,
            "mode": self.mode,
            "terminal": self.terminal,
            "terminal_only": self.terminal_only,
            "terminal_reason": self.terminal_reason,
            "stale_milestones": sorted(self.stale_milestones),
            "best_candidate_id": self.best_candidate_id,
            "debt_count": len(self.debt_queue),
            "budgets": self.budgets.to_json(),
            "counters": self.counters.to_json(),
            "evidence": self.evidence_ledger.summary(),
        }


@dataclass
class GateViolation:
    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SubmitGateResult:
    submit_tool: str
    accepted: bool
    milestone: Milestone
    candidate_id: str | None = None
    violations: list[GateViolation] = field(default_factory=list)
    new_debts: list[EvidenceDebt] = field(default_factory=list)
    challenged_claims: list[str] = field(default_factory=list)
    stale_milestones: list[str] = field(default_factory=list)
    required_next_action: NextAction = "continue"

    def to_json(self) -> dict[str, Any]:
        return {
            "submit_tool": self.submit_tool,
            "accepted": self.accepted,
            "milestone": self.milestone,
            "candidate_id": self.candidate_id,
            "violations": [item.to_json() for item in self.violations],
            "new_debts": [item.to_json() for item in self.new_debts],
            "challenged_claims": list(self.challenged_claims),
            "stale_milestones": list(self.stale_milestones),
            "required_next_action": self.required_next_action,
        }


@dataclass
class ToolObservation:
    name: str
    tool_call_id: str
    ok: bool
    result: dict[str, Any]
    evidence_ids: list[str] = field(default_factory=list)
    gate_ref: str | None = None
    llm_visible_content: dict[str, Any] = field(default_factory=dict)


@dataclass
class SmartEGPrediction:
    result_type: Literal["solver_prediction"]
    db_id: str
    record_id: str | int | None
    nlq: str
    collection: str
    pipeline: list[dict[str, Any]]
    MQL: str
    disclosure: dict[str, Any]
    environment_model_ref: str
    intent_ref: str
    query_plan_ref: str
    execution_trace_ref: str
    evidence_ledger_ref: str
    agent_session_ref: str
    submit_gate_refs: list[str]
    transcript_refs: list[str] = field(default_factory=list)
    diagnostics_refs: list[str] = field(default_factory=list)
    error_refs: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SmartEGFailure:
    result_type: Literal["solver_failure"]
    db_id: str
    record_id: str | int | None
    nlq: str
    error_code: Literal[
        "INSUFFICIENT_EVIDENCE",
        "TOOL_BUDGET_EXHAUSTED",
        "PROVIDER_FAILURE",
        "BOUNDARY_REJECTED",
        "EXECUTION_UNRESOLVED",
        "NO_VALID_QUERY_FOUND",
    ]
    message: str
    last_candidate_ref: str | None
    unresolved_debts: list[str]
    evidence_ledger_ref: str
    execution_trace_ref: str
    agent_session_ref: str
    transcript_refs: list[str] = field(default_factory=list)
    diagnostics_refs: list[str] = field(default_factory=list)
    error_refs: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def mode_for_milestone(milestone: Milestone) -> SmartEGMode:
    if milestone == "plan":
        return "planning"
    if milestone == "final":
        return "execution"
    return milestone


def downstream_milestones(milestone: Milestone) -> set[str]:
    if milestone == "environment":
        return {"environment", "intent", "plan", "final"}
    if milestone == "intent":
        return {"intent", "plan", "final"}
    if milestone == "plan":
        return {"plan", "final"}
    return {"final"}
