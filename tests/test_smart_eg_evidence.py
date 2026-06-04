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


def test_submit_plan_rejects_string_constant_without_literal_grounding() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(counterexample_gate=False))
    state = SmartEGState(
        nlq="compare loan-account share by district and fee frequency",
        db_id="financial",
        mode="planning",
    )
    state.intent = {"task_kind": "aggregation"}
    record = state.evidence_ledger.add_record(
        source_tool="profile_path_values",
        tool_call_id="call_values",
        observation_ref="agent/session.jsonl#values",
        summary={
            "tool": "profile_path_values",
            "collection": "district_market_contexts",
            "path": "accounts_by_frequency.*[].loan_presence_state",
            "values": [
                {"value": {"type": "str", "hash": "sha256:present", "length": 7, "literal": "present"}, "count": 4},
                {"value": {"type": "str", "hash": "sha256:absent", "length": 6, "literal": "absent"}, "count": 2},
            ],
            "redaction": {"raw_rows": False, "scalar_values": "bounded_enum_literals"},
        },
        redaction={"raw_rows": False, "scalar_values": "bounded_enum_literals"},
    )

    observation = api.execute(
        {
            "id": "call_submit",
            "type": "function",
            "function": {
                "name": "submit_query_plan",
                "arguments": json.dumps(
                    {
                        "collection": "district_market_contexts",
                        "stages": [{"$match": {"loan_presence_state": "loan"}}],
                        "evidence_refs": [record.evidence_id],
                    }
                ),
            },
        },
        state,
    )

    assert observation.result["accepted"] is False
    assert [item["code"] for item in observation.result["violations"]] == [
        "ungrounded_value_constant"
    ]
    assert state.evidence_ledger.blocking_debts(milestone="plan")[0].claim_type == "value_grounding"


def test_submit_plan_rejects_raw_object_context_in_final_projection() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(counterexample_gate=False, value_grounding=False))
    state = SmartEGState(
        nlq="compare loan-account share by district with gender pool and salary context",
        db_id="financial",
        mode="planning",
    )
    state.intent = {"task_kind": "aggregation"}
    refs = []
    for path, type_counts in [
        ("district", {"object": 5}),
        ("clients_by_gender", {"object": 5}),
        ("salary_band", {"str": 5}),
    ]:
        record = state.evidence_ledger.add_record(
            source_tool="profile_path",
            tool_call_id=f"call_{path}",
            observation_ref=f"agent/session.jsonl#{path}",
            summary={
                "tool": "profile_path",
                "collection": "district_market_contexts",
                "path": path,
                "type_counts": type_counts,
            },
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
                        "collection": "district_market_contexts",
                        "stages": [
                            {"$project": {"district": 1, "clients_by_gender": 1, "salary_band": 1}}
                        ],
                        "evidence_refs": refs,
                    }
                ),
            },
        },
        state,
    )

    assert observation.result["accepted"] is False
    assert [item["code"] for item in observation.result["violations"]] == ["raw_complex_output"]
    assert state.evidence_ledger.blocking_debts(milestone="plan")[0].claim_type == "output_contract"


def test_submit_plan_rejects_raw_object_context_after_addfields_alias() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(counterexample_gate=False, value_grounding=False))
    state = SmartEGState(
        nlq="compare loan-account share by district with gender pool",
        db_id="financial",
        mode="planning",
    )
    state.intent = {"task_kind": "aggregation"}
    refs = []
    for path, type_counts in [
        ("clients_by_gender", {"object": 5}),
        ("district.name", {"str": 5}),
    ]:
        record = state.evidence_ledger.add_record(
            source_tool="profile_path",
            tool_call_id=f"call_{path}",
            observation_ref=f"agent/session.jsonl#{path}",
            summary={
                "tool": "profile_path",
                "collection": "district_market_contexts",
                "path": path,
                "type_counts": type_counts,
            },
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
                        "collection": "district_market_contexts",
                        "stages": [
                            {
                                "$addFields": {
                                    "gender_pool": "$clients_by_gender",
                                    "district_name": "$district.name",
                                }
                            },
                            {"$project": {"_id": 0, "gender_pool": 1, "district_name": 1}},
                        ],
                        "evidence_refs": refs,
                    }
                ),
            },
        },
        state,
    )

    assert observation.result["accepted"] is False
    assert [item["code"] for item in observation.result["violations"]] == ["raw_complex_output"]
    assert observation.result["violations"][0]["context"]["raw_outputs"] == [
        {"output": "gender_pool", "source_path": "clients_by_gender"}
    ]


