"""Native Phase B slot planning and deterministic gold-MQL compilers."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Iterable

from tend.construct.native_recipe import NativeFeature, NativeFeatureManifest
from tend.execution import (
    derive_canonical_form_set,
    mql_signature,
    mql_skeleton_signature,
    mql_skeleton_summary,
)

from .native_verify import verify_native_record


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
    threshold = _pick(profile["thresholds"].get(metric, []), slot, default=0) if metric else 0
    safe_events: dict[str, Any] = {"$ifNull": [f"${field}", []]}
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
        slot_id=f"native:{ref.manifest.db_id}:{slot_index:04d}:{feature.type}:{feature.id}",
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


def _dynamic_profile(
    feature: NativeFeature,
    snapshot: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    field = _field(feature)
    key_counts: Counter[str] = Counter()
    key_lengths: list[int] = []
    metric_values: dict[str, list[int | float]] = {}
    for doc in _snapshot_docs(feature, snapshot):
        value = doc.get(field)
        if not isinstance(value, dict):
            continue
        key_lengths.append(len(value))
        for key, bucket in value.items():
            key_counts[str(key)] += 1
            if not isinstance(bucket, dict):
                continue
            for metric, metric_value in bucket.items():
                if isinstance(metric_value, bool) or not isinstance(metric_value, (int, float)):
                    continue
                metric_values.setdefault(str(metric), []).append(metric_value)
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


def _tag_values(
    feature: NativeFeature,
    snapshot: dict[str, list[dict[str, Any]]] | None,
) -> list[str]:
    field = _field(feature)
    counts: Counter[str] = Counter()
    for doc in _snapshot_docs(feature, snapshot):
        tags = doc.get(field)
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
    for doc in _snapshot_docs(feature, snapshot):
        events = doc.get(field)
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("event_type") is not None:
                event_types[str(event["event_type"])] += 1
            if event.get("event_time") is not None:
                event_times[str(event["event_time"])] += 1
            for key, value in event.items():
                if key in {"event_type", "event_time"}:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                numeric_values.setdefault(str(key), []).append(value)
    numeric_fields = sorted(numeric_values, key=lambda name: (-len(numeric_values[name]), name))
    return {
        "event_types": [value for value, _ in event_types.most_common(64)],
        "event_times": _spread_values([value for value, _ in event_times.most_common(256)], limit=64),
        "numeric_fields": numeric_fields[:16],
        "thresholds": {
            field_name: _sample_thresholds(values)
            for field_name, values in numeric_values.items()
            if values
        },
    }


def _variant_values(
    feature: NativeFeature,
    snapshot: dict[str, list[dict[str, Any]]] | None,
) -> list[str]:
    field = _field(feature)
    counts: Counter[str] = Counter()
    for doc in _snapshot_docs(feature, snapshot):
        value = doc.get(field)
        if value is not None:
            counts[str(value)] += 1
    return [value for value, _ in counts.most_common(16)]


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
        if part.isdigit():
            return int(part)
    digest = sha256(slot.slot_id.encode("utf-8")).hexdigest()
    return int(digest[:4], 16)


def _limit_value(slot: NativeCoverageSlot) -> int:
    return [10, 25, 50, 100][_slot_serial(slot) % 4]


def _result_order_and_limit(slot: NativeCoverageSlot, *, grouped: bool = False) -> list[dict[str, Any]]:
    sort_field = "native_subtype_bucket" if grouped else "_id"
    return [{"$sort": {sort_field: 1}}, {"$limit": _limit_value(slot)}]
