from __future__ import annotations

import json

from tend.solver.eg import SmartEGPolicy, SmartEGState, SmartEGToolAPI
from tend.solver.eg.contracts import EvidenceClaim, EvidenceDebt
from tend.solver.eg.counterexamples import mine_counterexamples
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


def test_counterexample_miner_supports_submit_plan_gate_contract() -> None:
    hits = mine_counterexamples(
        plan={
            "collection": "account",
            "stages": [
                {"$unwind": "$relationships.members"},
                {"$lookup": {"from": "district", "localField": "district_id", "foreignField": "_id", "as": "d"}},
            ],
        },
        ledger=EvidenceLedger(),
    )

    assert [hit.code for hit in hits] == [
        "unwind_risk",
        "relationship_mismatch_risk",
    ]
    assert hits[0].suggested_tools == ["inspect_array_shape", "run_readonly_probe"]
    assert hits[0].to_json()["code"] == "unwind_risk"


def test_counterexample_miner_skips_risk_after_required_tool_evidence() -> None:
    ledger = EvidenceLedger()
    ledger.add_record(
        source_tool="inspect_array_shape",
        tool_call_id="call_1",
        observation_ref="agent/session.jsonl#1",
        summary={"collection": "account", "path": "relationships.members", "array": True},
        redaction={"raw_rows": False},
    )
    ledger.add_record(
        source_tool="run_readonly_probe",
        tool_call_id="call_2",
        observation_ref="agent/session.jsonl#2",
        summary={"ok": True, "result_count": 3},
        redaction={"raw_rows": False},
    )

    hits = mine_counterexamples(
        plan={
            "collection": "account",
            "stages": [{"$unwind": "$relationships.members"}],
        },
        ledger=ledger,
    )

    assert hits == []


def test_submit_plan_accepts_unwind_after_counterexample_evidence() -> None:
    api = SmartEGToolAPI(SmartEGPolicy())
    state = SmartEGState(nlq="list member relationships", db_id="financial", mode="planning")
    state.intent = {"task_kind": "relationship_aggregation"}

    refs = []
    for tool_name in ["discover_paths", "inspect_array_shape", "run_readonly_probe"]:
        record = state.evidence_ledger.add_record(
            source_tool=tool_name,
            tool_call_id=f"call_{tool_name}",
            observation_ref=f"agent/session.jsonl#{tool_name}",
            summary={"ok": True},
            redaction={"raw_rows": False},
        )
        refs.append(record.evidence_id)

    observation = api.execute(
        {
            "id": "call_submit",
            "type": "function",
            "function": {
                "name": "submit_query_plan",
                "arguments": json.dumps(
                    {
                        "collection": "account",
                        "stages": [{"$unwind": "$relationships.members"}],
                        "evidence_refs": refs,
                    }
                ),
            },
        },
        state,
    )

    assert observation.ok is True
    assert observation.result["accepted"] is True
    assert observation.result["violations"] == []
    assert state.query_plan is not None
    assert state.mode == "execution"
