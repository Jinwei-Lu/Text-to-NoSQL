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
    cursors: dict[str, int] = {feature_type: 0 for feature_type in FEATURE_TYPE_ORDER}
    slot_index = 0

    while len(slots) < max(0, n_records):
        made_progress = False
        type_counts = Counter(slot.feature_type for slot in slots)
        active_types = [feature_type for feature_type in FEATURE_TYPE_ORDER if by_type[feature_type]]
        active_types.sort(key=lambda feature_type: (type_counts[feature_type], FEATURE_TYPE_ORDER.index(feature_type)))
        for feature_type in active_types:
            if len(slots) >= n_records:
                break
            ref = _next_ref_under_db_cap(
                by_type[feature_type],
                cursors,
                feature_type,
                db_counts,
                records_per_db,
            )
            if ref is None:
                continue
            cursors[feature_type] += 1
            slot_index += 1
            db_counts[ref.manifest.db_id] += 1
            slots.append(_slot_for_feature(ref, slot_index))
            made_progress = True
        if not made_progress:
            break
    return slots


def dynamic_key_comparison(
    slot: NativeCoverageSlot,
    manifest: NativeFeatureManifest | Iterable[NativeFeatureManifest],
) -> dict[str, Any]:
    feature, feature_manifest = _resolve_feature(slot, manifest)
    field = _field(feature)
    pipeline = [
        {"$addFields": {"__native_dynamic_entries": {"$objectToArray": f"${field}"}}},
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
        {
            "$project": {
                "_id": 1,
                field: 1,
                "native_matching_dynamic_keys": 1,
                "native_dynamic_key_count": {"$size": "$native_matching_dynamic_keys"},
            }
        },
    ]
    return _compiler_output(
        slot,
        feature,
        feature_manifest,
        pipeline,
        constructs=["$objectToArray", "$filter"],
        compiler="dynamic_key_comparison",
    )


def subtype_field_dispatch(
    slot: NativeCoverageSlot,
    manifest: NativeFeatureManifest | Iterable[NativeFeatureManifest],
) -> dict[str, Any]:
    feature, feature_manifest = _resolve_feature(slot, manifest)
    field = _field(feature)
    variants = _variants(feature)
    pipeline = [
        {
            "$addFields": {
                "native_subtype_bucket": {
                    "$switch": {
                        "branches": [
                            {"case": {"$eq": [f"${field}", variant]}, "then": variant}
                            for variant in variants[:5]
                        ],
                        "default": "other",
                    }
                }
            }
        },
        {"$project": {"_id": 1, field: 1, "native_subtype_bucket": 1}},
    ]
    return _compiler_output(
        slot,
        feature,
        feature_manifest,
        pipeline,
        constructs=["$switch"],
        compiler="subtype_field_dispatch",
    )


def tag_combination(
    slot: NativeCoverageSlot,
    manifest: NativeFeatureManifest | Iterable[NativeFeatureManifest],
) -> dict[str, Any]:
    feature, feature_manifest = _resolve_feature(slot, manifest)
    field = _field(feature)
    target_tags = _target_tags(feature)
    pipeline = [
        {"$addFields": {"native_tag_overlap": {"$setIntersection": [f"${field}", target_tags]}}},
        {"$match": {"$expr": {"$gt": [{"$size": "$native_tag_overlap"}, 0]}}},
        {"$project": {"_id": 1, field: 1, "native_tag_overlap": 1}},
    ]
    return _compiler_output(
        slot,
        feature,
        feature_manifest,
        pipeline,
        constructs=["$setIntersection", "$size"],
        compiler="tag_combination",
    )


def nested_event_filter(
    slot: NativeCoverageSlot,
    manifest: NativeFeatureManifest | Iterable[NativeFeatureManifest],
) -> dict[str, Any]:
    feature, feature_manifest = _resolve_feature(slot, manifest)
    field = _field(feature)
    pipeline = [
        {
            "$addFields": {
                "native_filtered_events": {
                    "$filter": {
                        "input": f"${field}",
                        "as": "event",
                        "cond": {
                            "$and": [
                                {"$ne": ["$$event.event_type", None]},
                                {"$ne": ["$$event.event_time", None]},
                            ]
                        },
                    }
                }
            }
        },
        {"$match": {"$expr": {"$gt": [{"$size": "$native_filtered_events"}, 0]}}},
        {"$project": {"_id": 1, field: 1, "native_filtered_events": 1}},
    ]
    return _compiler_output(
        slot,
        feature,
        feature_manifest,
        pipeline,
        constructs=["$filter"],
        compiler="nested_event_filter",
    )


