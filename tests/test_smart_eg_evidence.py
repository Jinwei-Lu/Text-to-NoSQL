from __future__ import annotations

from tend.solver.eg.contracts import EvidenceClaim, EvidenceDebt
from tend.solver.eg.evidence import EvidenceLedger


def test_submit_debt_clears_when_required_claim_gets_evidence() -> None:
    ledger = EvidenceLedger()
    claim = EvidenceClaim(
        claim_id="claim_1",
        claim_type="field_grounding",
        statement="account.loan.amount exists",
        status="unsupported",
        required_evidence=["profile_path"],
        evidence_refs=[],
        used_by=["plan_1"],
    )
    ledger.add_claim(claim)
    debt = EvidenceDebt(
        debt_id="debt_1",
        milestone="plan",
        claim_type="field_grounding",
        blocking=True,
        missing_evidence=["profile_path"],
        suggested_tools=["profile_path"],
        normalized_signature="field:account.loan.amount",
        attempts=0,
    )
    ledger.add_debt(debt)

    assert ledger.blocking_debts() == [debt]

    record = ledger.add_record(
        source_tool="profile_path",
        tool_call_id="call_1",
        observation_ref="agent/session.jsonl#1",
        summary={"path": "loan.amount", "exists": 3},
        supports_claims=["claim_1"],
        contradicts_claims=[],
        redaction={"raw_rows": False},
    )

    assert record.evidence_id
    assert ledger.claims["claim_1"].status == "supported"
    assert ledger.blocking_debts() == []


def test_counterexample_challenges_claim_and_marks_debt_blocking() -> None:
    ledger = EvidenceLedger()
    ledger.add_claim(
        EvidenceClaim(
            claim_id="claim_1",
            claim_type="value_grounding",
            statement="constant PRIJEM maps to trans.type",
            status="supported",
            required_evidence=["search_values"],
            evidence_refs=[],
            used_by=["candidate_1"],
        )
    )

    ledger.add_record(
        source_tool="mine_counterexamples",
        tool_call_id="call_2",
        observation_ref="agent/session.jsonl#2",
        summary={"constant": "PRIJEM", "alternate_path": "operations.symbol"},
        supports_claims=[],
        contradicts_claims=["claim_1"],
        redaction={"raw_rows": False},
    )

    debts = ledger.blocking_debts()
    assert ledger.claims["claim_1"].status == "challenged"
    assert debts and debts[0].claim_type == "value_grounding"

