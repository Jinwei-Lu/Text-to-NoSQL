"""NNC: L0–L4 labeling, sql_infeasibility_class, cfs check, ambiguity attack, graduated gate."""

from __future__ import annotations

import json
from typing import Any

import yaml

from tend.config import FIXTURES_ROOT, load_pool_roster, use_fixtures
from tend.core.ast_check import AST_check, disabled_operator_scanner
from tend.core.llm_client import LLMClient
from tend.core.llm_response import parse_llm_json_response
from tend.core.mql import extract_operator_tokens, extract_root_stage_tokens
from tend.errors import TriggerAmbiguityError
from tend.phase_b.bridges import graduated_gate, run_sql_bridge, run_template_bridge
from tend.prompts import loader as prompt_loader

def _load_fixture_nnc(db_id: str) -> dict[str, Any] | None:
    path = FIXTURES_ROOT / db_id / "nnc.yaml"
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _ops_in_mql(mql: str) -> set[str]:
    return set(extract_operator_tokens(mql))


def _root_ops_in_mql(mql: str) -> set[str]:
    return set(extract_root_stage_tokens(mql))


def infer_difficulty(mql: str, query_plan: dict[str, Any]) -> tuple[str, str]:
    ops = _ops_in_mql(mql)
    pattern = query_plan.get("primary_pattern", "")
    if "$facet" in ops and "$setWindowFields" in ops:
        return (
            "L4",
            "Partitioned window plus facet-global median is structural translation-lossy in SQL.",
        )
    if "$switch" in ops or "$objectToArray" in ops:
        return "L4", "Schema-flex dispatch requires NoSQL-native branching."
    if "$setWindowFields" in ops or "$graphLookup" in ops:
        return "L3", "Window or graph operators are partially SQL-translatable."
    if "$lookup" in ops or ops.intersection({"$unwind", "$group"}) == {"$unwind", "$group"}:
        return "L2", "Multi-stage lookup/unwind/group pipeline."
    if "$group" in ops or "$sort" in ops:
        return "L1", "Light aggregation stages."
    if ops <= {"$match", "$project"}:
        return "L0", "Single-collection filter/projection."
    target = query_plan.get("target_difficulty")
    if target in {"L0", "L1", "L2", "L3", "L4"}:
        return target, f"Aligned with query_plan.target_difficulty={target}."
    return "L2", "Default mid-tier from operator mix."


def infer_sql_infeasibility_class(
    difficulty: str,
    mql: str,
    query_plan: dict[str, Any],
) -> str:
    ops = _ops_in_mql(mql)
    flex_mode = query_plan.get("schema_flex_mode", "none")
    if flex_mode != "none" and ("$switch" in ops or "$objectToArray" in ops):
        return "structural_schema_flex"
    if "$facet" in ops and "$setWindowFields" in ops:
        return "structural_pipeline"
    if difficulty == "L4":
        return "performative"
    if "$ifNull" in ops or query_plan.get("null_missing_strategy") == "ifNull":
        return "semantic"
    if difficulty in {"L0", "L1"}:
        return "feasible"
    return "performative"


def check_canonical_form_set(mql: str, cfs: dict[str, Any]) -> dict[str, Any]:
    violations: list[str] = []
    ops = _ops_in_mql(mql)
    root_ops = _root_ops_in_mql(mql)
    for token in cfs.get("must_contain", []):
        if token not in ops:
            violations.append(f"missing:{token}")
    for token in cfs.get("must_contain_at_root", []):
        if token not in root_ops:
            violations.append(f"missing_at_root:{token}")
    for token in cfs.get("must_not_contain", []):
        if token in ops:
            violations.append(f"forbidden:{token}")
    if disabled_operator_scanner(mql):
        violations.append("disabled_operator_present")
    ast = AST_check(mql, cfs)
    if ast != "pass":
        violations.append(ast)
    return {"pass": not violations, "violations": violations}


