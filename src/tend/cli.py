"""TEND command-line entry point.

    tend construct --phase all --dbs financial --records 1 [--stub] [--quiet]
    tend validate --dataset-dir runs/<run_id>/dataset [--smoke]
    tend publish --dataset-dir runs/<run_id>/dataset --out release/TEND-dataset
    tend solve --db-id financial --record-id 1001 [--stub] [--quiet]

Assembles the runtime (logging + progress + BIRD source + LLM client + MongoDB executor),
runs the Phase A / Phase B workflow flows or the SMART solver, persists outputs, and
prints a run summary. The run id namespaces everything under ``runs/<run_id>/``.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._cli_summaries import (
    _count_by,
    _print_ablation_summary,
    _print_baseline_summary,
    _print_evaluation_block,
    _print_solve_summary,
    _print_summary,
)
from .agents import AgentContext
from .ablations import ABLATION_IDS, run_ablation_suite
from .baselines import BASELINE_IDS, run_baseline_suite
from .config import Settings
from .dataset import write_catalog, write_native_phase_a, write_phase_a, write_records
from .errors import Anomaly, SourceError, TendError, wrap_unexpected
from .evaluation import EvaluationOutput, evaluate_predictions
from .execution import mql_signature, mql_skeleton_signature
from .execution.mongo import MongoExecutor
from .llm import LLMClient
from .mechanisms import get_archetype
from .observability import make_reporter, new_run_id, setup_logging
from .publish import ReleaseReport, validate_release
from .source import BirdSource
from .source.census import (
    CoverageRequest,
    plan_coverage_slots,
    plan_source_full_structural_slots,
    run_census,
)
from .stubs import stub_fn
from .solver.workflow import (
    DEFAULT_R_MAX,
    DEFAULT_WITNESS_K,
    SmartSolveOptions,
    load_solver_release_inputs,
    smart_solve_nlq_db,
    smart_solve_record,
)
from .workflow import Workflow, run_phase_a, run_phase_b
from .workflow.flows import CoverageSlot, DbArtifacts
from .workflow.native_construction import NativeDbArtifacts, run_native_phase_a
from .workflow.native_phase_b import plan_native_slots, run_native_phase_b

PRODUCTION_RELEASE_DIR = Path("release/TEND-dataset")
VALIDATION_ISSUE_LIMIT = 12
SLOT_L4_MIN = 0.30
SLOT_L0_MAX = 0.05
SLOT_FLEX_MIN = 0.25
SLOT_SSF_MIN = 0.20


@dataclass
class Runtime:
    settings: Settings
    ctx: AgentContext
    workflow: Workflow
    progress: object
    log: object
    source: BirdSource | None
    mongo: MongoExecutor


def build_runtime(settings: Settings) -> Runtime:
    settings.paths.ensure()
    run_dir = settings.run_dir
    log = setup_logging(run_dir, console=False)
    log.info("run_start", run_id=settings.run_id, stub=settings.stub,
             model=settings.llm.model)
    progress = make_reporter(settings.run_id, log, enabled=not settings.quiet)
    source = BirdSource(settings.paths.bird_root)
    llm = LLMClient(settings, log)
    if settings.stub:
        llm.set_stub(stub_fn)
    mongo = MongoExecutor(settings, log)
    ctx = AgentContext(settings=settings, llm=llm, log=log, progress=progress,
                       source=source, mongo=mongo)
    return Runtime(settings, ctx, Workflow(ctx), progress, log, source, mongo)


def build_solver_runtime(settings: Settings, *, run_kind: str = "solver") -> Runtime:
    log = setup_logging(settings.run_dir, console=False)
    log.info(f"{run_kind}_run_start", run_id=settings.run_id, stub=settings.stub,
             model=settings.llm.model)
    progress = make_reporter(settings.run_id, log, enabled=not settings.quiet)
    llm = LLMClient(settings, log)
    if settings.stub:
        llm.set_stub(stub_fn)
    mongo = MongoExecutor(settings, log)
    ctx = AgentContext(settings=settings, llm=llm, log=log, progress=progress,
                       source=None, mongo=mongo)
    return Runtime(settings, ctx, Workflow(ctx), progress, log, None, mongo)


def _close_runtime(rt: Runtime) -> None:
    """Release the run's source/mongo/log handles; safe to call once in finally."""
    if rt.source is not None:
        rt.source.close()
    rt.mongo.close()
    rt.log.close()


def _slot_from_request(
    request: CoverageRequest,
    record_id: int,
    *,
    slot_index: int = 0,
    diversity_key: str = "",
    diversity_hint: str = "",
    schema_feature: str = "",
    reference_oracle_seed: dict[str, Any] | None = None,
    intent_seed: dict[str, Any] | None = None,
) -> CoverageSlot:
    target_schema_flex = (
        "polymorphic"
        if request.sql_infeasibility_class == "structural_schema_flex"
        else "none"
    )
    return CoverageSlot(
        db_id=request.db_id,
        mechanism=request.mechanism,
        archetype=request.archetype,
        record_id=record_id,
        target_difficulty=request.target_difficulty,
        target_sql_infeasibility_class=request.sql_infeasibility_class,
        target_schema_flex=target_schema_flex,
        slot_index=slot_index,
        diversity_key=diversity_key,
        diversity_hint=diversity_hint,
        schema_feature=schema_feature,
        reference_oracle_seed=reference_oracle_seed,
        intent_seed=intent_seed,
    )


def _coverage_slots_for(
    source: BirdSource,
    db_ids: list[str],
    n_records: int,
    *,
    seed: int,
    structural_only: bool = False,
    structural_fraction: float = 0.0,
) -> list[CoverageSlot]:
    """Plan coverage slots.

    ``structural_only`` schedules only source-full structural_schema_flex slots.
    ``structural_fraction`` (0..1) requests a *hybrid* plan: that fraction of slots are
    structural_schema_flex (each becomes a genuine ssf + polymorphic + L4 record) and the
    rest are the broad census mix — this is what lets a large run meet the release
    complexity floors (H5 L4>=30%, H7 flex>=25%, H9 ssf>=20%) while staying diverse.
    """
    census = run_census(source, db_ids=db_ids)
    if structural_only:
        requests = list(plan_source_full_structural_slots(census, n_records=n_records, seed=seed))
    elif structural_fraction > 0:
        n_struct = max(1, round(n_records * min(structural_fraction, 1.0)))
        n_broad = max(0, n_records - n_struct)
        try:
            requests = list(plan_source_full_structural_slots(census, n_records=n_struct, seed=seed))
        except SourceError:
            # This db (or selection) has no query-bearing structural supply. Degrade gracefully
            # to a pure broad-census mix instead of hard-failing the whole run: the complexity
            # floors (H7 flex / H9 ssf) are unreachable without structural supply, and validate
            # will report that honestly. No silent cap — emit a warning naming the affected dbs.
            import structlog
            structlog.get_logger("tend").warning(
                "structural_fraction_no_supply",
                db_ids=sorted(census.databases), requested_structural=n_struct,
                note="degraded to broad census mix; complexity floors unreachable for this db",
            )
            requests = []
            n_broad = n_records
        if n_broad:
            requests += list(plan_coverage_slots(census, n_records=n_broad, seed=seed + 1))
    else:
        requests = list(plan_coverage_slots(census, n_records=n_records, seed=seed))
    return [
        _slot_from_request(request, record_id=1001 + i, slot_index=i)
        for i, request in enumerate(requests)
    ]


def _artifact_diversity_slots_for(
    artifacts: dict[str, DbArtifacts],
    n_records: int,
    *,
    seed: int,
    records_per_db: int | None = None,
) -> list[CoverageSlot]:
    """Plan slots from the materialized schema so large runs have distinct oracle seeds."""
    targets = _artifact_target_counts(
        artifacts,
        n_records,
        records_per_db=records_per_db,
    )
    return _artifact_diversity_slots_for_targets(
        artifacts,
        targets,
        seed=seed,
    )


def _artifact_target_counts(
    artifacts: dict[str, DbArtifacts],
    n_records: int,
    *,
    records_per_db: int | None = None,
) -> dict[str, int]:
    db_ids = sorted(artifacts)
    if not db_ids or n_records <= 0:
        return {}
    if records_per_db is not None:
        return {db_id: records_per_db for db_id in db_ids}
    base, rem = divmod(n_records, len(db_ids))
    return {db_id: base + (1 if i < rem else 0) for i, db_id in enumerate(db_ids)}


def _artifact_diversity_slots_for_targets(
    artifacts: dict[str, DbArtifacts],
    targets: dict[str, int],
    *,
    seed: int,
    start_record_id: int = 1001,
    per_db_start_index: dict[str, int] | None = None,
) -> list[CoverageSlot]:
    """Plan one batch of never-before-attempted artifact slots for explicit DB targets."""
    if not targets:
        return []

    slots: list[CoverageSlot] = []
    next_record_id = start_record_id
    per_db_start_index = per_db_start_index or {}
    for db_id, target in sorted(targets.items()):
        if target <= 0 or db_id not in artifacts:
            continue
        pool = _artifact_slot_pool(artifacts[db_id], seed=seed)
        if not pool:
            continue
        start = per_db_start_index.get(db_id, 0)
        for i in range(target):
            pool_index = start + i
            if pool_index >= len(pool):
                break
            spec = pool[pool_index]
            slots.append(_slot_from_spec(
                db_id=db_id,
                record_id=next_record_id,
                slot_index=pool_index,
                spec=spec,
            ))
            next_record_id += 1
    return slots


def _slot_from_spec(
    *,
    db_id: str,
    record_id: int,
    slot_index: int,
    spec: dict[str, Any],
) -> CoverageSlot:
    arch = get_archetype(spec["archetype"])
    return CoverageSlot(
        db_id=db_id,
        mechanism=spec["mechanism"],
        archetype=spec["archetype"],
        record_id=record_id,
        target_difficulty=arch.difficulty,
        target_sql_infeasibility_class=arch.sql_infeasibility_class,
        target_schema_flex=_schema_flex_target(spec["mechanism"]),
        slot_index=slot_index,
        diversity_key=spec["diversity_key"],
        diversity_hint=spec["diversity_hint"],
        schema_feature=spec["schema_feature"],
        reference_oracle_seed=spec.get("reference_oracle"),
        intent_seed=spec.get("intent_seed") or _design_card_from_spec(spec),
    )


def _schema_flex_target(mechanism: str) -> str:
    return {
        "polymorphic": "polymorphic",
        "sparse_embed": "polymorphic",
        "sparse_scalar": "polymorphic",
        "dynamic_key": "dynamic_key",
        "versioning": "schema_versioning",
    }.get(mechanism, "none")


