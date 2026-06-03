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

_COLLECTION_METADATA_KEYS = {
    "array_object_dynamic_paths",
    "array_paths",
    "doc_count",
    "document_count",
    "dynamic_array_object_paths",
    "dynamic_key_paths",
    "dynamic_key_samples",
    "presence_state_counts",
    "root_entity",
    "schema_flex",
    "source_tables",
}


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


_NATIVE_PATTERN_TARGET_FIELDS: dict[str, list[str]] = {
    "disposition_role_card_network": [
        "native_context_bucket",
        "native_key",
        "native_value",
    ],
    "district_salary_frequency_segments": [
        "native_context_bucket",
        "native_key",
        "native_value",
    ],
    "financial.district_frequency_gender_loan_mix": [
        "district_id",
        "district_name",
        "region",
        "avg_salary",
        "salary_band",
        "frequency_key",
        "account_count",
        "loan_account_count",
        "female_count",
        "male_count",
        "loan_account_share",
        "female_share",
    ],
    "financial.loan_schedule": [
        "loan_status",
        "region",
        "year",
        "due_months",
        "scheduled_total",
        "paid_total",
        "avg_salary",
    ],
    "financial.party_role_card_loan_mix": [
        "account_id",
        "district_name",
        "region",
        "frequency",
        "loan_status_bucket",
        "role_keys",
        "owner_count",
        "disponent_count",
        "owner_cards",
        "disponent_cards",
    ],
    "loan_status_repayment_schedule": [
        "entry_count",
        "metric_total",
    ],
}

_NATIVE_PATTERN_SHAPE_POLICY: dict[str, str] = {
    "disposition_role_card_network": "reshape",
    "district_salary_frequency_segments": "reshape",
    "financial.district_frequency_gender_loan_mix": "reshape",
    "financial.loan_schedule": "reduce",
    "financial.party_role_card_loan_mix": "reshape",
    "loan_status_repayment_schedule": "reduce",
}


def _native_pattern_target_fields(pattern: str | None, nlq: str) -> list[str]:
    if pattern == "counterparty_operation_symbol_matrix":
        if _nlq_requests_dynamic_totals(nlq):
            return ["entry_count", "metric_total"]
        return ["native_context_bucket", "native_key", "sample_edges.transaction_id"]
    return list(_NATIVE_PATTERN_TARGET_FIELDS.get(pattern or "", []))


def _native_pattern_shape_policy(pattern: str | None, nlq: str) -> str:
    if pattern == "counterparty_operation_symbol_matrix":
        return "reduce" if _nlq_requests_dynamic_totals(nlq) else "reshape"
    return _NATIVE_PATTERN_SHAPE_POLICY.get(pattern or "")


def _nlq_requests_dynamic_totals(nlq: str) -> bool:
    low = nlq.lower()
    return "summarize" in low or "total" in low or "totals" in low


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
            + "\n```\n\npublic native task context:\n```json\n"
            + json.dumps(inputs.get("native_task_context", {}), ensure_ascii=False, indent=2)
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
        pattern = _native_query_pattern_from_inputs(inputs)
        target_fields = _native_pattern_target_fields(pattern, str(inputs.get("nlq", "")))
        shape_policy = _native_pattern_shape_policy(pattern, str(inputs.get("nlq", "")))
        if shape_policy:
            output["shape_policy"] = shape_policy
        if not target_fields:
            target_fields = list(output.get("target_fields") or [])
        if not target_fields:
            target_fields = extract_target_fields(str(inputs.get("nlq", "")))
        if target_fields:
            output["target_fields"] = target_fields
            output.setdefault("output", {})["target_fields"] = target_fields
            output.setdefault("output", {})["fields"] = target_fields
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
            + "\n```\n\npublic native task context:\n```json\n"
            + json.dumps(inputs.get("native_task_context", {}), ensure_ascii=False, indent=2)
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
            targets = _concrete_target_fields(logical.get("target_fields") or [])
            guard_targets = targets | _native_required_output_fields(inputs)
            if targets:
                missing = _missing_preserve_target_fields(stages, targets, inputs)
                if missing:
                    violations.append(f"preserve target_fields missing from plan output: {missing}")
            helper_fields = _lookup_helper_fields(stages)
            leaked = sorted(
                field for field in helper_fields
                if field not in guard_targets
                and not _helper_removed_after_lookup(stages, field)
            )
            if leaked:
                violations.append(
                    "preserve plan must remove helper lookup fields with $project/$unset: "
                    + str(leaked)
                )
            assigned_helpers = _assigned_helper_fields(stages, guard_targets)
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
        violations.extend(_native_task_contract_violations(inputs, stages))
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
        normalized = _canonicalize_native_dynamic_unwind(plan.to_json())
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