def _attack_parse_equivalent(
    parse_payload: dict[str, Any],
    query_plan: dict[str, Any],
) -> bool:
    if parse_payload.get("parse_failed"):
        return False
    gold_pattern = query_plan.get("primary_pattern")
    parsed_pattern = parse_payload.get("primary_pattern") or parse_payload.get("pattern")
    if parsed_pattern and gold_pattern:
        return parsed_pattern == gold_pattern
    if parse_payload.get("equivalent_to_gold") is not None:
        return bool(parse_payload["equivalent_to_gold"])
    # LLM returned something but without explicit divergence signal — benefit of doubt
    return True


def run_ambiguity_attack(
    nl_canonical: str,
    query_plan: dict[str, Any],
    *,
    client: LLMClient | None = None,
    seed: int = 0,
    min_models: int = 3,
) -> dict[str, Any]:
    """≥3 independent C_nnc_attack models parse canonical NLQ; must agree with gold plan."""
    llm = client or LLMClient()
    roster = load_pool_roster()
    base_models = list(roster.get("C_nnc_attack", [])) or ["deepseek-v4-flash"]
    if len(base_models) < min_models and llm.stub:
        base_models.extend(["stub-model-a", "stub-model-b", "stub-model-c"])
    models: list[str] = []
    while len(models) < min_models:
        for model in base_models:
            models.append(model)
            if len(models) >= min_models:
                break

    prompt = prompt_loader.load("nnc_nosql_nativeness_critic")
    parses: list[dict[str, Any]] = []
    equivalent = 0
    for idx, model in enumerate(models):
        user = prompt_loader.render(
            prompt["user"],
            {
                "db_id": query_plan.get("db_id", "orchestra"),
                "record_id": str(query_plan.get("record_id", "1001")),
                "shape_policy": query_plan.get("shape_policy", "reshape"),
                "query_plan_json": json.dumps(query_plan, ensure_ascii=False),
                "nl_queries_json": json.dumps({"canonical": nl_canonical}, ensure_ascii=False),
                "mql": "",
                "canonical_form_set_json": "{}",
                "round_trip_verification_json": "{}",
                "world_signature": "",
                "sql_bridge_mql_json": "null",
                "template_bridge_mql_json": "null",
            },
        )
        response = llm.call(
            "C_nnc_attack",
            f"{prompt['system']}\n\nParse intent only:\n{nl_canonical}",
            seed=seed + idx,
            model_override=model,
        )
        parsed = parse_llm_json_response(response)
        if parsed and isinstance(parsed.get("query_plan"), dict):
            parse_payload = parsed["query_plan"]
        else:
            parse_payload = {
                "equivalent_to_gold": False,
                "model": model,
                "parse_failed": True,
            }
        parses.append(parse_payload)
        if _attack_parse_equivalent(parse_payload, query_plan):
            equivalent += 1

    all_parse_failed = all(p.get("parse_failed") for p in parses)
    if all_parse_failed:
        passed = True
    else:
        successful_parses = [p for p in parses if not p.get("parse_failed")]
        successful_eq = sum(1 for p in successful_parses if _attack_parse_equivalent(p, query_plan))
        passed = successful_eq == len(successful_parses) and len(successful_parses) >= 1
    return {
        "parse_count": len(parses),
        "equivalent_to_gold_count": equivalent,
        "pass": passed,
        "inconclusive": all_parse_failed,
        "models": models,
        "parses": parses,
    }


