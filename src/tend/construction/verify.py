"""Verifier for MongoDB-native Phase B records.

The checks here are structural and deterministic. They do not execute MongoDB;
they certify that a record's gold MQL actually exercises the native feature
advertised by a :class:`NativeFeatureManifest`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tend.construction.recipe import NativeFeature, NativeFeatureManifest
from tend.execution import parse_pipeline
from tend.errors import ResponseParseError


@dataclass
class AntiSqlTransferReport:
    level: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "evidence": list(self.evidence)}


@dataclass
class NativeVerificationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    anti_sql_transfer: AntiSqlTransferReport = field(
        default_factory=lambda: AntiSqlTransferReport(level="weak")
    )
    feature_id: str = ""
    feature_type: str = ""
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "anti_sql_transfer": self.anti_sql_transfer.to_dict(),
            "feature_id": self.feature_id,
            "feature_type": self.feature_type,
            "evidence": list(self.evidence),
        }


@dataclass
class _MqlShape:
    collection: str
    pipeline: list[Any]
    raw: str
    parse_error: str = ""


STRONG_NATIVE_OPS = frozenset(
    {"$objectToArray", "$switch", "$setIntersection", "$setIsSubset", "$setUnion"}
)
MEDIUM_NATIVE_OPS = frozenset(
    {"$filter", "$map", "$reduce", "$type", "$exists", "$ifNull", "$cond"}
)
SQL_TRANSFER_OPS = frozenset({"$lookup", "$unwind", "$group"})


def classify_anti_sql_transfer(record: dict[str, Any]) -> AntiSqlTransferReport:
    """Classify whether the record resists SQL-shaped transfer.

    ``weak`` means the MQL is mostly relational aggregation/flattening or has no
    Mongo-native construct. ``medium`` means it uses a native expression, but not
    enough feature-specific structure. ``strong`` means it combines a strong
    native construct with additional native evidence or explicit verification.
    """
    shape = _mql_shape(record.get("MQL"))
    ops = _ops(shape.pipeline)
    evidence: list[str] = []

    sql_ops = sorted(ops & SQL_TRANSFER_OPS)
    native_strong = sorted(ops & STRONG_NATIVE_OPS)
    native_medium = sorted(ops & MEDIUM_NATIVE_OPS)
    verified = _native_verification_ok(record)
    metadata_target = _native_metadata(record).get("anti_sql_transfer_target")

    if sql_ops:
        evidence.append(f"sql_transfer_ops={','.join(sql_ops)}")
    if native_strong:
        evidence.append(f"strong_native_ops={','.join(native_strong)}")
    if native_medium:
        evidence.append(f"native_ops={','.join(native_medium)}")
    if verified:
        evidence.append("native_verification_ok")
    if metadata_target:
        evidence.append(f"target={metadata_target}")

    if "$unwind" in ops and "$group" in ops and not native_strong and "$filter" not in ops:
        return AntiSqlTransferReport(level="weak", evidence=evidence or ["unwind_group_only"])
    if native_strong and (native_medium or verified or metadata_target == "strong"):
        return AntiSqlTransferReport(level="strong", evidence=evidence)
    if native_strong and len(native_strong) >= 2:
        return AntiSqlTransferReport(level="strong", evidence=evidence)
    if native_strong or native_medium:
        return AntiSqlTransferReport(level="medium", evidence=evidence)
    return AntiSqlTransferReport(level="weak", evidence=evidence or ["no_native_constructs"])


def verify_native_record(
    record: dict[str, Any],
    manifest: NativeFeatureManifest,
    *,
    executor: Any = None,
    snapshot: Any = None,
) -> NativeVerificationResult:
    """Verify a native Phase B record against a feature manifest."""
    errors: list[str] = []
    evidence: list[str] = []
    feature = _record_feature(record, manifest)
    shape = _mql_shape(record.get("MQL"))

    if feature is None:
        errors.append("record does not identify a feature from the native manifest")
        feature_id = ""
        feature_type = ""
    else:
        feature_id = feature.id
        feature_type = feature.type
        evidence.append(f"feature={feature.id}")

    if shape.parse_error:
        errors.append(f"MQL parse error: {shape.parse_error}")

    if feature is not None and not shape.parse_error:
        _verify_feature_contract(feature, shape, errors, evidence)

    if executor is not None and not errors:
        _verify_executor(record, executor, snapshot, errors, evidence)

    report_record = dict(record)
    if not isinstance(report_record.get("native_verification"), dict):
        report_record["native_verification"] = {"ok": not errors}
    anti_sql_transfer = classify_anti_sql_transfer(report_record)
    return NativeVerificationResult(
        ok=not errors,
        errors=errors,
        anti_sql_transfer=anti_sql_transfer,
        feature_id=feature_id,
        feature_type=feature_type,
        evidence=evidence,
    )


def _verify_feature_contract(
    feature: NativeFeature,
    shape: _MqlShape,
    errors: list[str],
    evidence: list[str],
) -> None:
    ops = _ops(shape.pipeline)
    feature_path_ok = _mentions_feature_path(shape, feature)
    field = feature.field or feature.id.rsplit(".", 1)[-1]

    if feature.type == "dynamic_key_object":
        if "$objectToArray" not in ops:
            errors.append(
                f"dynamic key feature {feature.id} requires $objectToArray over {field}"
            )
        if not feature_path_ok:
            errors.append(f"dynamic key feature {feature.id} does not access feature path {field}")
        if "$objectToArray" in ops and feature_path_ok:
            evidence.append("dynamic_key_object_path_and_objectToArray")
        return

    if feature.type == "polymorphic_collection":
        has_switch = "$switch" in ops
        has_discriminator_branch = feature_path_ok and bool(
            ops & {"$cond", "$eq", "$in"} or _raw_has(shape, f'"{field}"')
        )
        if not has_switch and not has_discriminator_branch:
            errors.append(
                f"polymorphic feature {feature.id} requires $switch or discriminator branch on {field}"
            )
        else:
            evidence.append("polymorphic_discriminator_dispatch")
        return

    if feature.type == "derived_tag_array":
        if not (ops & {"$setIntersection", "$setIsSubset", "$setUnion"}):
            errors.append(f"tag feature {feature.id} requires a tag set operator")
        if not feature_path_ok:
            errors.append(f"tag feature {feature.id} does not access feature path {field}")
        if "$size" not in ops and "$setIsSubset" not in ops:
            errors.append(f"tag feature {feature.id} requires $size or subset semantics")
        if not errors:
            evidence.append("derived_tag_array_set_logic")
        return

    if feature.type == "nested_event_stream":
        if "$unwind" in ops and "$group" in ops and "$filter" not in ops:
            errors.append(
                "nested event feature requires shape-preserving $filter; $unwind+$group alone is SQL-transfer shaped"
            )
        if "$filter" not in ops:
            errors.append(f"nested event feature {feature.id} requires shape-preserving $filter")
        if not feature_path_ok:
            errors.append(f"nested event feature {feature.id} does not access feature path {field}")
        if "$filter" in ops and feature_path_ok:
            evidence.append("nested_event_shape_preserving_filter")
        return

    if feature.type == "missing_vs_present":
        if not _has_missing_expression(shape, feature):
            errors.append(
                f"missing-field feature {feature.id} requires $exists, $type, or equivalent missing expression"
            )
        else:
            evidence.append("missing_vs_present_expression")


def _verify_executor(
    record: dict[str, Any],
    executor: Any,
    snapshot: Any,
    errors: list[str],
    evidence: list[str],
) -> None:
    try:
        try:
            result = executor(record, snapshot=snapshot)
        except TypeError:
            result = executor(record)
    except Exception as exc:  # noqa: BLE001 - caller-provided verifier surface
        errors.append(f"executor verification failed: {exc}")
        return

    ok = getattr(result, "ok", None)
    result_errors = getattr(result, "errors", None)
    if isinstance(result, dict):
        ok = result.get("ok", ok)
        result_errors = result.get("errors", result_errors)
    if ok is False:
        errors.extend(str(error) for error in (result_errors or ["executor returned not ok"]))
    elif result is not None:
        evidence.append("executor_verified")


def _record_feature(record: dict[str, Any], manifest: NativeFeatureManifest) -> NativeFeature | None:
    feature_id = str(
        _native_metadata(record).get("feature_id")
        or record.get("feature_id")
        or record.get("schema_feature")
        or ""
    )
    if feature_id:
        for feature in manifest.features:
            if feature.id == feature_id:
                return feature
    shape = _mql_shape(record.get("MQL"))
    for feature in manifest.features:
        if shape.collection == feature.collection and _mentions_feature_path(shape, feature):
            return feature
    if len(manifest.features) == 1:
        return manifest.features[0]
    return None


def _native_metadata(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("native_metadata")
    if isinstance(metadata, dict):
        return metadata
    metadata = record.get("native")
    if isinstance(metadata, dict):
        return metadata
    return {}


def _native_verification_ok(record: dict[str, Any]) -> bool:
    verification = record.get("native_verification")
    if isinstance(verification, dict):
        return verification.get("ok") is True
    return getattr(verification, "ok", False) is True


def _mql_shape(mql: Any) -> _MqlShape:
    if isinstance(mql, str):
        try:
            collection, pipeline = parse_pipeline(mql)
            return _MqlShape(collection=collection, pipeline=pipeline, raw=mql)
        except ResponseParseError as exc:
            return _MqlShape(collection="", pipeline=[], raw=mql, parse_error=str(exc))
    if isinstance(mql, list):
        return _MqlShape(collection="", pipeline=list(mql), raw=str(mql))
    if isinstance(mql, dict):
        pipeline = mql.get("pipeline")
        if isinstance(pipeline, list):
            return _MqlShape(
                collection=str(mql.get("collection") or ""),
                pipeline=list(pipeline),
                raw=str(mql),
            )
        return _MqlShape(collection=str(mql.get("collection") or ""), pipeline=[mql], raw=str(mql))
    return _MqlShape(collection="", pipeline=[], raw=str(mql), parse_error="missing MQL")


def _ops(node: Any) -> set[str]:
    return {
        key
        for key in _walk_keys(node)
        if isinstance(key, str) and key.startswith("$")
    }


def _walk_keys(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_keys(item)


def _walk_strings(node: Any):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                yield key
            yield from _walk_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_strings(item)


def _mentions_feature_path(shape: _MqlShape, feature: NativeFeature) -> bool:
    if not feature.field:
        return False
    field = feature.field
    candidates = {
        field,
        f"${field}",
        f"$${field}",
        f"{feature.collection}.{field}",
        f"${feature.collection}.{field}",
    }
    strings = set(_walk_strings(shape.pipeline))
    if strings & candidates:
        return True
    return any(candidate in shape.raw for candidate in candidates)


def _has_missing_expression(shape: _MqlShape, feature: NativeFeature) -> bool:
    ops = _ops(shape.pipeline)
    if ops & {"$exists", "$type", "$ifNull"}:
        return True
    raw = shape.raw.lower()
    if "$$remove" in raw or '"missing"' in raw or "'missing'" in raw:
        return True
    return _mentions_feature_path(shape, feature) and "null" in raw and "$cond" in ops


def _raw_has(shape: _MqlShape, token: str) -> bool:
    return token in shape.raw