def _design_card_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    params = spec.get("reference_oracle", {}).get("params", {})
    if not isinstance(params, dict):
        params = {}
    collections = [
        params[key] for key in (
            "collection",
            "parent_collection",
            "child_collection",
        )
        if isinstance(params.get(key), str)
    ]
    fields = [
        value for key, value in params.items()
        if isinstance(value, str)
        and key not in {
            "collection",
            "parent_collection",
            "child_collection",
            "agg",
            "order",
        }
    ]
    if isinstance(params.get("match"), dict):
        match = params["match"]
        if isinstance(match.get("field"), str):
            fields.append(match["field"])
    card = {
        "schema_feature": spec.get("schema_feature", ""),
        "feature_family": spec.get("feature_family", ""),
        "collection_hints": _ordered_unique([str(value) for value in collections]),
        "field_hints": _ordered_unique([str(value) for value in fields]),
        "complexity_score": spec.get("complexity_score", 0),
        "query_pressure": _design_pressure_for(
            str(spec.get("mechanism", "")),
            str(spec.get("archetype", "")),
            str(spec.get("feature_family", "")),
        ),
        "avoid": [
            "do not emit avg/sum/min/max over identifier-like fields",
            "do not make a near copy by only changing a field name or accumulator",
            "do not expose helper fields in the final output",
        ],
        "certification": (
            "A hidden reference oracle will certify the answer; treat this card as "
            "schema pressure, not a query template."
        ),
    }
    for key in ("target_field", "missing_default", "absent_value", "default"):
        if key in params:
            card[key] = params[key]
    return card


def _design_pressure_for(mechanism: str, archetype: str, family: str) -> list[str]:
    if family == "cross_collection" or archetype == "fk_rollup":
        return [
            "use a real multi-collection financial rollup",
            "prefer $lookup with a child pipeline or equivalent staged aggregation",
            "include a business filter such as transaction type/operation when available",
        ]
    if family == "nested_array" or archetype == "join_nested_group":
        return [
            "use $unwind over an embedded array and regroup at a meaningful grain",
            "aggregate a measure field, not an id",
        ]
    if mechanism == "sparse_embed":
        return [
            "branch on optional embedded-object presence",
            "combine the optional branch with a meaningful financial metric when possible",
        ]
    if mechanism == "polymorphic":
        return [
            "make subtype branches semantically different",
            "avoid a $switch whose every branch reads the same field",
        ]
    if mechanism == "sparse_scalar":
        return [
            "use missing-field behavior as part of a larger business question",
            "avoid a one-stage global coalesce aggregate unless no richer option exists",
        ]
    return ["design a realistic multi-stage query when the schema supports it"]


def _artifact_slot_pool(artifact: DbArtifacts, *, seed: int) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    schema = artifact.mongodb_schema
    data = artifact.mongodb_data if isinstance(artifact.mongodb_data, dict) else {}
    for coll, node in sorted(_schema_collections(schema).items()):
        if not isinstance(node, dict):
            continue
        specs.extend(_variant_slot_specs(coll, node))
        specs.extend(_array_slot_specs(coll, node))
        specs.extend(_generic_slot_specs(
            coll,
            node,
            data.get(coll, []),
            sparse_scalar_fields=_feature_names_by_family(node, "sparse_scalar"),
        ))
    specs.extend(_fk_rollup_slot_specs(schema, data))

    deduped: dict[str, dict[str, Any]] = {}
    for spec in specs:
        key = json.dumps(spec["reference_oracle"], ensure_ascii=False, sort_keys=True)
        deduped.setdefault(key, spec)
    return _balanced_slot_specs(list(deduped.values()), seed=seed)


def _balanced_slot_specs(specs: list[dict[str, Any]], *, seed: int) -> list[dict[str, Any]]:
    """Order slots so prefixes stay balanced across diversity axes."""
    remaining = sorted(
        specs,
        key=lambda spec: _stable_slot_rank(spec.get("diversity_key", ""), seed=seed),
    )
    ordered: list[dict[str, Any]] = []
    mechanism_counts: Counter[str] = Counter()
    archetype_counts: Counter[str] = Counter()
    collection_counts: Counter[str] = Counter()
    feature_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    composition_counts: Counter[str] = Counter()

    while remaining:
        next_n = len(ordered) + 1
        non_l0_available = any(not _slot_composition_traits(spec)["l0"] for spec in remaining)
        best_index = min(
            range(len(remaining)),
            key=lambda i: (
                _slot_composition_key(
                    remaining[i],
                    composition_counts=composition_counts,
                    next_n=next_n,
                    non_l0_available=non_l0_available,
                ),
                _slot_practicality_key(remaining[i]),
                _slot_balance_key(
                    remaining[i],
                    mechanism_counts=mechanism_counts,
                    archetype_counts=archetype_counts,
                    collection_counts=collection_counts,
                    feature_counts=feature_counts,
                    family_counts=family_counts,
                    seed=seed,
                ),
            ),
        )
        spec = remaining.pop(best_index)
        ordered.append(spec)
        mechanism_counts[str(spec.get("mechanism", ""))] += 1
        archetype_counts[str(spec.get("archetype", ""))] += 1
        collection_counts[str(spec.get("collection", ""))] += 1
        feature_counts[str(spec.get("schema_feature", ""))] += 1
        family_counts[str(spec.get("feature_family", ""))] += 1
        for key, enabled in _slot_composition_traits(spec).items():
            if enabled:
                composition_counts[key] += 1
    return ordered


def _slot_composition_traits(spec: dict[str, Any]) -> dict[str, bool]:
    arch = get_archetype(str(spec.get("archetype", "")))
    return {
        "l4": arch.difficulty == "L4",
        "l0": arch.difficulty == "L0",
        "flex": _schema_flex_target(str(spec.get("mechanism", ""))) != "none",
        "ssf": arch.sql_infeasibility_class == "structural_schema_flex",
    }


def _slot_composition_key(
    spec: dict[str, Any],
    *,
    composition_counts: Counter[str],
    next_n: int,
    non_l0_available: bool,
) -> tuple[int, int, int, int, int]:
    traits = _slot_composition_traits(spec)
    l0_limit = math.floor(SLOT_L0_MAX * next_n)
    l0_over_cap = (
        traits["l0"]
        and composition_counts["l0"] + 1 > l0_limit
        and non_l0_available
    )
    needs = {
        "l4": composition_counts["l4"] < math.ceil(SLOT_L4_MIN * next_n),
        "flex": composition_counts["flex"] < math.ceil(SLOT_FLEX_MIN * next_n),
        "ssf": composition_counts["ssf"] < math.ceil(SLOT_SSF_MIN * next_n),
    }
    hits = sum(1 for key, needed in needs.items() if needed and traits[key])
    misses = sum(1 for key, needed in needs.items() if needed and not traits[key])
    return (
        1 if l0_over_cap else 0,
        -hits,
        misses,
        1 if traits["l0"] else 0,
        0 if traits["l4"] else 1,
    )


def _slot_practicality_key(spec: dict[str, Any]) -> tuple[int, int]:
    """Prefer candidates that are both structurally useful and verifier-friendly."""
    archetype = str(spec.get("archetype", ""))
    traits = _slot_composition_traits(spec)
    if traits["l4"] and traits["ssf"]:
        return (0, _archetype_rank(archetype))
    if archetype in {
        "existence_count",
        "null_coalesce_agg",
        "join_nested_group",
        "group_count",
    }:
        return (1, _archetype_rank(archetype))
    if archetype == "fk_rollup":
        return (2, _archetype_rank(archetype))
    if archetype in {"topn", "simple_filter"}:
        return (3, _archetype_rank(archetype))
    return (2, _archetype_rank(archetype))


def _slot_balance_key(
    spec: dict[str, Any],
    *,
    mechanism_counts: Counter[str],
    archetype_counts: Counter[str],
    collection_counts: Counter[str],
    feature_counts: Counter[str],
    family_counts: Counter[str],
    seed: int,
) -> tuple[int, int, int, int, int, int, int, int, int]:
    mechanism = str(spec.get("mechanism", ""))
    archetype = str(spec.get("archetype", ""))
    collection = str(spec.get("collection", ""))
    feature = str(spec.get("schema_feature", ""))
    family = str(spec.get("feature_family", ""))
    complexity = int(spec.get("complexity_score") or 0)
    return (
        archetype_counts[archetype],
        feature_counts[feature],
        family_counts[family],
        collection_counts[collection],
        mechanism_counts[mechanism],
        -complexity,
        _mechanism_rank(mechanism),
        _archetype_rank(archetype),
        _stable_slot_rank(str(spec.get("diversity_key", "")), seed=seed),
    )


