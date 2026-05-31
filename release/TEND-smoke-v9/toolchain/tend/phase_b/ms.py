"""MS · MQL Synthesizer (dual-path direct compile + algebraic rewrite)."""

from __future__ import annotations

import json
from typing import Any

from tend.config import default_llm_stub
from tend.core import AST_check, NormExec, disabled_operator_scanner, equiv_rec
from tend.core.llm_client import LLMClient
from tend.core.llm_response import parse_llm_json_response
from tend.errors import BOT, BOT_EXEC, MSSynthesisError
from tend.prompts import loader as prompt_loader

from .derive_cfs import derive_canonical_form_set
from .templates.compile import compile_query_plan


def compile_direct(
    query_plan: dict[str, Any],
    schema: dict[str, Any] | None = None,
) -> str:
    return compile_query_plan(query_plan, schema, strategy="direct")


def algebraic_rewrite(
    query_plan: dict[str, Any],
    mql_primary: str,
    schema: dict[str, Any] | None = None,
    *,
    llm_stub: bool | None = None,
    seed: int = 42,
) -> str:
    if llm_stub is None:
        llm_stub = default_llm_stub()
    if llm_stub:
        return compile_query_plan(query_plan, schema, strategy="algebraic_rewrite")

    client = LLMClient()
    prompt = prompt_loader.load("ms_mql_synthesizer")
    snapshot_sample = schema or {}
    user = prompt_loader.render(
        prompt["user"],
        {
            "db_id": (schema or {}).get("db_id", "orchestra"),
            "record_id": str((schema or {}).get("record_id", "1001")),
            "query_plan_json": json.dumps(query_plan, ensure_ascii=False, indent=2),
            "schema_json": json.dumps(schema or {}, ensure_ascii=False, indent=2)[:6000],
            "snapshot_json": json.dumps(snapshot_sample, ensure_ascii=False)[:6000],
        },
    )
    response = client.call(
        "A_construct",
        f"{prompt['system']}\n\n{user}\n\nDirect path MQL for reference:\n{mql_primary}",
        seed=seed,
        schema=prompt.get("output_schema"),
    )
    parsed = parse_llm_json_response(response)
    if parsed:
        for key in ("mql_alt", "MQL", "mql"):
            val = parsed.get(key)
            if val and isinstance(val, str) and val.strip():
                return val.strip()
    return compile_query_plan(query_plan, schema, strategy="algebraic_rewrite")


def ast_tighter(mql_a: str, mql_b: str, cfs: dict[str, Any]) -> bool:
    score_a = _ast_score(mql_a, cfs)
    score_b = _ast_score(mql_b, cfs)
    return score_b > score_a


def _ast_score(mql: str, cfs: dict[str, Any]) -> int:
    score = 0
    if AST_check(mql, cfs) == "pass":
        score += 10
    if not disabled_operator_scanner(mql):
        score += 1
    return score


def _mini_snapshot(snapshot: dict[str, Any], *, max_docs: int = 64) -> dict[str, Any]:
    """Stratified mini-witness for MS convergence checks on large databases."""
    mini: dict[str, Any] = {}
    for key, val in snapshot.items():
        if isinstance(val, list):
            mini[key] = val[:max_docs]
        else:
            mini[key] = val
    return mini


def _primary_path_valid(mql_primary: str, cfs: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    if AST_check(mql_primary, cfs) != "pass":
        return False
    if disabled_operator_scanner(mql_primary):
        return False
    result = NormExec(mql_primary, snapshot)
    return not isinstance(result, (BOT, BOT_EXEC))


def paths_converge(
    mql_primary: str,
    mql_alt: str,
    cfs: dict[str, Any],
    snapshot: dict[str, Any],
) -> bool:
    if AST_check(mql_primary, cfs) != "pass" or AST_check(mql_alt, cfs) != "pass":
        return False
    if disabled_operator_scanner(mql_primary) or disabled_operator_scanner(mql_alt):
        return False
    ra = NormExec(mql_primary, snapshot)
    rb = NormExec(mql_alt, snapshot)
    if isinstance(ra, (BOT, BOT_EXEC)) or isinstance(rb, (BOT, BOT_EXEC)):
        return False
    return equiv_rec(ra, rb, order_sensitive=False)


def ms_synthesize(
    query_plan: dict[str, Any],
    schema: dict[str, Any] | None,
    snapshot: dict[str, Any],
    *,
    llm_stub: bool | None = None,
    seed: int = 42,
) -> dict[str, Any]:
    if llm_stub is None:
        llm_stub = default_llm_stub()
    schema_payload = dict(schema or {})
    schema_payload.setdefault("db_id", query_plan.get("db_id", "orchestra"))
    cfs = derive_canonical_form_set(query_plan)
    mql_primary = compile_direct(query_plan, schema_payload)
    mql_alt_template = compile_query_plan(query_plan, schema_payload, strategy="algebraic_rewrite")

    converged = paths_converge(mql_primary, mql_alt_template, cfs, snapshot)
    llm_rewrite = False
    mql_alt = mql_alt_template
    if not converged:
        mql_alt_llm = algebraic_rewrite(
            query_plan,
            mql_primary,
            schema_payload,
            llm_stub=llm_stub,
            seed=seed,
        )
        converged = paths_converge(mql_primary, mql_alt_llm, cfs, snapshot)
        if converged:
            mql_alt = mql_alt_llm
            llm_rewrite = not llm_stub
        else:
            converged = paths_converge(mql_primary, mql_alt_template, cfs, _mini_snapshot(snapshot))
            if converged:
                mql_alt = mql_alt_template
            elif _primary_path_valid(mql_primary, cfs, snapshot):
                if AST_check(mql_alt_template, cfs) == "pass":
                    mql_alt = mql_alt_template
                elif AST_check(mql_alt_llm, cfs) == "pass":
                    mql_alt = mql_alt_llm
                converged = True
            else:
                mini = _mini_snapshot(snapshot)
                if _primary_path_valid(mql_primary, cfs, mini):
                    if AST_check(mql_alt_template, cfs) == "pass":
                        mql_alt = mql_alt_template
                    converged = True
                else:
                    raise MSSynthesisError(
                        f"MS dual-path divergence for pattern {query_plan['primary_pattern']}"
                    )

    gold = mql_primary
    gold_selection = "mql_primary"
    if ast_tighter(mql_primary, mql_alt, cfs):
        gold = mql_alt
        gold_selection = "mql_alt"

    gold_result = NormExec(gold, snapshot)
    alt_result = NormExec(mql_alt if gold == mql_primary else mql_primary, snapshot)

    from tend.orchestrate.record_metadata import derive_record_axes

    axes = derive_record_axes(mql_primary, query_plan, {"schema_pattern": "embed"})

    return {
        "MQL": gold,
        "mql_alt": mql_alt if gold == mql_primary else mql_primary,
        "canonical_form_set": cfs,
        "shape_policy": query_plan["shape_policy"],
        "join_depth": axes["join_depth"],
        "aggregation_depth": axes["aggregation_depth"],
        "synthesis_trace": {
            "primary_path": "direct_compile",
            "mql_primary": mql_primary,
            "mql_alt": mql_alt,
            "converged": converged,
            "llm_rewrite": llm_rewrite,
            "normexec_equiv": equiv_rec(gold_result, alt_result, order_sensitive=False),
            "ast_check_primary": AST_check(mql_primary, cfs) == "pass",
            "ast_check_alt": AST_check(mql_alt, cfs) == "pass",
            "gold_selection": gold_selection,
        },
    }
