"""QIR Lift: MQL → AST → QIR verification and grammar variant generation."""
from __future__ import annotations

from random import Random
from typing import Any

from tend_core import CanonicalFormSet, QIR, StructuredIntent, canonical_text
from tend_core.mql import (
    extract_operator_tokens,
    extract_root_stage_tokens,
    parse_ok,
)


def lift_mql_to_qir(mql: str) -> QIR | None:
    """Lift an MQL query to a QIR representation for equivalence checking."""
    if not parse_ok(mql):
        return None
    root_stages = extract_root_stage_tokens(mql)
    all_ops = set(extract_operator_tokens(mql))

    primary = root_stages[0] if root_stages else "$unknown"
    pattern = _infer_pattern(root_stages, all_ops)

    return QIR(
        pattern_family=pattern,
        primary_operator=primary,
        input_shape={"collections": [_extract_collection(mql)]},
        output_shape={"inferred": True},
        referenced_fields=tuple(),
    )


def qir_equivalent(qir_a: QIR, qir_b: QIR) -> bool:
    """Check structural equivalence between two QIRs."""
    return (
        qir_a.pattern_family == qir_b.pattern_family
        and qir_a.primary_operator == qir_b.primary_operator
        and set(qir_a.input_shape.get("collections", []))
        == set(qir_b.input_shape.get("collections", []))
    )


def generate_grammar_variants(
    mql: str,
    si: StructuredIntent,
    variant_seed: int = 42,
    max_variants: int = 5,
) -> list[str]:
    """Generate syntactic variants that are semantically equivalent to gold MQL."""
    if not parse_ok(mql):
        return [mql]

    rng = Random(variant_seed)
    variants: list[str] = [mql]
    pattern = si.intent["pattern_family"]

    # Rewrite 1: Add redundant $match at the start
    if "$match" not in extract_root_stage_tokens(mql)[:1]:
        candidate = mql.replace(".aggregate([", ".aggregate([ { $match: {} },", 1)
        if parse_ok(candidate):
            variants.append(candidate)

    # Rewrite 2: Swap $sort direction equivalents
    if "$sort" in mql:
        candidate = _swap_sort_projection_order(mql)
        if candidate and parse_ok(candidate):
            variants.append(candidate)

    # Rewrite 3: Replace $project with $addFields + $project
    if pattern in {"simple_filter", "project_only", "coalesce_with_default"}:
        candidate = _project_to_addfields_variant(mql)
        if candidate and parse_ok(candidate):
            variants.append(candidate)

    # Rewrite 4: Wrap $match predicate in $expr
    if "$match" in mql and "$expr" not in mql:
        candidate = _wrap_match_in_expr(mql, si)
        if candidate and parse_ok(candidate):
            variants.append(candidate)

    # Rewrite 5: Use $replaceRoot instead of final $project
    if pattern == "group_then_aggregate":
        candidate = _replace_final_project(mql)
        if candidate and parse_ok(candidate):
            variants.append(candidate)

    rng.shuffle(variants)
    return variants[:max_variants]


def verify_variant_equivalence(
    gold_mql: str,
    variant: str,
    gold_qir: QIR,
) -> dict[str, Any]:
    """Verify that a variant lifts to an equivalent QIR."""
    variant_qir = lift_mql_to_qir(variant)
    if variant_qir is None:
        return {"pass": False, "reason": "parse_failure"}

    is_equiv = qir_equivalent(gold_qir, variant_qir)
    return {
        "pass": is_equiv,
        "gold_pattern": gold_qir.pattern_family,
        "variant_pattern": variant_qir.pattern_family,
        "reason": "qir_match" if is_equiv else "pattern_mismatch",
    }