def _canonicalize_native_dynamic_unwind(plan: dict[str, Any]) -> dict[str, Any]:
    out = dict(plan)
    stages: list[dict[str, Any]] = []
    for item in plan.get("stages", []):
        if not isinstance(item, dict):
            continue
        stage = item.get("stage")
        if isinstance(stage, dict):
            unwind = stage.get("$unwind")
            if (
                isinstance(unwind, dict)
                and unwind.get("path") == "$native_dynamic_entries"
                and not unwind.get("includeArrayIndex")
                and unwind.get("preserveNullAndEmptyArrays") in (None, False)
            ):
                item = {**item, "stage": {"$unwind": "$native_dynamic_entries"}}
        stages.append(item)
    out["stages"] = stages
    return out


def _has_stage_diagnostic(stage: dict[str, Any]) -> bool:
    note = stage.get("note")
    if isinstance(note, str) and note.strip():
        return True
    for key in ("rationale", "diagnostic", "diagnostics"):
        value = stage.get(key)
        if isinstance(value, dict) and value:
            return True
    return False


def _concrete_target_fields(raw_fields: list[Any]) -> set[str]:
    fields = {str(field) for field in raw_fields if str(field).strip()}
    if "*" in fields:
        return set()
    fields.discard("_id")
    return fields


