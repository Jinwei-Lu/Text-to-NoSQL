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
    binding: dict[str, Any] = field(default_factory=dict)

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
    binding: dict[str, Any] = field(default_factory=dict)

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
        binding: dict[str, Any] | None = None,
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
            binding=_clean_binding(binding if binding is not None else summary),
        )
        self.records[record.evidence_id] = record
        for claim_id in record.supports_claims:
            self.link_evidence(claim_id, record.evidence_id)
        for claim_id in record.contradicts_claims:
            if claim_id in self.claims:
                self.claims[claim_id].status = "challenged"
                self.ensure_debt_for_claim(self.claims[claim_id])
        return record

    def link_evidence(self, claim_id: str, evidence_id: str) -> bool:
        claim = self.claims.get(claim_id)
        if claim is None or evidence_id not in self.records:
            return False
        if evidence_id not in claim.evidence_refs:
            claim.evidence_refs.append(evidence_id)
        record = self.records[evidence_id]
        if claim_id not in record.supports_claims:
            record.supports_claims.append(claim_id)
        self._refresh_claim_status(claim_id)
        return True

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
        binding: dict[str, Any] | None = None,
    ) -> EvidenceDebt:
        clean_binding = _clean_binding(binding)
        signature = self._signature(milestone, claim_type, missing_evidence, clean_binding)
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
            binding=clean_binding,
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
        markers = _evidence_markers(self.records.values())
        for debt in self.debts.values():
            if debt.resolved:
                continue
            missing = set(debt.missing_evidence)
            if not missing:
                continue
            if debt.binding:
                matching_records = [record for record in self.records.values() if _record_matches_debt(record, debt)]
                matching_sources = {record.source_tool for record in matching_records}
                matching_markers = _evidence_markers(matching_records)
                if missing.issubset(matching_sources) or missing.issubset(matching_markers):
                    debt.resolved = True
                continue
            if missing.issubset(sources) or missing.issubset(markers):
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
    def _signature(
        milestone: str,
        claim_type: str,
        missing: list[str],
        binding: dict[str, Any] | None = None,
    ) -> str:
        parts = [str(item).strip().lower() for item in missing if str(item).strip()]
        binding_text = json.dumps(_clean_binding(binding), ensure_ascii=False, sort_keys=True, default=str)
        return f"{milestone}:{claim_type}:{','.join(sorted(parts))}:{binding_text}"

    def __repr__(self) -> str:
        return json.dumps(self.summary(), sort_keys=True)


def _sequence_suffix(value: str) -> int:
    tail = str(value).replace("_", "-").rsplit("-", 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return 0


def _evidence_markers(records: Any) -> set[str]:
    markers: set[str] = set()
    for record in records:
        _collect_evidence_markers(getattr(record, "summary", {}), markers)
        _collect_evidence_markers(getattr(record, "binding", {}), markers)
    return markers


def _collect_evidence_markers(payload: Any, markers: set[str]) -> None:
    if isinstance(payload, dict):
        tool = payload.get("tool")
        if isinstance(tool, str):
            markers.add(tool)
        source_tool = payload.get("source_tool")
        if isinstance(source_tool, str):
            markers.add(source_tool)
        collection = payload.get("collection")
        if isinstance(collection, str):
            markers.add(f"collection:{collection}")
        operator = payload.get("operator")
        if isinstance(operator, str):
            markers.add(f"operator:{operator}")
        elif isinstance(operator, list):
            for item in operator:
                markers.add(f"operator:{item}")
        milestone = payload.get("milestone")
        if isinstance(milestone, str):
            markers.add(f"milestone:{milestone}")
        mql_hash = payload.get("mql_hash")
        if isinstance(mql_hash, str):
            markers.add(f"mql_hash:{mql_hash}")
        candidate_id = payload.get("candidate_id")
        if isinstance(candidate_id, str):
            markers.add(f"candidate_id:{candidate_id}")
        path = payload.get("path")
        if isinstance(path, str):
            markers.add(f"field_path:{path}")
            markers.add(f"path:{path}")
        paths = payload.get("paths")
        if isinstance(paths, dict):
            for key in paths:
                markers.add(f"field_path:{key}")
                markers.add(f"path:{key}")
        elif isinstance(paths, list):
            for item in paths:
                if isinstance(item, str):
                    markers.add(f"field_path:{item}")
                    markers.add(f"path:{item}")
                elif isinstance(item, dict):
                    nested = item.get("path")
                    if isinstance(nested, str):
                        markers.add(f"field_path:{nested}")
                        markers.add(f"path:{nested}")
        relationship_pair = payload.get("relationship_pair")
        if isinstance(relationship_pair, str):
            markers.add(f"relationship_pair:{relationship_pair}")
        elif isinstance(relationship_pair, (list, tuple)):
            markers.add("relationship_pair:" + "|".join(str(item) for item in relationship_pair))
        literal = payload.get("literal")
        if isinstance(literal, str):
            markers.add(f"literal:{literal}")
        token = payload.get("token")
        if isinstance(token, str):
            markers.add(f"token:{token}")
        for value in payload.values():
            _collect_evidence_markers(value, markers)
        return
    if isinstance(payload, list):
        for item in payload:
            _collect_evidence_markers(item, markers)


_BINDING_KEYS = {
    "collection",
    "paths",
    "operator",
    "relationship_pair",
    "mql_hash",
    "candidate_id",
    "milestone",
}


def _clean_binding(binding: Any | None) -> dict[str, Any]:
    if not isinstance(binding, dict):
        return {}
    clean: dict[str, Any] = {}
    if binding.get("path") and not binding.get("paths"):
        clean["paths"] = [str(binding["path"])]
    for key in _BINDING_KEYS:
        value = binding.get(key)
        if value in (None, "", [], {}, ()):
            continue
        if key in {"paths", "operator"}:
            paths = _normalize_list(value)
            if paths:
                clean[key] = sorted(paths)
        elif key == "relationship_pair":
            pair = _normalize_list(value)
            clean[key] = pair if pair else str(value)
        else:
            clean[key] = str(value)
    return clean


def _normalize_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return sorted(str(key) for key in value if key)
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted(str(item) for item in value if item)
    return []


def _record_matches_debt(record: EvidenceRecord, debt: EvidenceDebt) -> bool:
    debt_binding = _clean_binding(debt.binding)
    if not debt_binding:
        return True
    record_binding = _clean_binding(record.binding)
    if not record_binding:
        record_binding = _clean_binding(record.summary)
    matched_keys = 0
    for key, debt_value in debt_binding.items():
        record_value = record_binding.get(key)
        if record_value in (None, "", [], {}, ()):
            continue
        if key == "paths":
            if not set(_normalize_list(debt_value)).issubset(set(_normalize_list(record_value))):
                return False
            matched_keys += 1
            continue
        if key == "relationship_pair":
            if _normalize_list(debt_value) and _normalize_list(record_value):
                if set(_normalize_list(debt_value)) != set(_normalize_list(record_value)):
                    return False
            elif str(debt_value) != str(record_value):
                return False
            matched_keys += 1
            continue
        if str(debt_value) != str(record_value):
            return False
        matched_keys += 1
    return matched_keys > 0
