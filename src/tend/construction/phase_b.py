"""Native Phase B slot planning and deterministic gold-MQL compilers."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Iterable

from tend.construction.recipe import NativeFeature, NativeFeatureManifest
from tend.execution import (
    derive_canonical_form_set,
    mql_signature,
    mql_skeleton_signature,
    mql_skeleton_summary,
)

from .verify import verify_native_record


FEATURE_TYPE_ORDER = (
    "dynamic_key_object",
    "polymorphic_collection",
    "derived_tag_array",
    "nested_event_stream",
    "missing_vs_present",
)

DEFAULT_QUERY_PATTERN = {
    "dynamic_key_object": "dynamic_key_comparison",
    "polymorphic_collection": "subtype_field_dispatch",
    "derived_tag_array": "tag_combination",
    "nested_event_stream": "nested_event_filter",
    "missing_vs_present": "missing_vs_present",
}

DEFAULT_SHAPE_POLICY = {
    "dynamic_key_object": "preserve",
    "polymorphic_collection": "reshape",
    "derived_tag_array": "preserve",
    "nested_event_stream": "preserve",
    "missing_vs_present": "preserve",
}

DEFAULT_DIFFICULTY = {
    "dynamic_key_object": "L4",
    "polymorphic_collection": "L4",
    "derived_tag_array": "L4",
    "nested_event_stream": "L4",
    "missing_vs_present": "L4",
}

DEFAULT_CONSTRUCTS = {
    "dynamic_key_object": ["$objectToArray", "$filter"],
    "polymorphic_collection": ["$switch"],
    "derived_tag_array": ["$setIntersection", "$size"],
    "nested_event_stream": ["$filter"],
    "missing_vs_present": ["$type"],
}


@dataclass(frozen=True)
class NativeCoverageSlot:
    slot_id: str
    db_id: str
    feature_id: str
    feature_type: str
    query_pattern: str
    target_shape_policy: str
    target_difficulty: str
    required_native_constructs: list[str]
    anti_sql_transfer_target: str


@dataclass(frozen=True)
class _FeatureRef:
    manifest: NativeFeatureManifest
    feature: NativeFeature


def plan_native_slots(
    manifests: Iterable[NativeFeatureManifest],
    n_records: int,
    seed: int,
    records_per_db: dict[str, int] | int | None = None,
) -> list[NativeCoverageSlot]:
    """Plan native coverage slots directly from ``NativeFeatureManifest`` objects."""
    refs = _feature_refs(manifests, seed)
    if records_per_db is not None:
        return _plan_native_slots_by_db_features(
            refs,
            n_records,
            seed=seed,
            records_per_db=records_per_db,
        )

    by_type: dict[str, list[_FeatureRef]] = {
        feature_type: [] for feature_type in FEATURE_TYPE_ORDER
    }
    for ref in refs:
        if ref.feature.type in by_type:
            by_type[ref.feature.type].append(ref)

    slots: list[NativeCoverageSlot] = []
    db_counts: Counter[str] = Counter()
    feature_usage_counts: Counter[tuple[str, str]] = Counter()
    cursors: dict[str, int] = {feature_type: 0 for feature_type in FEATURE_TYPE_ORDER}
    slot_index = 0

    while len(slots) < max(0, n_records):
        made_progress = False
        type_counts = Counter(slot.feature_type for slot in slots)
        active_types = [feature_type for feature_type in FEATURE_TYPE_ORDER if by_type[feature_type]]
        active_types.sort(
            key=lambda feature_type: (
                _type_has_no_uncovered_refs(
                    by_type[feature_type],
                    db_counts,
                    records_per_db,
                    feature_usage_counts,
                ),
                type_counts[feature_type],
                FEATURE_TYPE_ORDER.index(feature_type),
            )
        )
        for feature_type in active_types:
            ref = _next_ref_under_db_cap(
                by_type[feature_type],
                cursors,
                feature_type,
                db_counts,
                records_per_db,
                feature_usage_counts,
            )
            if ref is None:
                continue
            cursors[feature_type] += 1
            slot_index += 1
            db_counts[ref.manifest.db_id] += 1
            feature_key = (ref.manifest.db_id, ref.feature.id)
            feature_usage_counts[feature_key] += 1
            slots.append(_slot_for_feature(ref, slot_index, feature_usage_counts[feature_key]))
            made_progress = True
            break
        if not made_progress:
            break
    return slots


def _plan_native_slots_by_db_features(
    refs: list[_FeatureRef],
    n_records: int,
    *,
    seed: int,
    records_per_db: dict[str, int] | int,
) -> list[NativeCoverageSlot]:
    """Plan high-volume native slots by balancing features within each database.

    The small-run planner balances feature types, which is useful for smoke tests. For
    100+ records per database, that strategy can over-repeat a sparse type such as
    ``missing_vs_present``. Large native releases need every database-specific feature to
    keep receiving slots so semantic variants have real surface area.
    """
    by_db: dict[str, list[_FeatureRef]] = defaultdict(list)
    for ref in refs:
        by_db[ref.manifest.db_id].append(ref)

    db_order = sorted(
        by_db,
        key=lambda db_id: (_stable_rank(seed, "db", db_id), db_id),
    )
    db_counts: Counter[str] = Counter()
    feature_usage_counts: Counter[tuple[str, str]] = Counter()
    type_usage_counts: Counter[tuple[str, str]] = Counter()
    slots: list[NativeCoverageSlot] = []
    slot_index = 0

    while len(slots) < max(0, n_records):
        made_progress = False
        for db_id in db_order:
            target = _db_cap(records_per_db, db_id)
            if target is None or target <= 0 or db_counts[db_id] >= target:
                continue
            candidates = by_db.get(db_id) or []
            if not candidates:
                continue
            ref = min(
                candidates,
                key=lambda item: (
                    feature_usage_counts[(item.manifest.db_id, item.feature.id)],
                    type_usage_counts[(item.manifest.db_id, item.feature.type)],
                    FEATURE_TYPE_ORDER.index(item.feature.type),
                    _stable_rank(seed, item.manifest.db_id, item.feature.id),
                    item.feature.id,
                ),
            )
            slot_index += 1
            db_counts[db_id] += 1
            feature_key = (ref.manifest.db_id, ref.feature.id)
            type_key = (ref.manifest.db_id, ref.feature.type)
            feature_usage_counts[feature_key] += 1
            type_usage_counts[type_key] += 1
            slots.append(_slot_for_feature(ref, slot_index, feature_usage_counts[feature_key]))
            made_progress = True
            if len(slots) >= n_records:
                break
        if not made_progress:
            break
    return slots


def dynamic_key_comparison(
    slot: NativeCoverageSlot,
    manifest: NativeFeatureManifest | Iterable[NativeFeatureManifest],
    *,
    snapshot: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    feature, feature_manifest = _resolve_feature(slot, manifest)
    field = _field(feature)
    profile = _dynamic_profile(feature, snapshot)
    variant = _slot_serial(slot) % 6
    safe_object: dict[str, Any] = {"$ifNull": [f"${field}", {}]}
    pipeline = [
        {"$addFields": {"__native_dynamic_entries": {"$objectToArray": safe_object}}},
    ]
    target_key = _pick(profile["keys"], slot, default="")
    metric = _pick(profile["metrics"], slot, default="")
    threshold = _pick(profile["thresholds"].get(metric, []), slot, default=0)
    min_key_count = max(1, min(int(profile["max_key_count"] or 1), 2 + (_slot_serial(slot) % 4)))

    if variant == 1 and target_key:
        pipeline.extend([
            {
                "$addFields": {
                    "native_matching_dynamic_keys": {
                        "$filter": {
                            "input": "$__native_dynamic_entries",
                            "as": "kv",
                            "cond": {"$eq": ["$$kv.k", target_key]},
                        }
                    }
                }
            },
            {"$match": {"$expr": {"$gt": [{"$size": "$native_matching_dynamic_keys"}, 0]}}},
        ])
        intent = f"dynamic key {target_key!r} is present"
    elif variant == 2 and target_key and metric:
        pipeline.extend([
            {
                "$addFields": {
                    "native_matching_dynamic_keys": {
                        "$filter": {
                            "input": "$__native_dynamic_entries",
                            "as": "kv",
                            "cond": {
                                "$and": [
                                    {"$eq": ["$$kv.k", target_key]},
                                    {"$gt": [f"$$kv.v.{metric}", threshold]},
                                ]
                            },
                        }
                    }
                }
            },
            {"$match": {"$expr": {"$gt": [{"$size": "$native_matching_dynamic_keys"}, 0]}}},
        ])
        intent = f"dynamic key {target_key!r} has {metric} above {threshold}"
    elif variant == 3:
        pipeline.extend([
            {
                "$addFields": {
                    "native_matching_dynamic_keys": {
                        "$filter": {
                            "input": "$__native_dynamic_entries",
                            "as": "kv",
                            "cond": {"$ne": ["$$kv.v", None]},
                        }
                    }
                }
            },
            {"$match": {"$expr": {"$gte": [{"$size": "$native_matching_dynamic_keys"}, min_key_count]}}},
        ])
        intent = f"at least {min_key_count} dynamic keys are populated"
    elif variant == 4:
        pipeline.extend([
            {
                "$addFields": {
                    "native_matching_dynamic_keys": {
                        "$filter": {
                            "input": "$__native_dynamic_entries",
                            "as": "kv",
                            "cond": {"$ne": ["$$kv.v", None]},
                        }
                    },
                    "native_dynamic_key_names": {
                        "$map": {
                            "input": "$__native_dynamic_entries",
                            "as": "kv",
                            "in": "$$kv.k",
                        }
                    },
                }
            },
            {"$match": {"$expr": {"$gt": [{"$size": "$native_matching_dynamic_keys"}, 0]}}},
        ])
        intent = "return the populated dynamic key names"
    elif variant == 5 and metric:
        pipeline.extend([
            {
                "$addFields": {
                    "native_matching_dynamic_keys": {
                        "$filter": {
                            "input": "$__native_dynamic_entries",
                            "as": "kv",
                            "cond": {"$gt": [f"$$kv.v.{metric}", threshold]},
                        }
                    }
                }
            },
            {"$match": {"$expr": {"$gt": [{"$size": "$native_matching_dynamic_keys"}, 0]}}},
        ])
        intent = f"any dynamic bucket has {metric} above {threshold}"
    else:
        pipeline.extend([
            {
                "$addFields": {
                    "native_matching_dynamic_keys": {
                        "$filter": {
                            "input": "$__native_dynamic_entries",
                            "as": "kv",
                            "cond": {"$ne": ["$$kv.v", None]},
                        }
                    }
                }
            },
            {"$match": {"$expr": {"$gt": [{"$size": "$native_matching_dynamic_keys"}, 0]}}},
        ])
        intent = "dynamic keys contain non-null values"

    project = {
        "_id": 1,
        field: 1,
        "native_matching_dynamic_keys": 1,
        "native_dynamic_key_count": {"$size": "$native_matching_dynamic_keys"},
    }
    if variant == 4:
        project["native_dynamic_key_names"] = 1
    pipeline.append({"$project": project})
    pipeline.extend(_result_order_and_limit(slot))
    return _compiler_output(
        slot,
        feature,
        feature_manifest,
        pipeline,
        constructs=["$objectToArray", "$filter", "$ifNull"],
        compiler="dynamic_key_comparison",
        intent=intent,
    )


def subtype_field_dispatch(
    slot: NativeCoverageSlot,
    manifest: NativeFeatureManifest | Iterable[NativeFeatureManifest],
    *,
    snapshot: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    feature, feature_manifest = _resolve_feature(slot, manifest)
    field = _field(feature)
    variants = _variant_values(feature, snapshot) or _variants(feature)
    target_variant = _pick(variants, slot, default="")
    variant = _slot_serial(slot) % 5
    switch_expr = {
        "$switch": {
            "branches": [
                {"case": {"$eq": [f"${field}", value]}, "then": value}
                for value in variants[:8]
            ],
            "default": "other",
        }
    }
    pipeline = [
        {"$addFields": {"native_subtype_bucket": switch_expr}},
    ]
    if variant in {1, 3} and target_variant:
        pipeline.append({"$match": {"native_subtype_bucket": target_variant}})
    if variant == 2:
        pipeline.append({
            "$addFields": {
                "native_subtype_field_names": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": {"$objectToArray": "$$ROOT"},
                                "as": "kv",
                                "cond": {"$not": [{"$in": ["$$kv.k", ["_id", field]]}]},
                            }
                        },
                        "as": "kv",
                        "in": "$$kv.k",
                    }
                }
            }
        })
    if variant == 4:
        pipeline.extend([
            {"$group": {"_id": "$native_subtype_bucket", "native_subtype_count": {"$sum": 1}}},
            {"$project": {"_id": 0, "native_subtype_bucket": "$_id", "native_subtype_count": 1}},
        ])
        intent = "count documents in each discriminator bucket"
    else:
        project = {"_id": 1, field: 1, "native_subtype_bucket": 1}
        if variant == 2:
            project["native_subtype_field_names"] = 1
        pipeline.append({"$project": project})
        intent = (
            f"dispatch only subtype {target_variant!r}"
            if variant in {1, 3} and target_variant
            else "dispatch documents by discriminator"
        )
    pipeline.extend(_result_order_and_limit(slot, grouped=variant == 4))
    return _compiler_output(
        slot,
        feature,
        feature_manifest,
        pipeline,
        constructs=["$switch"] + (["$objectToArray", "$filter"] if variant == 2 else []),
        compiler="subtype_field_dispatch",
        intent=intent,
    )


def tag_combination(
    slot: NativeCoverageSlot,
    manifest: NativeFeatureManifest | Iterable[NativeFeatureManifest],
    *,
    snapshot: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    feature, feature_manifest = _resolve_feature(slot, manifest)
    field = _field(feature)
    observed_tags = _tag_values(feature, snapshot)
    target_tags = observed_tags or _target_tags(feature)
    selected_tags = _tag_subset(target_tags, slot)
    safe_tags: dict[str, Any] = {"$ifNull": [f"${field}", []]}
    variant = _slot_serial(slot) % 5
    pipeline = []
    constructs = ["$setIntersection", "$size", "$ifNull"]
    if variant == 1 and selected_tags:
        pipeline.extend([
            {"$addFields": {"native_tag_overlap": {"$setIntersection": [safe_tags, selected_tags]}}},
            {"$match": {"$expr": {"$eq": [{"$size": "$native_tag_overlap"}, len(selected_tags)]}}},
        ])
        intent = f"all selected tags {selected_tags} are present"
    elif variant == 2 and selected_tags:
        pipeline.extend([
            {"$addFields": {"native_tag_subset_match": {"$setIsSubset": [selected_tags, safe_tags]}}},
            {"$match": {"native_tag_subset_match": True}},
            {"$addFields": {"native_tag_overlap": {"$setIntersection": [safe_tags, selected_tags]}}},
        ])
        constructs = ["$setIsSubset", "$setIntersection", "$ifNull"]
        intent = f"the tag array contains subset {selected_tags}"
    elif variant == 3:
        min_overlap = 2 if len(selected_tags) >= 2 else 1
        pipeline.extend([
            {"$addFields": {"native_tag_overlap": {"$setIntersection": [safe_tags, selected_tags]}}},
            {"$match": {"$expr": {"$gte": [{"$size": "$native_tag_overlap"}, min_overlap]}}},
        ])
        intent = f"at least {min_overlap} selected tags overlap"
    elif variant == 4:
        pipeline.extend([
            {"$addFields": {"native_tag_overlap": {"$setIntersection": [safe_tags, selected_tags]}}},
            {
                "$addFields": {
                    "native_tag_bucket": {
                        "$cond": [
                            {"$gt": [{"$size": "$native_tag_overlap"}, 1]},
                            "multi_tag",
                            "single_tag",
                        ]
                    }
                }
            },
            {"$match": {"$expr": {"$gt": [{"$size": "$native_tag_overlap"}, 0]}}},
        ])
        constructs.append("$cond")
        intent = "classify matching documents by single-tag versus multi-tag overlap"
    else:
        pipeline.extend([
            {"$addFields": {"native_tag_overlap": {"$setIntersection": [safe_tags, selected_tags]}}},
            {"$match": {"$expr": {"$gt": [{"$size": "$native_tag_overlap"}, 0]}}},
        ])
        intent = f"any selected tag in {selected_tags} overlaps"
    project = {"_id": 1, field: 1, "native_tag_overlap": 1}
    if variant == 2:
        project["native_tag_subset_match"] = 1
    if variant == 4:
        project["native_tag_bucket"] = 1
    pipeline.append({"$project": project})
    pipeline.extend(_result_order_and_limit(slot))
    return _compiler_output(
        slot,
        feature,
        feature_manifest,
        pipeline,
        constructs=constructs,
        compiler="tag_combination",
        intent=intent,
    )


def nested_event_filter(
    slot: NativeCoverageSlot,
    manifest: NativeFeatureManifest | Iterable[NativeFeatureManifest],
    *,
    snapshot: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    feature, feature_manifest = _resolve_feature(slot, manifest)
    field = _field(feature)
    profile = _event_profile(feature, snapshot)
    variant = _slot_serial(slot) % 6
    event_type = _pick(profile["event_types"], slot, default=None)
    time_cutoff = _pick(profile["event_times"], slot, default=None)
    metric = _pick(profile["numeric_fields"], slot, default=None)
    if event_type is not None and time_cutoff is not None:
        time_cutoff = _pick(
            profile["event_times_by_type"].get(event_type, []),
            slot,
            default=time_cutoff,
        )
    if event_type is not None and metric is not None:
        typed_thresholds = profile["thresholds_by_type"].get(event_type, {})
        if metric not in typed_thresholds:
            metric = _pick(sorted(typed_thresholds), slot, default=metric)
    threshold = (
        _pick(
            profile["thresholds_by_type"].get(event_type, {}).get(metric, [])
            or profile["thresholds"].get(metric, []),
            slot,
            default=0,
        )
        if metric
        else 0
    )
    safe_events: dict[str, Any] = _safe_array_expr(field)
    cond: Any
    intent: str
    if variant == 1 and event_type is not None:
        cond = {"$eq": ["$$event.event_type", event_type]}
        intent = f"nested events whose event_type is {event_type!r}"
    elif variant == 2 and time_cutoff is not None:
        cond = {"$gte": ["$$event.event_time", time_cutoff]}
        intent = f"nested events on or after {time_cutoff!r}"
    elif variant == 3 and metric is not None:
        cond = {"$gt": [f"$$event.{metric}", threshold]}
        intent = f"nested events with {metric} above {threshold}"
    elif variant == 4 and event_type is not None and time_cutoff is not None:
        cond = {
            "$and": [
                {"$eq": ["$$event.event_type", event_type]},
                {"$gte": ["$$event.event_time", time_cutoff]},
            ]
        }
        intent = f"nested {event_type!r} events on or after {time_cutoff!r}"
    elif variant == 5:
        cond = {"$ne": ["$$event.event_time", None]}
        intent = "nested events with recorded event time"
    else:
        cond = {
            "$and": [
                {"$ne": ["$$event.event_type", None]},
                {"$ne": ["$$event.event_time", None]},
            ]
        }
        intent = "nested events with event type and time present"
    pipeline = [
        {
            "$addFields": {
                "native_filtered_events": {
                    "$filter": {
                        "input": safe_events,
                        "as": "event",
                        "cond": cond,
                    }
                }
            }
        },
        {"$match": {"$expr": {"$gt": [{"$size": "$native_filtered_events"}, 0]}}},
        {"$project": {"_id": 1, field: 1, "native_filtered_events": 1}},
    ]
    pipeline.extend(_result_order_and_limit(slot))
    return _compiler_output(
        slot,
        feature,
        feature_manifest,
        pipeline,
        constructs=["$filter", "$ifNull"],
        compiler="nested_event_filter",
        intent=intent,
    )


def missing_vs_present(
    slot: NativeCoverageSlot,
    manifest: NativeFeatureManifest | Iterable[NativeFeatureManifest],
) -> dict[str, Any]:
    feature, feature_manifest = _resolve_feature(slot, manifest)
    field = _field(feature)
    pipeline = [
        {"$addFields": {"native_presence_state": {"$ifNull": [f"${field}", "missing"]}}},
        {"$match": {"native_presence_state": {"$in": ["missing", "null", "empty"]}}},
        {"$project": {"_id": 1, field: 1, "native_presence_state": 1}},
    ]
    pipeline.extend(_result_order_and_limit(slot))
    return _compiler_output(
        slot,
        feature,
        feature_manifest,
        pipeline,
        constructs=["$ifNull"],
        compiler="missing_vs_present",
    )


def build_native_record(
    slot: NativeCoverageSlot,
    manifest: NativeFeatureManifest | Iterable[NativeFeatureManifest],
    *,
    record_id: int | None = None,
    canonical_nl: str | None = None,
    colloquial_nl: str | None = None,
    executor: Any = None,
    snapshot: Any = None,
    world_signature: str = "sha256:" + "0" * 64,
    migration_recipe_ref: str | None = None,
) -> dict[str, Any]:
    """Build one stub-friendly native record from a planned native slot."""
    feature, feature_manifest = _resolve_feature(slot, manifest)
    compiled = _compile_slot(slot, feature_manifest, snapshot=snapshot)
    native_stub = {
        "db_id": slot.db_id,
        "MQL": compiled["MQL"],
        "native_metadata": {
            "feature_id": slot.feature_id,
            "feature_type": slot.feature_type,
            "query_pattern": slot.query_pattern,
            "anti_sql_transfer_target": slot.anti_sql_transfer_target,
        },
    }
    verification = verify_native_record(
        native_stub,
        feature_manifest,
        executor=executor,
        snapshot=snapshot,
    )
    nl_queries = {
        "canonical": canonical_nl or _canonical_nl(slot, feature, compiled),
        "colloquial": colloquial_nl or _colloquial_nl(slot, feature, compiled),
    }
    rid = int(record_id if record_id is not None else _record_id_from_slot(slot))
    record = {
        "record_id": rid,
        "db_id": slot.db_id,
        "mechanism": "native_schema_flex",
        "archetype": slot.query_pattern,
        "schema_feature": slot.feature_id,
        "nl_queries": nl_queries,
        "MQL": compiled["MQL"],
        "mql_signature": mql_signature(compiled["MQL"]),
        "mql_skeleton_signature": mql_skeleton_signature(compiled["MQL"]),
        "mql_skeleton_summary": mql_skeleton_summary(compiled["MQL"]),
        "canonical_form_set": compiled["canonical_form_set"],
        "difficulty": slot.target_difficulty,
        "sql_infeasibility_class": "structural_schema_flex",
        "shape_policy": slot.target_shape_policy,
        "schema_flex": _schema_flex_for_feature_type(slot.feature_type),
        "world_signature": world_signature,
        "native_feature_id": slot.feature_id,
        "native_feature_type": slot.feature_type,
        "native_query_pattern": slot.query_pattern,
        "mongo_native_constructs": list(compiled["mongo_native_constructs"]),
        "anti_sql_transfer_level": verification.anti_sql_transfer.level,
        "anti_sql_transfer_evidence": list(verification.anti_sql_transfer.evidence),
        "provenance_refs": list(compiled["provenance_refs"]),
        "migration_recipe_ref": migration_recipe_ref or f"migration_recipe/{slot.db_id}.yaml",
        "native_metadata": {
            "feature_id": slot.feature_id,
            "feature_type": slot.feature_type,
            "feature_field": feature.field,
            "query_pattern": slot.query_pattern,
            "target_shape_policy": slot.target_shape_policy,
            "target_difficulty": slot.target_difficulty,
            "required_native_constructs": list(slot.required_native_constructs),
            "mongo_native_constructs": list(compiled["mongo_native_constructs"]),
            "anti_sql_transfer_target": slot.anti_sql_transfer_target,
            "anti_sql_transfer": verification.anti_sql_transfer.to_dict(),
            "provenance_refs": list(compiled["provenance_refs"]),
            "compiler": compiled["native_verification"].get("compiler"),
        },
        "native_verification": verification.to_dict(),
    }
    return record


async def run_native_phase_b(
    wf: Any,
    artifacts: dict[str, Any],
    slots: list[NativeCoverageSlot],
) -> list[dict[str, Any]]:
    """Build native Phase B records from native slots using isolated pipeline items."""
    if hasattr(wf, "phase"):
        wf.phase("B.native")

    manifests = {
        db_id: artifact.native_feature_manifest
        for db_id, artifact in artifacts.items()
    }

    async def build(slot: NativeCoverageSlot) -> dict[str, Any] | None:
        artifact = artifacts.get(slot.db_id)
        manifest = manifests.get(slot.db_id)
        if artifact is None or manifest is None:
            return None
        record = build_native_record(
            slot,
            manifest,
            snapshot=getattr(artifact, "mongodb_data", None),
            world_signature=getattr(artifact, "world_signature", "sha256:" + "0" * 64),
            migration_recipe_ref=f"migration_recipe/{slot.db_id}.yaml",
        )
        log = getattr(getattr(wf, "ctx", None), "log", None)
        if log is not None and hasattr(log, "info"):
            log.info(
                "native_record_built",
                db_id=slot.db_id,
                record_id=record["record_id"],
                native_feature_id=slot.feature_id,
                native_query_pattern=slot.query_pattern,
            )
        return record

    results = await wf.pipeline(slots, build, isolate=True)
    return [record for record in results if isinstance(record, dict)]


def _feature_refs(
    manifests: Iterable[NativeFeatureManifest],
    seed: int,
) -> list[_FeatureRef]:
    refs = [
        _FeatureRef(manifest=manifest, feature=feature)
        for manifest in sorted(manifests, key=lambda item: item.db_id)
        for feature in manifest.features
        if feature.type in FEATURE_TYPE_ORDER
    ]
    return sorted(
        refs,
        key=lambda ref: (
            FEATURE_TYPE_ORDER.index(ref.feature.type),
            _stable_rank(seed, ref.manifest.db_id, ref.feature.id),
            ref.manifest.db_id,
            ref.feature.id,
        ),
    )


def _next_ref_under_db_cap(
    refs: list[_FeatureRef],
    cursors: dict[str, int],
    feature_type: str,
    db_counts: Counter[str],
    records_per_db: dict[str, int] | int | None,
    feature_usage_counts: Counter[tuple[str, str]],
) -> _FeatureRef | None:
    if not refs:
        return None
    start = cursors[feature_type]
    for require_uncovered in (True, False):
        for offset in range(len(refs)):
            ref = refs[(start + offset) % len(refs)]
            cap = _db_cap(records_per_db, ref.manifest.db_id)
            if cap is not None and db_counts[ref.manifest.db_id] >= cap:
                continue
            if require_uncovered and feature_usage_counts[(ref.manifest.db_id, ref.feature.id)] > 0:
                continue
            cursors[feature_type] = start + offset
            return ref
    return None


def _type_has_no_uncovered_refs(
    refs: list[_FeatureRef],
    db_counts: Counter[str],
    records_per_db: dict[str, int] | int | None,
    feature_usage_counts: Counter[tuple[str, str]],
) -> bool:
    for ref in refs:
        cap = _db_cap(records_per_db, ref.manifest.db_id)
        if cap is not None and db_counts[ref.manifest.db_id] >= cap:
            continue
        if feature_usage_counts[(ref.manifest.db_id, ref.feature.id)] == 0:
            return False
    return True


def _slot_for_feature(
    ref: _FeatureRef,
    slot_index: int,
    feature_use_index: int,
) -> NativeCoverageSlot:
    feature = ref.feature
    query_patterns = (
        list(feature.query_patterns)
        if feature.query_patterns
        else [DEFAULT_QUERY_PATTERN[feature.type]]
    )
    query_pattern = query_patterns[(feature_use_index - 1) % len(query_patterns)]
    constructs = (
        list(feature.required_constructs)
        if feature.required_constructs
        else list(DEFAULT_CONSTRUCTS[feature.type])
    )
    return NativeCoverageSlot(
        slot_id=(
            f"native:{ref.manifest.db_id}:{slot_index:04d}:"
            f"u{feature_use_index:04d}:{feature.type}:{feature.id}"
        ),
        db_id=ref.manifest.db_id,
        feature_id=feature.id,
        feature_type=feature.type,
        query_pattern=query_pattern,
        target_shape_policy=DEFAULT_SHAPE_POLICY[feature.type],
        target_difficulty=DEFAULT_DIFFICULTY[feature.type],
        required_native_constructs=constructs,
        anti_sql_transfer_target="strong",
    )


def _db_cap(records_per_db: dict[str, int] | int | None, db_id: str) -> int | None:
    if records_per_db is None:
        return None
    if isinstance(records_per_db, int):
        return records_per_db
    return records_per_db.get(db_id, 0)


def _resolve_feature(
    slot: NativeCoverageSlot,
    manifest: NativeFeatureManifest | Iterable[NativeFeatureManifest],
) -> tuple[NativeFeature, NativeFeatureManifest]:
    manifests = [manifest] if isinstance(manifest, NativeFeatureManifest) else list(manifest)
    for item in manifests:
        if item.db_id != slot.db_id:
            continue
        for feature in item.features:
            if feature.id == slot.feature_id:
                return feature, item
    raise ValueError(f"native slot feature not found: {slot.db_id}/{slot.feature_id}")


def _compile_slot(
    slot: NativeCoverageSlot,
    manifest: NativeFeatureManifest | Iterable[NativeFeatureManifest],
    *,
    snapshot: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    semantic_variant = _semantic_snapshot_variant(slot, manifest, snapshot=snapshot)
    if semantic_variant is not None:
        return semantic_variant
    if _feature_pipeline_blueprint(slot, manifest) is not None:
        return pipeline_blueprint(slot, manifest, snapshot=snapshot)
    if slot.query_pattern == "dynamic_key_comparison":
        return dynamic_key_comparison(slot, manifest, snapshot=snapshot)
    if slot.query_pattern == "subtype_field_dispatch":
        return subtype_field_dispatch(slot, manifest, snapshot=snapshot)
    if slot.query_pattern == "tag_combination":
        return tag_combination(slot, manifest, snapshot=snapshot)
    if slot.query_pattern == "nested_event_filter":
        return nested_event_filter(slot, manifest, snapshot=snapshot)
    if slot.query_pattern == "missing_vs_present":
        return missing_vs_present(slot, manifest)
    raise ValueError(f"unsupported native query pattern: {slot.query_pattern}")


def pipeline_blueprint(
    slot: NativeCoverageSlot,
    manifest: NativeFeatureManifest | Iterable[NativeFeatureManifest],
    *,
    snapshot: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    feature, feature_manifest = _resolve_feature(slot, manifest)
    blueprint = _feature_pipeline_blueprint(slot, feature_manifest)
    if blueprint is None:
        raise ValueError(f"no pipeline blueprint for native query pattern: {slot.query_pattern}")
    pipeline = blueprint.get("pipeline")
    if not isinstance(pipeline, list) or not all(isinstance(stage, dict) for stage in pipeline):
        raise ValueError(f"pipeline blueprint for {slot.feature_id} must be a list of stage objects")
    constructs_raw = blueprint.get("mongo_native_constructs")
    constructs = (
        [str(value) for value in constructs_raw]
        if isinstance(constructs_raw, list) and constructs_raw
        else list(slot.required_native_constructs)
    )
    return _compiler_output(
        slot,
        feature,
        feature_manifest,
        json.loads(json.dumps(pipeline)),
        constructs=constructs,
        compiler="pipeline_blueprint",
        intent=str(blueprint.get("intent") or slot.query_pattern),
    )


def _semantic_snapshot_variant(
    slot: NativeCoverageSlot,
    manifest: NativeFeatureManifest | Iterable[NativeFeatureManifest],
    *,
    snapshot: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    feature, feature_manifest = _resolve_feature(slot, manifest)
    docs = _snapshot_docs(feature, snapshot)
    if not docs:
        return None
    if feature.type == "dynamic_key_object":
        return _semantic_dynamic_key_variant(slot, feature, feature_manifest, snapshot)
    if feature.type == "derived_tag_array":
        return _semantic_tag_variant(slot, feature, feature_manifest, snapshot)
    if feature.type == "nested_event_stream":
        return _semantic_nested_event_variant(slot, feature, feature_manifest, snapshot)
    if feature.type == "polymorphic_collection":
        return _semantic_polymorphic_variant(slot, feature, feature_manifest, snapshot)
    if feature.type == "missing_vs_present":
        return _semantic_presence_variant(slot, feature, feature_manifest, snapshot)
    return None


def _semantic_dynamic_key_variant(
    slot: NativeCoverageSlot,
    feature: NativeFeature,
    manifest: NativeFeatureManifest,
    snapshot: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, Any] | None:
    profile = _dynamic_profile(feature, snapshot)
    keys = profile["keys"] or []
    if not keys:
        return None
    field = _field(feature)
    serial = _slot_serial(slot)
    docs = _snapshot_docs(feature, snapshot)
    array_prefix = _array_prefix_for_path(docs, field)
    context = _semantic_context(docs, skip_prefixes={field}, slot=slot)
    prefix_stages = ([{"$unwind": f"${array_prefix}"}] if array_prefix else []) + context["stages"]
    context_project = context["project"]
    context_intent = context["intent"]
    key = _pick(keys, slot, default=keys[0])
    subset = _window(keys, serial, width=1 + (serial % min(4, len(keys))))
    metric = _pick(profile["metrics"], slot, default="")
    metric_values: list[int | float] = []
    if metric:
        metric_values = _dynamic_key_metric_values(feature, snapshot, key, metric)
        if not metric_values:
            pairs = _compatible_dynamic_key_metric_pairs(
                feature,
                snapshot,
                keys=keys,
                metrics=profile["metrics"],
            )
            pair = _pick(pairs, slot, default=None)
            if pair is None:
                metric = ""
            else:
                key, metric, metric_values = pair
    threshold = (
        _dynamic_key_metric_threshold(metric_values, slot)
        if metric
        else 0
    )
    min_entry_count = 1 + (serial % min(5, max(1, int(profile["max_key_count"]))))
    entry_array = {"$objectToArray": {"$ifNull": [f"${field}", {}]}}
    entry_value = "$native_dynamic_entries.v"
    metric_ref = f"{entry_value}.{metric}" if metric else entry_value
    variant = serial % 8
    pipeline: list[dict[str, Any]]
    constructs = ["$objectToArray", "$unwind", "$ifNull"] + list(context["constructs"])

    if variant == 0:
        pipeline = [
            *prefix_stages,
            {"$project": {"_id": 1, "native_dynamic_entries": entry_array, **context_project}},
            {"$unwind": "$native_dynamic_entries"},
            {"$match": {"native_dynamic_entries.k": key}},
            {"$project": {"_id": 1, "native_key": "$native_dynamic_entries.k", "native_value": "$native_dynamic_entries.v", **context_project}},
            *_result_order_and_limit(slot),
        ]
        intent = f"dynamic key {key!r} is present under {field}"
    elif variant == 1 and metric:
        pipeline = [
            *prefix_stages,
            {"$project": {"_id": 1, "native_dynamic_entries": entry_array, **context_project}},
            {"$unwind": "$native_dynamic_entries"},
            {"$match": {"native_dynamic_entries.k": key, f"native_dynamic_entries.v.{metric}": {"$gte": threshold}}},
            {"$project": {"_id": 1, "native_key": "$native_dynamic_entries.k", metric: metric_ref, **context_project}},
            {"$sort": {metric: -1, "_id": 1}},
            {"$limit": _limit_value(slot)},
        ]
        intent = f"dynamic key {key!r} has {metric} at least {threshold}"
    elif variant == 2 and metric:
        pipeline = [
            *prefix_stages,
            {"$project": {"native_dynamic_entries": entry_array, **context_project}},
            {"$unwind": "$native_dynamic_entries"},
            {"$match": {f"native_dynamic_entries.v.{metric}": {"$ne": None}}},
            {
                "$group": {
                    "_id": {
                        "native_key": "$native_dynamic_entries.k",
                        "context": "$native_context_bucket",
                    } if context_project else "$native_dynamic_entries.k",
                    "entry_count": {"$sum": 1},
                    "metric_total": {"$sum": metric_ref},
                }
            },
            {"$match": {"entry_count": {"$gte": 1}}},
            {"$sort": {"metric_total": -1, "_id": 1}},
            {"$limit": _limit_value(slot)},
        ]
        constructs.extend(["$group", "$sum"])
        intent = f"summarize {metric} totals across dynamic {field} keys"
    elif variant == 3:
        pipeline = [
            *prefix_stages,
            {
                "$project": {
                    "_id": 1,
                    "native_dynamic_entries": entry_array,
                    "native_dynamic_key_count": {"$size": entry_array},
                    **context_project,
                }
            },
            {"$match": {"native_dynamic_key_count": {"$gte": min_entry_count}}},
            {"$project": {"_id": 1, "native_dynamic_key_count": 1, "native_dynamic_entries.k": 1, **context_project}},
            {"$sort": {"native_dynamic_key_count": -1, "_id": 1}},
            {"$limit": _limit_value(slot)},
        ]
        constructs.append("$size")
        intent = f"at least {min_entry_count} dynamic keys exist under {field}"
    elif variant == 4:
        pipeline = [
            *prefix_stages,
            {"$project": {"_id": 1, "native_dynamic_entries": entry_array, **context_project}},
            {"$unwind": "$native_dynamic_entries"},
            {"$match": {"native_dynamic_entries.k": {"$in": subset}}},
            {
                "$group": {
                    "_id": {
                        "native_key": "$native_dynamic_entries.k",
                        "context": "$native_context_bucket",
                    } if context_project else "$native_dynamic_entries.k",
                    "document_count": {"$sum": 1},
                }
            },
            {"$sort": {"document_count": -1, "_id": 1}},
            {"$limit": _limit_value(slot)},
        ]
        constructs.extend(["$group", "$sum"])
        intent = f"dynamic key is one of {subset!r} under {field}"
    elif variant == 5:
        pipeline = [
            *prefix_stages,
            {
                "$project": {
                    "_id": 1,
                    **context_project,
                    "native_matching_dynamic_entries": {
                        "$filter": {
                            "input": entry_array,
                            "as": "entry",
                            "cond": {"$in": ["$$entry.k", subset]},
                        }
                    },
                }
            },
            {"$match": {"$expr": {"$gt": [{"$size": "$native_matching_dynamic_entries"}, 0]}}},
            {"$project": {"_id": 1, "native_matching_dynamic_entries": 1, **context_project}},
            *_result_order_and_limit(slot),
        ]
        constructs.extend(["$filter", "$size"])
        intent = f"shape-preserving filter keeps dynamic keys {subset!r} under {field}"
    elif variant == 6 and metric:
        pipeline = [
            *prefix_stages,
            {"$project": {"_id": 1, "native_dynamic_entries": entry_array, **context_project}},
            {"$unwind": "$native_dynamic_entries"},
            {
                "$group": {
                    "_id": {
                        "native_key": "$native_dynamic_entries.k",
                        "context": "$native_context_bucket",
                    } if context_project else "$native_dynamic_entries.k",
                    "above_threshold": {
                        "$sum": {
                            "$cond": [
                                {"$gte": [metric_ref, threshold]},
                                1,
                                0,
                            ]
                        }
                    },
                    "observed": {"$sum": 1},
                }
            },
            {"$sort": {"above_threshold": -1, "observed": -1, "_id": 1}},
            {"$limit": _limit_value(slot)},
        ]
        constructs.extend(["$group", "$sum", "$cond"])
        intent = f"count dynamic {field} keys whose {metric} reaches {threshold}"
    else:
        grouped_project = {
            "_id": 0,
            "native_key": "$_id.native_key" if context_project else "$_id",
            "document_count": 1,
            "example_count": {"$size": "$example_ids"},
        }
        if context_project:
            grouped_project["native_context_bucket"] = "$_id.context"
        pipeline = [
            *prefix_stages,
            {"$project": {"_id": 1, "native_dynamic_entries": entry_array, **context_project}},
            {"$unwind": "$native_dynamic_entries"},
            {"$match": {"native_dynamic_entries.k": key}},
            {
                "$group": {
                    "_id": {
                        "native_key": "$native_dynamic_entries.k",
                        "context": "$native_context_bucket",
                    } if context_project else "$native_dynamic_entries.k",
                    "document_count": {"$sum": 1},
                    "example_ids": {"$addToSet": "$_id"},
                }
            },
            {"$project": grouped_project},
            {"$sort": {"document_count": -1, "native_key": 1}},
            {"$limit": _limit_value(slot)},
        ]
        constructs.extend(["$group", "$sum", "$size"])
        intent = f"count documents carrying dynamic key {key!r} under {field}"

    if context_intent:
        intent = f"{intent} with {context_intent}"
    return _compiler_output(
        slot,
        feature,
        manifest,
        pipeline,
        constructs=list(dict.fromkeys(constructs)),
        compiler="semantic_snapshot_variant",
        intent=intent,
    )


def _semantic_tag_variant(
    slot: NativeCoverageSlot,
    feature: NativeFeature,
    manifest: NativeFeatureManifest,
    snapshot: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, Any] | None:
    tags = _tag_values(feature, snapshot) or _target_tags(feature)
    if not tags:
        return None
    field = _field(feature)
    serial = _slot_serial(slot)
    selected = _tag_subset(tags, slot)
    excluded = _pick([tag for tag in tags if tag not in selected], slot, default="")
    safe_tags: dict[str, Any] = {"$ifNull": [f"${field}", []]}
    variant = serial % 6
    constructs = ["$ifNull", "$size"]

    if variant == 0:
        pipeline = [
            {"$addFields": {"native_tag_overlap": {"$setIntersection": [safe_tags, selected]}}},
            {"$match": {"$expr": {"$gt": [{"$size": "$native_tag_overlap"}, 0]}}},
            {"$project": {"_id": 1, field: 1, "native_tag_overlap": 1}},
            *_result_order_and_limit(slot),
        ]
        constructs.append("$setIntersection")
        intent = f"at least one of tags {selected!r} appears in {field}"
    elif variant == 1:
        pipeline = [
            {"$addFields": {"native_tag_subset_match": {"$setIsSubset": [selected, safe_tags]}}},
            {"$match": {"native_tag_subset_match": True}},
            {"$project": {"_id": 1, field: 1, "native_tag_subset_match": 1}},
            *_result_order_and_limit(slot),
        ]
        constructs.append("$setIsSubset")
        intent = f"all tags {selected!r} appear in {field}"
    elif variant == 2 and excluded:
        pipeline = [
            {"$addFields": {"native_tag_overlap": {"$setIntersection": [safe_tags, selected]}}},
            {
                "$match": {
                    "$expr": {
                        "$and": [
                            {"$gt": [{"$size": "$native_tag_overlap"}, 0]},
                            {"$not": [{"$in": [excluded, safe_tags]}]},
                        ]
                    }
                }
            },
            {"$project": {"_id": 1, field: 1, "native_tag_overlap": 1}},
            *_result_order_and_limit(slot),
        ]
        constructs.append("$setIntersection")
        intent = f"tags overlap {selected!r} while excluding {excluded!r}"
    elif variant == 3:
        min_overlap = min(len(selected), 2) or 1
        pipeline = [
            {"$addFields": {"native_tag_overlap": {"$setIntersection": [safe_tags, selected]}}},
            {"$match": {"$expr": {"$gte": [{"$size": "$native_tag_overlap"}, min_overlap]}}},
            {"$project": {"_id": 1, field: 1, "native_tag_overlap": 1}},
            *_result_order_and_limit(slot),
        ]
        constructs.append("$setIntersection")
        intent = f"at least {min_overlap} of tags {selected!r} appear in {field}"
    elif variant == 4:
        pipeline = [
            {"$unwind": f"${field}"},
            {"$match": {field: {"$in": selected}}},
            {"$group": {"_id": f"${field}", "document_count": {"$sum": 1}}},
            {"$sort": {"document_count": -1, "_id": 1}},
            {"$limit": _limit_value(slot)},
        ]
        constructs.extend(["$unwind", "$group", "$sum"])
        intent = f"rank selected tags {selected!r} from {field}"
    else:
        pipeline = [
            {"$addFields": {"native_tag_count": {"$size": safe_tags}}},
            {"$match": {"native_tag_count": {"$gte": max(1, len(selected))}}},
            {"$project": {"_id": 1, field: 1, "native_tag_count": 1}},
            {"$sort": {"native_tag_count": -1, "_id": 1}},
            {"$limit": _limit_value(slot)},
        ]
        intent = f"{field} contains at least {max(1, len(selected))} tags"

    return _compiler_output(
        slot,
        feature,
        manifest,
        pipeline,
        constructs=list(dict.fromkeys(constructs)),
        compiler="semantic_snapshot_variant",
        intent=intent,
    )


def _semantic_nested_event_variant(
    slot: NativeCoverageSlot,
    feature: NativeFeature,
    manifest: NativeFeatureManifest,
    snapshot: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, Any] | None:
    docs = _snapshot_docs(feature, snapshot)
    context = _semantic_context(docs, skip_prefixes={_field(feature)}, slot=slot)
    context_project = context["project"]
    profile = _event_profile(feature, snapshot)
    if not profile["event_types"] and not profile["event_times"] and not profile["numeric_fields"]:
        return None
    field = _field(feature)
    serial = _slot_serial(slot)
    event_type = _pick(profile["event_types"], slot, default=None)
    event_time = _pick(profile["event_times"], slot, default=None)
    metric = _pick(profile["numeric_fields"], slot, default=None)
    if event_type is not None and event_time is not None:
        event_time = _pick(
            profile["event_times_by_type"].get(event_type, []),
            slot,
            default=event_time,
        )
    if event_type is not None and metric is not None:
        typed_thresholds = profile["thresholds_by_type"].get(event_type, {})
        if metric not in typed_thresholds:
            metric = _pick(sorted(typed_thresholds), slot, default=metric)
    threshold = (
        _pick(
            profile["thresholds_by_type"].get(event_type, {}).get(metric, [])
            or profile["thresholds"].get(metric, []),
            slot,
            default=0,
        )
        if metric
        else 0
    )
    safe_events: dict[str, Any] = _safe_array_expr(field)
    variant = serial % 7
    if variant == 0 and event_type is not None:
        cond: Any = {"$eq": ["$$event.event_type", event_type]}
        intent = f"nested {field} events have event_type {event_type!r}"
    elif variant == 1 and event_time is not None:
        cond = {"$gte": ["$$event.event_time", event_time]}
        intent = f"nested {field} events occur on or after {event_time!r}"
    elif variant == 2 and metric is not None:
        cond = {"$gte": [f"$$event.{metric}", threshold]}
        intent = f"nested {field} events have {metric} at least {threshold}"
    elif variant == 3 and event_type is not None and event_time is not None:
        cond = {"$and": [{"$eq": ["$$event.event_type", event_type]}, {"$gte": ["$$event.event_time", event_time]}]}
        intent = f"nested {field} events are {event_type!r} on or after {event_time!r}"
    elif variant == 4 and event_type is not None and metric is not None:
        cond = {"$and": [{"$eq": ["$$event.event_type", event_type]}, {"$gte": [f"$$event.{metric}", threshold]}]}
        intent = f"nested {field} events are {event_type!r} with {metric} at least {threshold}"
    elif variant == 5:
        cond = {"$ne": ["$$event.event_time", None]}
        intent = f"nested {field} events have recorded event_time"
    else:
        cond = {"$and": [{"$ne": ["$$event.event_type", None]}, {"$ne": ["$$event.event_time", None]}]}
        intent = f"nested {field} events have both event_type and event_time"
    pipeline = [
        *context["stages"],
        {
            "$addFields": {
                "native_filtered_events": {
                    "$filter": {
                        "input": safe_events,
                        "as": "event",
                        "cond": cond,
                    }
                }
            }
        },
        {"$match": {"$expr": {"$gt": [{"$size": "$native_filtered_events"}, 0]}}},
        {
            "$project": {
                "_id": 1,
                field: 1,
                **context_project,
                "native_filtered_events": 1,
                "native_event_count": {"$size": "$native_filtered_events"},
            }
        },
        {"$sort": {"native_event_count": -1, "_id": 1}},
        {"$limit": _limit_value(slot)},
    ]
    if context["intent"]:
        intent = f"{intent} with {context['intent']}"
    return _compiler_output(
        slot,
        feature,
        manifest,
        pipeline,
        constructs=["$filter", "$ifNull", "$size"] + list(context["constructs"]),
        compiler="semantic_snapshot_variant",
        intent=intent,
    )


def _semantic_polymorphic_variant(
    slot: NativeCoverageSlot,
    feature: NativeFeature,
    manifest: NativeFeatureManifest,
    snapshot: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, Any] | None:
    variants = _variant_values(feature, snapshot) or _variants(feature)
    if not variants:
        return None
    field = _field(feature)
    serial = _slot_serial(slot)
    selected = _pick(variants, slot, default=variants[0])
    subset = _window(variants, serial, width=1 + (serial % min(3, len(variants))))
    switch_expr = {
        "$switch": {
            "branches": [
                {"case": {"$eq": [f"${field}", value]}, "then": value}
                for value in variants[:8]
            ],
            "default": "other",
        }
    }
    variant = serial % 5
    if variant == 0:
        pipeline = [
            {"$addFields": {"native_subtype_bucket": switch_expr}},
            {"$match": {"native_subtype_bucket": selected}},
            {"$project": {"_id": 1, field: 1, "native_subtype_bucket": 1}},
            *_result_order_and_limit(slot),
        ]
        intent = f"subtype discriminator {field} resolves to {selected!r}"
    elif variant == 1:
        pipeline = [
            {"$addFields": {"native_subtype_bucket": switch_expr}},
            {"$match": {"native_subtype_bucket": {"$in": subset}}},
            {"$project": {"_id": 1, field: 1, "native_subtype_bucket": 1}},
            *_result_order_and_limit(slot),
        ]
        intent = f"subtype discriminator {field} is one of {subset!r}"
    elif variant == 2:
        pipeline = [
            {"$addFields": {"native_subtype_bucket": switch_expr}},
            {"$group": {"_id": "$native_subtype_bucket", "document_count": {"$sum": 1}}},
            {"$sort": {"document_count": -1, "_id": 1}},
            {"$limit": _limit_value(slot)},
        ]
        intent = f"count documents by native subtype discriminator {field}"
    elif variant == 3:
        pipeline = [
            {"$addFields": {"native_subtype_bucket": switch_expr}},
            {
                "$project": {
                    "_id": 1,
                    field: 1,
                    "native_subtype_bucket": 1,
                    "native_document_field_names": {
                        "$map": {
                            "input": {"$objectToArray": "$$ROOT"},
                            "as": "kv",
                            "in": "$$kv.k",
                        }
                    },
                }
            },
            {"$match": {"native_subtype_bucket": selected}},
            *_result_order_and_limit(slot),
        ]
        intent = f"inspect document field names for subtype {selected!r} under {field}"
    else:
        pipeline = [
            {"$addFields": {"native_subtype_bucket": switch_expr}},
            {"$match": {field: {"$ne": None}}},
            {"$project": {"_id": 1, field: 1, "native_subtype_bucket": 1}},
            *_result_order_and_limit(slot),
        ]
        intent = f"dispatch non-null {field} values into native subtype buckets"
    return _compiler_output(
        slot,
        feature,
        manifest,
        pipeline,
        constructs=["$switch"],
        compiler="semantic_snapshot_variant",
        intent=intent,
    )


def _semantic_presence_variant(
    slot: NativeCoverageSlot,
    feature: NativeFeature,
    manifest: NativeFeatureManifest,
    snapshot: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, Any] | None:
    docs = _snapshot_docs(feature, snapshot)
    if not docs:
        return None
    field = _field(feature)
    serial = _slot_serial(slot)
    context = _semantic_context(docs, skip_prefixes={field}, slot=slot)
    context_project = context["project"]
    states = _presence_states(feature, docs)
    state = _pick(states, slot, default="missing")
    scalar_profile = _collection_scalar_profile(docs, skip_prefixes={field})
    numeric_path = _pick(scalar_profile["numeric_paths"], slot, default="")
    threshold = (
        _pick(scalar_profile["thresholds"].get(numeric_path, []), slot, default=0)
        if numeric_path
        else 0
    )
    categorical_path = _pick(scalar_profile["categorical_paths"], slot, default="")
    categorical_value = (
        _pick(scalar_profile["categorical_values"].get(categorical_path, []), slot, default=None)
        if categorical_path
        else None
    )
    base: list[dict[str, Any]] = [
        *context["stages"],
        {"$addFields": {"native_presence_state": {"$ifNull": [f"${field}", "missing"]}}},
        {"$match": {"native_presence_state": state}},
    ]
    variant = serial % 7
    intent = f"{field} has explicit presence state {state!r}"
    if context["intent"]:
        intent += f" with {context['intent']}"
    if state == "present" and variant in {1, 4} and numeric_path:
        base.append({"$match": {numeric_path: {"$gte": threshold}}})
        intent += f" while {numeric_path} is at least {threshold}"
    elif state == "present" and variant in {2, 5} and categorical_path and categorical_value is not None:
        base.append({"$match": {categorical_path: categorical_value}})
        intent += f" while {categorical_path} equals {categorical_value!r}"

    if variant in {3, 4, 5}:
        group_id: Any = "$native_presence_state"
        if variant == 4 and numeric_path:
            group_id = {"state": "$native_presence_state", "numeric_path": numeric_path}
        if variant == 5 and categorical_path:
            group_id = {"state": "$native_presence_state", "category": f"${categorical_path}"}
        pipeline = [
            *base,
            {
                "$group": {
                    "_id": {
                        "presence": group_id,
                        "context": "$native_context_bucket",
                    } if context_project else group_id,
                    "document_count": {"$sum": 1},
                }
            },
            {"$sort": {"document_count": -1, "_id": 1}},
            {"$limit": _limit_value(slot)},
        ]
        constructs = ["$ifNull", "$group", "$sum"] + list(context["constructs"])
    else:
        project: dict[str, Any] = {"_id": 1, "native_presence_state": 1, field: 1, **context_project}
        if numeric_path:
            project[numeric_path.replace(".", "_")] = f"${numeric_path}"
        if categorical_path:
            project[categorical_path.replace(".", "_")] = f"${categorical_path}"
        pipeline = [
            *base,
            {"$project": project},
            *_result_order_and_limit(slot),
        ]
        constructs = ["$ifNull"] + list(context["constructs"])
    return _compiler_output(
        slot,
        feature,
        manifest,
        pipeline,
        constructs=constructs,
        compiler="semantic_snapshot_variant",
        intent=intent,
    )


def _feature_pipeline_blueprint(
    slot: NativeCoverageSlot,
    manifest: NativeFeatureManifest | Iterable[NativeFeatureManifest],
) -> dict[str, Any] | None:
    feature, _feature_manifest = _resolve_feature(slot, manifest)
    blueprints = feature.extra.get("pipeline_blueprints") if isinstance(feature.extra, dict) else None
    if not isinstance(blueprints, list):
        return None
    fallback_without_pattern: dict[str, Any] | None = None
    for item in blueprints:
        if not isinstance(item, dict):
            continue
        item_pattern = str(item.get("query_pattern") or "")
        if not item_pattern and fallback_without_pattern is None:
            fallback_without_pattern = item
        if item_pattern == slot.query_pattern:
            return item
    return fallback_without_pattern


def _compiler_output(
    slot: NativeCoverageSlot,
    feature: NativeFeature,
    manifest: NativeFeatureManifest,
    pipeline: list[dict[str, Any]],
    *,
    constructs: list[str],
    compiler: str,
    intent: str = "",
) -> dict[str, Any]:
    mql = _mql(feature.collection, pipeline)
    cfs = derive_canonical_form_set(mql, slot.target_shape_policy)
    cfs["native_must_contain"] = list(constructs)
    verification = verify_native_record(
        {
            "db_id": slot.db_id,
            "MQL": mql,
            "native_metadata": {
                "feature_id": feature.id,
                "feature_type": feature.type,
                "query_pattern": slot.query_pattern,
                "anti_sql_transfer_target": slot.anti_sql_transfer_target,
            },
        },
        manifest,
    )
    verification_payload = verification.to_dict()
    verification_payload["compiler"] = compiler
    if intent:
        verification_payload["intent"] = intent
    return {
        "MQL": mql,
        "canonical_form_set": cfs,
        "shape_policy": slot.target_shape_policy,
        "mongo_native_constructs": list(constructs),
        "provenance_refs": list(feature.provenance_refs),
        "native_verification": verification_payload,
        "intent": intent,
    }


def _mql(collection: str, pipeline: list[dict[str, Any]]) -> str:
    return f"db.{collection}.aggregate({json.dumps(pipeline, sort_keys=True, separators=(',', ':'))})"


def _field(feature: NativeFeature) -> str:
    return feature.field or feature.id.rsplit(".", 1)[-1]


def _target_tags(feature: NativeFeature) -> list[str]:
    tags = feature.extra.get("target_tags") if isinstance(feature.extra, dict) else None
    if isinstance(tags, list) and tags:
        return [str(tag) for tag in tags]
    return ["active_debt", "low_balance"]


def _variants(feature: NativeFeature) -> list[str]:
    variants = feature.extra.get("variants") if isinstance(feature.extra, dict) else None
    if isinstance(variants, list) and variants:
        return [str(variant) for variant in variants]
    return ["account", "card", "loan"]


def _snapshot_docs(
    feature: NativeFeature,
    snapshot: dict[str, list[dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return []
    docs = snapshot.get(feature.collection)
    if not isinstance(docs, list):
        return []
    return [doc for doc in docs if isinstance(doc, dict)]


def _window(values: list[str], serial: int, *, width: int) -> list[str]:
    ordered = list(dict.fromkeys(str(value) for value in values if value is not None))
    if not ordered:
        return []
    width = max(1, min(width, len(ordered)))
    start = serial % len(ordered)
    return [ordered[(start + offset) % len(ordered)] for offset in range(width)]


def _dynamic_profile(
    feature: NativeFeature,
    snapshot: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    field = _field(feature)
    key_counts: Counter[str] = Counter()
    key_lengths: list[int] = []
    metric_values: dict[str, list[int | float]] = {}
    for doc in _snapshot_docs(feature, snapshot):
        for value in _get_path_values(doc, field):
            if not isinstance(value, dict):
                continue
            key_lengths.append(len(value))
            for key, bucket in value.items():
                key_counts[str(key)] += 1
                if not isinstance(bucket, dict):
                    continue
                _collect_numeric_paths(bucket, (), metric_values, max_depth=4)
    metrics = sorted(
        metric_values,
        key=lambda name: (-len(metric_values[name]), name),
    )
    thresholds = {
        metric: _sample_thresholds(values)
        for metric, values in metric_values.items()
        if values
    }
    return {
        "keys": [key for key, _ in key_counts.most_common(64)],
        "metrics": metrics[:16],
        "thresholds": thresholds,
        "max_key_count": max(key_lengths) if key_lengths else 1,
    }


def _collect_numeric_paths(
    value: Any,
    path: tuple[str, ...],
    out: dict[str, list[int | float]],
    *,
    max_depth: int,
) -> None:
    if len(path) > max_depth:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _collect_numeric_paths(item, path + (str(key),), out, max_depth=max_depth)
        return
    if isinstance(value, list):
        for item in value[:8]:
            _collect_numeric_paths(item, path, out, max_depth=max_depth)
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not path:
        return
    out.setdefault(".".join(path), []).append(value)


def _dynamic_key_metric_values(
    feature: NativeFeature,
    snapshot: dict[str, list[dict[str, Any]]] | None,
    key: str,
    metric: str,
) -> list[int | float]:
    field = _field(feature)
    values: list[int | float] = []
    for doc in _snapshot_docs(feature, snapshot):
        for dynamic_object in _get_path_values(doc, field):
            if not isinstance(dynamic_object, dict):
                continue
            bucket = dynamic_object.get(key)
            for value in _get_path_values(bucket, metric) if isinstance(bucket, dict) else []:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                values.append(value)
    return values


def _compatible_dynamic_key_metric_pairs(
    feature: NativeFeature,
    snapshot: dict[str, list[dict[str, Any]]] | None,
    *,
    keys: list[str],
    metrics: list[str],
) -> list[tuple[str, str, list[int | float]]]:
    pairs: list[tuple[str, str, list[int | float]]] = []
    for key in keys:
        for metric in metrics:
            values = _dynamic_key_metric_values(feature, snapshot, key, metric)
            if values:
                pairs.append((key, metric, values))
    return sorted(pairs, key=lambda item: (-len(item[2]), item[0], item[1]))


def _dynamic_key_metric_threshold(
    values: list[int | float],
    slot: NativeCoverageSlot,
) -> int | float:
    thresholds = _sample_thresholds(values)
    return _pick(thresholds, slot, default=0)


def _tag_values(
    feature: NativeFeature,
    snapshot: dict[str, list[dict[str, Any]]] | None,
) -> list[str]:
    field = _field(feature)
    counts: Counter[str] = Counter()
    for doc in _snapshot_docs(feature, snapshot):
        for tags in _get_path_values(doc, field):
            if isinstance(tags, list):
                counts.update(str(tag) for tag in tags if tag is not None)
    return [tag for tag, _ in counts.most_common(32)]


def _tag_subset(tags: list[str], slot: NativeCoverageSlot) -> list[str]:
    ordered = sorted(dict.fromkeys(str(tag) for tag in tags if tag is not None))
    if not ordered:
        return []
    serial = _slot_serial(slot)
    width = 1 + (serial % min(3, len(ordered)))
    start = serial % len(ordered)
    subset = [ordered[(start + offset) % len(ordered)] for offset in range(width)]
    return sorted(dict.fromkeys(subset))


def _event_profile(
    feature: NativeFeature,
    snapshot: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    field = _field(feature)
    event_types: Counter[str] = Counter()
    event_times: Counter[str] = Counter()
    numeric_values: dict[str, list[int | float]] = {}
    event_times_by_type: dict[str, list[str]] = {}
    numeric_values_by_type: dict[str, dict[str, list[int | float]]] = {}
    for doc in _snapshot_docs(feature, snapshot):
        for events in _get_path_values(doc, field):
            if not isinstance(events, list):
                continue
            for event in events:
                if not isinstance(event, dict):
                    continue
                event_type = event.get("event_type")
                event_time = event.get("event_time")
                if event.get("event_type") is not None:
                    event_types[str(event_type)] += 1
                if event_time is not None:
                    event_times[str(event_time)] += 1
                    if event_type is not None:
                        event_times_by_type.setdefault(str(event_type), []).append(str(event_time))
                for key, value in event.items():
                    if key in {"event_type", "event_time"}:
                        continue
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        continue
                    metric_name = str(key)
                    numeric_values.setdefault(metric_name, []).append(value)
                    if event_type is not None:
                        numeric_values_by_type.setdefault(str(event_type), {}).setdefault(metric_name, []).append(value)
    numeric_fields = sorted(numeric_values, key=lambda name: (-len(numeric_values[name]), name))
    return {
        "event_types": [value for value, _ in event_types.most_common(64)],
        "event_times": _spread_values([value for value, _ in event_times.most_common(256)], limit=64),
        "event_times_by_type": {
            key: _spread_values(sorted(dict.fromkeys(values)), limit=64)
            for key, values in event_times_by_type.items()
        },
        "numeric_fields": numeric_fields[:16],
        "thresholds": {
            field_name: _sample_thresholds(values)
            for field_name, values in numeric_values.items()
            if values
        },
        "thresholds_by_type": {
            event_type: {
                field_name: _sample_thresholds(values)
                for field_name, values in fields.items()
                if values
            }
            for event_type, fields in numeric_values_by_type.items()
        },
    }


def _variant_values(
    feature: NativeFeature,
    snapshot: dict[str, list[dict[str, Any]]] | None,
) -> list[str]:
    field = _field(feature)
    counts: Counter[str] = Counter()
    for doc in _snapshot_docs(feature, snapshot):
        for value in _get_path_values(doc, field):
            if value is not None:
                counts[str(value)] += 1
    return [value for value, _ in counts.most_common(16)]


def _presence_states(feature: NativeFeature, docs: list[dict[str, Any]]) -> list[str]:
    field = _field(feature)
    counts: Counter[str] = Counter()
    for doc in docs:
        value = _get_path(doc, field)
        if isinstance(value, str) and value.lower() in {"present", "missing", "empty", "null"}:
            counts[value.lower()] += 1
        elif value is None:
            counts["missing"] += 1
        else:
            counts["present"] += 1
    if not counts:
        return ["missing"]
    return [state for state, _ in counts.most_common()]


def _collection_scalar_profile(
    docs: list[dict[str, Any]],
    *,
    skip_prefixes: set[str],
) -> dict[str, Any]:
    numeric_values: dict[str, list[int | float]] = defaultdict(list)
    categorical_values: dict[str, Counter[str]] = defaultdict(Counter)
    for doc in docs[:2000]:
        _collect_scalar_paths(
            doc,
            (),
            numeric_values,
            categorical_values,
            skip_prefixes=skip_prefixes,
            max_depth=5,
        )
    numeric_paths = sorted(
        numeric_values,
        key=lambda path: (-len(numeric_values[path]), path),
    )[:32]
    categorical_paths = sorted(
        categorical_values,
        key=lambda path: (-sum(categorical_values[path].values()), path),
    )[:32]
    return {
        "numeric_paths": numeric_paths,
        "thresholds": {
            path: _sample_thresholds(values)
            for path, values in numeric_values.items()
            if values
        },
        "categorical_paths": categorical_paths,
        "categorical_values": {
            path: [value for value, _ in counts.most_common(16)]
            for path, counts in categorical_values.items()
        },
    }


def _semantic_context(
    docs: list[dict[str, Any]],
    *,
    skip_prefixes: set[str],
    slot: NativeCoverageSlot,
) -> dict[str, Any]:
    profile = _collection_scalar_profile(docs, skip_prefixes=skip_prefixes)
    serial = _slot_serial(slot)
    numeric_paths = profile["numeric_paths"]
    categorical_paths = profile["categorical_paths"]
    if numeric_paths and (serial % 2 == 0 or not categorical_paths):
        path = numeric_paths[(serial // 2) % len(numeric_paths)]
        thresholds = profile["thresholds"].get(path, [])
        threshold = thresholds[(serial // max(1, len(numeric_paths))) % len(thresholds)] if thresholds else 0
        return {
            "stages": [
                {
                    "$addFields": {
                        "native_context_bucket": {
                            "$cond": [
                                {"$gte": [f"${path}", threshold]},
                                f"{path}>= {threshold}",
                                f"{path}< {threshold}",
                            ]
                        }
                    }
                }
            ],
            "project": {"native_context_bucket": 1},
            "intent": f"context bucketed by {path} around {threshold}",
            "constructs": ["$cond"],
        }
    if categorical_paths:
        path = categorical_paths[serial % len(categorical_paths)]
        return {
            "stages": [
                {
                    "$addFields": {
                        "native_context_bucket": {
                            "$ifNull": [f"${path}", "missing"]
                        }
                    }
                }
            ],
            "project": {"native_context_bucket": 1},
            "intent": f"context bucketed by {path}",
            "constructs": ["$ifNull"],
        }
    return {"stages": [], "project": {}, "intent": "", "constructs": []}


def _collect_scalar_paths(
    value: Any,
    path: tuple[str, ...],
    numeric_values: dict[str, list[int | float]],
    categorical_values: dict[str, Counter[str]],
    *,
    skip_prefixes: set[str],
    max_depth: int,
) -> None:
    path_text = ".".join(path)
    if path_text and any(path_text == prefix or path_text.startswith(prefix + ".") for prefix in skip_prefixes):
        return
    if len(path) > max_depth:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            if key_text.startswith("_"):
                continue
            _collect_scalar_paths(
                item,
                path + (key_text,),
                numeric_values,
                categorical_values,
                skip_prefixes=skip_prefixes,
                max_depth=max_depth,
            )
        return
    if isinstance(value, list):
        return
    if not path_text:
        return
    if isinstance(value, bool):
        categorical_values[path_text][str(value).lower()] += 1
    elif isinstance(value, (int, float)):
        numeric_values[path_text].append(value)
    elif isinstance(value, str) and 0 < len(value) <= 64:
        categorical_values[path_text][value] += 1


def _get_path(doc: dict[str, Any], path: str) -> Any:
    current: Any = doc
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _get_path_values(doc: dict[str, Any], path: str) -> list[Any]:
    values: list[Any] = [doc]
    for part in path.split("."):
        next_values: list[Any] = []
        for value in values:
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and part in item:
                        next_values.append(item[part])
            elif isinstance(value, dict) and part in value:
                next_values.append(value[part])
        values = next_values
        if not values:
            return []
    return values


def _array_prefix_for_path(docs: list[dict[str, Any]], path: str) -> str | None:
    parts = path.split(".")
    for doc in docs[:200]:
        current: Any = doc
        prefix: list[str] = []
        for part in parts[:-1]:
            if not isinstance(current, dict) or part not in current:
                break
            current = current[part]
            prefix.append(part)
            if isinstance(current, list):
                return ".".join(prefix)
    return None


def _safe_array_expr(path: str) -> dict[str, Any]:
    value = f"${path}"
    return {"$cond": [{"$isArray": value}, value, []]}


def _sample_thresholds(values: list[int | float]) -> list[int | float]:
    ordered = sorted(value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool))
    if not ordered:
        return [0]
    points = [0.25, 0.5, 0.75]
    out: list[int | float] = []
    for point in points:
        index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * point)))
        out.append(ordered[index])
    return sorted(dict.fromkeys(out))


def _spread_values(values: list[str], *, limit: int) -> list[str]:
    if len(values) <= limit:
        return values
    step = max(1, len(values) // limit)
    return values[::step][:limit]


def _pick(values: list[Any], slot: NativeCoverageSlot, *, default: Any) -> Any:
    if not values:
        return default
    return values[_slot_serial(slot) % len(values)]


def _schema_flex_for_feature_type(feature_type: str) -> str:
    return {
        "dynamic_key_object": "dynamic_key",
        "polymorphic_collection": "polymorphic",
        "derived_tag_array": "derived_tag_array",
        "nested_event_stream": "nested_event_stream",
        "missing_vs_present": "missing_vs_present",
    }.get(feature_type, "attribute_bag")


def _canonical_nl(
    slot: NativeCoverageSlot,
    feature: NativeFeature,
    compiled: dict[str, Any] | None = None,
) -> str:
    field = _field(feature)
    limit = _limit_value(slot)
    intent = str((compiled or {}).get("intent") or "")
    if slot.feature_type == "dynamic_key_object":
        return (
            f"For each {feature.collection} document, inspect the dynamic keys under {field} "
            f"and keep entries where {intent or 'the native key condition holds'}, "
            f"returning up to {limit} documents."
        )
    if slot.feature_type == "polymorphic_collection":
        return (
            f"Dispatch each {feature.collection} document by its {field} discriminator "
            f"and {intent or 'output the native subtype bucket'}, returning up to {limit} documents."
        )
    if slot.feature_type == "derived_tag_array":
        return (
            f"Find up to {limit} {feature.collection} documents whose {field} tag array "
            f"satisfies this set condition: {intent or 'overlaps the selected tag set'}."
        )
    if slot.feature_type == "nested_event_stream":
        return (
            f"Filter each {feature.collection} document's nested {field} array in place for "
            f"{intent or 'matching events'}."
            f" Return up to {limit} documents."
        )
    return (
        f"Classify up to {limit} {feature.collection} documents by whether native field {field} is missing or present."
    )


def _colloquial_nl(
    slot: NativeCoverageSlot,
    feature: NativeFeature,
    compiled: dict[str, Any] | None = None,
) -> str:
    field = _field(feature)
    limit = _limit_value(slot)
    intent = str((compiled or {}).get("intent") or "")
    if slot.feature_type == "dynamic_key_object":
        return f"Show up to {limit} {feature.collection} rows where {field} has {intent or 'useful dynamic entries'}."
    if slot.feature_type == "polymorphic_collection":
        return f"Label up to {limit} {feature.collection} items by {field}; {intent or 'keep the subtype bucket'}."
    if slot.feature_type == "derived_tag_array":
        return f"Show up to {limit} {feature.collection} items where {field} tags match: {intent or 'selected tags'}."
    if slot.feature_type == "nested_event_stream":
        return f"Keep {intent or 'matching'} nested {field} events for up to {limit} {feature.collection} items."
    return f"Show up to {limit} {feature.collection} items that are missing {field}."


def _record_id_from_slot(slot: NativeCoverageSlot) -> int:
    digest = sha256(slot.slot_id.encode("utf-8")).hexdigest()
    return 1000 + int(digest[:6], 16)


def _stable_rank(seed: int, *parts: str) -> str:
    return sha256(("|".join([str(seed), *parts])).encode("utf-8")).hexdigest()


def _slot_serial(slot: NativeCoverageSlot) -> int:
    parts = slot.slot_id.split(":")
    for part in parts:
        if len(part) > 1 and part[0] == "u" and part[1:].isdigit():
            return int(part[1:])
    for part in parts:
        if part.isdigit():
            return int(part)
    digest = sha256(slot.slot_id.encode("utf-8")).hexdigest()
    return int(digest[:4], 16)


def _limit_value(slot: NativeCoverageSlot) -> int:
    return [10, 25, 50, 100][_slot_serial(slot) % 4]


def _result_order_and_limit(slot: NativeCoverageSlot, *, grouped: bool = False) -> list[dict[str, Any]]:
    sort_field = "native_subtype_bucket" if grouped else "_id"
    return [{"$sort": {sort_field: 1}}, {"$limit": _limit_value(slot)}]