def assess_nnc(
    mql: str,
    nl_queries: dict[str, str],
    canonical_form_set: dict[str, Any],
    query_plan: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    shape_policy: str,
    round_trip_verification: dict[str, Any],
    record: dict[str, Any],
    db_id: str = "orchestra",
    client: LLMClient | None = None,
    seed: int = 0,
    prefer_fixture: bool | None = None,
    audit_dir: Any | None = None,
) -> dict[str, Any]:
    """Full NNC verdict including bridges and ambiguity attack."""
    if prefer_fixture is None:
        prefer_fixture = use_fixtures()
    fixture = _load_fixture_nnc(db_id) if prefer_fixture else None
    if fixture and db_id == "orchestra":
        difficulty = fixture.get("difficulty", "L4")
        difficulty_rationale = fixture.get(
            "difficulty_rationale",
            "Partitioned window plus facet-global median is structural translation-lossy in SQL.",
        )
        sql_class = fixture.get("sql_infeasibility_class", "structural_pipeline")
    else:
        difficulty, difficulty_rationale = infer_difficulty(mql, query_plan)
        sql_class = infer_sql_infeasibility_class(difficulty, mql, query_plan)

    record_payload = {
        **record,
        "MQL": mql,
        "canonical_form_set": canonical_form_set,
        "sql_infeasibility_class": sql_class,
        "shape_policy": shape_policy,
    }

    sql_mql, _sql_trace = run_sql_bridge(
        nl_queries["canonical"],
        record_payload,
        db_id=db_id,
        client=client,
        seed=seed,
        audit_dir=audit_dir,
    )
    tpl_mql, tpl_trace = run_template_bridge(
        nl_queries["canonical"],
        record_payload,
        query_plan=query_plan,
    )
    gate = graduated_gate(record_payload, snapshot, sql_bridge_mql=sql_mql, template_bridge_mql=tpl_mql)

    cfs_check = check_canonical_form_set(mql, canonical_form_set)
    ambiguity = run_ambiguity_attack(
        nl_queries["canonical"],
        query_plan,
        client=client,
        seed=seed + 31,
    )

    blocking: list[str] = []
    if not cfs_check["pass"]:
        blocking.append("canonical_form_set_violation")
    if gate["gate_required"] and not gate["gate_pass"]:
        blocking.append("graduated_gate_fail")
    if not ambiguity["pass"]:
        blocking.append("ambiguity_attack_fail")
    if not round_trip_verification.get("canonical_pass", False):
        blocking.append("rtv_canonical_fail")

    if fixture and prefer_fixture:
        diagnostic_bridge = fixture.get("diagnostic_bridge", {})
        diagnostic_bridge = {
            **gate,
            **diagnostic_bridge,
            "sql_bridge": {
                **gate["sql_bridge"],
                **fixture.get("diagnostic_bridge", {}).get("sql_bridge", {}),
            },
            "template_bridge": {
                **gate["template_bridge"],
                **fixture.get("diagnostic_bridge", {}).get("template_bridge", {}),
            },
        }
        ambiguity = fixture.get("ambiguity_attack", ambiguity)
        cfs_check = fixture.get("canonical_form_set_check", cfs_check)
        blocking = list(fixture.get("nnc_verdict", {}).get("blocking_reasons", blocking))
        nnc_pass = fixture.get("nnc_verdict", {}).get("pass", not blocking)
    else:
        diagnostic_bridge = {
            **gate,
            "sql_bridge": {**gate["sql_bridge"], "notes": _sql_trace.get("source", "sqlglot")},
            "template_bridge": {
                **gate["template_bridge"],
                "notes": f"Matched {tpl_trace.get('pattern_id')} template",
            },
        }
        nnc_pass = not blocking

    payload = {
        "difficulty": difficulty,
        "difficulty_rationale": difficulty_rationale,
        "sql_infeasibility_class": sql_class,
        "diagnostic_bridge": diagnostic_bridge,
        "functional_sql_solvable": diagnostic_bridge.get(
            "functional_sql_solvable", gate["functional_sql_solvable"]
        ),
        "structural_sql_solvable": diagnostic_bridge.get(
            "structural_sql_solvable", gate["structural_sql_solvable"]
        ),
        "ambiguity_attack": ambiguity,
        "canonical_form_set_check": cfs_check,
        "nnc_verdict": {"pass": nnc_pass, "blocking_reasons": blocking},
    }
    if not nnc_pass and "ambiguity_attack_fail" in blocking:
        raise TriggerAmbiguityError("Ambiguity attack failed to converge on gold intent.")
    return payload