def test_submit_plan_rejects_nested_field_below_observed_scalar_path() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(counterexample_gate=False, value_grounding=False))
    state = SmartEGState(
        nlq="compare loan-account share by district and fee frequency",
        db_id="financial",
        mode="planning",
    )
    state.intent = {"task_kind": "aggregation"}
    refs = []
    for path, type_counts in [
        ("accounts_by_frequency", {"object": 5}),
        ("accounts_by_frequency.POPLATEK_MESICNE[]", {"object": 20}),
        ("accounts_by_frequency.POPLATEK_MESICNE[].loan_presence_state", {"str": 20}),
    ]:
        record = state.evidence_ledger.add_record(
            source_tool="profile_path",
            tool_call_id=f"call_{path}",
            observation_ref=f"agent/session.jsonl#{path}",
            summary={
                "tool": "profile_path",
                "collection": "district_market_contexts",
                "path": path,
                "type_counts": type_counts,
            },
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
                        "collection": "district_market_contexts",
                        "stages": [
                            {
                                "$addFields": {
                                    "frequency_array": {
                                        "$objectToArray": "$accounts_by_frequency"
                                    }
                                }
                            },
                            {"$unwind": "$frequency_array"},
                            {
                                "$project": {
                                    "_id": 0,
                                    "loan_account_share": {
                                        "$divide": [
                                            "$frequency_array.v.loan_presence_state.with_loan",
                                            "$frequency_array.v.loan_presence_state.total",
                                        ]
                                    },
                                }
                            },
                        ],
                        "evidence_refs": refs,
                    }
                ),
            },
        },
        state,
    )

    assert observation.result["accepted"] is False
    assert [item["code"] for item in observation.result["violations"]] == [
        "unknown_field_path"
    ]
    unknown_paths = observation.result["violations"][0]["context"]["paths"]
    assert "frequency_array.v.loan_presence_state.with_loan" in unknown_paths


def test_submit_plan_allows_group_id_projection_paths() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(counterexample_gate=False, value_grounding=False))
    state = SmartEGState(
        nlq="compare loan-account share by district and fee frequency",
        db_id="financial",
        mode="planning",
    )
    state.intent = {"task_kind": "aggregation"}
    refs = []
    for path, type_counts in [
        ("_id", {"str": 5}),
        ("district.name", {"str": 5}),
        ("fee_frequency", {"str": 5}),
    ]:
        record = state.evidence_ledger.add_record(
            source_tool="profile_path",
            tool_call_id=f"call_{path}",
            observation_ref=f"agent/session.jsonl#{path}",
            summary={
                "tool": "profile_path",
                "collection": "district_market_contexts",
                "path": path,
                "type_counts": type_counts,
            },
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
                        "collection": "district_market_contexts",
                        "stages": [
                            {
                                "$group": {
                                    "_id": {
                                        "district": "$district.name",
                                        "fee_frequency": "$fee_frequency",
                                    },
                                    "account_count": {"$sum": 1},
                                }
                            },
                            {
                                "$project": {
                                    "_id": 0,
                                    "district": "$_id.district",
                                    "fee_frequency": "$_id.fee_frequency",
                                    "account_count": 1,
                                }
                            },
                        ],
                        "evidence_refs": refs,
                    }
                ),
            },
        },
        state,
    )

    assert observation.result["accepted"] is True


def test_submit_plan_allows_scalar_alias_that_reuses_complex_field_name() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(counterexample_gate=False, value_grounding=False))
    state = SmartEGState(
        nlq="compare loan-account share by district",
        db_id="financial",
        mode="planning",
    )
    state.intent = {"task_kind": "aggregation"}
    refs = []
    for path, type_counts in [
        ("district", {"object": 5}),
        ("district.name", {"str": 5}),
    ]:
        record = state.evidence_ledger.add_record(
            source_tool="profile_path",
            tool_call_id=f"call_{path}",
            observation_ref=f"agent/session.jsonl#{path}",
            summary={
                "tool": "profile_path",
                "collection": "district_market_contexts",
                "path": path,
                "type_counts": type_counts,
            },
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
                        "collection": "district_market_contexts",
                        "stages": [
                            {"$project": {"district": "$district.name", "salary_band": 1}}
                        ],
                        "evidence_refs": refs,
                    }
                ),
            },
        },
        state,
    )

    assert observation.result["accepted"] is True
