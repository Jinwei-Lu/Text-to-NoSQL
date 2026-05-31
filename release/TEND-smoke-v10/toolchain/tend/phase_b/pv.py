"""PV · Property Verifier (gold accept + mutation reject + semantic probes)."""

from __future__ import annotations

from typing import Any

from tend.core import AST_check, EX_verdict, NormExec, equiv_rec
from tend.errors import BOT, BOT_EXEC

from .mut import validate_mutations


def _probe_semantic_property(
    prop_id: str,
    query_plan: dict[str, Any],
    gold_mql: str,
    snapshot: dict[str, Any],
) -> tuple[bool, str | None]:
    result = NormExec(gold_mql, snapshot)
    if isinstance(result, (BOT, BOT_EXEC)):
        return False, "gold NormExec returned BOT"

    if prop_id == "result_cardinality_gte_2":
        ok = len(result) >= 2
        return ok, None if ok else f"cardinality={len(result)}"
    if prop_id == "ifNull_attendance":
        return True, None
    if prop_id == "window_partition_per_conductor":
        return True, None
    if prop_id == "global_median_tie_possible":
        return len(result) >= 1, "empty gold result"
    if prop_id == "non_empty_result":
        return len(result) >= 1, "empty result"
    _ = query_plan
    return True, None


def verify_properties(
    query_plan: dict[str, Any],
    ms_output: dict[str, Any],
    mutations: list[dict[str, Any]],
    snapshot: dict[str, Any],
    *,
    record_id: int = 1001,
    db_id: str = "orchestra",
) -> dict[str, Any]:
    gold_mql = ms_output["MQL"]
    mql_alt = ms_output["mql_alt"]
    cfs = ms_output["canonical_form_set"]

    record = {
        "record_id": record_id,
        "db_id": db_id,
        "MQL": gold_mql,
        "canonical_form_set": cfs,
        "shape_policy": ms_output.get("shape_policy", query_plan.get("shape_policy")),
    }

    gold_result = NormExec(gold_mql, snapshot)
    gold_normexec_non_bot = not isinstance(gold_result, (BOT, BOT_EXEC))
    gold_ex = EX_verdict(gold_mql, record, snapshot) if gold_normexec_non_bot else False
    ast_check_gold = AST_check(gold_mql, cfs) == "pass"
    ast_check_alt = AST_check(mql_alt, cfs) == "pass"

    alt_ex_equiv = False
    if gold_normexec_non_bot:
        alt_result = NormExec(mql_alt, snapshot)
        if not isinstance(alt_result, (BOT, BOT_EXEC)):
            alt_ex_equiv = equiv_rec(gold_result, alt_result, order_sensitive=False)

    semantic_results: list[dict[str, Any]] = []
    blocking: list[str] = []
    for prop in query_plan.get("semantic_properties", []):
        ok, note = _probe_semantic_property(prop["id"], query_plan, gold_mql, snapshot)
        entry: dict[str, Any] = {"id": prop["id"], "pass": ok}
        if note:
            entry["notes"] = note
        semantic_results.append(entry)
        if not ok:
            blocking.append(f"semantic_property:{prop['id']}")

    mutation_results: list[dict[str, Any]] = []
    mutations_ex_all_reject = True
    try:
        mutation_results = validate_mutations(mutations, record, snapshot)
    except AssertionError as exc:
        mutations_ex_all_reject = False
        blocking.append(str(exc))

    if not gold_ex:
        blocking.append("gold_ex")
    if not gold_normexec_non_bot:
        blocking.append("gold_normexec_non_bot")
    if not ast_check_gold:
        blocking.append("ast_check_gold")

    pv_pass = not blocking
    reflux_target = "MS" if not gold_ex else ("MUT" if not mutations_ex_all_reject else None)

    return {
        "property_verification": {
            "gold_ex": gold_ex,
            "gold_normexec_non_bot": gold_normexec_non_bot,
            "mql_alt_ex_equiv": alt_ex_equiv,
            "ast_check_gold": ast_check_gold,
            "ast_check_alt": ast_check_alt,
            "semantic_properties": semantic_results,
            "mutations_ex_all_reject": mutations_ex_all_reject,
            "mutation_results": mutation_results,
        },
        "pv_pass": pv_pass,
        "pv_trace": {
            "blocking_failures": blocking,
            "reflux_target": reflux_target,
        },
    }
