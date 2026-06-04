from __future__ import annotations

import json

from tend.solver.eg import SmartEGPolicy, SmartEGState, SmartEGToolAPI
from tend.solver.eg.contracts import EvidenceClaim, EvidenceDebt
from tend.solver.eg.counterexamples import mine_counterexamples
from tend.solver.eg.evidence import EvidenceLedger
from tend.solver.eg.safety import value_token


class _Executor:
    def norm_exec(self, _db_id: str, _mql: str):
        return [{"_id": 1}]


def _call(name: str, arguments: dict, call_id: str = "call_submit") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _staged_execution_state() -> SmartEGState:
    state = SmartEGState(nlq="list accounts", db_id="financial", mode="execution")
    state.environment = {"candidate_collections": ["account"]}
    state.intent = {"task_kind": "list", "target_collection": "account"}
    state.query_plan = {"collection": "account", "stages": [{"$limit": 2}]}
    return state


def _add_evidence(state: SmartEGState, source_tool: str, summary: dict | None = None) -> str:
    record = state.evidence_ledger.add_record(
        source_tool=source_tool,
        tool_call_id=f"call_{source_tool}",
        observation_ref=f"agent/session.jsonl#{source_tool}",
        summary=summary or {"tool": source_tool, "ok": True},
        redaction={"raw_rows": False},
    )
    return record.evidence_id


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


def test_submit_final_rejects_direct_first_turn_even_if_exposed() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(), executor=_Executor())
    state = SmartEGState(nlq="list accounts", db_id="financial")

    observation = api.execute(
        _call(
            "submit_final_mql",
            {
                "collection": "account",
                "pipeline": [{"$limit": 2}],
                "MQL": 'db.account.aggregate([{"$limit":2}])',
            },
        ),
        state,
        exposed_tool_names={"submit_final_mql"},
    )

    assert observation.ok is False
    assert observation.llm_visible_content["ok"] is False
    assert observation.result["accepted"] is False
    codes = [item["code"] for item in observation.result["violations"]]
    assert "wrong_mode" in codes
    assert "missing_milestone" in codes
    assert state.terminal is False


def test_staged_final_requires_relevant_refs_and_accepts_with_real_executor() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(), executor=_Executor())
    irrelevant_state = _staged_execution_state()
    bad_ref = _add_evidence(
        irrelevant_state,
        "list_collections",
        {"tool": "list_collections", "collections": ["account"]},
    )

    bad = api.execute(
        _call(
            "submit_final_mql",
            {
                "collection": "account",
                "pipeline": [{"$limit": 2}],
                "MQL": 'db.account.aggregate([{"$limit":2}])',
                "evidence_refs": [bad_ref],
            },
        ),
        irrelevant_state,
        exposed_tool_names={"submit_final_mql"},
    )

    assert bad.ok is False
    assert bad.llm_visible_content["ok"] is False
    assert bad.result["accepted"] is False
    assert [item["code"] for item in bad.result["violations"]] == [
        "irrelevant_evidence_refs",
        "final_execution_evidence_missing",
    ]

    accepted_state = _staged_execution_state()
    sanity = api.execute(
        _call(
            "run_final_sanity_execution",
            {
                "collection": "account",
                "pipeline": [{"$limit": 2}],
                "MQL": 'db.account.aggregate([{"$limit":2}])',
            },
            call_id="call_sanity",
        ),
        accepted_state,
        exposed_tool_names={"run_final_sanity_execution"},
    )
    good_ref = sanity.result["evidence_id"]

    good = api.execute(
        _call(
            "submit_final_mql",
            {
                "collection": "account",
                "pipeline": [{"$limit": 2}],
                "MQL": 'db.account.aggregate([{"$limit":2}])',
                "evidence_refs": [good_ref],
            },
        ),
        accepted_state,
        exposed_tool_names={"submit_final_mql"},
    )

    assert good.ok is True
    assert good.llm_visible_content["ok"] is True
    assert good.result["accepted"] is True
    assert accepted_state.terminal is True


def test_final_submit_rejects_final_sanity_evidence_for_different_mql() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(), executor=_Executor())
    state = _staged_execution_state()
    sanity = api.execute(
        _call(
            "run_final_sanity_execution",
            {
                "collection": "account",
                "pipeline": [{"$limit": 2}],
                "MQL": 'db.account.aggregate([{"$limit":2}])',
            },
            call_id="call_sanity",
        ),
        state,
        exposed_tool_names={"run_final_sanity_execution"},
    )

    observation = api.execute(
        _call(
            "submit_final_mql",
            {
                "collection": "account",
                "pipeline": [{"$limit": 3}],
                "MQL": 'db.account.aggregate([{"$limit":3}])',
                "evidence_refs": [sanity.result["evidence_id"]],
            },
        ),
        state,
        exposed_tool_names={"submit_final_mql"},
    )

    assert observation.ok is False
    assert "final_execution_mql_mismatch" in [
        item["code"] for item in observation.result["violations"]
    ]