def missing_vs_present(
    slot: NativeCoverageSlot,
    manifest: NativeFeatureManifest | Iterable[NativeFeatureManifest],
) -> dict[str, Any]:
    feature, feature_manifest = _resolve_feature(slot, manifest)
    field = _field(feature)
    pipeline = [
        {"$addFields": {"native_missing_state": {"$type": f"${field}"}}},
        {"$match": {"$expr": {"$eq": ["$native_missing_state", "missing"]}}},
        {"$project": {"_id": 1, field: 1, "native_missing_state": 1}},
    ]
    return _compiler_output(
        slot,
        feature,
        feature_manifest,
        pipeline,
        constructs=["$type"],
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
    compiled = _compile_slot(slot, feature_manifest)
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
        "canonical": canonical_nl or _canonical_nl(slot, feature),
        "colloquial": colloquial_nl or _colloquial_nl(slot, feature),
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
) -> _FeatureRef | None:
    if not refs:
        return None
    start = cursors[feature_type]
    for offset in range(len(refs)):
        ref = refs[(start + offset) % len(refs)]
        cap = _db_cap(records_per_db, ref.manifest.db_id)
        if cap is None or db_counts[ref.manifest.db_id] < cap:
            cursors[feature_type] = start + offset
            return ref
    return None


def _slot_for_feature(ref: _FeatureRef, slot_index: int) -> NativeCoverageSlot:
    feature = ref.feature
    query_pattern = (
        feature.query_patterns[0]
        if feature.query_patterns
        else DEFAULT_QUERY_PATTERN[feature.type]
    )
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
) -> dict[str, Any]:
    if slot.query_pattern == "dynamic_key_comparison":
        return dynamic_key_comparison(slot, manifest)
    if slot.query_pattern == "subtype_field_dispatch":
        return subtype_field_dispatch(slot, manifest)
    if slot.query_pattern == "tag_combination":
        return tag_combination(slot, manifest)
    if slot.query_pattern == "nested_event_filter":
        return nested_event_filter(slot, manifest)
    if slot.query_pattern == "missing_vs_present":
        return missing_vs_present(slot, manifest)
    raise ValueError(f"unsupported native query pattern: {slot.query_pattern}")


def _compiler_output(
    slot: NativeCoverageSlot,
    feature: NativeFeature,
    manifest: NativeFeatureManifest,
    pipeline: list[dict[str, Any]],
    *,
    constructs: list[str],
    compiler: str,
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
    return {
        "MQL": mql,
        "canonical_form_set": cfs,
        "shape_policy": slot.target_shape_policy,
        "mongo_native_constructs": list(constructs),
        "provenance_refs": list(feature.provenance_refs),
        "native_verification": verification_payload,
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


def _schema_flex_for_feature_type(feature_type: str) -> str:
    return {
        "dynamic_key_object": "dynamic_key",
        "polymorphic_collection": "polymorphic",
        "derived_tag_array": "derived_tag_array",
        "nested_event_stream": "nested_event_stream",
        "missing_vs_present": "missing_vs_present",
    }.get(feature_type, "attribute_bag")


def _canonical_nl(slot: NativeCoverageSlot, feature: NativeFeature) -> str:
    field = _field(feature)
    if slot.feature_type == "dynamic_key_object":
        return (
            f"For each {feature.collection} document, inspect the dynamic keys under {field} "
            "and keep the non-empty native key entries."
        )
    if slot.feature_type == "polymorphic_collection":
        return (
            f"Dispatch each {feature.collection} document by its {field} discriminator "
            "and output the native subtype bucket."
        )
    if slot.feature_type == "derived_tag_array":
        return (
            f"Find {feature.collection} documents whose {field} tag array overlaps the target tag set."
        )
    if slot.feature_type == "nested_event_stream":
        return (
            f"Filter each {feature.collection} document's nested {field} array in place and keep matching events."
        )
    return (
        f"Classify {feature.collection} documents by whether native field {field} is missing or present."
    )


def _colloquial_nl(slot: NativeCoverageSlot, feature: NativeFeature) -> str:
    field = _field(feature)
    if slot.feature_type == "dynamic_key_object":
        return f"Show the {feature.collection} rows with useful dynamic {field} entries."
    if slot.feature_type == "polymorphic_collection":
        return f"Label each {feature.collection} item by its {field} subtype."
    if slot.feature_type == "derived_tag_array":
        return f"Show {feature.collection} items with any of the target {field} tags."
    if slot.feature_type == "nested_event_stream":
        return f"Keep only matching nested {field} events for each {feature.collection} item."
    return f"Show which {feature.collection} items are missing {field}."


def _record_id_from_slot(slot: NativeCoverageSlot) -> int:
    digest = sha256(slot.slot_id.encode("utf-8")).hexdigest()
    return 1000 + int(digest[:6], 16)


def _stable_rank(seed: int, *parts: str) -> str:
    return sha256(("|".join([str(seed), *parts])).encode("utf-8")).hexdigest()
