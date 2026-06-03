"""Evidence ledger and debt tracking for SMART-EG."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ClaimStatus = Literal["unsupported", "partial", "supported", "challenged", "rejected"]


@dataclass
class EvidenceClaim:
    claim_id: str
    claim_type: str
    statement: str
    status: ClaimStatus = "unsupported"
    required_evidence: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    used_by: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceRecord:
    evidence_id: str
    source_tool: str
    tool_call_id: str
    observation_ref: str
    summary: dict[str, Any]
    supports_claims: list[str] = field(default_factory=list)
    contradicts_claims: list[str] = field(default_factory=list)
    redaction: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceDebt:
    debt_id: str
    milestone: Literal["environment", "intent", "plan", "final"]
    claim_type: str
    blocking: bool = True
    missing_evidence: list[str] = field(default_factory=list)
    suggested_tools: list[str] = field(default_factory=list)
    normalized_signature: str = ""
    attempts: int = 0
    resolved: bool = False
    claim_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class EvidenceLedger:
    """Append-friendly claim/evidence/debt index used by submit gates."""

    def __init__(self) -> None:
        self.claims: dict[str, EvidenceClaim] = {}
        self.records: dict[str, EvidenceRecord] = {}
        self.debts: dict[str, EvidenceDebt] = {}
        self._claim_seq = 0
        self._evidence_seq = 0
        self._debt_seq = 0

    def add_claim(
        self,
        claim: EvidenceClaim | None = None,
        *,
        claim_type: str | None = None,
        statement: str | None = None,
        required_evidence: list[str] | None = None,
        evidence_refs: list[str] | None = None,
        used_by: list[str] | None = None,
        status: ClaimStatus | None = None,
    ) -> EvidenceClaim:
        generated = claim is None
        if claim is None:
            self._claim_seq += 1
            claim = EvidenceClaim(
                claim_id=f"claim-{self._claim_seq:04d}",
                claim_type=str(claim_type or "unknown"),
                statement=str(statement or ""),
                status=status or "unsupported",
                required_evidence=list(required_evidence or []),
                evidence_refs=list(evidence_refs or []),
                used_by=list(used_by or []),
            )
        else:
            self._claim_seq = max(self._claim_seq, _sequence_suffix(claim.claim_id))
        self.claims[claim.claim_id] = claim
        if not generated:
            return claim
        self._refresh_claim_status(claim.claim_id)
        if claim.status != "supported":
            self.ensure_debt_for_claim(claim)
        return claim

    def add_debt(self, debt: EvidenceDebt) -> EvidenceDebt:
        self._debt_seq = max(self._debt_seq, _sequence_suffix(debt.debt_id))
        self.debts[debt.debt_id] = debt
        return debt

    def add_record(
        self,
        *,
        source_tool: str,
        tool_call_id: str,
        observation_ref: str,
        summary: dict[str, Any],
        supports_claims: list[str] | None = None,
        contradicts_claims: list[str] | None = None,
        redaction: dict[str, Any] | None = None,
    ) -> EvidenceRecord:
        self._evidence_seq += 1
        record = EvidenceRecord(
            evidence_id=f"ev-{self._evidence_seq:04d}",
            source_tool=source_tool,
            tool_call_id=tool_call_id,
            observation_ref=observation_ref,
            summary=dict(summary),
            supports_claims=list(supports_claims or []),
            contradicts_claims=list(contradicts_claims or []),
            redaction=dict(redaction or {}),
        )
        self.records[record.evidence_id] = record
        for claim_id in record.supports_claims:
            self.link_evidence(claim_id, record.evidence_id)
        for claim_id in record.contradicts_claims:
            if claim_id in self.claims:
                self.claims[claim_id].status = "challenged"
                self.ensure_debt_for_claim(self.claims[claim_id])
        return record

    def link_evidence(self, claim_id: str, evidence_id: str) -> None:
        claim = self.claims.get(claim_id)
        if claim is None or evidence_id not in self.records:
            return
        if evidence_id not in claim.evidence_refs:
            claim.evidence_refs.append(evidence_id)
        record = self.records[evidence_id]
        if claim_id not in record.supports_claims:
            record.supports_claims.append(claim_id)
        self._refresh_claim_status(claim_id)

    def challenge_claims(self, claim_ids: list[str]) -> None:
        for claim_id in claim_ids:
            claim = self.claims.get(claim_id)
            if claim is None:
                continue
            claim.status = "challenged"
            self.ensure_debt_for_claim(claim)

    def ensure_debt_for_claim(self, claim: EvidenceClaim) -> EvidenceDebt:
        missing = self._missing_for_claim(claim)
        signature = self._signature(
            self._milestone_for_claim(claim),
            claim.claim_type,
            missing or claim.required_evidence or ["support"],
        )
        for debt in self.debts.values():
            if debt.normalized_signature == signature and not debt.resolved:
                debt.attempts += 1
                return debt
        self._debt_seq += 1
        debt = EvidenceDebt(
            debt_id=f"debt-{self._debt_seq:04d}",
            milestone=self._milestone_for_claim(claim),
            claim_type=claim.claim_type,
            blocking=True,
            missing_evidence=missing or claim.required_evidence or ["support"],
            suggested_tools=missing or claim.required_evidence or ["inspect_evidence_ledger"],
            normalized_signature=signature,
            attempts=1,
            claim_id=claim.claim_id,
        )
        self.debts[debt.debt_id] = debt
        return debt

    def ensure_debt(
        self,
        *,
        milestone: Literal["environment", "intent", "plan", "final"],
        claim_type: str,
        missing_evidence: list[str],
        suggested_tools: list[str] | None = None,
        claim_id: str | None = None,
    ) -> EvidenceDebt:
        signature = self._signature(milestone, claim_type, missing_evidence)
        for debt in self.debts.values():
            if debt.normalized_signature == signature and not debt.resolved:
                debt.attempts += 1
                return debt
        self._debt_seq += 1
        debt = EvidenceDebt(
            debt_id=f"debt-{self._debt_seq:04d}",
            milestone=milestone,
            claim_type=claim_type,
            blocking=True,
            missing_evidence=list(missing_evidence),
            suggested_tools=list(suggested_tools or missing_evidence),
            normalized_signature=signature,
            attempts=1,
            claim_id=claim_id,
        )
        self.debts[debt.debt_id] = debt
        return debt

    def blocking_debts(self, *, milestone: str | None = None) -> list[EvidenceDebt]:
        self._resolve_supported_debts()
        return [
            debt
            for debt in self.debts.values()
            if debt.blocking
            and not debt.resolved
            and (milestone is None or debt.milestone == milestone)
        ]

    def has_evidence_refs(self, refs: list[str]) -> bool:
        return all(ref in self.records for ref in refs)

    def has_evidence_sources(self, sources: list[str]) -> bool:
        available = {record.source_tool for record in self.records.values()}
        return set(sources).issubset(available)

    def summary(self) -> dict[str, Any]:
        self._resolve_supported_debts()
        return {
            "claims": len(self.claims),
            "evidence_records": len(self.records),
            "debts": len(self.debts),
            "blocking_debts": len(self.blocking_debts()),
            "supported_claims": sum(1 for claim in self.claims.values() if claim.status == "supported"),
            "challenged_claims": [
                claim.claim_id for claim in self.claims.values() if claim.status == "challenged"
            ],
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "claims": {key: value.to_json() for key, value in self.claims.items()},
            "records": {key: value.to_json() for key, value in self.records.items()},
            "debts": {key: value.to_json() for key, value in self.debts.items()},
            "summary": self.summary(),
        }

    def _refresh_claim_status(self, claim_id: str) -> None:
        claim = self.claims[claim_id]
        if claim.status in {"challenged", "rejected"}:
            return
        if not claim.required_evidence and claim.evidence_refs:
            claim.status = "supported"
            return
        sources = {
            self.records[ref].source_tool
            for ref in claim.evidence_refs
            if ref in self.records
        }
        if claim.required_evidence and set(claim.required_evidence).issubset(sources):
            claim.status = "supported"
        elif claim.evidence_refs:
            claim.status = "partial"
        else:
            claim.status = "unsupported"
        if claim.status == "supported":
            for debt in self.debts.values():
                debt_matches_claim = debt.claim_id == claim_id
                debt_matches_type = (
                    debt.claim_type == claim.claim_type
                    and set(debt.missing_evidence).issubset(set(claim.required_evidence))
                )
                if debt_matches_claim or debt_matches_type:
                    debt.resolved = True

    def _resolve_supported_debts(self) -> None:
        for claim_id in list(self.claims):
            self._refresh_claim_status(claim_id)
        sources = {record.source_tool for record in self.records.values()}
        for debt in self.debts.values():
            if debt.resolved:
                continue
            if debt.missing_evidence and set(debt.missing_evidence).issubset(sources):
                debt.resolved = True

    def _missing_for_claim(self, claim: EvidenceClaim) -> list[str]:
        sources = {
            self.records[ref].source_tool
            for ref in claim.evidence_refs
            if ref in self.records
        }
        return [source for source in claim.required_evidence if source not in sources]

    @staticmethod
    def _milestone_for_claim(
        claim: EvidenceClaim,
    ) -> Literal["environment", "intent", "plan", "final"]:
        for milestone in ("environment", "intent", "plan", "final"):
            if milestone in claim.used_by:
                return milestone  # type: ignore[return-value]
        if claim.claim_type in {
            "collection_selection",
            "field_grounding",
            "value_grounding",
            "shape_branch",
            "relationship_grounding",
        }:
            return "environment"
        if claim.claim_type in {"operator_idiom", "execution_checkpoint"}:
            return "plan"
        return "intent"

    @staticmethod
    def _signature(milestone: str, claim_type: str, missing: list[str]) -> str:
        parts = [str(item).strip().lower() for item in missing if str(item).strip()]
        return f"{milestone}:{claim_type}:{','.join(sorted(parts))}"

    def __repr__(self) -> str:
        return json.dumps(self.summary(), sort_keys=True)


def _sequence_suffix(value: str) -> int:
    tail = str(value).replace("_", "-").rsplit("-", 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return 0