def test_submit_final_rejects_missing_executor_even_with_relevant_refs() -> None:
    setup_api = SmartEGToolAPI(SmartEGPolicy(), executor=_Executor())
    api = SmartEGToolAPI(SmartEGPolicy(), executor=None)
    state = _staged_execution_state()
    _add_evidence(
        state,
        "run_readonly_probe",
        {"tool": "run_readonly_probe", "ok": True, "count": 1},
    )
    sanity = setup_api.execute(
        _call(
            "run_final_sanity_execution",
            {
                "collection": "account",
                "pipeline": [{"$limit": 2}],
                "MQL": 'db.account.aggregate([{"$limit":2}])',
            },
            call_id="call_sanity",
        ),
        state,
        exposed_tool_names={"run_final_sanity_execution"},
    )

    observation = api.execute(
        _call(
            "submit_final_mql",
            {
                "collection": "account",
                "pipeline": [{"$limit": 2}],
                "MQL": 'db.account.aggregate([{"$limit":2}])',
                "evidence_refs": [sanity.result["evidence_id"]],
            },
        ),
        state,
        exposed_tool_names={"submit_final_mql"},
    )

    assert observation.ok is False
    assert observation.result["accepted"] is False
    assert [item["code"] for item in observation.result["violations"]] == [
        "execution_unresolved"
    ]
    assert state.execution_trace.final_sanity_runs[-1]["ok"] is False
    assert state.execution_trace.final_sanity_runs[-1]["reason"] == "no_executor"


def test_submit_final_can_repair_previous_value_grounding_debt() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(counterexample_gate=False), executor=_Executor())
    state = _staged_execution_state()
    sanity = api.execute(
        _call(
            "run_final_sanity_execution",
            {
                "collection": "account",
                "pipeline": [{"$match": {"status": "ACTIVE"}}, {"$limit": 1}],
                "MQL": 'db.account.aggregate([{"$match":{"status":"ACTIVE"}},{"$limit":1}])',
            },
            call_id="call_sanity",
        ),
        state,
        exposed_tool_names={"run_final_sanity_execution"},
    )
    probe_ref = sanity.result["evidence_id"]

    first = api.execute(
        _call(
            "submit_final_mql",
            {
                "collection": "account",
                "pipeline": [{"$match": {"status": "ACTIVE"}}, {"$limit": 1}],
                "MQL": 'db.account.aggregate([{"$match":{"status":"ACTIVE"}},{"$limit":1}])',
                "evidence_refs": [probe_ref],
            },
        ),
        state,
        exposed_tool_names={"submit_final_mql"},
    )

    assert first.ok is False
    assert "ungrounded_value_constant" in [
        item["code"] for item in first.result["violations"]
    ]
    assert state.evidence_ledger.blocking_debts(milestone="final")

    value_ref = _add_evidence(
        state,
        "profile_path_values",
        {
            "tool": "profile_path_values",
            "collection": "account",
            "path": "status",
            "values": [
                {
                    "value": {
                        "type": "str",
                        "hash": "sha256:active",
                        "token": "value:str:active",
                        "literal": "ACTIVE",
                    },
                    "count": 2,
                }
            ],
        },
    )

    repaired = api.execute(
        _call(
            "submit_final_mql",
            {
                "collection": "account",
                "pipeline": [{"$match": {"status": "ACTIVE"}}, {"$limit": 1}],
                "MQL": 'db.account.aggregate([{"$match":{"status":"ACTIVE"}},{"$limit":1}])',
                "evidence_refs": [probe_ref, value_ref],
            },
        ),
        state,
        exposed_tool_names={"submit_final_mql"},
    )

    assert repaired.ok is True
    assert repaired.result["accepted"] is True
    assert state.evidence_ledger.blocking_debts(milestone="final") == []


