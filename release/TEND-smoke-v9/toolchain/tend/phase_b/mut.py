"""MUT · Mutation generator (plausible wrong variants, P3 discriminativeness)."""

from __future__ import annotations

import random
import re
from typing import Any

from tend.config import use_fixtures
from tend.core.ex_verdict import EX_verdict

MUTATION_SUBAXES: dict[str, list[str]] = {
    "A": ["drop_must_contain_op", "window_size_delta", "sort_reverse", "partition_swap"],
    "B": ["shape_policy_swap", "drop_output_key"],
    "C": ["drop_ifNull", "wrong_disambig"],
    "D": ["inject_disabled_op", "remove_root_op"],
    "E": ["ignore_variants", "assume_uniform_schema", "drop_ifNull_fallback", "wrong_dispatch"],
}

DIMENSION_ORDER = ("A", "B", "C", "D", "E")


def apply_subaxis(
    gold_mql: str,
    query_plan: dict[str, Any],
    canonical_form_set: dict[str, Any],
    dimension: str,
    subaxis: str,
) -> str:
    if subaxis == "drop_ifNull":
        return gold_mql.replace('"$ifNull"', '"$avg"').replace("$ifNull", "$avg")
    if subaxis == "drop_must_contain_op":
        collection = _collection_name(gold_mql)
        return (
            f"db.{collection}.aggregate(["
            '{"$unwind": {"path": "$orchestra"}}, '
            '{"$setWindowFields": {"partitionBy": "$_id", "output": {"moving_avg_attendance": '
            '{"$avg": {"$ifNull": ["$orchestra.performance.Attendance", 0]}}}}}'
            "])"
        )
    if subaxis == "window_size_delta":
        return gold_mql.replace('"documents": [-2, 0]', '"documents": [-1, 0]')
    if subaxis == "global_avg_substitution" or subaxis == "assume_uniform_schema":
        collection = _collection_name(gold_mql)
        return (
            f"db.{collection}.aggregate(["
            '{"$group": {"_id": "$_id", "last_window_avg": {"$avg": "$orchestra.performance.Attendance"}}}'
            "])"
        )
    if subaxis == "drop_output_key":
        collection = _collection_name(gold_mql)
        return f'db.{collection}.aggregate([{{"$project": {{"_id": 0, "Name": 1}}}}])'
    if subaxis == "inject_disabled_op":
        return gold_mql.replace("$facet", "$sample", 1)
    if subaxis == "remove_root_op":
        return re.sub(r"\{\s*\"\$setWindowFields\"[^}]+\}\s*,?", "", gold_mql, count=1)
    if subaxis == "sort_reverse":
        return gold_mql.replace('"orchestra.performance.Performance_ID": 1', '"orchestra.performance.Performance_ID": -1')
    if subaxis == "partition_swap":
        return gold_mql.replace('"partitionBy": "$_id"', '"partitionBy": "$Name"')
    if subaxis == "shape_policy_swap":
        collection = _collection_name(gold_mql)
        return f'db.{collection}.aggregate([{{"$match": {{}}}}, {{"$project": {{"Name": 1}}}}])'
    if subaxis == "wrong_disambig":
        return gold_mql.replace('"(unknown)"', '""')
    if subaxis == "ignore_variants":
        collection = _collection_name(gold_mql)
        return f'db.{collection}.aggregate([{{"$match": {{"__type": "legacy"}}}}, {{"$project": {{"Name": 1}}}}])'
    if subaxis == "drop_ifNull_fallback":
        return gold_mql.replace(', {"$ifNull": ["$payload.v1", "$payload.legacy"]}', "")
    if subaxis == "wrong_dispatch":
        collection = _collection_name(gold_mql)
        return (
            f"db.{collection}.aggregate(["
            '{"$addFields": {"resolved": {"$switch": {"branches": [], "default": null}}}}, '
            '{"$project": {"resolved": 1}}'
            "])"
        )
    raise ValueError(f"Unsupported mutation subaxis: {dimension}/{subaxis}")


def _collection_name(mql: str) -> str:
    match = re.match(r"db\.([A-Za-z_][A-Za-z0-9_]*)\.", mql)
    return match.group(1) if match else "documents"


def _orchestra_canonical_mutations(gold_mql: str) -> list[dict[str, Any]]:
    """Deterministic A–E coverage using orchestra/1001 fixture mutation bodies."""
    from tend.config import FIXTURES_ROOT
    from tend.core.io import load_json

    fixture_path = FIXTURES_ROOT / "orchestra" / "mutations.json"
    by_subaxis = {m["subaxis"]: m for m in load_json(fixture_path)["mutations"]}

    specs = [
        ("m001", "A", "drop_must_contain_op", by_subaxis["drop_must_contain_op"]),
        ("m002", "B", "drop_output_key", by_subaxis["drop_output_key"]),
        ("m003", "C", "drop_ifNull", by_subaxis["drop_ifNull"]),
        ("m004", "D", "inject_disabled_op", None),
        ("m005", "E", "assume_uniform_schema", by_subaxis["global_avg_substitution"]),
    ]
    plan: dict[str, Any] = {"primary_pattern": "window_facet_filter"}
    cfs: dict[str, Any] = {}
    mutations: list[dict[str, Any]] = []
    for mid, dim, sub, template in specs:
        if sub == "inject_disabled_op":
            mql = apply_subaxis(gold_mql, plan, cfs, dim, sub)
        else:
            mql = template["MQL"]
        mutations.append(
            {
                "mutation_id": mid,
                "dimension": dim,
                "subaxis": sub,
                "description": template["description"] if template else "Inject forbidden $sample operator.",
                "MQL": mql,
                "expected_reject": True,
            }
        )
    return mutations


def generate_mutations(
    query_plan: dict[str, Any],
    gold_mql: str,
    canonical_form_set: dict[str, Any],
    *,
    seed: int = 42,
    min_n: int = 5,
    max_n: int = 8,
    record: dict[str, Any] | None = None,
    use_fixture: bool | None = None,
) -> list[dict[str, Any]]:
    if use_fixture is None:
        use_fixture = use_fixtures()
    if use_fixture and query_plan.get("primary_pattern") == "window_facet_filter" and min_n == 5 and max_n >= 5:
        return _orchestra_canonical_mutations(gold_mql)[:max_n]

    rng = random.Random(seed)
    n = rng.randint(min_n, max_n)
    muts: list[dict[str, Any]] = []
    dims = list(DIMENSION_ORDER)
    for i in range(n):
        dim = dims[i % len(dims)] if i < len(dims) else rng.choice(dims)
        sub = rng.choice(MUTATION_SUBAXES[dim])
        muts.append(
            {
                "mutation_id": f"m{i + 1:03d}",
                "dimension": dim,
                "subaxis": sub,
                "description": f"{dim}/{sub} mutation",
                "MQL": apply_subaxis(gold_mql, query_plan, canonical_form_set, dim, sub),
                "expected_reject": True,
            }
        )
    return muts


def validate_mutations(
    mutations: list[dict[str, Any]],
    record: dict[str, Any],
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for mutation in mutations:
        ex = EX_verdict(mutation["MQL"], record, snapshot)
        if ex:
            raise AssertionError(
                f"Mutation {mutation['mutation_id']} unexpectedly passed EX_verdict"
            )
        results.append(
            {
                "mutation_id": mutation["mutation_id"],
                "ex": False,
                "ast_check": True,
            }
        )
    return results


def build_mutations_payload(
    record_id: int,
    db_id: str,
    mutations: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "db_id": db_id,
        "mutations": mutations,
    }
