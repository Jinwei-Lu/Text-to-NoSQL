"""SMART solver agents.

The construction pipeline already has a dynamic sub-agent engine; the solver reuses that
engine but keeps its own agent ids and contracts. Shape comprehension is schema-only and
fan-out/fan-in deterministic so it cannot accidentally read witness data. Intent and
planning are LLM-backed and therefore produce transcripts/anomalies through the shared
LLM client.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from ..agents import Agent, AgentContext, LLMAgent, register
from ..errors import ContractViolationError
from .contracts import (
    CollectionShape,
    FieldLocus,
    LogicalSpec,
    PhysicalPlan,
    ShapeModel,
    ShapeVariant,
)
from .guards import disabled_hits_in_node, extract_target_fields, infer_shape_policy


_INTENT_SCHEMA = {
    "type": "object",
    "required": ["entity", "per", "compute", "output", "shape_policy"],
    "properties": {
        "entity": {"type": "string"},
        "per": {"type": "string"},
        "compute": {"type": "array", "items": {"type": "object"}},
        "aggregate": {"type": "array", "items": {"type": "object"}},
        "filter": {"type": "array", "items": {"type": "object"}},
        "output": {"type": "object"},
        "shape_policy": {"enum": ["preserve", "reshape", "reduce"]},
        "target_fields": {"type": "array", "items": {"type": "string"}},
        "clause_coverage": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}


_PLAN_SCHEMA = {
    "type": "object",
    "required": ["collection", "stages", "variant_handling"],
    "properties": {
        "collection": {"type": "string"},
        "stages": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["op", "stage"],
                "properties": {
                    "op": {"type": "string"},
                    "note": {"type": "string", "minLength": 1},
                    "rationale": {"type": "object", "minProperties": 1},
                    "diagnostic": {"type": "object", "minProperties": 1},
                    "diagnostics": {"type": "object", "minProperties": 1},
                    "stage": {"type": "object"},
                },
                "anyOf": [
                    {"required": ["note"]},
                    {"required": ["rationale"]},
                    {"required": ["diagnostic"]},
                    {"required": ["diagnostics"]},
                ],
                "additionalProperties": True,
            },
        },
        "variant_handling": {"type": "array", "items": {"type": "object"}},
    },
    "additionalProperties": True,
}


@register
class SmartShapeProbe(Agent):
    id = "smart_shape_probe"
    phase = "SOLVE-1"
    title = "SMART 1 · Shape Probe"

    async def run(self, ctx: AgentContext, inputs: dict[str, Any]) -> dict[str, Any]:
        collection = str(inputs["collection"])
        schema = dict(inputs.get("schema") or {})
        fragment = _collection_shape(collection, schema)
        ctx.log.info(
            "smart_shape_probe_done",
            collection=collection,
            variants=len(fragment["collections"][collection]["variants"]),
        )
        return fragment


@register
class SmartShapeReduce(Agent):
    id = "smart_shape_reduce"
    phase = "SOLVE-1"
    title = "SMART 1 · Shape Reduce"

    async def run(self, ctx: AgentContext, inputs: dict[str, Any]) -> dict[str, Any]:
        fragments = [f for f in inputs.get("fragments", []) if isinstance(f, dict)]
        model = _reduce_shape_fragments(fragments)
        out = model.to_json()
        ctx.log.info(
            "smart_shape_reduce_done",
            collections=sorted(out["collections"]),
            shape_flex_signature=out["shape_flex_signature"],
            coverage_gaps=out["coverage_gaps"],
        )
        return out


@register
class SmartIntentFormalizer(LLMAgent):
    id = "smart_intent"
    phase = "SOLVE-2"
    title = "SMART 2 · Intent"
    prompt_file = "smart_intent_formalizer.md"
    output_schema = _INTENT_SCHEMA

    def render_inputs(self, ctx: AgentContext, inputs: dict[str, Any]) -> str:
        return (
            "# SMART Intent Formalization\n"
            "Formalize the NLQ into a paradigm-neutral logical spec. Do not choose Mongo "
            "operators here.\n\n"
            f"db_id: {ctx.db_id}\nrecord_id: {ctx.record_id}\n"
            f"canonical NLQ:\n{inputs.get('nlq', '')}\n\n"
            f"colloquial NLQ:\n{inputs.get('colloquial', '')}\n\n"
            "shape_model S_hat:\n```json\n"
            + json.dumps(inputs.get("shape_model", {}), ensure_ascii=False, indent=2)
            + "\n```\n\n"
            "bounded checkpoint feedback from a later stage, if any:\n```json\n"
            + json.dumps(inputs.get("feedback"), ensure_ascii=False, indent=2)
            + "\n```"
        )

    def check_contract(self, ctx, inputs, output) -> list[str]:
        extra = getattr(ctx, "extra", {}) if ctx is not None else {}
        if extra.get("solver_use_intent_contracts") is False:
            return []
        violations: list[str] = []
        inferred = infer_shape_policy(str(inputs.get("nlq", "")))
        if inferred == "preserve" and output.get("shape_policy") != "preserve":
            violations.append("NLQ preserve cues require shape_policy=preserve")
        if output.get("shape_policy") == "preserve" and not output.get("target_fields"):
            violations.append("preserve specs must declare target_fields")
        if not output.get("clause_coverage"):
            violations.append("clause_coverage must list covered NLQ clauses")
        return violations

    def postprocess(self, ctx, inputs, output, result) -> dict[str, Any]:
        target_fields = list(output.get("target_fields") or [])
        if not target_fields:
            target_fields = extract_target_fields(str(inputs.get("nlq", "")))
        if target_fields:
            output["target_fields"] = target_fields
            output.setdefault("output", {})["target_fields"] = target_fields
        spec = LogicalSpec.from_json(output).to_json()
        ctx.log.info(
            "smart_intent_done",
            shape_policy=spec["shape_policy"],
            target_fields=spec["target_fields"],
            transcript_ref=result.transcript_ref,
            diagnostics_ref=result.diagnostics_ref,
        )
        return spec


@register
class SmartNosqlPlanner(LLMAgent):
    id = "smart_plan"
    phase = "SOLVE-3"
    title = "SMART 3 · NoSQL Plan"
    prompt_file = "smart_nosql_planner.md"
    output_schema = _PLAN_SCHEMA

    def render_inputs(self, ctx: AgentContext, inputs: dict[str, Any]) -> str:
        return (
            "# SMART Heterogeneity Reconciliation and NoSQL Planning\n"
            "Produce a Mongo-native physical plan. Each stage must include a concrete JSON "
            "stage document in `stage`; do not emit a string query.\n\n"
            f"db_id: {ctx.db_id}\nrecord_id: {ctx.record_id}\n"
            "logical_spec:\n```json\n"
            + json.dumps(inputs.get("logical_spec", {}), ensure_ascii=False, indent=2)
            + "\n```\n\nshape_model S_hat:\n```json\n"
            + json.dumps(inputs.get("shape_model", {}), ensure_ascii=False, indent=2)
            + "\n```\n\nbounded checkpoint feedback:\n```json\n"
            + json.dumps(inputs.get("feedback"), ensure_ascii=False, indent=2)
            + "\n```\n\ndisclosed witness_digest (≤K docs per collection, planning only):\n```json\n"
            + json.dumps(inputs.get("witness_digest", {}), ensure_ascii=False, indent=2)
            + "\n```"
        )

    def check_contract(self, ctx, inputs, output) -> list[str]:
        extra = getattr(ctx, "extra", {}) if ctx is not None else {}
        violations: list[str] = []
        logical = inputs.get("logical_spec", {})
        stages = _normalized_stage_items(output.get("stages", []))
        if (
            logical.get("shape_policy") == "preserve"
            and extra.get("solver_use_preserve_guard") is not False
        ):
            root_ops = [next(iter(s.get("stage", {}) or {}), "") for s in stages]
            for op in ("$group", "$unwind"):
                if op in root_ops:
                    violations.append(f"preserve plan must not use root {op}")
            targets = set(logical.get("target_fields") or [])
            if targets:
                add_fields: set[str] = set()
                for stage in stages:
                    body = (stage.get("stage") or {}).get("$addFields") or (
                        stage.get("stage") or {}
                    ).get("$set")
                    if isinstance(body, dict):
                        add_fields.update(body)
                missing = sorted(targets - add_fields)
                if missing:
                    violations.append(f"preserve target_fields missing from $addFields: {missing}")
            helper_fields = _lookup_helper_fields(stages)
            leaked = sorted(
                field for field in helper_fields
                if field not in set(logical.get("target_fields") or [])
                and not _helper_removed_after_lookup(stages, field)
            )
            if leaked:
                violations.append(
                    "preserve plan must remove helper lookup fields with $project/$unset: "
                    + str(leaked)
                )
            assigned_helpers = _assigned_helper_fields(stages, set(logical.get("target_fields") or []))
            leaked_assigned = sorted(
                field for field in assigned_helpers
                if not _helper_removed_after_lookup(stages, field)
            )
            if leaked_assigned:
                violations.append(
                    "preserve plan must not leak non-target helper fields assigned by "
                    "$addFields/$set; remove them or inline with $let: " + str(leaked_assigned)
                )
        for i, stage in enumerate(stages):
            if not _has_stage_diagnostic(stage):
                violations.append(
                    f"stage {i} must include a non-empty diagnostic note or structured rationale"
                )
            hits = disabled_hits_in_node(stage.get("stage"))
            if hits:
                violations.append(f"stage {i} uses disabled operators: {hits}")
        literal_violations = _observed_literal_violations(
            output.get("collection", ""),
            stages,
            inputs.get("witness_digest", {}),
        )
        violations.extend(literal_violations)
        violations.extend(
            _dynamic_key_path_violations(
                output.get("collection", ""),
                stages,
                inputs.get("shape_model", {}),
            )
        )
        if (
            not output.get("variant_handling")
            and extra.get("solver_require_variant_handling") is not False
        ):
            flex = inputs.get("shape_model", {}).get("shape_flex_signature", [])
            if flex:
                violations.append("shape-flex plans must declare variant_handling")
        return violations

    def postprocess(self, ctx, inputs, output, result) -> dict[str, Any]:
        try:
            plan = PhysicalPlan.from_json(output)
        except Exception as exc:  # noqa: BLE001 - converted into contract anomaly by Agent
            raise ContractViolationError(
                "physical plan is not normalizable",
                context={"agent": self.id, "error": str(exc)},
            ) from exc
        normalized = plan.to_json()
        ctx.log.info(
            "smart_plan_done",
            collection=normalized["collection"],
            stages=len(normalized["stages"]),
            variant_handling=len(normalized["variant_handling"]),
            transcript_ref=result.transcript_ref,
            diagnostics_ref=result.diagnostics_ref,
        )
        return normalized


def _normalized_stage_items(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in stages:
        if not isinstance(item, dict):
            continue
        op = str(item.get("op") or "")
        stage = item.get("stage") or {}
        if op.startswith("$") and isinstance(stage, dict) and op not in stage:
            stage = {op: stage}
        normalized.append({**item, "stage": stage})
    return normalized


def _has_stage_diagnostic(stage: dict[str, Any]) -> bool:
    note = stage.get("note")
    if isinstance(note, str) and note.strip():
        return True
    for key in ("rationale", "diagnostic", "diagnostics"):
        value = stage.get(key)
        if isinstance(value, dict) and value:
            return True
    return False


def _collection_shape(collection: str, schema: dict[str, Any]) -> dict[str, Any]:
    fields = list(schema.get("fields") or [])
    if not fields:
        fields = [
            key for key in schema
            if not key.startswith("__") and key not in {"doc_count", "schema_flex"}
        ]
    variants = _variants_for_schema(schema)
    loci: dict[str, list[dict[str, Any]]] = {}
    if variants:
        for field in fields:
            entries = []
            for variant in variants:
                presence = _field_presence(field, variant)
                entries.append(
                    FieldLocus(
                        variant=variant.id,
                        path=field,
                        type="unknown",
                        presence=presence,
                    ).__dict__
                )
            loci[field] = entries
    else:
        variants = [ShapeVariant(id="*", discriminator={}, coverage=1.0, fields={})]
        loci = {
            field: [FieldLocus(variant="*", path=field, type="unknown", presence="always").__dict__]
            for field in fields
        }
    flex = []
    if schema.get("schema_flex"):
        flex.append(str(schema["schema_flex"]))
    elif schema.get("__variants"):
        flex.append("polymorphic_collection")
    return {
        "collections": {
            collection: asdict(CollectionShape(
                variants=variants,
                field_locus={
                    k: [FieldLocus(**entry) for entry in entries] for k, entries in loci.items()
                },
                doc_count=schema.get("doc_count"),
                dynamic_key_paths=_str_list(schema.get("dynamic_key_paths")),
                dynamic_key_samples=_str_list_map(schema.get("dynamic_key_samples")),
                array_paths=_str_list(schema.get("array_paths")),
                dynamic_array_object_paths=_str_list(schema.get("dynamic_array_object_paths")),
                array_object_dynamic_paths=_str_list(schema.get("array_object_dynamic_paths")),
                presence_state_counts=_int_map(schema.get("presence_state_counts")),
            ))
        },
        "coverage_gaps": [],
        "shape_flex_signature": flex,
    }


def _variants_for_schema(schema: dict[str, Any]) -> list[ShapeVariant]:
    raw_variants = list(schema.get("__variants") or [])
    explicit_absent_fields = {
        next(iter(discriminator))
        for raw in raw_variants
        for discriminator in [dict(raw.get("discriminator") or {})]
        if len(discriminator) == 1
        and str(next(iter(discriminator.values()))).lower() in {"absent", "missing", "false"}
    }
    variants: list[ShapeVariant] = []
    for i, raw in enumerate(raw_variants):
        discriminator = dict(raw.get("discriminator") or {})
        variants.append(
            ShapeVariant(
                id=str(raw.get("id") or f"v{i}"),
                discriminator=discriminator,
                coverage=raw.get("coverage"),
                fields=dict(raw.get("fields") or {}),
            )
        )
        if len(discriminator) == 1:
            field, value = next(iter(discriminator.items()))
            if str(value).lower() in {"present", "exists", "true"} and field not in explicit_absent_fields:
                coverage = raw.get("coverage")
                complement = None if coverage is None else max(0.0, round(1.0 - float(coverage), 6))
                variants.append(
                    ShapeVariant(
                        id=f"v{i}_missing",
                        discriminator={field: "absent"},
                        coverage=complement,
                        fields={},
                    )
                )
    return variants


def _field_presence(field: str, variant: ShapeVariant) -> str:
    if field in variant.discriminator:
        val = str(variant.discriminator[field]).lower()
        if val in {"absent", "missing", "false"}:
            return "missing"
        return "sometimes"
    return "always"


def _reduce_shape_fragments(fragments: list[dict[str, Any]]) -> ShapeModel:
    collections: dict[str, CollectionShape] = {}
    flex: set[str] = set()
    gaps: list[str] = []
    for frag in fragments:
        for signature in frag.get("shape_flex_signature", []):
            flex.add(str(signature))
        gaps.extend(str(g) for g in frag.get("coverage_gaps", []))
        for name, raw in frag.get("collections", {}).items():
            collections[name] = ShapeModel.from_json({"collections": {name: raw}}).collections[name]
    return ShapeModel(
        collections=collections,
        coverage_gaps=sorted(set(gaps)),
        shape_flex_signature=sorted(flex),
    )


def _lookup_helper_fields(stages: list[dict[str, Any]]) -> set[str]:
    helpers: set[str] = set()
    for item in stages:
        stage = item.get("stage") or {}
        lookup = stage.get("$lookup")
        if isinstance(lookup, dict) and lookup.get("as"):
            helpers.add(str(lookup["as"]))
    return helpers


def _helper_removed_after_lookup(stages: list[dict[str, Any]], field: str) -> bool:
    for item in stages:
        stage = item.get("stage") or {}
        project = stage.get("$project")
        if isinstance(project, dict) and project.get(field) == 0:
            return True
        unset = stage.get("$unset")
        if unset == field:
            return True
        if isinstance(unset, list) and field in unset:
            return True
    return False


def _assigned_helper_fields(stages: list[dict[str, Any]], target_fields: set[str]) -> set[str]:
    helpers: set[str] = set()
    for item in stages:
        stage = item.get("stage") or {}
        for op in ("$addFields", "$set"):
            body = stage.get(op)
            if isinstance(body, dict):
                helpers.update(field for field in body if field not in target_fields)
    return helpers


def _observed_literal_violations(
    root_collection: str,
    stages: list[dict[str, Any]],
    witness_digest: dict[str, Any],
) -> list[str]:
    violations: list[str] = []
    observed = {
        collection: {
            field: set(values)
            for field, values in digest.get("string_values_in_sample", {}).items()
            if values
        }
        for collection, digest in witness_digest.items()
        if isinstance(digest, dict)
    }
    if not observed:
        return violations
    for item in stages:
        stage = item.get("stage") or {}
        violations.extend(_literal_violations_in_node(stage, root_collection, observed))
        lookup = stage.get("$lookup")
        if isinstance(lookup, dict):
            foreign = str(lookup.get("from", ""))
            for substage in lookup.get("pipeline", []) or []:
                violations.extend(_literal_violations_in_node(substage, foreign, observed))
    return sorted(set(violations))


def _literal_violations_in_node(
    node: Any,
    collection: str,
    observed: dict[str, dict[str, set[str]]],
) -> list[str]:
    values_by_field = observed.get(collection, {})
    violations: list[str] = []

    def check(field: str, literal: str) -> None:
        allowed = values_by_field.get(field)
        if allowed and literal not in allowed:
            violations.append(
                f"literal {literal!r} for {collection}.{field} was not observed in "
                f"witness_digest values {sorted(allowed)}; use an observed exact value or "
                "avoid an equality literal"
            )

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(key, str) and not key.startswith("$") and isinstance(child, str):
                    check(key, child)
                if key == "$eq" and isinstance(child, list) and len(child) == 2:
                    left, right = child
                    if isinstance(left, str) and left.startswith("$") and isinstance(right, str):
                        check(left[1:], right)
                    if isinstance(right, str) and right.startswith("$") and isinstance(left, str):
                        check(right[1:], left)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return violations


def _dynamic_key_path_violations(
    root_collection: str,
    stages: list[dict[str, Any]],
    shape_model: dict[str, Any],
) -> list[str]:
    dynamic_paths = _dynamic_key_paths_for_collection(root_collection, shape_model)
    if not dynamic_paths:
        return []
    violations: list[str] = []
    for i, item in enumerate(stages):
        stage = item.get("stage") or {}
        for path in dynamic_paths:
            refs = _direct_dynamic_key_refs(stage, path)
            if refs:
                violations.append(
                    f"stage {i} uses brittle dotted dynamic-key path {path}: "
                    f"{sorted(refs)}; use $objectToArray or $getField before filtering/projecting "
                    "observed dynamic keys"
                )
    return sorted(set(violations))


def _dynamic_key_paths_for_collection(
    root_collection: str,
    shape_model: dict[str, Any],
) -> list[str]:
    collections = shape_model.get("collections", {})
    if not isinstance(collections, dict):
        return []
    candidates: list[dict[str, Any]] = []
    if root_collection and isinstance(collections.get(root_collection), dict):
        candidates.append(collections[root_collection])
    if not candidates:
        candidates.extend(raw for raw in collections.values() if isinstance(raw, dict))
    paths: set[str] = set()
    for raw in candidates:
        paths.update(str(path) for path in raw.get("dynamic_key_paths", []) if str(path))
    return sorted(paths, key=lambda item: (-len(item), item))


def _direct_dynamic_key_refs(node: Any, dynamic_path: str) -> set[str]:
    prefix = dynamic_path + "."
    refs: set[str] = set()

    def check(value: str) -> None:
        path = value[1:] if value.startswith("$") and not value.startswith("$$") else value
        if path.startswith(prefix):
            refs.add(path)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(key, str) and not key.startswith("$"):
                    check(key)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str):
            check(value)

    walk(node)
    return refs


def _str_list(value: Any) -> list[str]:
    return [str(item) for item in value or []]


def _str_list_map(value: Any) -> dict[str, list[str]]:
    return {
        str(key): [str(item) for item in items or []]
        for key, items in dict(value or {}).items()
    }


def _int_map(value: Any) -> dict[str, int]:
    return {str(key): int(count) for key, count in dict(value or {}).items()}