def test_no_evidence_gate_relaxes_only_evidence_ref_checks_for_final() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(evidence_gate=False), executor=_Executor())
    staged_state = _staged_execution_state()

    accepted = api.execute(
        _call(
            "submit_final_mql",
            {
                "collection": "account",
                "pipeline": [{"$limit": 2}],
                "MQL": 'db.account.aggregate([{"$limit":2}])',
            },
        ),
        staged_state,
        exposed_tool_names={"submit_final_mql"},
    )

    assert accepted.ok is True
    assert accepted.result["accepted"] is True

    direct_state = SmartEGState(nlq="list accounts", db_id="financial")
    rejected = api.execute(
        _call(
            "submit_final_mql",
            {
                "collection": "account",
                "pipeline": [{"$limit": 2}],
                "MQL": 'db.account.aggregate([{"$limit":2}])',
            },
        ),
        direct_state,
        exposed_tool_names={"submit_final_mql"},
    )

    assert rejected.ok is False
    assert rejected.result["accepted"] is False
    assert "wrong_mode" in [item["code"] for item in rejected.result["violations"]]


def test_submit_plan_rejects_irrelevant_existing_evidence_refs() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(counterexample_gate=False, value_grounding=False))
    state = SmartEGState(nlq="list accounts", db_id="financial", mode="planning")
    state.intent = {"task_kind": "list"}
    ref = _add_evidence(
        state,
        "list_collections",
        {"tool": "list_collections", "collections": ["account"]},
    )

    observation = api.execute(
        _call(
            "submit_query_plan",
            {
                "collection": "account",
                "stages": [{"$limit": 2}],
                "evidence_refs": [ref],
            },
        ),
        state,
        exposed_tool_names={"submit_query_plan"},
    )

    assert observation.ok is False
    assert observation.result["accepted"] is False
    assert [item["code"] for item in observation.result["violations"]] == [
        "irrelevant_evidence_refs"
    ]


def test_invalid_link_evidence_ref_is_not_success() -> None:
    api = SmartEGToolAPI(SmartEGPolicy())
    state = SmartEGState(nlq="list accounts", db_id="financial")
    claim = state.evidence_ledger.add_claim(
        claim_type="field_grounding",
        statement="account.status exists",
        required_evidence=["profile_path"],
    )

    observation = api.execute(
        _call(
            "link_evidence",
            {"claim_id": claim.claim_id, "evidence_id": "ev-missing"},
            call_id="call_link",
        ),
        state,
        exposed_tool_names={"link_evidence"},
    )

    assert observation.ok is False
    assert observation.result["reason"] == "invalid_evidence_ref"


def test_intent_submit_requires_target_collection_and_contract() -> None:
    api = SmartEGToolAPI(SmartEGPolicy())
    state = SmartEGState(nlq="list accounts", db_id="financial", mode="intent")
    state.environment = {"candidate_collections": ["account"]}
    ref = _add_evidence(
        state,
        "profile_path_values",
        {"tool": "profile_path_values", "collection": "account", "path": "_id"},
    )

    weak = api.execute(
        _call(
            "submit_intent_hypothesis",
            {"task_kind": "list", "evidence_refs": [ref]},
            call_id="call_weak",
        ),
        state,
        exposed_tool_names={"submit_intent_hypothesis"},
    )

    assert weak.ok is False
    assert weak.result["accepted"] is False
    assert [item["code"] for item in weak.result["violations"]].count("contract_invalid") >= 2

    wrong_collection = api.execute(
        _call(
            "submit_intent_hypothesis",
            {
                "task_kind": "list",
                "target_collection": "loan",
                "target_fields": ["_id"],
                "evidence_refs": [ref],
            },
            call_id="call_wrong",
        ),
        state,
        exposed_tool_names={"submit_intent_hypothesis"},
    )

    assert wrong_collection.ok is False
    assert "target_collection_unaccepted" in [
        item["code"] for item in wrong_collection.result["violations"]
    ]


def test_request_mode_shift_backward_obeys_revisit_policy_and_budget() -> None:
    no_revisit_api = SmartEGToolAPI(SmartEGPolicy(revisit=False))
    state = SmartEGState(nlq="list accounts", db_id="financial", mode="execution")

    rejected = no_revisit_api.execute(
        _call("request_mode_shift", {"target_mode": "planning"}, call_id="call_shift"),
        state,
        exposed_tool_names={"request_mode_shift"},
    )

    assert rejected.ok is False
    assert rejected.result["reason"] == "no_revisit"
    assert state.mode == "execution"

    budgeted_state = SmartEGState(nlq="list accounts", db_id="financial", mode="execution")
    budgeted_state.budgets.max_revisits = 1
    budgeted_state.counters.revisits = 1
    budgeted = SmartEGToolAPI(SmartEGPolicy())

    exhausted = budgeted.execute(
        _call("request_mode_shift", {"target_mode": "planning"}, call_id="call_budget"),
        budgeted_state,
        exposed_tool_names={"request_mode_shift"},
    )

    assert exhausted.ok is False
    assert exhausted.result["reason"] == "revisit_budget_exhausted"
    assert budgeted_state.terminal_only is True


