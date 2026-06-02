"""Offline stub responses — canned per-agent output so the whole pipeline runs without a
live LLM. Used when ``settings.stub`` is True (tests, CI, plumbing demos). Outputs are
schema-valid; execution-dependent agents (MS/RTV/PV) treat stub mode as a pass.
"""
from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from .llm import Message

# A deterministic sparse-embed preserve query matching the financial present/missing anchor.
# Stub mode does not execute it, but the record still passes static publish gates.
_STUB_MQL = (
    'db.account.aggregate([{ "$addFields": { "loan_amount": { "$cond": ['
    '{ "$eq": [{ "$type": "$loan" }, "object"] }, "$loan.amount", 0 ] } } }])'
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
        "intent": {"seed_mechanism": "sparse_embed",
                   "seed_signal": {"collection": "account", "field": "loan"},
                   "archetype": "present_missing_projection",
                   "target_difficulty": "L4",
                   "target_sql_infeasibility_class": "structural_schema_flex",
                   "schema_flex_mode": "polymorphic",
                   "analytical_op": {
                       "target_field": "loan_amount",
                       "formula": "IF loan is present THEN loan.amount ELSE 0",
                       "missing_default": 0,
                       "preserve_existing": True,
                   },
                   "shape_policy": "preserve",
                   "output": {"fields": ["loan_amount"], "missing": 0},
                   "semantic_properties": ["present/missing both covered"]},
        "reference_oracle": {
            "template": "optional_embed_projection",
            "params": {
                "parent_collection": "account",
                "embed_field": "loan",
                "value_path": "amount",
                "target_field": "loan_amount",
                "missing_default": 0,
            },
        },
    },
    "ms": {"MQL": _STUB_MQL, "mql_alt": _STUB_MQL, "shape_policy": "preserve",
           "schema_flex": "polymorphic"},
    "mut": {"mutations": [
        {"mutation_id": f"m{i:03d}", "dimension": d, "MQL": _STUB_MQL}
        for i, d in enumerate(["A", "B", "C", "D", "E"], 1)
    ]},
    "nlp": {"nl_queries": {
        "canonical": "For every account, add loan_amount equal to loan.amount when the account "
                     "has a loan subdocument and 0 when it does not; keep every existing "
                     "account field unchanged.",
        "colloquial": "Show each account with its loan amount, using zero for accounts without loans.",
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
    if agent == "qps":
        seed = _seed_from_messages(messages)
        if seed:
            return _seeded_qps_stub(seed, messages)
        design_card = _design_card_from_messages(messages)
        if design_card:
            return _design_card_qps_stub(design_card, messages)
    if agent == "ms":
        seeded = _seeded_ms_stub(messages)
        if seeded:
            return seeded
    if agent == "nnc":
        return _seeded_nnc_stub(messages)
    if agent == "nlp":
        return _seeded_nlp_stub(messages)
    if agent.startswith("baseline_"):
        if agent.endswith("_sql"):
            return {
                "SQL": (
                    "SELECT account.*, CASE WHEN loan.account_id IS NOT NULL THEN "
                    "loan.amount / GREATEST(SUM(CASE WHEN trans.type = 'PRIJEM' "
                    "THEN trans.amount ELSE 0 END), 1) ELSE 0 END AS loan_to_credit_ratio "
                    "FROM account LEFT JOIN loan ON loan.account_id = account._id "
                    "LEFT JOIN trans ON trans.account_id = account._id GROUP BY account._id"
                ),
                "notes": "Relational sketch used only by the SQL pivot baseline.",
            }
        if agent.endswith("_plan"):
            return {
                "target_collection": "account",
                "steps": [
                    "Start from account.",
                    "Lookup credit transactions from trans where type is PRIJEM.",
                    "Attach loan_to_credit_ratio with 0 for accounts without loan.",
                    "Remove helper lookup array.",
                ],
                "risks": ["Sparse loan embed may be missed by direct baselines."],
            }
        if agent.endswith("_think"):
            return {
                "thoughts": [
                    "The query is per account and must preserve accounts without loans.",
                    "A lookup over trans is needed for credit_sum.",
                ],
                "needed_observations": [
                    "account contains sparse loan embeds",
                    "trans.type uses PRIJEM for credit transactions",
                ],
            }
        return {
            "MQL": _STUB_MQL,
            "rationale": "Deterministic stub MQL for offline baseline plumbing.",
            "assumptions": ["financial mini-dev stub case"],
        }
    return _STUB.get(agent, {"_stub": True, "agent": agent})


def _seed_from_messages(messages: list[Message]) -> dict[str, Any] | None:
    seed = _json_block_from_messages(messages, "## deterministic diversity seed")
    return seed if isinstance(seed, dict) else None


def _design_card_from_messages(messages: list[Message]) -> dict[str, Any] | None:
    card = _json_block_from_messages(messages, "## LLM-first design card")
    return card if isinstance(card, dict) else None


def _seeded_ms_stub(messages: list[Message]) -> dict[str, Any] | None:
    prompt = "\n".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
    intent = _json_block_from_messages(messages, "## intent") or {}
    oracle = (
        _json_block_from_messages(messages, "## optional reference_oracle")
        or (intent.get("reference_oracle") if isinstance(intent.get("reference_oracle"), dict) else None)
    )
    if not isinstance(oracle, dict):
        return None
    try:
        from .agents.phase_b import _canonical_reference_mql

        built = _canonical_reference_mql({"intent": intent, "reference_oracle": oracle})
    except Exception:
        built = None
    if built is None:
        return None
    mql, shape = built
    schema_flex = _prompt_line(prompt, "target_schema_flex") or "none"
    if schema_flex == "none" and _prompt_line(prompt, "target_sql_infeasibility_class") == "structural_schema_flex":
        schema_flex = "polymorphic"
    return {
        "MQL": mql,
        "mql_alt": mql,
        "shape_policy": shape,
        "schema_flex": schema_flex,
        "stub_reference_oracle_mql": True,
    }


def _seeded_nlp_stub(messages: list[Message]) -> dict[str, Any]:
    intent = _json_block_from_messages(messages, "## intent")
    if not isinstance(intent, dict):
        return _STUB["nlp"]
    shape = str(intent.get("shape_policy") or "preserve")
    archetype = str(intent.get("archetype") or "query")
    output = intent.get("output") if isinstance(intent.get("output"), dict) else {}
    fields = [str(field) for field in output.get("fields", []) if field]
    field_text = ", ".join(fields) if fields else str(
        (intent.get("analytical_op") or {}).get("target_field") or "result"
    )
    seed_signal = intent.get("seed_signal") if isinstance(intent.get("seed_signal"), dict) else {}
    collection = str(seed_signal.get("collection") or "the collection")
    signal_field = str(seed_signal.get("field") or field_text)
    fingerprint = _mql_fingerprint_from_messages(messages)
    case_note = f" for case {fingerprint}" if fingerprint else ""

    if shape in {"reduce", "reshape"}:
        canonical = (
            f"Summarize {collection}{case_note} using the {archetype} pattern over {signal_field} "
            f"and output {field_text}."
        )
        if archetype == "schema_flex_variant_summary":
            canonical += " Use exact variant labels present and missing."
        colloquial = f"Give the grouped {field_text} summary for {collection}{case_note}."
    else:
        canonical = (
            f"For every {collection} record{case_note}, compute {field_text} from {signal_field} "
            "and keep the original record fields."
        )
        colloquial = f"Show every {collection} record{case_note} with {field_text} filled in."
    return {"nl_queries": {"canonical": canonical, "colloquial": colloquial}}


def _mql_fingerprint_from_messages(messages: list[Message]) -> str:
    text = "\n".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
    pos = text.find("db.")
    if pos < 0:
        return ""
    mql = " ".join(text[pos:].split())
    return sha256(mql.encode("utf-8")).hexdigest()[:10]


def _seeded_nnc_stub(messages: list[Message]) -> dict[str, Any]:
    prompt = "\n".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
    sql_class = _prompt_line(prompt, "target_sql_infeasibility_class") or "semantic"
    difficulty = _prompt_line(prompt, "target_difficulty") or (
        "L4" if sql_class == "structural_schema_flex" else "L2"
    )
    if sql_class == "structural_schema_flex":
        difficulty = "L4"
    return {
        "difficulty": difficulty,
        "sql_infeasibility_class": sql_class,
        "gate_pass": True,
        "nnc_verdict": {"reason": "stub echoes the requested construction target"},
    }


def _json_block_from_messages(messages: list[Message], marker: str) -> dict[str, Any] | None:
    text = "\n".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
    pos = text.find(marker)
    if pos < 0:
        return None
    fenced = text.find("```json", pos)
    if fenced < 0:
        return None
    start = text.find("\n", fenced)
    end = text.find("```", start + 1)
    if start < 0 or end < 0:
        return None
    try:
        payload = json.loads(text[start:end].strip())
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _seeded_qps_stub(seed: dict[str, Any], messages: list[Message]) -> dict[str, Any]:
    prompt = "\n".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
    archetype = _prompt_line(prompt, "archetype") or "group_count"
    mechanism = _prompt_line(prompt, "seed_mechanism") or "none"
    params = seed.get("params") if isinstance(seed.get("params"), dict) else {}
    target = (
        params.get("target_field")
        or params.get("field")
        or params.get("group_by")
        or params.get("discriminator")
        or "value"
    )
    preserve_templates = {"optional_embed_projection", "present_missing_projection",
                          "subtype_cond_projection"}
    shape = "preserve" if seed.get("template") in preserve_templates else "reduce"
    return {
        "intent": {
            "seed_mechanism": mechanism,
            "seed_signal": {
                "collection": params.get("collection") or params.get("parent_collection") or "",
                "field": target,
            },
            "archetype": archetype,
            "target_difficulty": _prompt_line(prompt, "target_difficulty") or "L4",
            "target_sql_infeasibility_class": (
                _prompt_line(prompt, "target_sql_infeasibility_class")
                or "structural_schema_flex"
            ),
            "schema_flex_mode": _prompt_line(prompt, "target_schema_flex") or "polymorphic",
            "analytical_op": {
                "target_field": str(target),
                "formula": f"deterministic seeded oracle {seed.get('template')}",
                "missing_default": params.get("missing_default", params.get("default", 0)),
            },
            "shape_policy": shape,
            "output": {"fields": [str(target)], "missing": params.get("missing_default", 0)},
            "semantic_properties": ["deterministic diversity seed honored"],
        },
        "reference_oracle": seed,
    }


def _design_card_qps_stub(card: dict[str, Any], messages: list[Message]) -> dict[str, Any]:
    prompt = "\n".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
    archetype = _prompt_line(prompt, "archetype") or "group_count"
    mechanism = _prompt_line(prompt, "seed_mechanism") or "none"
    field_hints = card.get("field_hints") if isinstance(card.get("field_hints"), list) else []
    collection_hints = (
        card.get("collection_hints") if isinstance(card.get("collection_hints"), list) else []
    )
    schema_feature = str(card.get("schema_feature") or "")
    collection = str(collection_hints[0]) if collection_hints else schema_feature.split(".", 1)[0]
    target = str(field_hints[0]) if field_hints else (
        schema_feature.split(".", 1)[1] if "." in schema_feature else "value"
    )
    preserve_archetypes = {
        "optional_embed_projection",
        "present_missing_projection",
        "subtype_cond_projection",
    }
    reshape_archetypes = {"simple_filter", "topn", "subtype_specific_field", "fk_rollup"}
    if archetype in preserve_archetypes:
        shape = "preserve"
    elif archetype in reshape_archetypes:
        shape = "reshape"
    else:
        shape = "reduce"
    return {
        "intent": {
            "seed_mechanism": mechanism,
            "seed_signal": {"collection": collection, "field": target},
            "archetype": archetype,
            "target_difficulty": _prompt_line(prompt, "target_difficulty") or "L4",
            "target_sql_infeasibility_class": (
                _prompt_line(prompt, "target_sql_infeasibility_class")
                or "structural_schema_flex"
            ),
            "schema_flex_mode": _prompt_line(prompt, "target_schema_flex") or "polymorphic",
            "analytical_op": {
                "target_field": target,
                "formula": f"stub-designed {archetype} over {schema_feature or target}",
                "missing_default": 0,
            },
            "shape_policy": shape,
            "output": {"fields": [target], "missing": 0},
            "semantic_properties": ["stub design card honored"],
        }
    }


def _prompt_line(text: str, key: str) -> str | None:
    prefix = f"{key}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip() or None
    return None
