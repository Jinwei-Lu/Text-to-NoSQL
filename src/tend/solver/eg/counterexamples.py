"""Deterministic SMART-EG counterexample helpers."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class CounterexampleHit:
    code: str
    message: str
    suggested_tools: list[str]
    challenged_claims: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def mine_counterexamples(
    candidate: dict[str, Any] | None = None,
    *,
    plan: dict[str, Any] | None = None,
    final_candidate: dict[str, Any] | None = None,
    ledger: Any | None = None,
) -> list[CounterexampleHit]:
    """Return simple deterministic risks extracted from a candidate pipeline.

    ``candidate`` is kept for backward-compatible callers. Runtime gates pass
    ``plan=...``; final checks may pass ``final_candidate=...``.
    """
    source = final_candidate if isinstance(final_candidate, dict) else None
    source = source or (plan if isinstance(plan, dict) else None)
    source = source or (candidate if isinstance(candidate, dict) else {})
    stages = _stages(source)
    risks: list[CounterexampleHit] = []
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            continue
        if "$unwind" in stage:
            suggested_tools = ["inspect_array_shape", "run_readonly_probe"]
            if _has_evidence_sources(ledger, suggested_tools):
                continue
            risks.append(
                CounterexampleHit(
                    code="unwind_risk",
                    message="Plan unwinds an array; prove missing/null/non-array behavior and row-grain.",
                    suggested_tools=suggested_tools,
                    context={"stage_index": index, "operator": "$unwind"},
                )
            )
        if "$lookup" in stage:
            suggested_tools = ["profile_relationship_candidates", "run_readonly_probe"]
            if _has_evidence_sources(ledger, suggested_tools):
                continue
            risks.append(
                CounterexampleHit(
                    code="relationship_mismatch_risk",
                    message="Plan joins collections; prove key compatibility and relationship cardinality.",
                    suggested_tools=suggested_tools,
                    context={"stage_index": index, "operator": "$lookup"},
                )
            )
    return risks


def _stages(candidate: dict[str, Any]) -> list[Any]:
    stages = candidate.get("stages")
    if isinstance(stages, list):
        return stages
    pipeline = candidate.get("pipeline")
    if isinstance(pipeline, list):
        return pipeline
    return []


def _has_evidence_sources(ledger: Any | None, sources: list[str]) -> bool:
    if ledger is None:
        return False
    has_sources = getattr(ledger, "has_evidence_sources", None)
    if not callable(has_sources):
        return False
    try:
        return bool(has_sources(sources))
    except Exception:  # noqa: BLE001 - counterexample mining should stay best-effort
        return False


__all__ = ["CounterexampleHit", "mine_counterexamples"]