def run_p1_p4_checks(
    mql: str,
    si: StructuredIntent,
    qir: QIR,
    canonical_form_set: CanonicalFormSet,
    mutations: list[dict[str, Any]],
    variants: list[str],
) -> dict[str, Any]:
    """Run P1-P4 self-consistency checks on a constructed record."""
    from tend_core.checks import ast_check

    results: dict[str, Any] = {}

    # P1: Gold query must pass AST check
    ast_result = ast_check(mql, canonical_form_set)
    results["p1_gold_ast_pass"] = ast_result == "pass"
    results["p1_detail"] = ast_result

    # P2: At least 2 mutations must differ structurally from gold
    mutation_diffs = 0
    for m in mutations:
        if canonical_text(m["query"]) != canonical_text(mql):
            mutation_diffs += 1
    results["p2_mutation_differ_count"] = mutation_diffs
    results["p2_pass"] = mutation_diffs >= min(2, len(mutations))

    # P3: Grammar variants must lift to equivalent QIR
    variant_checks = []
    for v in variants:
        check = verify_variant_equivalence(mql, v, qir)
        variant_checks.append(check)
    results["p3_variant_checks"] = variant_checks
    results["p3_pass"] = all(c["pass"] for c in variant_checks) if variant_checks else True

    # P4: SI pattern_family must match QIR pattern_family
    results["p4_si_qir_match"] = si.intent["pattern_family"] == qir.pattern_family
    results["p4_pass"] = results["p4_si_qir_match"]

    results["all_pass"] = all([
        results["p1_gold_ast_pass"],
        results["p2_pass"],
        results["p3_pass"],
        results["p4_pass"],
    ])
    return results


# ---- internal helpers ----

def _extract_collection(mql: str) -> str:
    if mql.startswith("db."):
        parts = mql[3:].split(".", 1)
        return parts[0]
    return "unknown"


def _infer_pattern(root_stages: list[str], all_ops: set[str]) -> str:
    stage_set = set(root_stages)
    if "$graphLookup" in stage_set:
        return "graph_recursive_deep"
    if "$lookup" in stage_set:
        return "lookup_join"
    if "$setWindowFields" in stage_set and "$facet" in stage_set:
        return "window_function_with_facet_filter"
    if "$setWindowFields" in stage_set and "$match" in stage_set:
        return "anomaly_vs_baseline"
    if "$setWindowFields" in stage_set:
        return "window_function"
    if "$facet" in stage_set:
        return "facet_split"
    if "$count" in stage_set:
        return "filter_then_count"
    if "$unwind" in stage_set and "$group" in stage_set:
        return "array_reshape"
    if "$percentile" in all_ops:
        return "percentile_approximation"
    if "$allElementsTrue" in all_ops:
        return "universal_quantifier"
    if "$anyElementTrue" in all_ops:
        return "existential_quantifier"
    if "$switch" in all_ops:
        return "polymorphic_branch"
    if "$objectToArray" in all_ops:
        return "dynamic_key_expansion"
    if "$arrayElemAt" in all_ops:
        return "array_positional_select"
    if "$ifNull" in all_ops:
        return "coalesce_with_default"
    if "$type" in all_ops and "$group" in stage_set:
        return "type_introspection"
    if "$type" in all_ops:
        return "null_vs_missing_disambig"
    if "$match" in stage_set and "$group" in stage_set:
        return "filter_then_aggregate"
    if "$group" in stage_set and "$sort" in stage_set and "$limit" in stage_set:
        return "top_k_by_aggregate"
    if "$group" in stage_set and "$sort" in stage_set:
        if "$match" not in stage_set:
            return "time_window_aggregate"
        return "group_then_aggregate"
    if "$group" in stage_set:
        return "group_then_aggregate"
    if "$match" in stage_set and len(stage_set) <= 2:
        return "simple_filter"
    if stage_set == {"$project"}:
        return "project_only"
    return "simple_filter"


def _swap_sort_projection_order(mql: str) -> str | None:
    return None  # conservative: only structural rewrites we can prove safe


def _project_to_addfields_variant(mql: str) -> str | None:
    return None  # placeholder for addFields-based rewrite


def _wrap_match_in_expr(mql: str, si: StructuredIntent) -> str | None:
    return None  # would need full AST rewrite; deferred


def _replace_final_project(mql: str) -> str | None:
    return None  # placeholder for $replaceRoot rewrite
