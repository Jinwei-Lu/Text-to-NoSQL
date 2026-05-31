"""Offline stub responses — canned per-agent output so the whole pipeline runs without a
live LLM. Used when ``settings.stub`` is True (tests, CI, plumbing demos). Outputs are
schema-valid; execution-dependent agents (MS/RTV/PV) treat stub mode as a pass.
"""
from __future__ import annotations

from typing import Any

from .llm import Message

# A deterministic schema-flex summary matching the financial present/missing anchor. Stub mode
# does not execute it, but the record still passes static publish gates.
_STUB_MQL = (
    'db.account.aggregate([{ "$addFields": { "loan_variant": { "$cond": ['
    '{ "$eq": [{ "$type": "$loan" }, "missing"] }, "missing", "present"] }, '
    '"loan_amount_for_summary": { "$ifNull": ["$loan.amount", 0] } } }, '
    '{ "$group": { "_id": "$loan_variant", "account_count": { "$sum": 1 }, '
    '"total_loan_amount": { "$sum": "$loan_amount_for_summary" }, '
    '"avg_loan_amount": { "$avg": "$loan_amount_for_summary" } } }, '
    '{ "$project": { "_id": 0, "loan_variant": "$_id", "account_count": 1, '
    '"total_loan_amount": 1, "avg_loan_amount": 1 } }])'
)

_STUB: dict[str, dict[str, Any]] = {
    "wp": {
        "scenario_summary": "A financial institution tracks accounts, their loans and "
                            "transactions. Analysts ask about per-account credit activity, "
                            "whether an account carries a loan, and settlement behavior.",
        "access_patterns": [{"path": "account->trans", "frequency": 0.6}],
        "hot_fields": [{"field": "trans.amount"}],
        "design_constraints": ["account is the hub entity"],
    },
    "sra": {
        "mongodb_schema": {"collections": {"account": {"embeds": ["loan"]}}},
        "agent_design_rationale": {"decisions": [{"id": "D01", "type": "embed"}]},
    },
    "sc": {"verdict": "pass", "issues": [], "coverage_gaps": [],
           "suggested_fixes": [], "query_bearing": True},
    "qps": {
        "intent": {"seed_mechanism": "optional_embed",
                   "archetype": "schema_flex_variant_summary",
                   "target_difficulty": "L4",
                   "target_sql_infeasibility_class": "structural_schema_flex",
                   "schema_flex_mode": "polymorphic",
                   "analytical_op": {
                       "per": "account.loan present/missing variant",
                       "compute": "count accounts and summarize loan.amount by variant",
                   },
                   "shape_policy": "reduce",
                   "semantic_properties": ["present/missing both covered"]},
        "reference_oracle": {"impl": "group accounts by $type(loan) missing vs present"},
    },
    "ms": {"MQL": _STUB_MQL, "mql_alt": _STUB_MQL, "shape_policy": "reduce",
           "schema_flex": "optional_embed"},
    "mut": {"mutations": [
        {"mutation_id": f"m{i:03d}", "dimension": d, "MQL": _STUB_MQL}
        for i, d in enumerate(["A", "B", "C", "D", "E"], 1)
    ]},
    "nlp": {"nl_queries": {
        "canonical": "Group accounts by whether the embedded loan subdocument is present or "
                     "missing, then report the account count plus total and average loan amount "
                     "for each variant, treating missing loan amounts as 0.",
        "colloquial": "Split accounts into with-loan and no-loan groups, then summarize loan amounts.",
    }},
    "rtv": {"mql_round_trip_canonical": _STUB_MQL},
    "nnc": {"difficulty": "L4", "sql_infeasibility_class": "structural_schema_flex",
            "gate_pass": True, "nnc_verdict": {"reason": "present/missing not SQL-translatable"}},
    "ra": {"ra_pass": True, "ra_audit": {"cardinality_ok": True}},
    "smart_intent": {
        "entity": "account",
        "per": "account",
        "compute": [{
            "name": "loan_to_credit_ratio",
            "op": "conditional_ratio",
            "over": "loan.amount / max(sum(trans.amount where trans.type == PRIJEM), 1)",
            "window": None,
            "order": None,
            "missing": {"loan": 0, "credit_sum": 1},
        }],
        "aggregate": [{
            "name": "credit_sum",
            "op": "sum",
            "of": "trans.amount",
            "scope": "per account where trans.type == PRIJEM",
        }],
        "filter": [{"keep": "all account documents"}],
        "output": {"fields": ["*"], "target_fields": ["loan_to_credit_ratio"], "order": "none"},
        "shape_policy": "preserve",
        "target_fields": ["loan_to_credit_ratio"],
        "clause_coverage": [
            "for each account",
            "loan accounts divide loan amount by credit transaction sum",
            "no loan accounts get 0",
            "keep every account document",
        ],
    },
    "smart_plan": {
        "collection": "account",
        "stages": [
            {
                "op": "$lookup",
                "note": "Compute per-account credit transaction sum without dropping accounts.",
                "stage": {
                    "$lookup": {
                        "from": "trans",
                        "let": {"aid": "$_id"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {"$eq": ["$account_id", "$$aid"]},
                                            {"$eq": ["$type", "PRIJEM"]},
                                        ]
                                    }
                                }
                            },
                            {"$group": {"_id": None, "credit_sum": {"$sum": "$amount"}}},
                        ],
                        "as": "_credit",
                    }
                },
            },
            {
                "op": "$addFields",
                "note": "Preserve every account and branch on sparse loan embed.",
                "stage": {
                    "$addFields": {
                        "loan_to_credit_ratio": {
                            "$cond": [
                                {"$ne": [{"$type": "$loan"}, "missing"]},
                                {
                                    "$divide": [
                                        "$loan.amount",
                                        {
                                            "$max": [
                                                {
                                                    "$ifNull": [
                                                        {"$arrayElemAt": ["$_credit.credit_sum", 0]},
                                                        0,
                                                    ]
                                                },
                                                1,
                                            ]
                                        },
                                    ]
                                },
                                0,
                            ]
                        }
                    }
                },
            },
            {
                "op": "$project",
                "note": "Remove helper lookup field only.",
                "stage": {"$project": {"_credit": 0}},
            },
        ],
        "variant_handling": [
            {"strategy": "$cond on $type:'$loan'", "on": "loan present|missing"},
            {"strategy": "$lookup subpipeline match type == PRIJEM", "on": "trans polymorphic type"},
        ],
    },
}


def stub_fn(agent: str, messages: list[Message], schema: dict | None) -> dict[str, Any]:
    """Return canned output for ``agent`` (falls back to an empty object)."""
    return _STUB.get(agent, {"_stub": True, "agent": agent})