def _collection_shape(collection: str, schema: dict[str, Any]) -> dict[str, Any]:
    fields = _field_names(schema)
    if not fields:
        fields = _audit_field_candidates(schema)
    if not fields:
        fields = [
            key for key in schema
            if not key.startswith("__") and key not in _COLLECTION_METADATA_KEYS
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
                doc_count=schema.get("doc_count", schema.get("document_count")),
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


def _field_names(schema: dict[str, Any]) -> list[str]:
    raw = schema.get("fields")
    if isinstance(raw, dict):
        return [str(key) for key in raw]
    return _str_list(raw)


def _audit_field_candidates(schema: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    candidates.extend(_str_list(schema.get("dynamic_key_paths")))
    for key in ("array_paths", "dynamic_array_object_paths", "array_object_dynamic_paths"):
        candidates.extend(_root_dynamic_path(path) for path in _str_list(schema.get(key)))
    return _unique_nonempty(candidates)


def _root_dynamic_path(path: str) -> str:
    for token in (".*[]", "[]", ".*"):
        if token in path:
            return path.split(token, maxsplit=1)[0]
    return path


def _unique_nonempty(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value)
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


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
                helpers.update(
                    field for field in body
                    if field != "_id" and field not in target_fields
                )
    return helpers


def _defined_output_fields(stages: list[dict[str, Any]]) -> set[str]:
    fields: set[str] = set()
    for item in stages:
        stage = item.get("stage") or {}
        for op in ("$addFields", "$set", "$project"):
            body = stage.get(op)
            if isinstance(body, dict):
                fields.update(str(field) for field in body)
    fields.discard("_id")
    return fields


def _missing_preserve_target_fields(
    stages: list[dict[str, Any]],
    targets: set[str],
    inputs: dict[str, Any],
) -> list[str]:
    return sorted(
        target for target in targets
        if not _preserve_target_available(stages, target, inputs)
    )


def _preserve_target_available(
    stages: list[dict[str, Any]],
    target: str,
    inputs: dict[str, Any],
) -> bool:
    available = _target_known_original_field(target, inputs)
    for item in stages:
        stage = item.get("stage") or {}
        for op in ("$addFields", "$set"):
            body = stage.get(op)
            if isinstance(body, dict) and target in body:
                available = True
        project = stage.get("$project")
        if isinstance(project, dict):
            if _projection_has_inclusions(project):
                available = _projection_includes_target(project, target)
            elif _projection_excludes_target(project, target):
                available = False
        unset = stage.get("$unset")
        if _unset_removes_target(unset, target):
            available = False
        if "$replaceRoot" in stage or "$replaceWith" in stage:
            available = False
    return available


def _target_known_original_field(target: str, inputs: dict[str, Any]) -> bool:
    native = inputs.get("native_task_context")
    if isinstance(native, dict):
        candidates = [
            native.get("feature_field"),
            native.get("native_feature_field"),
        ]
        candidates.extend(native.get("relevant_paths") or [])
        if any(str(candidate) == target for candidate in candidates if candidate):
            return True

    collections = (inputs.get("shape_model") or {}).get("collections", {})
    if not isinstance(collections, dict):
        return False
    for raw in collections.values():
        if not isinstance(raw, dict):
            continue
        field_locus = raw.get("field_locus")
        if isinstance(field_locus, dict) and target in field_locus:
            return True
        for key in (
            "dynamic_key_paths",
            "array_paths",
            "dynamic_array_object_paths",
            "array_object_dynamic_paths",
        ):
            if target in {_root_dynamic_path(str(path)) for path in raw.get(key, []) or []}:
                return True
    return False


def _projection_has_inclusions(project: dict[str, Any]) -> bool:
    return any(
        field != "_id" and not _is_projection_exclusion(value)
        for field, value in project.items()
    )


def _projection_includes_target(project: dict[str, Any], target: str) -> bool:
    return any(
        not _is_projection_exclusion(value)
        and _path_is_same_or_parent(str(field), target)
        for field, value in project.items()
        if field != "_id"
    )


def _projection_excludes_target(project: dict[str, Any], target: str) -> bool:
    return any(
        _is_projection_exclusion(value)
        and _paths_overlap(str(field), target)
        for field, value in project.items()
        if field != "_id"
    )


def _unset_removes_target(unset: Any, target: str) -> bool:
    if isinstance(unset, str):
        return _paths_overlap(unset, target)
    if isinstance(unset, dict):
        return any(_paths_overlap(str(field), target) for field in unset)
    if isinstance(unset, (list, tuple, set)):
        return any(_paths_overlap(str(field), target) for field in unset)
    return False


def _is_projection_exclusion(value: Any) -> bool:
    return value is False or value == 0


def _path_is_same_or_parent(path: str, target: str) -> bool:
    return path == target or target.startswith(path + ".")


def _paths_overlap(left: str, right: str) -> bool:
    return _path_is_same_or_parent(left, right) or _path_is_same_or_parent(right, left)


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
                    if not child.startswith("$"):
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


def _native_task_contract_violations(
    inputs: dict[str, Any],
    stages: list[dict[str, Any]],
) -> list[str]:
    native = inputs.get("native_task_context")
    if not isinstance(native, dict):
        return []
    feature_type = str(native.get("feature_type") or native.get("native_feature_type") or "")
    query_pattern = str(native.get("query_pattern") or native.get("native_query_pattern") or "")
    violations: list[str] = []

    if feature_type == "nested_event_stream" or query_pattern.endswith("event_evidence_filter"):
        for field in ("native_context_bucket", "native_filtered_events", "native_event_count"):
            if not _field_defined(stages, field):
                violations.append(f"nested_event_stream plan must define {field}")
        if not (
            _match_filters_nonempty_array(stages, "native_filtered_events")
            or _match_filters_positive_count(stages, "native_event_count")
        ):
            violations.append(
                "nested_event_stream plan must filter to documents where "
                "native_filtered_events has size > 0"
            )
        if _uses_unobserved_event_date_alias(stages):
            violations.append(
                "nested_event_stream date filters must use the observed event_time field, "
                "not event_date or bare date aliases"
            )

    if feature_type == "missing_vs_present" or query_pattern == "missing_vs_present":
        for field in ("native_presence_state", "native_context_bucket"):
            if not _field_defined(stages, field):
                violations.append(f"missing_vs_present plan must define {field}")

    if query_pattern == "financial.loan_schedule":
        violations.extend(_required_output_field_violations(
            query_pattern,
            stages,
            {
                "loan_status",
                "region",
                "year",
                "due_months",
                "scheduled_total",
                "paid_total",
                "avg_salary",
            },
        ))
        violations.extend(_required_plan_reference_violations(
            query_pattern,
            stages,
            (
                "loan.contract.status_bucket",
                "loan.repayment_schedule.by_due_month",
                "scheduled_amount",
                "observed_payment_total",
                "district_context.avg_salary",
            ),
        ))
        violations.extend(_invented_sentinel_string_violations(
            query_pattern,
            stages,
            ("unknown", "unknown region"),
        ))
        violations.extend(_forbidden_stage_reference_violations(
            query_pattern,
            stages,
            ("scheduled_payment",),
            "use scheduled_amount only",
        ))

    if query_pattern == "financial.district_frequency_gender_loan_mix":
        violations.extend(_required_output_field_violations(
            query_pattern,
            stages,
            {
                "district_id",
                "district_name",
                "region",
                "avg_salary",
                "salary_band",
                "frequency_key",
                "account_count",
                "loan_account_count",
                "female_count",
                "male_count",
                "loan_account_share",
                "female_share",
            },
        ))
        violations.extend(_required_plan_reference_violations(
            query_pattern,
            stages,
            (
                "accounts_by_frequency",
                "clients_by_gender",
                "loan_presence_state",
                "district.name",
                "district.region",
                "district.avg_salary",
            ),
        ))
        violations.extend(_required_match_threshold_violations(
            query_pattern,
            stages,
            {
                "account_count": ("$gte", 20),
                "loan_account_count": ("$gte", 1),
                "female_count": ("$gte", 10),
                "male_count": ("$gte", 10),
            },
        ))
        if not _sort_contains_ordered_keys(
            stages,
            (("loan_account_share", -1), ("account_count", -1)),
        ):
            violations.append(
                f"{query_pattern} plan must sort by loan_account_share and account_count"
            )
        violations.extend(_root_id_preservation_violations(query_pattern, stages))

    if query_pattern == "financial.party_role_card_loan_mix":
        violations.extend(_required_output_field_violations(
            query_pattern,
            stages,
            {
                "account_id",
                "district_name",
                "region",
                "frequency",
                "loan_status_bucket",
                "role_keys",
                "owner_count",
                "disponent_count",
                "owner_cards",
                "disponent_cards",
            },
        ))
        violations.extend(_required_plan_reference_violations(
            query_pattern,
            stages,
            (
                "relationships.members_by_role",
                "account.account_id",
                "account.district.name",
                "account.district.region",
                "account.frequency",
                "loan_link.status_bucket",
            ),
        ))
        if not _sort_contains_ordered_keys(
            stages,
            (("disponent_cards", -1), ("owner_cards", -1), ("account_id", 1)),
        ):
            violations.append(
                f"{query_pattern} plan must sort by disponent_cards, owner_cards, and account_id"
            )

    if query_pattern == "disposition_role_card_network":
        violations.extend(_dynamic_entry_filter_contract_violations(
            query_pattern,
            stages,
            feature_path="relationships.members_by_role",
            context_path="loan_link.loan_id",
            required_key="OWNER",
            output_fields={"native_context_bucket", "native_key", "native_value"},
        ))

    if query_pattern == "district_salary_frequency_segments":
        violations.extend(_dynamic_entry_filter_contract_violations(
            query_pattern,
            stages,
            feature_path="accounts_by_frequency",
            context_path="district.crime.current.value",
            required_key="POPLATEK_TYDNE",
            output_fields={"native_context_bucket", "native_key", "native_value"},
        ))

    if query_pattern == "counterparty_operation_symbol_matrix":
        if _logical_requests_dynamic_totals(inputs):
            violations.extend(_dynamic_totals_contract_violations(
                query_pattern,
                stages,
                feature_path="flows_by_symbol",
                metric_path="sample_edges.account_id",
            ))
        else:
            violations.extend(_dynamic_entry_filter_contract_violations(
                query_pattern,
                stages,
                feature_path="flows_by_symbol",
                context_path="monthly_flow_index",
                required_key="UROK",
                output_fields={
                    "native_context_bucket",
                    "native_key",
                    "sample_edges.transaction_id",
                },
                required_payload_ref="sample_edges.transaction_id",
                allow_context_match=True,
            ))

    if query_pattern == "loan_status_repayment_schedule":
        violations.extend(_dynamic_totals_contract_violations(
            query_pattern,
            stages,
            feature_path="loan.repayment_schedule.by_due_month",
            metric_path="installment_index",
        ))
    return violations


def _native_query_pattern_from_inputs(inputs: dict[str, Any]) -> str | None:
    native = inputs.get("native_task_context")
    if not isinstance(native, dict):
        return None
    for key in ("query_pattern", "native_query_pattern"):
        value = native.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _required_output_field_violations(
    query_pattern: str,
    stages: list[dict[str, Any]],
    required_fields: set[str],
) -> list[str]:
    missing = sorted(required_fields - _defined_output_fields(stages))
    if not missing:
        return []
    return [f"{query_pattern} plan must output fields: {missing}"]


def _dynamic_entry_filter_contract_violations(
    query_pattern: str,
    stages: list[dict[str, Any]],
    *,
    feature_path: str,
    context_path: str,
    required_key: str,
    output_fields: set[str],
    required_payload_ref: str | None = None,
    allow_context_match: bool = False,
) -> list[str]:
    required_refs = [feature_path, "native_dynamic_entries", required_key]
    if required_payload_ref:
        required_refs.append(required_payload_ref)
    violations: list[str] = []
    violations.extend(_required_output_field_violations(query_pattern, stages, output_fields))
    violations.extend(_required_plan_reference_violations(
        query_pattern,
        stages,
        tuple(required_refs),
    ))
    violations.extend(_required_stage_operator_violations(
        query_pattern,
        stages,
        "$unwind",
        "native_dynamic_entries",
    ))
    violations.extend(_native_dynamic_unwind_form_violations(query_pattern, stages))
    violations.extend(_forbidden_stage_reference_violations(
        query_pattern,
        stages,
        ("native_matching_dynamic_entries",),
        "use native_dynamic_entries with $unwind and native_key/native_value",
    ))
    if not allow_context_match:
        violations.extend(_context_path_match_violations(query_pattern, stages, context_path))
    return violations


def _dynamic_totals_contract_violations(
    query_pattern: str,
    stages: list[dict[str, Any]],
    *,
    feature_path: str,
    metric_path: str,
) -> list[str]:
    violations: list[str] = []
    violations.extend(_required_output_field_violations(
        query_pattern,
        stages,
        {"entry_count", "metric_total"},
    ))
    violations.extend(_required_plan_reference_violations(
        query_pattern,
        stages,
        (feature_path, "native_dynamic_entries", metric_path, "native_key"),
    ))
    violations.extend(_required_stage_operator_violations(
        query_pattern,
        stages,
        "$unwind",
        "native_dynamic_entries",
    ))
    violations.extend(_native_dynamic_unwind_form_violations(query_pattern, stages))
    violations.extend(_required_stage_operator_violations(
        query_pattern,
        stages,
        "$group",
        "metric_total",
    ))
    violations.extend(_forbidden_stage_reference_violations(
        query_pattern,
        stages,
        ("native_matching_dynamic_entries",),
        "use native_dynamic_entries with $unwind before grouping",
    ))
    return violations


def _required_stage_operator_violations(
    query_pattern: str,
    stages: list[dict[str, Any]],
    op: str,
    required_ref: str,
) -> list[str]:
    for item in stages:
        stage = item.get("stage") or {}
        body = stage.get(op)
        if body is None:
            continue
        text = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
        if required_ref in text:
            return []
    return [f"{query_pattern} plan must use {op} with {required_ref}"]


def _native_dynamic_unwind_form_violations(
    query_pattern: str,
    stages: list[dict[str, Any]],
) -> list[str]:
    for item in stages:
        unwind = (item.get("stage") or {}).get("$unwind")
        if unwind is None:
            continue
        if unwind == "$native_dynamic_entries":
            return []
        if (
            isinstance(unwind, dict)
            and unwind.get("path") == "$native_dynamic_entries"
            and not unwind.get("includeArrayIndex")
            and unwind.get("preserveNullAndEmptyArrays") in (None, False)
        ):
            return []
        text = json.dumps(unwind, ensure_ascii=False, sort_keys=True, default=str)
        if "native_dynamic_entries" in text:
            return [
                f"{query_pattern} plan must use simple $unwind over "
                "'$native_dynamic_entries' without includeArrayIndex or preserveNullAndEmptyArrays"
            ]
    return []


def _context_path_match_violations(
    query_pattern: str,
    stages: list[dict[str, Any]],
    context_path: str,
) -> list[str]:
    for item in stages:
        match = (item.get("stage") or {}).get("$match")
        if not isinstance(match, dict):
            continue
        text = json.dumps(match, ensure_ascii=False, sort_keys=True, default=str)
        if context_path in text or "native_context_bucket" in text:
            return [
                f"{query_pattern} plan must bucket {context_path} into "
                "native_context_bucket, not filter records by that context path"
            ]
    return []


def _logical_requests_dynamic_totals(inputs: dict[str, Any]) -> bool:
    text = json.dumps(inputs.get("logical_spec") or {}, ensure_ascii=False, default=str).lower()
    return "summarize" in text or "total" in text or "totals" in text


def _required_plan_reference_violations(
    query_pattern: str,
    stages: list[dict[str, Any]],
    required_refs: tuple[str, ...],
) -> list[str]:
    text = json.dumps(stages, ensure_ascii=False, sort_keys=True, default=str)
    missing = sorted(ref for ref in required_refs if ref not in text)
    if not missing:
        return []
    return [f"{query_pattern} plan must reference fields/values: {missing}"]


def _required_match_threshold_violations(
    query_pattern: str,
    stages: list[dict[str, Any]],
    required: dict[str, tuple[str, int | float]],
) -> list[str]:
    missing = [
        f"{field} {op.replace('$', '')} {value}"
        for field, (op, value) in required.items()
        if not _match_has_threshold(stages, field, op, value)
    ]
    if not missing:
        return []
    readable = [
        item.replace("gte", ">=").replace("gt", ">").replace("lte", "<=").replace("lt", "<")
        for item in missing
    ]
    return [f"{query_pattern} plan must apply support filters: {readable}"]


def _match_has_threshold(
    stages: list[dict[str, Any]],
    field: str,
    op: str,
    value: int | float,
) -> bool:
    def walk(node: Any) -> bool:
        if isinstance(node, dict):
            predicate = node.get(field)
            if isinstance(predicate, dict) and predicate.get(op) == value:
                return True
            expr = node.get(op)
            if (
                isinstance(expr, list)
                and len(expr) == 2
                and f"${field}" in expr
                and value in expr
            ):
                return True
            return any(walk(child) for child in node.values())
        if isinstance(node, list):
            return any(walk(child) for child in node)
        return False

    for item in stages:
        match = (item.get("stage") or {}).get("$match")
        if isinstance(match, dict) and walk(match):
            return True
    return False


def _sort_contains_ordered_keys(
    stages: list[dict[str, Any]],
    required: tuple[tuple[str, int], ...],
) -> bool:
    for item in stages:
        sort = (item.get("stage") or {}).get("$sort")
        if not isinstance(sort, dict):
            continue
        keys = list(sort)
        position = 0
        matched = True
        for field, direction in required:
            try:
                index = keys.index(field, position)
            except ValueError:
                matched = False
                break
            if sort.get(field) != direction:
                matched = False
                break
            position = index + 1
        if matched:
            return True
    return False


def _invented_sentinel_string_violations(
    query_pattern: str,
    stages: list[dict[str, Any]],
    sentinels: tuple[str, ...],
) -> list[str]:
    text = json.dumps(stages, ensure_ascii=False, sort_keys=True, default=str)
    hits = sorted(value for value in sentinels if json.dumps(value) in text)
    if not hits:
        return []
    return [
        f"{query_pattern} plan must not group by invented sentinel strings: {hits}; "
        "use the observed/raw grouping fields"
    ]


def _forbidden_stage_reference_violations(
    query_pattern: str,
    stages: list[dict[str, Any]],
    forbidden_refs: tuple[str, ...],
    guidance: str,
) -> list[str]:
    stage_bodies = [(item.get("stage") or {}) for item in stages]
    text = json.dumps(stage_bodies, ensure_ascii=False, sort_keys=True, default=str)
    hits = sorted(ref for ref in forbidden_refs if ref in text)
    if not hits:
        return []
    return [f"{query_pattern} plan must {guidance}; forbidden refs: {hits}"]


def _root_id_preservation_violations(
    query_pattern: str,
    stages: list[dict[str, Any]],
) -> list[str]:
    text = json.dumps(stages, ensure_ascii=False, sort_keys=True, default=str)
    violations: list[str] = []
    if "$facet" in text and '"$_id"' not in text and '"root_id"' not in text:
        violations.append(
            f"{query_pattern} plan must preserve the root _id when using $facet"
        )

    final_project: dict[str, Any] | None = None
    for item in stages:
        project = (item.get("stage") or {}).get("$project")
        if isinstance(project, dict):
            final_project = project
    if final_project is not None and final_project.get("_id") == 0:
        violations.append(f"{query_pattern} plan must preserve the root _id in output")
    return violations


def _native_required_output_fields(inputs: dict[str, Any]) -> set[str]:
    native = inputs.get("native_task_context")
    if not isinstance(native, dict):
        return set()
    feature_type = str(native.get("feature_type") or native.get("native_feature_type") or "")
    query_pattern = str(native.get("query_pattern") or native.get("native_query_pattern") or "")
    if feature_type == "nested_event_stream" or query_pattern.endswith("event_evidence_filter"):
        return {"native_context_bucket", "native_filtered_events", "native_event_count"}
    if feature_type == "missing_vs_present" or query_pattern == "missing_vs_present":
        return {"native_presence_state", "native_context_bucket"}
    return set()


def _field_defined(stages: list[dict[str, Any]], field: str) -> bool:
    for item in stages:
        stage = item.get("stage") or {}
        for op in ("$addFields", "$set", "$project"):
            body = stage.get(op)
            if isinstance(body, dict) and field in body:
                return True
    return False


def _match_filters_nonempty_array(stages: list[dict[str, Any]], field: str) -> bool:
    for item in stages:
        stage = item.get("stage") or {}
        match = stage.get("$match")
        if not isinstance(match, dict):
            continue
        text = json.dumps(match, ensure_ascii=False, sort_keys=True, default=str)
        if field in text and "$size" in text and any(op in text for op in ("$gt", "$gte")):
            return True
    return False


def _match_filters_positive_count(stages: list[dict[str, Any]], field: str) -> bool:
    for item in stages:
        stage = item.get("stage") or {}
        match = stage.get("$match")
        if not isinstance(match, dict):
            continue
        text = json.dumps(match, ensure_ascii=False, sort_keys=True, default=str)
        if field in text and any(op in text for op in ("$gt", "$gte")):
            return True
    return False


def _uses_unobserved_event_date_alias(stages: list[dict[str, Any]]) -> bool:
    text = json.dumps(stages, ensure_ascii=False, sort_keys=True, default=str)
    if "event_time" in text:
        return False
    return any(alias in text for alias in ("event_date", "$$this.date", "$$event.date", "$$evt.date"))


def _str_list(value: Any) -> list[str]:
    return [str(item) for item in value or []]


def _str_list_map(value: Any) -> dict[str, list[str]]:
    return {
        str(key): [str(item) for item in items or []]
        for key, items in dict(value or {}).items()
    }


def _int_map(value: Any) -> dict[str, int]:
    return {str(key): int(count) for key, count in dict(value or {}).items()}