def _stable_slot_rank(value: str, *, seed: int) -> int:
    digest = hashlib.sha256(f"{seed}:{value}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _feature_names_by_family(node: dict[str, Any], family: str) -> set[str]:
    features = node.get("__schema_less_features")
    if not isinstance(features, list):
        return set()
    out: set[str] = set()
    for feature in features:
        if not isinstance(feature, dict) or feature.get("family") != family:
            continue
        name = feature.get("feature")
        if isinstance(name, str) and name:
            out.add(name)
    return out


def _mechanism_rank(mechanism: str) -> int:
    return {
        "sparse_embed": 0,
        "polymorphic": 1,
        "dynamic_key": 2,
        "sparse_scalar": 3,
        "versioning": 4,
        "none": 5,
    }.get(mechanism, 99)


def _archetype_rank(archetype: str) -> int:
    return {
        "optional_embed_projection": 0,
        "present_missing_projection": 1,
        "subtype_cond_projection": 2,
        "simple_filter": 3,
        "subtype_specific_field": 4,
        "topn": 5,
        "existence_count": 6,
        "group_count": 7,
        "has_vs_absent_compare": 8,
        "null_coalesce_agg": 9,
        "per_subtype_agg": 10,
        "join_nested_group": 11,
        "fk_rollup": 12,
    }.get(archetype, 99)


def _slot_spec(
    *,
    mechanism: str,
    archetype: str,
    reference_oracle: dict[str, Any],
    schema_feature: str,
    diversity_hint: str,
    feature_family: str | None = None,
) -> dict[str, Any]:
    return {
        "mechanism": mechanism,
        "archetype": archetype,
        "reference_oracle": reference_oracle,
        "schema_feature": schema_feature,
        "collection": schema_feature.split(".", 1)[0],
        "feature_family": feature_family or _default_feature_family(
            mechanism, archetype, schema_feature
        ),
        "complexity_score": _slot_complexity_score(
            mechanism=mechanism,
            archetype=archetype,
            feature_family=feature_family or _default_feature_family(
                mechanism, archetype, schema_feature
            ),
            reference_oracle=reference_oracle,
        ),
        "diversity_hint": diversity_hint,
        "diversity_key": (
            f"{mechanism}:{archetype}:"
            f"{json.dumps(reference_oracle, ensure_ascii=False, sort_keys=True)}"
        ),
    }


def _slot_complexity_score(
    *,
    mechanism: str,
    archetype: str,
    feature_family: str,
    reference_oracle: dict[str, Any],
) -> int:
    """Small ranking hint: prefer structural, multi-stage certifiers over template-simple ones."""
    score = 0
    if feature_family == "cross_collection" or archetype == "fk_rollup":
        score += 8
    if feature_family == "nested_array" or archetype == "join_nested_group":
        score += 7
    if mechanism == "polymorphic":
        score += 6
    if mechanism == "sparse_embed":
        score += 5
    if mechanism == "sparse_scalar":
        score += 2
    if archetype in {"simple_filter", "group_count", "topn"}:
        score += 1
    params = reference_oracle.get("params") if isinstance(reference_oracle, dict) else {}
    if isinstance(params, dict):
        if isinstance(params.get("match"), dict):
            score += 2
        if params.get("value_field"):
            score += 1
        predicates = params.get("predicates")
        if isinstance(predicates, list):
            score += max(0, len(predicates) - 1)
    return score


def _default_feature_family(mechanism: str, archetype: str, schema_feature: str) -> str:
    if schema_feature.endswith("[]"):
        return "nested_array"
    if mechanism == "polymorphic":
        return "polymorphic"
    if mechanism == "sparse_embed":
        return "optional_embed"
    if mechanism == "sparse_scalar":
        return "sparse_scalar"
    return archetype or "baseline"


def _schema_collections(schema: dict[str, Any]) -> dict[str, Any]:
    colls = schema.get("collections", schema)
    return colls if isinstance(colls, dict) else {}


def _variant_slot_specs(coll: str, node: dict[str, Any]) -> list[dict[str, Any]]:
    by_disc: dict[str, list[Any]] = {}
    variants = node.get("__variants") if isinstance(node.get("__variants"), list) else []
    for variant in variants:
        disc = variant.get("discriminator") if isinstance(variant, dict) else None
        if not isinstance(disc, dict) or len(disc) != 1:
            continue
        field, value = next(iter(disc.items()))
        by_disc.setdefault(str(field), []).append(value)

    specs: list[dict[str, Any]] = []
    for field, values in sorted(by_disc.items()):
        value_set = {str(v) for v in values}
        feature = f"{coll}.{field}"
        field_spec = node.get(field)
        if {"present", "missing"} & value_set:
            specs.extend(_present_missing_specs(coll, field, field_spec, node, feature))
        else:
            specs.extend(_polymorphic_specs(coll, field, sorted(value_set), node, feature))
    return specs


def _present_missing_specs(
    coll: str, field: str, field_spec: Any, node: dict[str, Any], feature: str
) -> list[dict[str, Any]]:
    specs = [
        _slot_spec(
            mechanism="sparse_scalar",
            archetype="existence_count",
            reference_oracle={
                "template": "existence_count",
                "params": {"collection": coll, "field": field},
            },
            schema_feature=feature,
            feature_family=(
                "optional_embed" if _is_object_spec(field_spec)
                else "optional_array" if _is_array_object_spec(field_spec)
                else "sparse_scalar"
            ),
            diversity_hint=f"count documents where {field} is present",
        )
    ]
    if _is_object_spec(field_spec):
        nested = _nested_scalar_paths(field_spec)
        useful_nested = [
            (p, spec) for p, spec in nested
            if not _is_identifier_like_field(p)
        ]
        numeric = [
            p for p, spec in useful_nested
            if _is_numeric_spec(spec) and _is_measure_like_field(p)
        ]
        for value_path, spec in useful_nested[:6]:
            specs.append(_slot_spec(
                mechanism="sparse_embed",
                archetype="present_missing_projection",
                reference_oracle={
                    "template": "optional_embed_projection",
                    "params": {
                        "parent_collection": coll,
                        "embed_field": field,
                        "value_path": value_path,
                        "target_field": f"{field}_{_safe_field(value_path)}_or_default",
                        "missing_default": 0 if _is_numeric_spec(spec) else "",
                    },
                },
                schema_feature=feature,
                feature_family="optional_embed",
                diversity_hint=f"project {field}.{value_path} with an explicit missing default",
            ))
        for metric in numeric[:5]:
            path = f"{field}.{metric}"
            for agg in ("sum", "avg", "min", "max"):
                specs.append(_slot_spec(
                    mechanism="sparse_embed",
                    archetype="has_vs_absent_compare",
                    reference_oracle={
                        "template": "has_vs_absent_compare",
                        "params": {
                            "parent_collection": coll,
                            "embed_field": field,
                            "metric_field": path,
                            "agg": agg,
                        },
                    },
                    schema_feature=feature,
                    feature_family="optional_embed",
                    diversity_hint=f"compare {field} present vs missing using {agg}({path})",
                ))
    elif _is_numeric_spec(field_spec) and _is_measure_like_field(field):
        for agg in ("sum", "avg", "min", "max"):
            specs.append(_slot_spec(
                mechanism="sparse_scalar",
                archetype="null_coalesce_agg",
                reference_oracle={
                    "template": "null_coalesce_agg",
                    "params": {"collection": coll, "field": field, "agg": agg, "default": 0},
                },
                schema_feature=feature,
                feature_family="sparse_scalar",
                diversity_hint=f"aggregate sparse scalar {field} with {agg} and missing=0",
            ))
    return specs


def _polymorphic_specs(
    coll: str, discriminator: str, values: list[str], node: dict[str, Any], feature: str
) -> list[dict[str, Any]]:
    variants = node.get("__variants") if isinstance(node.get("__variants"), list) else []
    variant_fields = _variant_field_names(variants, discriminator, values)
    variant_numeric_fields = _variant_measure_fields(variants, discriminator, values)
    variant_specific = sorted({
        field for fields in variant_fields.values() for field in fields
        if field != discriminator
        and not _is_identifier_like_field(field)
    })
    scalar = [
        p for p, _spec in _top_scalar_paths(node)
        if p != discriminator
        and not _is_identifier_like_field(p)
    ]
    projection_fields = _ordered_unique(variant_specific + scalar)
    specs: list[dict[str, Any]] = []
    field_maps = _subtype_measure_field_maps(variant_numeric_fields, values)
    if field_maps:
        for field_by_subtype in field_maps[:3]:
            for agg in ("sum", "avg", "max"):
                specs.append(_slot_spec(
                    mechanism="polymorphic",
                    archetype="per_subtype_agg",
                    reference_oracle={
                        "template": "per_subtype_agg",
                        "params": {
                            "collection": coll,
                            "discriminator": discriminator,
                            "field_by_subtype": field_by_subtype,
                            "agg": agg,
                        },
                    },
                    schema_feature=feature,
                    feature_family="polymorphic",
                    diversity_hint=(
                        f"group {coll} by {discriminator} and {agg} subtype-specific metrics"
                    ),
                ))
            specs.append(_slot_spec(
                mechanism="polymorphic",
                archetype="subtype_cond_projection",
                reference_oracle={
                    "template": "subtype_cond_projection",
                    "params": {
                        "collection": coll,
                        "discriminator": discriminator,
                        "field_by_subtype": field_by_subtype,
                        "target_field": f"{_safe_field(discriminator)}_metric_by_subtype",
                        "default": 0,
                    },
                },
                schema_feature=feature,
                feature_family="polymorphic",
                diversity_hint=(
                    f"preserve docs and attach a metric chosen by {discriminator} subtype"
                ),
                ))
    for value in values[:6]:
        for field in projection_fields[:6]:
            specs.append(_slot_spec(
                mechanism="polymorphic",
                archetype="subtype_specific_field",
                reference_oracle={
                    "template": "subtype_specific_field",
                    "params": {
                        "collection": coll,
                        "discriminator": discriminator,
                        "subtype_value": value,
                        "field": field,
                        "project": [discriminator, field],
                    },
                },
                schema_feature=feature,
                feature_family="polymorphic",
                diversity_hint=f"read field {field} only for subtype {discriminator}={value}",
            ))
    return specs


def _array_slot_specs(coll: str, node: dict[str, Any]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for field, spec in sorted(node.items()):
        if field.startswith("__") or not _is_array_object_spec(spec):
            continue
        item = spec.get("items", {}) if isinstance(spec, dict) else {}
        item_node = item.get("fields", item) if isinstance(item, dict) else {}
        item_fields = _top_scalar_paths(item_node if isinstance(item_node, dict) else {})
        group_fields = [
            p for p, s in item_fields
            if not _is_numeric_spec(s) and not _is_identifier_like_field(p)
        ] or [
            p for p, _s in item_fields
            if not _is_identifier_like_field(p)
        ]
        numeric_fields = [
            p for p, s in item_fields
            if _is_numeric_spec(s)
            and _is_measure_like_field(p)
            and not _is_identifier_like_field(p)
        ]
        for group_by in group_fields[:5]:
            specs.append(_slot_spec(
                mechanism="none",
                archetype="join_nested_group",
                reference_oracle={
                    "template": "join_nested_group",
                    "params": {"collection": coll, "array_field": field, "group_by": group_by},
            },
            schema_feature=f"{coll}.{field}[]",
            feature_family="nested_array",
            diversity_hint=f"unwind nested array {field} and count by {group_by}",
        ))
            for value_field in numeric_fields[:4]:
                for agg in ("sum", "avg", "max"):
                    specs.append(_slot_spec(
                        mechanism="none",
                        archetype="join_nested_group",
                        reference_oracle={
                            "template": "join_nested_group",
                            "params": {
                                "collection": coll,
                                "array_field": field,
                                "group_by": group_by,
                                "value_field": value_field,
                                "agg": agg,
                            },
                        },
                        schema_feature=f"{coll}.{field}[]",
                        feature_family="nested_array",
                        diversity_hint=f"unwind {field}, group by {group_by}, {agg}({value_field})",
                    ))
    return specs


def _fk_rollup_slot_specs(schema: dict[str, Any], data: dict[str, Any]) -> list[dict[str, Any]]:
    """Suggest LLM-designed multi-collection rollups from real id-like relationships."""
    collections = _schema_collections(schema)
    specs: list[dict[str, Any]] = []
    for child, child_node in sorted(collections.items()):
        if not isinstance(child_node, dict):
            continue
        child_scalars = _top_scalar_paths(child_node)
        child_fields = {field for field, _spec in child_scalars}
        numeric_measures = [
            field for field, spec in child_scalars
            if _is_numeric_spec(spec)
            and _is_measure_like_field(field)
            and not _is_identifier_like_field(field)
        ]
        categorical = [
            field for field, spec in child_scalars
            if not _is_numeric_spec(spec)
            and not _is_identifier_like_field(field)
        ]
        for parent, parent_node in sorted(collections.items()):
            if child == parent or not isinstance(parent_node, dict):
                continue
            parent_fields = {field for field, _spec in _top_scalar_paths(parent_node)}
            for parent_key, foreign_key in _relationship_key_pairs(
                parent, parent_fields, child_fields
            ):
                specs.append(_slot_spec(
                    mechanism="none",
                    archetype="fk_rollup",
                    reference_oracle={
                        "template": "fk_rollup",
                        "params": {
                            "parent_collection": parent,
                            "child_collection": child,
                            "parent_key": parent_key,
                            "foreign_key": foreign_key,
                            "agg": "count",
                        },
                    },
                    schema_feature=f"{parent}.{parent_key}->{child}.{foreign_key}",
                    feature_family="cross_collection",
                    diversity_hint=(
                        f"roll up {child} rows to each {parent} via "
                        f"{child}.{foreign_key}={parent}.{parent_key}"
                    ),
                ))
                for value_field in numeric_measures[:3]:
                    for agg in ("sum", "avg", "max"):
                        specs.append(_slot_spec(
                            mechanism="none",
                            archetype="fk_rollup",
                            reference_oracle={
                                "template": "fk_rollup",
                                "params": {
                                    "parent_collection": parent,
                                    "child_collection": child,
                                    "parent_key": parent_key,
                                    "foreign_key": foreign_key,
                                    "agg": agg,
                                    "value_field": value_field,
                                },
                            },
                            schema_feature=f"{parent}.{parent_key}->{child}.{foreign_key}",
                            feature_family="cross_collection",
                            diversity_hint=(
                                f"compute per-{parent} {agg} of {child}.{value_field} "
                                f"through a real foreign-key rollup"
                            ),
                        ))
                    for match_field in categorical[:2]:
                        for match_value in _sample_values(data.get(child, []), match_field, limit=3):
                            specs.append(_slot_spec(
                                mechanism="none",
                                archetype="fk_rollup",
                                reference_oracle={
                                    "template": "fk_rollup",
                                    "params": {
                                        "parent_collection": parent,
                                        "child_collection": child,
                                        "parent_key": parent_key,
                                        "foreign_key": foreign_key,
                                        "agg": "sum",
                                        "value_field": value_field,
                                        "match": {"field": match_field, "value": match_value},
                                    },
                                },
                                schema_feature=(
                                    f"{parent}.{parent_key}->{child}.{foreign_key}"
                                    f"[{match_field}]"
                                ),
                                feature_family="cross_collection",
                                diversity_hint=(
                                    f"sum {child}.{value_field} per {parent} after filtering "
                                    f"{child}.{match_field}={match_value!r}"
                                ),
                            ))
    return specs


def _relationship_key_pairs(
    parent: str, parent_fields: set[str], child_fields: set[str]
) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    parent_id = f"{parent}_id"
    if parent_id in parent_fields and parent_id in child_fields:
        pairs.append((parent_id, parent_id))
    if "_id" in parent_fields and parent_id in child_fields:
        pairs.append(("_id", parent_id))
    return _ordered_unique_pairs(pairs)


def _generic_slot_specs(
    coll: str,
    node: dict[str, Any],
    docs: Any = None,
    *,
    sparse_scalar_fields: set[str] | None = None,
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    sparse_scalar_fields = sparse_scalar_fields or set()
    scalars = _top_scalar_paths(node)
    numeric = [
        p for p, spec in scalars
        if _is_numeric_spec(spec) and not _is_identifier_like_field(p)
    ]
    categorical = [
        p for p, spec in scalars
        if not _is_numeric_spec(spec) and not _is_identifier_like_field(p)
    ]
    for field in categorical[:10]:
        specs.append(_slot_spec(
            mechanism="none",
            archetype="group_count",
            reference_oracle={
                "template": "group_count",
                "params": {"collection": coll, "group_by": field},
            },
            schema_feature=f"{coll}.{field}",
            diversity_hint=f"group baseline documents by {field}",
        ))
        for value in _sample_values(docs, field, limit=10):
            specs.append(_slot_spec(
                mechanism="none",
                archetype="simple_filter",
                reference_oracle={
                    "template": "simple_filter",
                    "params": {
                        "collection": coll,
                        "predicates": [{"field": field, "op": "eq", "value": value}],
                        "project": ["_id", field],
                    },
                },
                schema_feature=f"{coll}.{field}",
                diversity_hint=f"filter {field} to a real value {value!r}",
            ))
    for field in numeric[:10]:
        if (
            field in sparse_scalar_fields
            and _is_measure_like_field(field)
            and not _is_identifier_like_field(field)
        ):
            for agg in ("sum", "avg", "min", "max"):
                specs.append(_slot_spec(
                    mechanism="sparse_scalar",
                    archetype="null_coalesce_agg",
                    reference_oracle={
                        "template": "null_coalesce_agg",
                        "params": {"collection": coll, "field": field, "agg": agg, "default": 0},
                    },
                    schema_feature=f"{coll}.{field}",
                    feature_family="sparse_scalar",
                    diversity_hint=f"{agg} sparse numeric field {field} with explicit missing default",
                ))
        for order in ("desc", "asc"):
            for n in (3, 5, 10):
                specs.append(_slot_spec(
                    mechanism="none",
                    archetype="topn",
                    reference_oracle={
                        "template": "topn",
                        "params": {
                            "collection": coll,
                            "sort_key": field,
                            "n": n,
                            "order": order,
                            "project": ["_id", field],
                            "nulls": "last",
                        },
                    },
                    schema_feature=f"{coll}.{field}",
                    diversity_hint=f"top {n} documents by {field} ordered {order}",
                ))
    return specs


def _sample_values(docs: Any, field: str, *, limit: int) -> list[Any]:
    if not isinstance(docs, list) or limit <= 0:
        return []
    seen: set[str] = set()
    values: list[Any] = []
    for doc in docs:
        if not isinstance(doc, dict) or field not in doc:
            continue
        value = doc.get(field)
        if value is None or isinstance(value, (dict, list)):
            continue
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        values.append(value)
        if len(values) >= limit:
            break
    return values


def _top_scalar_paths(node: dict[str, Any]) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for field, spec in sorted(node.items()):
        if field.startswith("__") or isinstance(spec, dict):
            continue
        out.append((field, spec))
    return out


def _nested_scalar_paths(spec: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if not isinstance(spec, dict):
        return []
    fields = spec.get("fields") if spec.get("type") == "OBJECT" else spec
    if not isinstance(fields, dict):
        return []
    out: list[tuple[str, Any]] = []
    for field, child in sorted(fields.items()):
        if field.startswith("__"):
            continue
        path = f"{prefix}.{field}" if prefix else field
        if isinstance(child, dict) and child.get("type") == "OBJECT":
            out.extend(_nested_scalar_paths(child, path))
        elif not isinstance(child, dict):
            out.append((path, child))
    return out


def _is_object_spec(spec: Any) -> bool:
    return isinstance(spec, dict) and spec.get("type") == "OBJECT"


def _is_array_object_spec(spec: Any) -> bool:
    if not isinstance(spec, dict) or spec.get("type") != "ARRAY":
        return False
    item = spec.get("items")
    return isinstance(item, dict) and item.get("type") == "OBJECT"


def _is_numeric_spec(spec: Any) -> bool:
    if not isinstance(spec, str):
        return False
    return spec.upper() in {"INT", "INTEGER", "REAL", "FLOAT", "DOUBLE", "DECIMAL", "NUMBER"}


def _is_identifier_like_field(field: str) -> bool:
    leaf = str(field).split(".")[-1].lower()
    if leaf in {"_id", "id", "account", "account_to", "disp_id"}:
        return True
    return leaf.endswith("_id") or leaf.endswith(" id")


def _is_measure_like_field(field: str) -> bool:
    leaf = str(field).split(".")[-1].lower()
    if _is_identifier_like_field(leaf):
        return False
    if len(leaf) > 1 and leaf[0] == "a" and leaf[1:].isdigit():
        return True
    return any(
        token in leaf
        for token in (
            "amount",
            "balance",
            "payment",
            "value",
            "total",
            "sum",
            "cost",
            "price",
            "rate",
            "ratio",
            "salary",
            "score",
            "count",
        )
    )


def _variant_field_names(
    variants: list[Any], discriminator: str, values: list[str]
) -> dict[str, set[str]]:
    wanted = {str(value) for value in values}
    out: dict[str, set[str]] = {value: set() for value in wanted}
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        disc = variant.get("discriminator")
        if not isinstance(disc, dict) or str(disc.get(discriminator)) not in wanted:
            continue
        value = str(disc.get(discriminator))
        fields = variant.get("fields")
        if isinstance(fields, dict):
            out.setdefault(value, set()).update(
                str(field) for field in fields if not str(field).startswith("__")
            )
    return out


def _variant_measure_fields(
    variants: list[Any], discriminator: str, values: list[str]
) -> dict[str, list[str]]:
    wanted = {str(value) for value in values}
    out: dict[str, list[str]] = {value: [] for value in wanted}
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        disc = variant.get("discriminator")
        if not isinstance(disc, dict) or str(disc.get(discriminator)) not in wanted:
            continue
        value = str(disc.get(discriminator))
        fields = variant.get("fields")
        if not isinstance(fields, dict):
            continue
        measures = [
            str(field)
            for field, spec in sorted(fields.items())
            if _is_numeric_spec(spec)
            and _is_measure_like_field(str(field))
            and not _is_identifier_like_field(str(field))
        ]
        out[value] = _ordered_unique(out.get(value, []) + measures)
    return out


def _subtype_measure_field_maps(
    by_value: dict[str, list[str]], values: list[str]
) -> list[dict[str, str]]:
    usable_values = [value for value in values if by_value.get(str(value))]
    if len(usable_values) < 2:
        return []
    maps: list[dict[str, str]] = []
    max_width = max(len(by_value[str(value)]) for value in usable_values)
    for index in range(max_width):
        field_map = {
            str(value): by_value[str(value)][index % len(by_value[str(value)])]
            for value in usable_values
        }
        if len(set(field_map.values())) > 1:
            maps.append(field_map)
    return maps


def _ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _ordered_unique_pairs(values: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _safe_field(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value))
    return "_".join(part for part in cleaned.split("_") if part)[:48] or "field"


def _resolve_construct_records(source: BirdSource, db_ids: list[str], value: str) -> int:
    raw = value.strip().lower()
    if raw == "all":
        census = run_census(source, db_ids=db_ids)
        total = sum(db.query_count for db in census.databases.values())
        if total <= 0:
            raise ValueError(f"no source workload records found for dbs={db_ids}")
        return total
    try:
        n_records = int(raw)
    except ValueError as exc:
        raise ValueError("--records must be a positive integer or 'all'") from exc
    if n_records <= 0:
        raise ValueError("--records must be positive")
    return n_records


async def _run_artifact_diversity_phase_b(
    rt: Runtime,
    artifacts: dict[str, DbArtifacts],
    *,
    n_records: int,
    records_per_db: int | None,
) -> tuple[list[dict], int, dict[str, int], dict[str, int]]:
    """Run Phase B with artifact-seeded slots, refilling drops with unused seeds."""
    targets = _artifact_target_counts(
        artifacts,
        n_records,
        records_per_db=records_per_db,
    )
    pool_sizes = {
        db_id: len(_artifact_slot_pool(artifact, seed=rt.settings.seed))
        for db_id, artifact in sorted(artifacts.items())
    }
    rt.log.info(
        "artifact_diversity_plan",
        targets=targets,
        records_per_db=records_per_db,
        pool_sizes=pool_sizes,
        dbs=sorted(artifacts),
    )
    if not targets or not any(pool_sizes.values()):
        return [], 0, targets, pool_sizes

    records: list[dict] = []
    attempts_by_db: dict[str, int] = {db_id: 0 for db_id in targets}
    seen_mql: dict[tuple[str, str], int] = {}
    seen_skeleton: dict[tuple[str, str], list[int]] = {}
    seen_canonical_nl: dict[tuple[str, str], int] = {}
    seen_nl_mql_pair: dict[tuple[str, str, str], int] = {}
    total_slots = 0
    next_record_id = 1001
    batch = 0

    while True:
        built_by_db = Counter(str(record.get("db_id")) for record in records)
        remaining = {
            db_id: target - built_by_db.get(db_id, 0)
            for db_id, target in targets.items()
            if target > built_by_db.get(db_id, 0)
            and attempts_by_db.get(db_id, 0) < pool_sizes.get(db_id, 0)
        }
        if not remaining:
            break

        slots = _artifact_diversity_slots_for_targets(
            artifacts,
            remaining,
            seed=rt.settings.seed,
            start_record_id=next_record_id,
            per_db_start_index=attempts_by_db,
        )
        if not slots:
            break
        batch += 1
        total_slots += len(slots)
        next_record_id = max(slot.record_id for slot in slots) + 1
        for slot in slots:
            attempts_by_db[slot.db_id] = max(
                attempts_by_db.get(slot.db_id, 0),
                slot.slot_index + 1,
            )

        unique_keys = len({slot.diversity_key for slot in slots if slot.diversity_key})
        rt.log.info(
            "artifact_diversity_batch",
            batch=batch,
            slots=len(slots),
            remaining_targets=remaining,
            attempts_by_db=attempts_by_db,
            unique_diversity_keys=unique_keys,
        )
        before = len(records)
        records.extend(
            await run_phase_b(
                rt.workflow,
                artifacts,
                slots,
                seen_mql=seen_mql,
                seen_skeleton=seen_skeleton,
                seen_canonical_nl=seen_canonical_nl,
                seen_nl_mql_pair=seen_nl_mql_pair,
            )
        )
        built_this_batch = len(records) - before
        dropped_this_batch = len(slots) - built_this_batch
        yield_ratio = built_this_batch / len(slots) if slots else 0.0
        rt.log.info(
            "artifact_diversity_batch_done",
            batch=batch,
            slots=len(slots),
            built_records=built_this_batch,
            dropped_records=dropped_this_batch,
            yield_ratio=round(yield_ratio, 4),
            total_records=len(records),
            built_by_db=dict(Counter(str(record.get("db_id")) for record in records)),
        )
        if slots and yield_ratio < 0.25:
            rt.log.warning(
                "artifact_diversity_low_yield",
                batch=batch,
                slots=len(slots),
                built_records=built_this_batch,
                dropped_records=dropped_this_batch,
                yield_ratio=round(yield_ratio, 4),
                remaining_targets=remaining,
                attempts_by_db=attempts_by_db,
            )

    final_built_by_db = Counter(str(record.get("db_id")) for record in records)
    final_diversity = _phase_b_record_diversity(records)
    reserved_skeleton_family_sizes = [len(record_ids) for record_ids in seen_skeleton.values()]
    rt.log.info(
        "artifact_diversity_ledger_summary",
        total_slots=total_slots,
        built_records=len(records),
        built_by_db=dict(final_built_by_db),
        **final_diversity,
        reserved_mql=len(seen_mql),
        reserved_mql_skeletons=len(seen_skeleton),
        reserved_canonical_nl=len(seen_canonical_nl),
        reserved_nl_mql_pairs=len(seen_nl_mql_pair),
        reserved_max_mql_skeleton_family=max(reserved_skeleton_family_sizes, default=0),
    )
    shortfalls = {
        db_id: target - final_built_by_db.get(db_id, 0)
        for db_id, target in targets.items()
        if target > final_built_by_db.get(db_id, 0)
    }
    if shortfalls:
        rt.log.warning(
            "artifact_diversity_supply_shortfall",
            shortfalls=shortfalls,
            targets=targets,
            attempts_by_db=attempts_by_db,
            pool_sizes=pool_sizes,
        )
    return records, total_slots, targets, pool_sizes


def _phase_b_record_diversity(records: list[dict[str, Any]]) -> dict[str, int]:
    mql_sigs: set[tuple[str, str]] = set()
    skeleton_families: Counter[tuple[str, str]] = Counter()
    canonical_sigs: set[tuple[str, str]] = set()
    pair_sigs: set[tuple[str, str, str]] = set()

    for record in records:
        db_id = str(record.get("db_id") or "")
        mql = record.get("MQL")
        mql_sig = str(record.get("mql_signature") or "")
        if not mql_sig and isinstance(mql, str) and mql.strip():
            mql_sig = mql_signature(mql)
        skeleton_sig = str(record.get("mql_skeleton_signature") or "")
        if not skeleton_sig and isinstance(mql, str) and mql.strip():
            skeleton_sig = mql_skeleton_signature(mql)
        if mql_sig:
            mql_sigs.add((db_id, mql_sig))
        if skeleton_sig:
            skeleton_families[(db_id, skeleton_sig)] += 1

        nl_queries = record.get("nl_queries")
        canonical = (
            " ".join(str(nl_queries.get("canonical") or "").strip().lower().split())
            if isinstance(nl_queries, dict)
            else ""
        )
        if canonical:
            nl_sig = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            canonical_sigs.add((db_id, nl_sig))
            if mql_sig:
                pair_sigs.add((db_id, nl_sig, mql_sig))

    return {
        "distinct_mql": len(mql_sigs),
        "distinct_mql_skeletons": len(skeleton_families),
        "distinct_canonical_nl": len(canonical_sigs),
        "distinct_nl_mql_pairs": len(pair_sigs),
        "max_mql_skeleton_family": max(skeleton_families.values(), default=0),
    }


async def _run_construct(
    rt: Runtime,
    db_ids: list[str],
    phase: str,
    n_records: int,
    *,
    structural_only_records: bool = False,
    structural_fraction: float = 0.0,
    records_per_db: int | None = None,
    construction_mode: str = "legacy",
) -> int:
    if construction_mode == "native":
        return await _run_native_construct(
            rt,
            db_ids,
            phase,
            n_records,
            records_per_db=records_per_db,
        )
    if construction_mode != "legacy":
        raise ValueError(f"unknown construction mode: {construction_mode}")

    out_dir = rt.settings.paths.dataset_out
    artifacts: dict[str, DbArtifacts] = {}
    records: list[dict] = []
    failed: TendError | None = None
    summary: dict = {}
    try:
        with rt.progress:
            if phase in ("A", "all"):
                artifacts = await run_phase_a(rt.workflow, db_ids)
                write_phase_a(out_dir, artifacts)
                write_catalog(out_dir, artifacts)
                rt.log.info("phase_a_complete", dbs=sorted(artifacts),
                            signatures={d: a.world_signature for d, a in artifacts.items()})
            if phase in ("B", "all"):
                if not artifacts:
                    rt.log.anomaly(
                        kind=Anomaly.INTERNAL,
                        message="phase B requested without Phase A artifacts",
                        phase=phase,
                        requested_records=n_records,
                    )
                else:
                    if rt.source is None:
                        raise RuntimeError("construct requires a BIRD source")
                    slot_db_ids = sorted(artifacts)
                    non_query_bearing = sorted(
                        db_id for db_id, art in artifacts.items() if not art.query_bearing
                    )
                    if non_query_bearing:
                        rt.log.warning(
                            "phase_b_non_query_bearing_advisory",
                            dbs=non_query_bearing,
                            reason=(
                                "phase A SC marked artifacts as non query-bearing; "
                                "Phase B still uses deterministic census supply"
                            ),
                        )
                    records, slot_count, target_counts, pool_sizes = (
                        await _run_artifact_diversity_phase_b(
                            rt,
                            artifacts,
                            n_records=n_records,
                            records_per_db=records_per_db,
                        )
                    )
                    if slot_count == 0:
                        rt.log.warning(
                            "artifact_diversity_plan_empty",
                            dbs=slot_db_ids,
                            reason="no schema-derived oracle seed pool; falling back to census",
                        )
                        slots = _coverage_slots_for(
                            rt.source,
                            slot_db_ids,
                            n_records,
                            seed=rt.settings.seed,
                            structural_only=structural_only_records,
                            structural_fraction=structural_fraction,
                        )
                        slot_count = len(slots)
                        records = await run_phase_b(rt.workflow, artifacts, slots)
                    write_records(out_dir, records)
                    rt.log.info(
                        "phase_b_complete",
                        records=len(records),
                        slots=slot_count,
                        requested_records=n_records,
                        target_counts=target_counts,
                        pool_sizes=pool_sizes,
                    )
                    if len(records) < n_records:
                        rt.log.anomaly(
                            kind=Anomaly.SUPPLY_EXHAUSTED,
                            message="phase B record target not met",
                            requested_records=n_records,
                            built_records=len(records),
                        )
    except TendError as err:
        failed = err
        if not err.logged:
            rt.log.anomaly(err)
        rt.log.error("run_failed", error_type=type(err).__name__, message=err.message,
                     anomaly=err.anomaly.value if err.anomaly else None)
    except Exception as exc:  # noqa: BLE001 - final CLI boundary
        failed = wrap_unexpected(exc, stage="construct")
        rt.log.anomaly(failed)
        rt.log.error("run_failed", error_type=type(failed).__name__, message=failed.message,
                     anomaly=failed.anomaly.value if failed.anomaly else None)
    finally:
        summary = rt.progress.summary() if hasattr(rt.progress, "summary") else {}
        failed_run = failed is not None or summary.get("anomaly_total", 0) > 0
        rt.log.info("run_done", status="failed" if failed_run else "ok", **summary,
                    dbs=len(artifacts), records=len(records))
        _print_summary(rt, artifacts, records, summary, out_dir)
        _close_runtime(rt)

    return 1 if failed or summary.get("anomaly_total", 0) else 0


async def _run_native_construct(
    rt: Runtime,
    db_ids: list[str],
    phase: str,
    n_records: int,
    *,
    records_per_db: int | None = None,
) -> int:
    out_dir = rt.settings.paths.dataset_out
    artifacts: dict[str, NativeDbArtifacts] = {}
    records: list[dict[str, Any]] = []
    failed: TendError | None = None
    summary: dict[str, Any] = {}
    slot_count = 0
    try:
        with rt.progress:
            if phase in ("A", "all"):
                artifacts = await run_native_phase_a(rt.workflow, db_ids)
                write_native_phase_a(out_dir, artifacts)
                rt.log.info(
                    "native_phase_a_complete",
                    dbs=sorted(artifacts),
                    signatures={db_id: art.world_signature for db_id, art in artifacts.items()},
                    feature_counts={
                        db_id: len(getattr(art.native_feature_manifest, "features", []) or [])
                        for db_id, art in artifacts.items()
                    },
                )
            if phase in ("B", "all"):
                if not artifacts:
                    rt.log.anomaly(
                        kind=Anomaly.INTERNAL,
                        message="native phase B requested without Phase A artifacts",
                        phase=phase,
                        requested_records=n_records,
                    )
                else:
                    manifests = [art.native_feature_manifest for art in artifacts.values()]
                    slot_cap = records_per_db if records_per_db is not None else None
                    slots = plan_native_slots(
                        manifests,
                        n_records,
                        seed=rt.settings.seed,
                        records_per_db=slot_cap,
                    )
                    slot_count = len(slots)
                    rt.log.info(
                        "native_slot_plan",
                        requested_records=n_records,
                        slots=slot_count,
                        dbs=sorted(artifacts),
                        records_per_db=records_per_db,
                    )
                    records = await run_native_phase_b(rt.workflow, artifacts, slots)
                    write_records(out_dir, records)
                    built_by_db = dict(Counter(str(record.get("db_id")) for record in records))
                    rt.log.info(
                        "native_release_summary",
                        requested_records=n_records,
                        built_records=len(records),
                        slots=slot_count,
                        built_by_db=built_by_db,
                    )
                    if len(records) < n_records:
                        rt.log.anomaly(
                            kind=Anomaly.SUPPLY_EXHAUSTED,
                            message="native Phase B record target not met",
                            requested_records=n_records,
                            built_records=len(records),
                        )
    except TendError as err:
        failed = err
        if not err.logged:
            rt.log.anomaly(err)
        rt.log.error(
            "run_failed",
            error_type=type(err).__name__,
            message=err.message,
            anomaly=err.anomaly.value if err.anomaly else None,
        )
    except Exception as exc:  # noqa: BLE001 - final CLI boundary
        failed = wrap_unexpected(exc, stage="construct.native")
        rt.log.anomaly(failed)
        rt.log.error(
            "run_failed",
            error_type=type(failed).__name__,
            message=failed.message,
            anomaly=failed.anomaly.value if failed.anomaly else None,
        )
    finally:
        summary = rt.progress.summary() if hasattr(rt.progress, "summary") else {}
        failed_run = failed is not None or summary.get("anomaly_total", 0) > 0
        rt.log.info(
            "run_done",
            status="failed" if failed_run else "ok",
            **summary,
            dbs=len(artifacts),
            records=len(records),
            construction_mode="native",
        )
        _print_summary(rt, artifacts, records, summary, out_dir)
        _close_runtime(rt)

    return 1 if failed or summary.get("anomaly_total", 0) else 0


async def _preload_solver_witnesses(
    rt: Runtime,
    inputs: list[tuple[dict, dict, dict | None]],
) -> set[str]:
    """Load each db witness once before record-level solver fan-out."""
    if rt.settings.stub or rt.mongo is None or not rt.mongo.available():
        return set()
    by_db: dict[str, dict] = {}
    for record, _schema, data in inputs:
        db = str(record.get("db_id"))
        if db and data and db not in by_db:
            by_db[db] = data
    if not by_db:
        return set()

    async def load_one(db: str, data: dict) -> str:
        await asyncio.to_thread(rt.mongo.load_witness, db, data)
        return db

    loaded = await asyncio.gather(
        *(load_one(db, data) for db, data in sorted(by_db.items()))
    )
    rt.log.info("solver_witness_preloaded", db_ids=loaded, db_count=len(loaded))
    return set(loaded)


async def _evaluate_outputs(
    rt: Runtime,
    *,
    dataset_dir: Path,
    predictions_path: Path,
    experiment_kind: str,
    out_dir: Path | None = None,
    max_workers: int = 8,
) -> EvaluationOutput:
    """Run proposal-05 evaluation while keeping progress and logs in the run."""
    if out_dir is None:
        out_dir = rt.settings.run_dir / "evaluation" / experiment_kind
    return await asyncio.to_thread(
        evaluate_predictions,
        dataset_dir=dataset_dir,
        predictions_path=predictions_path,
        out_dir=out_dir,
        experiment_kind=experiment_kind,
        run_id=rt.settings.run_id,
        logger=rt.log,
        progress=rt.progress,
        executor=rt.mongo,
        max_workers=max_workers,
    )


async def _maybe_evaluate(
    rt: Runtime,
    *,
    predictions: list[dict],
    predictions_path: Path,
    dataset_dir: Path,
    experiment_kind: str,
    evaluate: bool,
    eval_out_dir: Path | None,
    eval_workers: int,
) -> EvaluationOutput | None:
    """Run post-run evaluation on the success path, or ``None`` when not applicable.

    Returns ``None`` when evaluation is disabled (``--no-eval``) or there are no
    predictions to score. Runs under the already-entered progress context, so the
    caller must advance ``rt.progress.phase("EVAL")`` before calling.
    """
    if not evaluate or not predictions:
        return None
    return await _evaluate_outputs(
        rt,
        dataset_dir=dataset_dir,
        predictions_path=predictions_path,
        experiment_kind=experiment_kind,
        out_dir=eval_out_dir,
        max_workers=eval_workers,
    )


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    """Write ``rows`` one JSON object per line, creating parents; no-op when empty."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        for row in rows:
            fp.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


async def _run_solve(
    rt: Runtime,
    *,
    dataset_dir: Path,
    db_id: str | None,
    record_id: int | None,
    limit: int,
    r_max: int,
    witness_k: int,
    nlq: str | None = None,
    evaluate: bool = True,
    eval_out_dir: Path | None = None,
    eval_workers: int = 8,
) -> int:
    predictions: list[dict] = []
    failures: list[dict] = []
    failed: TendError | None = None
    evaluation: EvaluationOutput | None = None
    summary: dict = {}
    out_path = rt.settings.run_dir / "solver_predictions.jsonl"
    failures_path = rt.settings.run_dir / "solver_failures.jsonl"
    evaluate_outputs = evaluate and nlq is None
    try:
        with rt.progress:
            rt.workflow.phase("SOLVE")
            if nlq is not None:
                if not db_id:
                    raise SourceError("NLQ+DB solver mode requires --db-id")
                result = await smart_solve_nlq_db(
                    rt.workflow,
                    db_id=str(db_id),
                    nlq=nlq,
                    record_id=record_id,
                    r_max=r_max,
                    witness_k=witness_k,
                )
                payload = result.to_json()
                payload["batch_index"] = 0
                payload["work_item_id"] = f"solve:0:{db_id}:{record_id}"
                if payload.get("result_type") == "solver_failure":
                    failures.append(payload)
                else:
                    predictions.append(payload)
            else:
                inputs = load_solver_release_inputs(
                    dataset_dir,
                    db_id=db_id,
                    record_id=record_id,
                    limit=limit,
                )
                if not inputs:
                    rt.log.anomaly(
                        kind=Anomaly.SUPPLY_EXHAUSTED,
                        message="no solver records matched filters",
                        dataset_dir=str(dataset_dir),
                        db_id=db_id,
                        record_id=record_id,
                    )
                preloaded_dbs = await _preload_solver_witnesses(rt, inputs)

                async def solve_one(
                    batch_index: int,
                    record: dict,
                    schema: dict,
                    data: dict | None,
                ) -> tuple[int, dict]:
                    db = str(record.get("db_id"))
                    result = await smart_solve_record(
                        rt.workflow,
                        record,
                        schema,
                        local_data=data,
                        r_max=r_max,
                        witness_k=witness_k,
                        options=SmartSolveOptions(
                            progress_work_item_id=f"batch_index={batch_index}",
                        ),
                        witness_preloaded=db in preloaded_dbs,
                    )
                    payload = result.to_json()
                    payload["batch_index"] = batch_index
                    payload["work_item_id"] = f"solve:{batch_index}:{db}:{record.get('record_id')}"
                    return batch_index, payload

                tasks = [
                    asyncio.create_task(solve_one(index, record, schema, data))
                    for index, (record, schema, data) in enumerate(inputs)
                ]
                try:
                    solved = await asyncio.gather(*tasks)
                except Exception:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for exc in results:
                        if isinstance(exc, Exception) and not isinstance(exc, asyncio.CancelledError):
                            rt.log.anomaly(wrap_unexpected(exc, stage="solve_gather"))
                    raise
                for _, payload in sorted(solved, key=lambda item: item[0]):
                    if payload.get("result_type") == "solver_failure":
                        failures.append(payload)
                    else:
                        predictions.append(payload)
            _write_jsonl(out_path, predictions)
            _write_jsonl(failures_path, failures)
            if predictions and evaluate_outputs:
                rt.progress.phase("EVAL")
                evaluation = await _maybe_evaluate(
                    rt,
                    predictions=predictions,
                    predictions_path=out_path,
                    dataset_dir=dataset_dir,
                    experiment_kind="solver",
                    evaluate=evaluate_outputs,
                    eval_out_dir=eval_out_dir,
                    eval_workers=eval_workers,
                )
    except TendError as err:
        failed = err
        if not err.logged:
            rt.log.anomaly(err)
        rt.log.error("solver_run_failed", error_type=type(err).__name__,
                     message=err.message, anomaly=err.anomaly.value if err.anomaly else None)
    except Exception as exc:  # noqa: BLE001 - final CLI boundary
        failed = wrap_unexpected(exc, stage="solve")
        rt.log.anomaly(failed)
        rt.log.error("solver_run_failed", error_type=type(failed).__name__,
                     message=failed.message,
                     anomaly=failed.anomaly.value if failed.anomaly else None)
    finally:
        summary = rt.progress.summary() if hasattr(rt.progress, "summary") else {}
        failed_run = (
            failed is not None
            or bool(failures)
            or not predictions
            or (evaluation is not None and not evaluation.ok)
            or summary.get("anomaly_total", 0) > 0
        )
        rt.log.info("solver_run_done", status="failed" if failed_run else "ok",
                    predictions=len(predictions), failures=len(failures),
                    output=str(out_path), failures_output=str(failures_path), **summary)
        _print_solve_summary(
            rt,
            predictions,
            failures,
            summary,
            out_path,
            failures_path,
            evaluation,
            evaluate=evaluate_outputs,
        )
        _close_runtime(rt)

    return 1 if failed_run else 0


async def _run_baseline(
    rt: Runtime,
    *,
    dataset_dir: Path,
    baselines: str,
    db_id: str | None,
    record_id: int | None,
    limit: int,
    witness_k: int,
    evaluate: bool = True,
    eval_out_dir: Path | None = None,
    eval_workers: int = 8,
) -> int:
    outputs: list[dict] = []
    predictions: list[dict] = []
    failures: list[dict] = []
    failed: TendError | None = None
    evaluation: EvaluationOutput | None = None
    summary: dict = {}
    out_path = rt.settings.run_dir / "baseline_predictions.jsonl"
    failures_path = rt.settings.run_dir / "baseline_failures.jsonl"
    try:
        with rt.progress:
            outputs = await run_baseline_suite(
                rt.workflow,
                dataset_dir=dataset_dir,
                baseline_selection=baselines,
                db_id=db_id,
                record_id=record_id,
                limit=limit,
                witness_k=witness_k,
            )
            predictions = [item for item in outputs if item.get("status") == "ok"]
            failures = [item for item in outputs if item.get("status") != "ok"]
            _write_jsonl(out_path, predictions)
            _write_jsonl(failures_path, failures)
            if predictions:
                rt.progress.phase("EVAL")
                evaluation = await _maybe_evaluate(
                    rt,
                    predictions=predictions,
                    predictions_path=out_path,
                    dataset_dir=dataset_dir,
                    experiment_kind="baseline",
                    evaluate=evaluate,
                    eval_out_dir=eval_out_dir,
                    eval_workers=eval_workers,
                )
    except TendError as err:
        failed = err
        if not err.logged:
            rt.log.anomaly(err)
        rt.log.error("baseline_run_failed", error_type=type(err).__name__,
                     message=err.message, anomaly=err.anomaly.value if err.anomaly else None)
    except Exception as exc:  # noqa: BLE001 - final CLI boundary
        failed = wrap_unexpected(exc, stage="baseline")
        rt.log.anomaly(failed)
        rt.log.error("baseline_run_failed", error_type=type(failed).__name__,
                     message=failed.message,
                     anomaly=failed.anomaly.value if failed.anomaly else None)
    finally:
        summary = rt.progress.summary() if hasattr(rt.progress, "summary") else {}
        failed_run = (
            failed is not None
            or not predictions
            or bool(failures)
            or (evaluation is not None and not evaluation.ok)
            or summary.get("anomaly_total", 0) > 0
        )
        rt.log.info("baseline_run_done", status="failed" if failed_run else "ok",
                    outputs=len(outputs), predictions=len(predictions),
                    failures=len(failures), output=str(out_path),
                    failures_output=str(failures_path), **summary)
        _print_baseline_summary(
            rt,
            predictions,
            failures,
            summary,
            out_path,
            failures_path,
            evaluation,
            evaluate=evaluate,
        )
        _close_runtime(rt)

    return 1 if failed_run else 0


async def _run_ablation(
    rt: Runtime,
    *,
    dataset_dir: Path,
    ablations: str,
    db_id: str | None,
    record_id: int | None,
    limit: int,
    r_max: int,
    witness_k: int,
    evaluate: bool = True,
    eval_out_dir: Path | None = None,
    eval_workers: int = 8,
) -> int:
    outputs: list[dict] = []
    predictions: list[dict] = []
    failures: list[dict] = []
    failed: TendError | None = None
    evaluation: EvaluationOutput | None = None
    summary: dict = {}
    out_path = rt.settings.run_dir / "ablation_predictions.jsonl"
    failures_path = rt.settings.run_dir / "ablation_failures.jsonl"
    summary_path = rt.settings.run_dir / "ablation_summary.json"
    try:
        with rt.progress:
            outputs = await run_ablation_suite(
                rt.workflow,
                dataset_dir=dataset_dir,
                ablation_selection=ablations,
                db_id=db_id,
                record_id=record_id,
                limit=limit,
                r_max=r_max,
                witness_k=witness_k,
            )
            predictions = [item for item in outputs if item.get("status") == "ok"]
            failures = [item for item in outputs if item.get("status") != "ok"]
            _write_jsonl(out_path, predictions)
            _write_jsonl(failures_path, failures)
            if predictions:
                rt.progress.phase("EVAL")
                evaluation = await _maybe_evaluate(
                    rt,
                    predictions=predictions,
                    predictions_path=out_path,
                    dataset_dir=dataset_dir,
                    experiment_kind="ablation",
                    evaluate=evaluate,
                    eval_out_dir=eval_out_dir,
                    eval_workers=eval_workers,
                )
    except TendError as err:
        failed = err
        if not err.logged:
            rt.log.anomaly(err)
        rt.log.error("ablation_run_failed", error_type=type(err).__name__,
                     message=err.message, anomaly=err.anomaly.value if err.anomaly else None)
    except Exception as exc:  # noqa: BLE001 - final CLI boundary
        failed = wrap_unexpected(exc, stage="ablation")
        rt.log.anomaly(failed)
        rt.log.error("ablation_run_failed", error_type=type(failed).__name__,
                     message=failed.message,
                     anomaly=failed.anomaly.value if failed.anomaly else None)
    finally:
        summary = rt.progress.summary() if hasattr(rt.progress, "summary") else {}
        failed_run = (
            failed is not None
            or not predictions
            or bool(failures)
            or (evaluation is not None and not evaluation.ok)
            or summary.get("anomaly_total", 0) > 0
        )
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps({
            "run_id": rt.settings.run_id,
            "status": "failed" if failed_run else "ok",
            "outputs": len(outputs),
            "predictions": len(predictions),
            "failures": len(failures),
            "by_ablation": _count_by(predictions, "ablation_id"),
            "failed_by_ablation": _count_by(failures, "ablation_id"),
            "evaluation": evaluation.report if evaluation else None,
            "progress": summary,
            "output": str(out_path),
            "failures_output": str(failures_path),
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        rt.log.info("ablation_run_done", status="failed" if failed_run else "ok",
                    outputs=len(outputs), predictions=len(predictions),
                    failures=len(failures), output=str(out_path),
                    failures_output=str(failures_path), summary_output=str(summary_path),
                    **summary)
        _print_ablation_summary(
            rt,
            predictions,
            failures,
            summary,
            out_path,
            failures_path,
            summary_path,
            evaluation,
            evaluate=evaluate,
        )
        _close_runtime(rt)

    return 1 if failed_run else 0


def _resolve_repo_path(settings: Settings, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else settings.paths.repo_root / p


def _release_dataset_dir(settings: Settings, dataset_dir: str | None) -> Path:
    """Resolve the solve/baseline/ablation dataset dir, defaulting to the release."""
    return _resolve_repo_path(settings, dataset_dir if dataset_dir else PRODUCTION_RELEASE_DIR)


def _collect_validation_issues(report: ReleaseReport) -> list[str]:
    return (
        report.composition.violations
        + report.record_violations
        + report.schema_violations
        + report.file_violations
    )


def _validate_dataset(
    settings: Settings,
    dataset_dir: Path,
    *,
    smoke: bool,
) -> tuple[ReleaseReport | None, str | None]:
    try:
        report = validate_release(
            dataset_dir,
            schemas_dir=settings.paths.schemas,
            require_all_dbs=not smoke,
        )
    except Exception as exc:  # noqa: BLE001 - CLI validation boundary
        return None, f"{type(exc).__name__}: {exc}"
    return report, None


def _print_validation_summary(
    *,
    title: str,
    dataset_dir: Path,
    mode: str,
    report: ReleaseReport | None,
    error: str | None = None,
    out_dir: Path | None = None,
) -> None:
    ok = bool(report and report.ok and error is None)
    status = "OK" if ok else "INVALID"
    print("\n" + "=" * 64)
    print(f"{title} · validation {status} · mode={mode}")
    print(f"  dataset : {dataset_dir}")
    if out_dir is not None:
        print(f"  out     : {out_dir}")
    if error is not None:
        print(f"  error   : {error}")
    if report is not None:
        c = report.composition
        d = report.diversity
        print(f"  records : {report.n_records}")
        print(f"  coverage: dbs={len(c.db_ids)} L4={c.l4_ratio:.0%} "
              f"L0={c.l0_ratio:.0%} flex={c.flex_ratio:.0%} ssf={c.ssf_ratio:.0%}")
        print(
            f"  diversity: mql={d.distinct_mql} skeletons={d.distinct_mql_skeletons} "
            f"canonical_nl={d.distinct_canonical_nl} "
            f"pairs={d.distinct_nl_mql_pairs} "
            f"max_skeleton_family={d.max_mql_skeleton_family}"
        )
        print(f"  status  : {'valid' if report.ok else 'invalid'}")
        issues = _collect_validation_issues(report)
        print(f"  issues  : {len(issues)}")
        for issue in issues[:VALIDATION_ISSUE_LIMIT]:
            print(f"    - {issue}")
        if len(issues) > VALIDATION_ISSUE_LIMIT:
            print(f"    - ... {len(issues) - VALIDATION_ISSUE_LIMIT} more")
    print("=" * 64)


def _run_validate(settings: Settings, *, dataset_dir: Path, smoke: bool) -> int:
    mode = "smoke" if smoke else "full"
    report, error = _validate_dataset(settings, dataset_dir, smoke=smoke)
    _print_validation_summary(
        title="TEND validate",
        dataset_dir=dataset_dir,
        mode=mode,
        report=report,
        error=error,
    )
    return 0 if report is not None and report.ok and error is None else 1


def _copy_release_tree(dataset_dir: Path, out_dir: Path) -> None:
    if dataset_dir.resolve() == out_dir.resolve():
        return
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{out_dir.name}.",
        dir=str(out_dir.parent),
    ) as tmp:
        staged = Path(tmp) / out_dir.name
        shutil.copytree(
            dataset_dir,
            staged,
            ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"),
        )
        if out_dir.exists():
            shutil.rmtree(out_dir)
        shutil.move(str(staged), str(out_dir))


def _run_publish(settings: Settings, *, dataset_dir: Path, out_dir: Path) -> int:
    mode = "full"
    report, error = _validate_dataset(settings, dataset_dir, smoke=False)
    _print_validation_summary(
        title="TEND publish",
        dataset_dir=dataset_dir,
        mode=mode,
        report=report,
        error=error,
        out_dir=out_dir,
    )
    if report is None or not report.ok or error is not None:
        print("publish refused: input dataset did not pass full validation")
        return 1
    _copy_release_tree(dataset_dir, out_dir)
    print(f"published release: {out_dir}")
    return 0


async def _run_evaluate(
    rt: Runtime,
    *,
    dataset_dir: Path,
    predictions_path: Path,
    kind: str,
    out_dir: Path | None,
    workers: int,
) -> int:
    evaluation: EvaluationOutput | None = None
    failed: TendError | None = None
    try:
        with rt.progress:
            evaluation = await _evaluate_outputs(
                rt,
                dataset_dir=dataset_dir,
                predictions_path=predictions_path,
                experiment_kind=kind,
                out_dir=out_dir or rt.settings.run_dir / "evaluation" / kind,
                max_workers=workers,
            )
    except TendError as err:
        failed = err
        if not err.logged:
            rt.log.anomaly(err)
        rt.log.error("evaluation_run_failed", error_type=type(err).__name__,
                     message=err.message, anomaly=err.anomaly.value if err.anomaly else None)
    except Exception as exc:  # noqa: BLE001 - final CLI boundary
        failed = wrap_unexpected(exc, stage="evaluate")
        rt.log.anomaly(failed)
        rt.log.error("evaluation_run_failed", error_type=type(failed).__name__,
                     message=failed.message,
                     anomaly=failed.anomaly.value if failed.anomaly else None)
    finally:
        summary = rt.progress.summary() if hasattr(rt.progress, "summary") else {}
        failed_run = (
            failed is not None
            or evaluation is None
            or not evaluation.ok
            or summary.get("anomaly_total", 0) > 0
        )
        rt.log.info("evaluation_run_done", status="failed" if failed_run else "ok", **summary)
        print("\n" + "=" * 64)
        print(f"TEND evaluate · run {rt.settings.run_id}")
        print(f"  predictions : {predictions_path}")
        print(f"  dataset     : {dataset_dir}")
        print(f"  anomalies   : {summary.get('anomaly_total', 0)} "
              f"{summary.get('anomalies_by_kind', {})}")
        _print_evaluation_block(evaluation)
        print(f"  logs        : {rt.settings.run_dir}/events.jsonl | anomalies.jsonl | progress.jsonl | llm/")
        print("=" * 64)
        _close_runtime(rt)
    return 1 if failed_run else 0


def _add_eval_args(parser: argparse.ArgumentParser) -> None:
    """Attach the shared automatic-evaluation flags to a solve-style subparser."""
    parser.add_argument("--no-eval", action="store_true",
                        help="skip automatic proposal-05 evaluation after generation")
    parser.add_argument("--eval-out", default=None,
                        help="override automatic evaluation output dir")
    parser.add_argument("--eval-workers", type=int, default=8,
                        help="parallel worker count for automatic evaluation")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tend",
        description="TEND construction pipeline and SMART solver",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("construct", help="run the construction pipeline")
    c.add_argument("--phase", choices=["A", "B", "all"], default="all")
    c.add_argument(
        "--construction-mode",
        choices=["legacy", "native"],
        default="legacy",
        help="construction route: legacy deterministic migration or per-database native designs",
    )
    c.add_argument("--dbs", default="financial",
                   help="comma-separated db_ids, or 'all' (default: financial)")
    c.add_argument(
        "--records",
        default="1",
        help="Phase B records to attempt, or 'all' for the selected source workload count",
    )
    c.add_argument(
        "--records-per-db",
        type=int,
        default=None,
        help="Phase B records to attempt for each selected db; useful for all-db 100+ runs",
    )
    c.add_argument(
        "--full-db",
        action="store_true",
        help="materialize full MongoDB data without reference-table row caps",
    )
    c.add_argument(
        "--structural-fraction",
        type=float,
        default=0.0,
        help="hybrid plan: fraction (0..1) of slots forced to structural_schema_flex so a "
             "large run meets the complexity floors (L4>=30%%/flex>=25%%/ssf>=20%%) while "
             "staying diverse; 0 = pure broad census mix",
    )
    c.add_argument("--stub", action="store_true", help="offline mode (no live LLM)")
    c.add_argument("--quiet", action="store_true", help="disable the live progress UI")
    c.add_argument("--run-id", default=None)

    v = sub.add_parser("validate", help="validate a dataset directory")
    v.add_argument("--dataset-dir", required=True, help="dataset dir to validate")
    v.add_argument("--smoke", action="store_true",
                   help="smoke validation: relax all-DB composition only")

    p = sub.add_parser("publish", help="validate and copy a production release")
    p.add_argument("--dataset-dir", required=True, help="candidate dataset dir")
    p.add_argument("--out", default=str(PRODUCTION_RELEASE_DIR),
                   help="production release dir (default: release/TEND-dataset)")

    s = sub.add_parser("solve", help="run the SMART schema-less reference solver")
    s.add_argument("--dataset-dir", default=None,
                   help="release dataset dir (default: release/TEND-dataset)")
    s.add_argument("--db-id", default=None, help="optional db_id filter")
    s.add_argument("--nlq", default=None,
                   help="solve one natural-language question against --db-id by querying MongoDB")
    s.add_argument("--record-id", type=int, default=None, help="optional record_id filter")
    s.add_argument("--limit", type=int, default=1, help="max records to solve")
    s.add_argument("--r-max", type=int, default=DEFAULT_R_MAX, help="SMART fallback limit")
    s.add_argument("--witness-k", type=int, default=DEFAULT_WITNESS_K,
                   help="prompt witness disclosure limit")
    s.add_argument("--stub", action="store_true", help="offline mode (no live LLM)")
    s.add_argument("--quiet", action="store_true", help="disable the live progress UI")
    _add_eval_args(s)
    s.add_argument("--run-id", default=None)

    b = sub.add_parser("baseline", help="run constrained LLM baselines")
    b.add_argument("--dataset-dir", default=None,
                   help="release dataset dir (default: release/TEND-dataset)")
    b.add_argument("--baselines", default="all",
                   help=f"comma-separated baseline ids or all; known={','.join(BASELINE_IDS)}")
    b.add_argument("--db-id", default=None, help="optional db_id filter")
    b.add_argument("--record-id", type=int, default=None, help="optional record_id filter")
    b.add_argument("--limit", type=int, default=1, help="max records per baseline")
    b.add_argument("--witness-k", type=int, default=DEFAULT_WITNESS_K,
                   help="public witness sample count for baselines that use samples")
    b.add_argument("--stub", action="store_true", help="offline mode (no live LLM)")
    b.add_argument("--quiet", action="store_true", help="disable the live progress UI")
    _add_eval_args(b)
    b.add_argument("--run-id", default=None)

    a = sub.add_parser("ablation", help="run SMART solver ablation study")
    a.add_argument("--dataset-dir", default=None,
                   help="release dataset dir (default: release/TEND-dataset)")
    a.add_argument("--ablations", default="all",
                   help=f"comma-separated ablation ids or all; known={','.join(ABLATION_IDS)}")
    a.add_argument("--db-id", default=None, help="optional db_id filter")
    a.add_argument("--record-id", type=int, default=None, help="optional record_id filter")
    a.add_argument("--limit", type=int, default=1, help="max records per ablation")
    a.add_argument("--r-max", type=int, default=DEFAULT_R_MAX, help="SMART fallback limit")
    a.add_argument("--witness-k", type=int, default=DEFAULT_WITNESS_K,
                   help="prompt witness sample count for ablations that use samples")
    a.add_argument("--stub", action="store_true", help="offline mode (no live LLM)")
    a.add_argument("--quiet", action="store_true", help="disable the live progress UI")
    _add_eval_args(a)
    a.add_argument("--run-id", default=None)

    e = sub.add_parser("evaluate", help="evaluate a prediction JSONL with proposal-05 metrics")
    e.add_argument("--dataset-dir", required=True, help="release dataset dir")
    e.add_argument("--predictions", required=True, help="prediction JSONL file")
    e.add_argument("--kind", default="manual",
                   help="experiment kind label: solver, baseline, ablation, or manual")
    e.add_argument("--out", default=None,
                   help="evaluation output dir (default: runs/<run_id>/evaluation/<kind>)")
    e.add_argument("--workers", type=int, default=8, help="parallel evaluator workers")
    e.add_argument("--quiet", action="store_true", help="disable the live progress UI")
    e.add_argument("--run-id", default=None)

    args = parser.parse_args(argv)

    overrides = {}
    if getattr(args, "stub", False):
        overrides["TEND_LLM_STUB"] = "1"
    if getattr(args, "quiet", False):
        overrides["TEND_QUIET"] = "1"
    if getattr(args, "full_db", False):
        overrides["TEND_MIGRATION_REF_SAMPLE_CAP"] = "0"
    run_id = getattr(args, "run_id", None) or new_run_id()
    settings = Settings.from_env(
        run_id=run_id,
        overrides=overrides,
        require_bird=args.command == "construct",
        require_llm=args.command in {"construct", "solve", "baseline", "ablation"},
    )

    if args.command == "validate":
        return _run_validate(
            settings,
            dataset_dir=_resolve_repo_path(settings, args.dataset_dir),
            smoke=args.smoke,
        )
    if args.command == "publish":
        return _run_publish(
            settings,
            dataset_dir=_resolve_repo_path(settings, args.dataset_dir),
            out_dir=_resolve_repo_path(settings, args.out),
        )
    if args.command == "evaluate":
        rt = build_solver_runtime(settings, run_kind="evaluation")
        return asyncio.run(_run_evaluate(
            rt,
            dataset_dir=_resolve_repo_path(settings, args.dataset_dir),
            predictions_path=_resolve_repo_path(settings, args.predictions),
            kind=args.kind,
            out_dir=_resolve_repo_path(settings, args.out) if args.out else None,
            workers=args.workers,
        ))
    if args.command == "construct":
        rt = build_runtime(settings)
        if rt.source is None:
            raise RuntimeError("construct requires a BIRD source")
        db_ids = list(rt.source.db_ids) if args.dbs == "all" else [
            db.strip() for db in args.dbs.split(",") if db.strip()
        ]
        structural_only_records = args.records.strip().lower() == "all"
        if args.records_per_db is not None:
            if args.records_per_db <= 0:
                raise ValueError("--records-per-db must be positive")
            n_records = args.records_per_db * len(db_ids)
        else:
            n_records = _resolve_construct_records(rt.source, db_ids, args.records)
        return asyncio.run(_run_construct(
            rt,
            db_ids,
            args.phase,
            n_records,
            structural_only_records=structural_only_records,
            structural_fraction=getattr(args, "structural_fraction", 0.0),
            records_per_db=args.records_per_db,
            construction_mode=args.construction_mode,
        ))
    if args.command == "solve":
        rt = build_solver_runtime(settings)
        return asyncio.run(_run_solve(
            rt,
            dataset_dir=_release_dataset_dir(settings, args.dataset_dir),
            db_id=args.db_id,
            record_id=args.record_id,
            limit=args.limit,
            r_max=args.r_max,
            witness_k=args.witness_k,
            nlq=args.nlq,
            evaluate=not args.no_eval,
            eval_out_dir=_resolve_repo_path(settings, args.eval_out) if args.eval_out else None,
            eval_workers=args.eval_workers,
        ))
    if args.command == "baseline":
        rt = build_solver_runtime(settings, run_kind="baseline")
        return asyncio.run(_run_baseline(
            rt,
            dataset_dir=_release_dataset_dir(settings, args.dataset_dir),
            baselines=args.baselines,
            db_id=args.db_id,
            record_id=args.record_id,
            limit=args.limit,
            witness_k=args.witness_k,
            evaluate=not args.no_eval,
            eval_out_dir=_resolve_repo_path(settings, args.eval_out) if args.eval_out else None,
            eval_workers=args.eval_workers,
        ))
    if args.command == "ablation":
        rt = build_solver_runtime(settings, run_kind="ablation")
        return asyncio.run(_run_ablation(
            rt,
            dataset_dir=_release_dataset_dir(settings, args.dataset_dir),
            ablations=args.ablations,
            db_id=args.db_id,
            record_id=args.record_id,
            limit=args.limit,
            r_max=args.r_max,
            witness_k=args.witness_k,
            evaluate=not args.no_eval,
            eval_out_dir=_resolve_repo_path(settings, args.eval_out) if args.eval_out else None,
            eval_workers=args.eval_workers,
        ))
    parser.error("unknown command")  # raises SystemExit; subparsers are required


if __name__ == "__main__":
    sys.exit(main())