def test_value_grounding_ignores_lookup_structural_strings_and_requires_literals() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(counterexample_gate=False))
    lookup_state = SmartEGState(nlq="join accounts to districts", db_id="financial", mode="planning")
    lookup_state.intent = {"task_kind": "lookup"}
    lookup_ref = _add_evidence(
        lookup_state,
        "discover_paths",
        {
            "tool": "discover_paths",
            "collection": "account",
            "paths": {
                "district_id": {"type_counts": {"int": 2}},
                "_id": {"type_counts": {"int": 2}},
            },
        },
    )

    lookup_observation = api.execute(
        _call(
            "submit_query_plan",
            {
                "collection": "account",
                "stages": [
                    {
                        "$lookup": {
                            "from": "district",
                            "localField": "district_id",
                            "foreignField": "_id",
                            "as": "district_docs",
                        }
                    }
                ],
                "evidence_refs": [lookup_ref],
            },
        ),
        lookup_state,
        exposed_tool_names={"submit_query_plan"},
    )

    assert lookup_observation.ok is True
    assert lookup_observation.result["accepted"] is True


def test_value_grounding_requires_token_proof_for_numeric_and_objectid_constants() -> None:
    api = SmartEGToolAPI(SmartEGPolicy(counterexample_gate=False))
    state = SmartEGState(nlq="find exact account ids and balances", db_id="financial", mode="planning")
    state.intent = {"task_kind": "filter"}
    balance_token = value_token(1250)
    objectid = "507f1f77bcf86cd799439011"
    objectid_token = value_token(objectid)
    ref = _add_evidence(
        state,
        "profile_path_values",
        {
            "tool": "profile_path_values",
            "collection": "account",
            "path": "balance",
            "values": [
                {
                    "value": {
                        "type": "int",
                        "hash": "sha256:1250",
                        "token": balance_token,
                        "proof": {
                            "token": balance_token,
                            "numeric_class": "positive",
                        },
                    },
                    "count": 1,
                }
            ],
        },
    )

    missing_objectid = api.execute(
        _call(
            "submit_query_plan",
            {
                "collection": "account",
                "stages": [
                    {
                        "$match": {
                            "_id": objectid,
                            "balance": {"$eq": 1250},
                        }
                    }
                ],
                "evidence_refs": [ref],
            },
        ),
        state,
        exposed_tool_names={"submit_query_plan"},
    )

    assert missing_objectid.ok is False
    assert missing_objectid.result["violations"][0]["code"] == "ungrounded_value_constant"
    assert any(
        item.startswith("token:")
        for debt in state.evidence_ledger.blocking_debts(milestone="plan")
        for item in debt.missing_evidence
    )

    objectid_ref = _add_evidence(
        state,
        "search_values",
        {
            "tool": "search_values",
            "collection": "account",
            "matches": [
                {
                    "path": "_id",
                    "value": {
                        "type": "str",
                        "hash": "sha256:oid",
                        "token": objectid_token,
                        "proof": {
                            "token": objectid_token,
                            "string_format": "object_id_like",
                        },
                    },
                }
            ],
        },
    )

    grounded = api.execute(
        _call(
            "submit_query_plan",
            {
                "collection": "account",
                "stages": [
                    {
                        "$match": {
                            "_id": objectid,
                            "balance": {"$eq": 1250},
                        }
                    }
                ],
                "evidence_refs": [ref, objectid_ref],
            },
        ),
        state,
        exposed_tool_names={"submit_query_plan"},
    )

    assert grounded.ok is True
    assert grounded.result["accepted"] is True

    match_state = SmartEGState(nlq="active accounts", db_id="financial", mode="planning")
    match_state.intent = {"task_kind": "filter"}
    match_ref = _add_evidence(
        match_state,
        "discover_paths",
        {
            "tool": "discover_paths",
            "collection": "account",
            "paths": {"status": {"type_counts": {"str": 2}}},
        },
    )

    match_observation = api.execute(
        _call(
            "submit_query_plan",
            {
                "collection": "account",
                "stages": [{"$match": {"status": "ACTIVE"}}],
                "evidence_refs": [match_ref],
            },
        ),
        match_state,
        exposed_tool_names={"submit_query_plan"},
    )

    assert match_observation.ok is False
    assert [item["code"] for item in match_observation.result["violations"]] == [
        "ungrounded_value_constant"
    ]
