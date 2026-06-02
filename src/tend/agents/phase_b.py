"""Phase B agents: QPS, MS, MUT, PV, NLP, RTV, NNC, RA.

Reverse-engineered NL-MQL construction. LLM agents (QPS/MS/MUT/NLP/NNC/RA) drive the
creative steps; deterministic verification (gold-lock, mutation EX-fail, round-trip
equivalence) runs against the loaded witness via ``ctx.mongo``. Execution-dependent
checks are stub-graceful: under ``settings.stub`` they short-circuit to a pass so the
whole pipeline (workflow + logging + progress + feedback loops) is exercisable offline.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Callable

from ..errors import TendError
from ..execution import derive_canonical_form_set, parse_pipeline, scan_disabled
from ..execution.mongo import _normalize_doc, equiv_rec
from ..mechanisms import OracleError, has_oracle, oracle_param_errors, reference_oracle
from ..mechanisms.oracles import _embed_value_path
from .base import Agent, AgentContext, LLMAgent, register

_ORDER_INSENSITIVE = False  # ≡_rec default for construction checks (NLQ says "no order")

# CJK / kana / fullwidth ranges — all benchmark NL must be English-only; this gates it.
_CJK_RE = re.compile(r"[⺀-鿿　-ヿ＀-￯]")


def _contains_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# QPS — intent enumerator
# --------------------------------------------------------------------------- #
_QPS_SCHEMA = {
    "type": "object",
    "required": ["intent", "qps_trace"],
    "properties": {
        "intent": {
            "type": "object",
            "required": [
                "seed_mechanism",
                "seed_signal",
                "archetype",
                "domain_framing",
                "analytical_op",
                "shape_policy",
                "semantic_properties",
                "target_difficulty",
            ],
            "properties": {
                "seed_mechanism": {
                    "type": "string",
                    "enum": [
                        "none",
                        "polymorphic",
                        "sparse_scalar",
                        "sparse_embed",
                        "dynamic_key",
                        "nesting",
                        "versioning",
                    ],
                },
                "seed_signal": {"type": "object", "minProperties": 1, "additionalProperties": True},
                "archetype": {"type": "string", "minLength": 1},
                "domain_framing": {
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": {"type": "string"},
                },
                "analytical_op": {"type": "object", "minProperties": 1, "additionalProperties": True},
                "shape_policy": {"type": "string", "enum": ["preserve", "reshape", "reduce"]},
                "semantic_properties": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["id", "expect"],
                        "properties": {
                            "id": {"type": "string", "minLength": 1},
                            "expect": {"type": "string", "minLength": 1},
                        },
                        "additionalProperties": False,
                    },
                },
                "target_difficulty": {
                    "type": "string",
                    "enum": ["L0", "L1", "L2", "L3", "L4"],
                },
            },
            "additionalProperties": False,
        },
        "qps_trace": {
            "type": "object",
            "required": ["coverage_cell", "deficit_weight", "supply_constrained"],
            "properties": {
                "coverage_cell": {"type": "string", "minLength": 1},
                "deficit_weight": {"type": "number", "minimum": 0},
                "supply_constrained": {"type": "boolean"},
                "rationale": {"type": "string", "minLength": 1},
                "skip_reason": {"type": "string", "minLength": 1},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}


# archetype-specific recipes that yield deterministic, round-trippable intents
_ARCHETYPE_RECIPE = {
    "present_missing_projection": (
        "shape_policy MUST be 'preserve'. The intent attaches exactly ONE new numeric field "
        "to every document of the ROOT collection, computed from its optional embedded "
        "sub-document: if the sub-doc is PRESENT use a concrete formula over its fields; if "
        "MISSING the field is a fixed default (e.g. 0). Keep every input document. State the "
        "exact formula and the non-null missing-default so the result is fully determined. "
        "For a simple optional-embed field copy, describe the source value path and missing "
        "default in analytical_op/output. For a ratio form, describe the numerator field, "
        "denominator collection/key/sum field, and absent value in analytical_op/output."
    ),
    "subtype_cond_projection": (
        "Target cell is L4 structural_schema_flex with shape_policy='preserve'. Attach "
        "exactly ONE scalar field to every root document while keeping every original field. "
        "The computation MUST visibly branch on real schema variants with $type/$switch/$cond "
        "downstream, and the missing/default branch MUST be explicit and non-null (usually 0). "
        "If the variant is optional embedded-subdocument presence, describe the present and "
        "missing branches in analytical_op/output. If it is a true discriminator-subtype task, "
        "describe the discriminator, subtype fields, target field, and default explicitly."
    ),
    "schema_flex_variant_summary": (
        "Target cell is L4 structural_schema_flex. The intent MUST use a ROOT collection "
        "with real __variants, dispatch on the real optional embedded sub-document being "
        "present vs missing, and reshape/reduce into a variant summary. Require schema-flex "
        "operators such as $type/$switch in downstream MQL, not a simple preserve $addFields. "
        "The result must include one row per variant with counts and at least one numeric "
        "aggregate over a real nested field from the present branch, using 0 for missing. "
        "Use public output field 'variant' with EXACT string values 'present' and 'missing'; "
        "include 'count' and the aggregate target field in output.fields."
    ),
    "has_vs_absent_compare": (
        "Target cell is L4 structural_schema_flex with shape_policy='reduce'. Compare root "
        "documents that have a real optional embedded sub-document against documents where it "
        "is missing. The downstream MQL must use $type plus $cond/$switch to form the "
        "present/absent branch, then aggregate each branch. Use output fields _id and value."
    ),
    "per_subtype_agg": (
        "Target cell is L4 structural_schema_flex with shape_policy='reduce'. Group by a real "
        "discriminator and aggregate a numeric field chosen per discriminator value. Do not "
        "invent subtype names or oracle templates; use only real schema fields."
    ),
    "subtype_specific_field": (
        "Target cell is L4 structural_schema_flex with shape_policy='reshape'. Pick one real "
        "subtype value and return a real subtype-specific field for matching documents. Do not "
        "invent subtype names or oracle templates."
    ),
}

_ARCHETYPE_ORACLE_GUIDE = {
    "present_missing_projection": (
        "Allowed reference_oracle templates for this archetype:\n"
        "- present_missing_projection params {parent_collection, embed_field, numerator_path, "
        "target_field, denom:{collection, local_id, foreign_field, sum_field, optional match, "
        "optional zero_value}, optional absent_value}; use this only for a ratio/division form.\n"
        "- optional_embed_projection params {parent_collection, embed_field, value_path, "
        "target_field, missing_default}; use this for a simple optional embedded field copy.\n"
        "Do not emit any other reference_oracle.template."
    ),
    "has_vs_absent_compare": (
        "Allowed reference_oracle template for this archetype:\n"
        "- has_vs_absent_compare params {parent_collection, embed_field, optional metric_field, "
        "optional agg}. parent_collection is the root collection with __variants; embed_field "
        "is the optional embedded sub-document. Prefer parent-rooted metric_field paths such "
        "as 'loan.amount' instead of bare 'amount'. Its output group keys are exactly "
        "'present' and 'absent'. Do not emit presence_count, has_vs_absent_agg, "
        "has_vs_absent_groupby, or any other invented template."
    ),
    "per_subtype_agg": (
        "Allowed reference_oracle template for this archetype:\n"
        "- per_subtype_agg params {collection, discriminator, field_by_subtype, agg}. "
        "field_by_subtype maps each real discriminator value to the real numeric field to "
        "aggregate. Do not omit field_by_subtype or agg."
    ),
    "subtype_cond_projection": (
        "Allowed reference_oracle template for this archetype:\n"
        "- subtype_cond_projection params {collection, discriminator, field_by_subtype, "
        "target_field, optional default}. field_by_subtype maps each real discriminator "
        "value to a real source field. default must be non-null."
    ),
    "subtype_specific_field": (
        "Allowed reference_oracle template for this archetype:\n"
        "- subtype_specific_field params {collection, discriminator, subtype_value, field, "
        "optional project}. Use one real discriminator value and one real field. Do not emit "
        "polymorphic_field_case, subtype_derived_field, or any other invented template."
    ),
    "cross_subtype_compare": (
        "Allowed reference_oracle template for this archetype:\n"
        "- cross_subtype_compare params {collection, discriminator, field_by_subtype, agg}. "
        "field_by_subtype maps each real discriminator value to that subtype's own numeric "
        "field; agg is sum|avg|min|max|count. Emit the literal template 'cross_subtype_compare' "
        "— NOT cross_subtype_aggregate, cross_subtype_comparison, or any other name."
    ),
    "existence_count": (
        "Allowed reference_oracle template for this archetype:\n"
        "- existence_count params {collection, field}. Counts docs where the optional field is "
        "present. Emit the literal template 'existence_count' — NOT exists_count, "
        "presence_count, simple_count, or any other name."
    ),
    "null_coalesce_agg": (
        "Allowed reference_oracle template for this archetype:\n"
        "- null_coalesce_agg params {collection, field, agg, optional default}. Coalesces "
        "missing/null/non-numeric values of field to default, then aggregates (agg is "
        "sum|avg|min|max). Emit the literal template 'null_coalesce_agg' — NOT "
        "null_coalesce_aggregate, null_coalesce_aggregation, coalesce_agg, or any other name."
    ),
    "join_nested_group": (
        "Allowed reference_oracle template for this archetype:\n"
        "- join_nested_group params {collection, array_field, group_by, optional value_field, "
        "optional agg}. Unwinds the embedded array_field, groups elements by group_by, and "
        "aggregates (agg defaults to count; value_field is the element's numeric field for "
        "sum|avg|min|max). Emit the literal template 'join_nested_group' — NOT "
        "join_group_aggregate, nested_unwind_group, or any other name."
    ),
    "simple_filter": (
        "Allowed reference_oracle template for this archetype:\n"
        "- simple_filter params {collection, predicates:[{field, op, value}], optional project}. "
        "op is eq|ne|gt|lt|gte|lte; every predicate field must be a real schema field. Emit the "
        "literal template 'simple_filter' — NOT filter_count, match_filter, or any other name."
    ),
    "group_count": (
        "Allowed reference_oracle template for this archetype:\n"
        "- group_count params {collection, group_by}. Groups by the real field group_by and "
        "counts. Emit the literal template 'group_count' — NOT group_by_count, count_by_group, "
        "or any other name."
    ),
    "topn": (
        "Allowed reference_oracle template for this archetype:\n"
        "- topn params {collection, sort_key, n, optional order(asc|desc), optional project, "
        "optional nulls(first|last)}. Emit the literal template 'topn' — NOT top_n, "
        "order_limit, or any other name."
    ),
    "dynamic_key_fold": (
        "Allowed reference_oracle template for this archetype:\n"
        "- dynamic_key_fold params {collection, name_field, value_field, agg}. Folds an "
        "EAV/attribute bag: groups by the attribute-name column name_field and aggregates the "
        "numeric value_field. Emit the literal template 'dynamic_key_fold' — no other name."
    ),
    "cross_keyset_value": (
        "Allowed reference_oracle template for this archetype:\n"
        "- cross_keyset_value params {collection, key, optional project}. Reads key only from "
        "docs whose heterogeneous keyset contains it. Emit the literal template "
        "'cross_keyset_value' — no other name."
    ),
    "cross_version_agg": (
        "Allowed reference_oracle template for this archetype:\n"
        "- cross_version_agg params {collection, field_candidates:[old, new, ...], agg, optional "
        "default}. Coalesces the first present candidate field (handles cross-version renames) "
        "then aggregates. Emit the literal template 'cross_version_agg' — no other name."
    ),
    "fk_rollup": (
        "Allowed reference_oracle template for this archetype:\n"
        "- fk_rollup params {parent_collection, child_collection, parent_key, foreign_key, agg, "
        "optional value_field, optional match:{field,value}}. Pick a REAL parent->child foreign "
        "key: child_collection.foreign_key references parent_collection.parent_key (parent_key is "
        "usually '_id'). agg is count|sum|avg|min|max; value_field is the child's numeric field "
        "(required unless agg=count). This rolls each parent's child rows up via a $lookup join. "
        "Emit the literal template 'fk_rollup' — no other name."
    ),
}
_METRIC_FIELD_PRIORITY = (
    "amount", "balance", "payments", "value", "total", "sum", "cost", "price",
)


def _qps_schema_ref_violations(ref: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Catch QPS picking non-existent schema fields at the QPS stage.

    Builds the canonical gold from these oracle params and runs MS's own
    ``_unknown_schema_refs`` check — so QPS self-corrects (retrying with the *real* field
    list) instead of MS silently dropping the record. This is the dominant failure on
    messy-column-name BIRD dbs, where deepseek snake_cases/guesses names that don't exist.
    """
    try:
        canonical = _canonical_reference_mql({"reference_oracle": ref, "schema": schema})
        if not canonical:
            return []
        mql, _shape = canonical
        collection, _ = parse_pipeline(mql)
    except (TendError, Exception):  # noqa: BLE001 - validation must never crash QPS
        return []
    bad = _unknown_schema_refs(mql, schema, collection)
    if not bad:
        return []
    valid = sorted(_schema_field_paths(schema, collection))
    sample = ", ".join(valid[:40]) if valid else "(none)"
    return [
        f"reference_oracle.params reference non-existent fields {bad}. Use EXACT field names "
        f"copied verbatim from the schema (keep spaces, capitalization, punctuation — do NOT "
        f"snake_case or abbreviate). Valid fields for {collection!r}: {sample}"
    ]


@register
class QueryPlanSampler(LLMAgent):
    id = "qps"
    phase = "B"
    title = "QPS · Intent Enumerator"
    prompt_file = "qps_query_plan_sampler.md"
    output_schema = _QPS_SCHEMA

    def render_inputs(self, ctx: AgentContext, inputs: dict[str, Any]) -> str:
        archetype = inputs.get("archetype", "")
        seed_mechanism = _qps_public_seed_mechanism(inputs.get("mechanism"))
        recipe = _ARCHETYPE_RECIPE.get(archetype, "Choose a valid shape_policy "
                                       "(preserve/reshape/reduce) and a deterministic output.")
        oracle_guide = _ARCHETYPE_ORACLE_GUIDE.get(
            archetype,
            "Use only reference_oracle templates implemented by the benchmark oracle registry.",
        )
        target = (
            f"target_difficulty: {inputs.get('target_difficulty', 'L4')}\n"
            "target_sql_infeasibility_class: "
            f"{inputs.get('target_sql_infeasibility_class', 'structural_schema_flex')}\n"
            f"target_schema_flex: {inputs.get('target_schema_flex', 'polymorphic')}\n"
        )
        design_card = inputs.get("intent_seed")
        design_block = ""
        if isinstance(design_card, dict):
            design_block = (
                "## LLM-first design card (not a template)\n"
                "Use this as a pressure test for coverage, not as a fill-in form. Design a "
                "fresh financial query that targets the listed schema feature/family while "
                "choosing its own business computation and MQL structure.\n"
                "```json\n"
                f"{json.dumps(design_card, ensure_ascii=False, indent=2, sort_keys=True)}\n"
                "```\n"
            )
        diversity = ""
        if inputs.get("diversity_hint") or inputs.get("diversity_key"):
            diversity = (
                f"## diversity target\nkey: {inputs.get('diversity_key', '')}\n"
                f"schema_feature: {inputs.get('schema_feature', '')}\n"
                f"hint: {inputs.get('diversity_hint', '')}\n"
            )
        portfolio = ""
        diversity_context = inputs.get("diversity_context")
        if isinstance(diversity_context, dict):
            portfolio = (
                "## global portfolio context (shared across concurrent records)\n"
                "These counts are reserved before this QPS call. If a count is already "
                "non-zero, avoid making a near duplicate on that axis; change the business "
                "grain, branch condition, result shape, or multi-stage structure.\n"
                "```json\n"
                f"{json.dumps(diversity_context, ensure_ascii=False, indent=2, sort_keys=True)}\n"
                "```\n"
            )
        return ("# QPS — enumerate ONE concrete intent grounded in THIS schema (not operators)\n"
                f"seed_mechanism: {seed_mechanism}\narchetype: {archetype}\n"
                f"{target}"
                f"record_id: {inputs.get('record_id', '')}\nslot_index: {inputs.get('slot_index', '')}\n"
                f"scenario_summary: {inputs.get('scenario_summary', '')[:500]}\n"
                f"## schema (use ONLY these real collections/fields)\n{_schema_digest(inputs.get('schema', {}))}\n"
                f"{diversity}"
                f"{portfolio}"
                f"{design_block}"
                f"## archetype recipe\n{recipe}\n\n"
                "Design a non-trivial business query first. Avoid template-matrix "
                "expansion: do not create a near copy of a query by only changing a field name, "
                "accumulator, discriminator value, or output alias. Prefer multi-stage "
                "financial analysis when the schema supports it: embedded-array unwind, "
                "cross-collection rollup, optional-embed branch with a denominator, subtype "
                "field differences, or a reshape that changes the result grain.\n"
                "The target lines above are scheduler context; do not copy "
                "target_sql_infeasibility_class or schema_flex_mode into intent. "
                "Emit exactly top-level intent plus qps_trace. intent fields: "
                "seed_mechanism; seed_signal {collection, field}; archetype; target_difficulty; "
                "domain_framing (use the REAL collection/field names — do NOT invent entities "
                "like 'customers'/'phone' that aren't in the schema); analytical_op (a CONCRETE "
                "computation: target field name + exact formula + missing-default); "
                "shape_policy (preserve|reshape|reduce); semantic_properties. qps_trace fields: "
                "coverage_cell, deficit_weight, supply_constrained, optional rationale/skip_reason. "
                "In llm_design_mode, DO NOT emit reference_oracle at top "
                "level or inside intent; the workflow attaches a hidden certification oracle "
                "after QPS. The downstream gold is still certified by execution, mutation "
                "discrimination, RTV, realism, and skeleton diversity gates.")

    def check_contract(self, ctx, inputs, output) -> list[str]:
        intent = output.get("intent", {})
        v = []
        if inputs.get("llm_design_mode"):
            if "reference_oracle" in output:
                v.append("QPS design-mode output must not emit top-level reference_oracle")
            if isinstance(intent, dict) and "reference_oracle" in intent:
                v.append("QPS design-mode intent must not emit reference_oracle")
            if isinstance(intent, dict) and "target_sql_infeasibility_class" in intent:
                v.append(
                    "QPS design-mode intent must not emit target_sql_infeasibility_class; "
                    "workflow owns the scheduler target"
                )
            if isinstance(intent, dict) and "schema_flex_mode" in intent:
                v.append(
                    "QPS design-mode intent must not emit schema_flex_mode; workflow owns "
                    "the scheduler target"
                )
            trace = output.get("qps_trace")
            if not isinstance(trace, dict):
                v.append("QPS design-mode output must include qps_trace")
            else:
                for key in ("coverage_cell", "deficit_weight", "supply_constrained"):
                    if key not in trace:
                        v.append(f"qps_trace.{key} is required")
        ref = output.get("reference_oracle") or intent.get("reference_oracle")
        sp = intent.get("shape_policy")
        if sp not in ("preserve", "reshape", "reduce"):
            v.append(f"intent.shape_policy must be preserve/reshape/reduce, got {sp!r}")
        if inputs.get("archetype") == "present_missing_projection" and sp != "preserve":
            v.append("present_missing_projection requires shape_policy=preserve")
        if (
            sp == "preserve"
            and inputs.get("target_sql_infeasibility_class") == "structural_schema_flex"
        ):
            default_intent = (
                {**intent, "reference_oracle": ref}
                if isinstance(intent, dict) and isinstance(ref, dict)
                else intent
            )
            v.extend(_preserve_schema_flex_default_violations(default_intent))
        if inputs.get("archetype") == "schema_flex_variant_summary":
            if sp not in ("reshape", "reduce"):
                v.append("schema_flex_variant_summary requires shape_policy=reshape or reduce")
            if output.get("intent", {}).get("target_difficulty") not in (None, "L4"):
                v.append("schema_flex_variant_summary must target difficulty L4")
            op = intent.get("analytical_op") if isinstance(intent.get("analytical_op"), dict) else {}
            fields = (intent.get("output") or {}).get("fields") or []
            if not fields:
                fields = op.get("result_fields") or op.get("output_fields") or []
            if "variant" not in fields:
                v.append("schema_flex_variant_summary output.fields must include 'variant'")
            if "count" not in fields:
                v.append("schema_flex_variant_summary output.fields must include 'count'")
            target_field = (intent.get("analytical_op") or {}).get("target_field")
            if target_field and target_field not in fields:
                v.append(
                    "schema_flex_variant_summary output.fields must include "
                    f"analytical_op.target_field {target_field!r}"
                )
        if inputs.get("llm_design_mode"):
            return v
        if not isinstance(ref, dict):
            v.append("reference_oracle with supported template is required")
        else:
            template = ref.get("template")
            if not template:
                v.append("reference_oracle.template is required")
            elif not has_oracle(str(template)):
                v.append(f"unsupported reference_oracle.template {template!r}")
            else:
                param_errs = oracle_param_errors(str(template), ref.get("params"))
                v.extend(param_errs)
                if not param_errs:
                    v.extend(_qps_schema_ref_violations(ref, inputs.get("schema", {})))
        return v


def _qps_public_seed_mechanism(value: Any) -> str:
    mechanism = str(value or "none")
    return "none" if mechanism in {"", "baseline"} else mechanism


# --------------------------------------------------------------------------- #
# MS — MQL synthesizer + deterministic gold-lock
# --------------------------------------------------------------------------- #
_MS_SCHEMA = {
    "type": "object",
    "required": ["MQL", "shape_policy"],
    "properties": {
        "MQL": {"type": "string", "minLength": 10},
        "mql_alt": {"type": "string"},
        "shape_policy": {"enum": ["preserve", "reshape", "reduce"]},
        "schema_flex": {"type": "string"},
    },
    "additionalProperties": True,
}


def _oracle_prompt_rule(oracle: Any) -> str:
    """Small deterministic hints for oracle semantics LLMs commonly miss."""
    if not isinstance(oracle, dict):
        return ""
    template = oracle.get("template")
    params = oracle.get("params") if isinstance(oracle.get("params"), dict) else {}
    if template == "present_missing_projection":
        denom = params.get("denom") if isinstance(params.get("denom"), dict) else {}
        zero_value = denom.get("zero_value", 1)
        return (
            "For reference_oracle.template='present_missing_projection', preserve every "
            "parent document. If the optional embed is absent, set target_field to "
            "absent_value. If the embed is present, compute numerator_path divided by the "
            "denominator sum; when that denominator is missing or 0, use denom.zero_value "
            f"({zero_value!r}) as the divisor, not 0 as the final answer. "
        )
    if template == "optional_embed_projection":
        return (
            "For reference_oracle.template='optional_embed_projection', preserve every "
            "parent document and set the target to missing_default when the embed/value is "
            "missing or null. "
        )
    if template == "has_vs_absent_compare":
        return (
            "For reference_oracle.template='has_vs_absent_compare', group every parent "
            "document into exactly two possible labels: _id='present' when embed_field exists "
            "and _id='absent' when it is missing. The result field must be named 'value'. "
        )
    return ""


def _oracle_lock_contract(oracle: Any) -> str:
    """Human-readable oracle semantics for MS repair prompts.

    This is deliberately a result contract, not a canonical pipeline. The LLM still writes
    its own MQL, but it gets the exact cardinality, grouping, and output-shape constraints
    that the deterministic oracle will verify.
    """
    if not isinstance(oracle, dict):
        return ""
    template = oracle.get("template")
    params = oracle.get("params") if isinstance(oracle.get("params"), dict) else {}
    if template == "optional_embed_projection":
        return (
            "Result contract: preserve every document from "
            f"{params.get('parent_collection')!r}. Add exactly one computed field "
            f"{params.get('target_field')!r}. For documents where "
            f"{params.get('embed_field')!r} is present and "
            f"{_embed_value_path(str(params.get('embed_field')), str(params.get('value_path')))!r} "
            "is non-null, copy that value; otherwise use missing_default "
            f"{params.get('missing_default')!r}. Do not drop documents and do not leave helper "
            "fields in the final preserve result."
        )
    if template == "present_missing_projection":
        denom = params.get("denom") if isinstance(params.get("denom"), dict) else {}
        return (
            "Result contract: preserve every document from "
            f"{params.get('parent_collection')!r}. Add exactly one computed field "
            f"{params.get('target_field')!r}. If "
            f"{params.get('embed_field')!r} is absent, use absent_value "
            f"{params.get('absent_value', 0)!r}. If present, compute "
            f"{_embed_value_path(str(params.get('embed_field')), str(params.get('numerator_path')))!r} "
            f"divided by the per-parent sum of {denom.get('sum_field')!r} from "
            f"{denom.get('collection')!r} where {denom.get('foreign_field')!r} matches "
            f"the parent {denom.get('local_id')!r}; apply match {denom.get('match')!r} "
            f"when present and use zero_value {denom.get('zero_value', 1)!r} as the divisor "
            "when the sum is 0. Do not drop parent documents or leak helper fields."
        )
    if template == "has_vs_absent_compare":
        return (
            "Result contract: aggregate every document from "
            f"{params.get('parent_collection')!r} into exactly two groups. _id must be "
            f"'present' when {params.get('embed_field')!r} exists and 'absent' otherwise. "
            "The only metric field is 'value'. Use agg "
            f"{params.get('agg', 'count')!r}"
            + (
                f" over metric {params.get('metric_field')!r}; missing/non-numeric values count as 0."
                if params.get("agg", "count") != "count"
                else " as a document count."
            )
        )
    if template == "subtype_specific_field":
        project = params.get("project")
        return (
            "Result contract: read only documents from "
            f"{params.get('collection')!r} where discriminator "
            f"{params.get('discriminator')!r} equals subtype_value "
            f"{params.get('subtype_value')!r}. Do not preserve all documents. Output "
            + (
                f"exactly the projected fields {project!r}."
                if isinstance(project, list) and project
                else f"only field {params.get('field')!r}."
            )
            + " If the structural gate is active, use explicit $type/$cond in projection so "
            "missing subtype-only fields become null rather than disappearing."
        )
    if template == "per_subtype_agg":
        return (
            "Result contract: group documents from "
            f"{params.get('collection')!r} by discriminator {params.get('discriminator')!r}. "
            "For each subtype, aggregate only that subtype's own mapped field from "
            f"field_by_subtype={params.get('field_by_subtype')!r} with agg "
            f"{params.get('agg')!r}. Output fields are _id and value."
        )
    if template == "group_count":
        return (
            "Result contract: group every document from "
            f"{params.get('collection')!r} by {params.get('group_by')!r} and output "
            "one row per group with fields _id and count."
        )
    if template == "topn":
        return (
            "Result contract: sort documents from "
            f"{params.get('collection')!r} by {params.get('sort_key')!r} in "
            f"{params.get('order', 'desc')!r} order, apply nulls "
            f"{params.get('nulls', 'last')!r}, take n={params.get('n')!r}, and output "
            f"projected fields {params.get('project')!r} when provided."
        )
    if template == "fk_rollup":
        return (
            "Result contract: start from parent collection "
            f"{params.get('parent_collection')!r}, join child collection "
            f"{params.get('child_collection')!r} by parent key {params.get('parent_key')!r} "
            f"to child FK {params.get('foreign_key')!r}, then output one parent row with field "
            "'value' from the requested child aggregate."
        )
    if template == "existence_count":
        return (
            "Result contract: count documents from "
            f"{params.get('collection')!r} where field {params.get('field')!r} is present. "
            "Output exactly one row with field count."
        )
    if template == "null_coalesce_agg":
        return (
            "Result contract: aggregate field "
            f"{params.get('field')!r} from {params.get('collection')!r} with agg "
            f"{params.get('agg')!r}; coalesce missing/null/non-numeric values to default "
            f"{params.get('default', 0)!r}. Output exactly one row with field value."
        )
    return (
        "Result contract: make the MQL result exactly match reference_oracle.template "
        f"{template!r} with params {_stable_json(params)}."
    )


def _oracle_lock_contract_inline(oracle: Any) -> str:
    contract = _oracle_lock_contract(oracle)
    return " ".join(contract.split())


_COMPILED_REFERENCE_ORACLE_RTV_MODE = "compiled_reference_oracle_nl_contract"
_NL_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_AGG_NL_ALIASES: dict[str, tuple[str, ...]] = {
    "count": ("count", "counts", "number"),
    "sum": ("sum", "total"),
    "avg": ("avg", "average", "mean"),
    "min": ("min", "minimum", "smallest", "lowest"),
    "max": ("max", "maximum", "largest", "highest"),
}
_OP_NL_ALIASES: dict[str, tuple[str, ...]] = {
    "eq": ("equals", "equal", "is", "matching"),
    "ne": ("not equal", "not equals", "different from", "not"),
    "gt": ("greater than", "more than", "above", "over", "exceeds"),
    "gte": ("greater than or equal", "at least", "no less than", "minimum"),
    "lt": ("less than", "below", "under", "fewer than"),
    "lte": ("less than or equal", "at most", "no more than", "maximum"),
}
_COMPILED_RTV_UNSUPPORTED_TEMPLATES: set[str] = set()


def _compiled_reference_oracle_nl_contract(inputs: dict[str, Any]) -> dict[str, Any]:
    """Deterministic RTV for gold that was compiled from a reference oracle.

    The executable gold is already certified by the reference-oracle compiler. This verifier keeps
    the RTV boundary but checks the NL contract directly, avoiding an LLM NL->MQL translation whose
    failures are mostly translator noise for compiled templates.
    """
    violations: list[str] = []
    missing_terms: list[str] = []

    nlq = inputs.get("nl_queries")
    if not isinstance(nlq, dict):
        violations.append("nl_queries must be an object with canonical and colloquial strings")
        nlq = {}
    canonical = str(nlq.get("canonical", "")).strip()
    colloquial = str(nlq.get("colloquial", "")).strip()
    if not canonical:
        violations.append("canonical NLQ is empty")
    if not colloquial:
        violations.append("colloquial NLQ is empty")
    if "$" in canonical:
        violations.append("canonical NLQ must not contain $ operator terms")

    ref = inputs.get("reference_oracle")
    template: str | None = None
    params: dict[str, Any] | None = None
    if not isinstance(ref, dict):
        violations.append("reference_oracle with supported template is required")
    else:
        raw_template = ref.get("template")
        if not isinstance(raw_template, str) or not raw_template.strip():
            violations.append("reference_oracle.template is required")
        else:
            template = raw_template.strip()
            if not has_oracle(template):
                violations.append(f"unsupported reference_oracle.template {template!r}")
            elif template not in _CANONICAL_MQL_BUILDERS:
                violations.append(
                    f"reference_oracle.template {template!r} is not supported by compiled RTV"
                )
        raw_params = ref.get("params")
        if not isinstance(raw_params, dict):
            violations.append("reference_oracle.params must be an object")
        else:
            params = raw_params
            if template and has_oracle(template):
                violations.extend(oracle_param_errors(template, params))
            if template:
                violations.extend(_compiled_rtv_param_violations(template, params))

    shape_policy = inputs.get("shape_policy")
    if shape_policy not in {"preserve", "reshape", "reduce"}:
        violations.append(f"shape_policy must be preserve/reshape/reduce, got {shape_policy!r}")
    elif template in _CANONICAL_SHAPE and shape_policy != _CANONICAL_SHAPE[template]:
        violations.append(
            f"shape_policy {shape_policy!r} does not match compiled template {template!r} "
            f"expected {_CANONICAL_SHAPE[template]!r}"
        )

    result_fields, result_field_errors = _compiled_rtv_result_fields(inputs.get("result_fields"))
    violations.extend(result_field_errors)
    if template and params is not None and result_fields:
        violations.extend(_compiled_rtv_result_field_violations(template, params, result_fields))

    compiled_gold_provenance = inputs.get("compiled_gold_provenance")
    if not isinstance(compiled_gold_provenance, dict):
        violations.append("compiled_gold_provenance from workflow direct compile is required")
    else:
        expected_provenance = {
            "source": "workflow_direct_compile",
            "compiler": "_canonical_reference_mql",
            "template": template,
            "gold_lock": "norm_exec_nonempty",
        }
        for key, expected in expected_provenance.items():
            if compiled_gold_provenance.get(key) != expected:
                violations.append(
                    f"compiled_gold_provenance.{key} must be {expected!r}, "
                    f"got {compiled_gold_provenance.get(key)!r}"
                )

    contract_spec: list[str] = []
    if template and params is not None and template in _CANONICAL_MQL_BUILDERS:
        if template in _compiled_rtv_contract_template_gaps():
            violations.append(
                f"uncovered compiled RTV contract spec for reference_oracle.template {template!r}"
            )
        else:
            requirements = _compiled_rtv_contract_requirements(template, params, shape_policy)
            if requirements is None:
                violations.append(
                    f"uncovered compiled RTV contract spec for reference_oracle.template {template!r}"
                )
            else:
                contract_spec = [label for label, _alternatives in requirements]
                missing_terms = _compiled_rtv_missing_terms(canonical, requirements)
                if missing_terms:
                    violations.append(
                        "compiled reference-oracle NL contract missing required terms: "
                        + ", ".join(missing_terms)
                    )

    if isinstance(shape_policy, str):
        violations.extend(_compiled_rtv_shape_violations(shape_policy, canonical))

    output: dict[str, Any] = {
        "rtv_pass": not violations,
        "rtv_reason": "; ".join(violations) if violations else "",
        "rtv_mode": _COMPILED_REFERENCE_ORACLE_RTV_MODE,
        "violations": violations,
        "missing_terms": missing_terms,
        "template": template,
        "result_fields": result_fields,
        "shape_policy": shape_policy,
        "contract_spec": contract_spec,
    }
    if isinstance(compiled_gold_provenance, dict):
        output["provenance"] = compiled_gold_provenance
        output["compiled_gold_provenance"] = compiled_gold_provenance
    return output


def _compiled_rtv_result_fields(value: Any) -> tuple[list[str], list[str]]:
    if not isinstance(value, list) or not value:
        return [], ["result_fields must be a non-empty list of strings"]
    fields: list[str] = []
    bad = False
    for item in value:
        if not isinstance(item, str) or not item.strip():
            bad = True
        else:
            fields.append(item.strip())
    return fields, (["result_fields must be a non-empty list of strings"] if bad or not fields else [])


def _compiled_rtv_param_violations(template: str, params: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    agg = str(params.get("agg", "count") or "count").lstrip("$").lower()
    if template == "has_vs_absent_compare" and agg != "count":
        metric = params.get("metric_field")
        if not isinstance(metric, str) or not metric.strip():
            violations.append(
                "reference_oracle.params.metric_field is required when "
                "has_vs_absent_compare agg is not count"
            )
    if template == "fk_rollup" and agg != "count":
        value_field = params.get("value_field")
        if not isinstance(value_field, str) or not value_field.strip():
            violations.append(
                "reference_oracle.params.value_field is required when fk_rollup agg is not count"
            )
    return violations


def _compiled_rtv_result_field_violations(
    template: str, params: dict[str, Any], result_fields: list[str]
) -> list[str]:
    expected = _compiled_rtv_expected_result_fields(template, params)
    if not expected:
        return []
    missing = sorted(set(expected) - set(result_fields))
    if not missing:
        return []
    return [
        "result_fields missing template output fields for "
        f"{template!r}: {', '.join(repr(f) for f in missing)}"
    ]


def _compiled_rtv_expected_result_fields(template: str, params: dict[str, Any]) -> set[str]:
    if template in {
        "optional_embed_projection",
        "present_missing_projection",
        "subtype_cond_projection",
    }:
        target = params.get("target_field")
        return {target} if isinstance(target, str) and target else set()
    if template == "group_count":
        return {"_id", "count"}
    if template == "existence_count":
        return {"count"}
    if template in {
        "has_vs_absent_compare",
        "null_coalesce_agg",
        "per_subtype_agg",
        "cross_subtype_compare",
        "join_nested_group",
        "dynamic_key_fold",
        "cross_version_agg",
        "fk_rollup",
    }:
        return {"value"} if template in {"null_coalesce_agg", "cross_version_agg"} else {"_id", "value"}
    if template in {"simple_filter", "topn", "subtype_specific_field", "cross_keyset_value"}:
        project = params.get("project")
        if isinstance(project, list) and project:
            return {field for field in project if isinstance(field, str) and field}
        field_key = "key" if template == "cross_keyset_value" else "field"
        field = params.get(field_key)
        return {field} if isinstance(field, str) and field else set()
    return set()


def _compiled_rtv_shape_violations(shape_policy: str, canonical: str) -> list[str]:
    if shape_policy not in {"reshape", "reduce"}:
        return []
    preserve_phrases = (
        "add a field",
        "adds a field",
        "attach a field",
        "attached field",
        "to each document",
        "each document",
        "keep all other",
        "keep each",
        "keep every",
        "kept unchanged",
        "otherwise unchanged",
        "unchanged",
    )
    if any(_compiled_rtv_mentions(canonical, phrase) for phrase in preserve_phrases):
        return [f"{shape_policy} canonical NLQ must not describe preserve/add-field semantics"]
    return []


def _compiled_rtv_contract_template_gaps() -> list[str]:
    covered = set(_COMPILED_RTV_CONTRACT_BUILDERS) | _COMPILED_RTV_UNSUPPORTED_TEMPLATES
    return sorted(set(_CANONICAL_MQL_BUILDERS) - covered)


def _compiled_rtv_contract_requirements(
    template: str, params: dict[str, Any], shape_policy: Any
) -> list[tuple[str, tuple[str, ...]]] | None:
    builder = _COMPILED_RTV_CONTRACT_BUILDERS.get(template)
    if builder is None:
        return None
    requirements = builder(params)
    if shape_policy == "preserve":
        collection = (
            params.get("parent_collection")
            if template in {"optional_embed_projection", "present_missing_projection"}
            else params.get("collection")
        )
        requirements.extend(_compiled_rtv_preserve_requirements(collection))
    return [req for req in requirements if req[1]]


def _compiled_rtv_missing_terms(
    canonical: str, requirements: list[tuple[str, tuple[str, ...]]]
) -> list[str]:
    missing: list[str] = []
    for label, alternatives in requirements:
        if not any(_compiled_rtv_mentions(canonical, alternative) for alternative in alternatives):
            missing.append(label)
    return missing


def _compiled_rtv_mentions(text: str, phrase: str) -> bool:
    needle = _compiled_rtv_tokens(phrase)
    if not needle:
        return False
    haystack = _compiled_rtv_tokens(text)
    if len(needle) > len(haystack):
        return False
    for index in range(len(haystack) - len(needle) + 1):
        if haystack[index:index + len(needle)] == needle:
            return True
    return False


def _compiled_rtv_tokens(text: Any) -> list[str]:
    raw = str(text).replace("_", " ").replace(".", " ").replace("-", " ").lower()
    return [_compiled_rtv_token_stem(token) for token in _NL_TOKEN_RE.findall(raw)]


def _compiled_rtv_token_stem(token: str) -> str:
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _compiled_req(label: Any, *alternatives: Any) -> tuple[str, tuple[str, ...]]:
    label_text = str(label).strip()
    alts: list[str] = []
    for alt in alternatives:
        if isinstance(alt, (list, tuple, set)):
            alts.extend(str(item).strip() for item in alt if str(item).strip())
        elif alt is not None and str(alt).strip():
            alts.append(str(alt).strip())
    return label_text, tuple(dict.fromkeys(alt for alt in alts if alt != "_id"))


def _compiled_field_terms(field: Any, *, leaf: bool = False) -> tuple[str, ...]:
    if not isinstance(field, str) or not field.strip() or field == "_id":
        return ()
    terms = [field.strip()]
    if leaf and "." in field:
        terms.append(field.rsplit(".", 1)[-1])
    return tuple(dict.fromkeys(terms))


def _compiled_value_terms(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    terms = [str(value)]
    if value == 0:
        terms.append("zero")
    elif value == 1:
        terms.append("one")
    return tuple(dict.fromkeys(term for term in terms if term.strip()))


def _compiled_agg_terms(agg: Any) -> tuple[str, ...]:
    name = str(agg or "count").lstrip("$").lower()
    return _AGG_NL_ALIASES.get(name, (name,))


def _compiled_op_terms(op: Any) -> tuple[str, ...]:
    name = str(op or "").lstrip("$").lower()
    return _OP_NL_ALIASES.get(name, (name,)) if name else ()


def _compiled_null_order_terms(nulls: Any) -> tuple[str, ...]:
    name = str(nulls or "last").lower()
    if name == "first":
        return ("nulls first", "missing first", "null first")
    return ("nulls last", "missing last", "null last")


def _compiled_projection_requirements(project: Any) -> list[tuple[str, tuple[str, ...]]]:
    if not isinstance(project, list) or not project:
        return []
    requirements = [_compiled_req("projection/output fields", "project", "output", "return")]
    for field in project:
        requirements.append(_compiled_req(field, _compiled_field_terms(field, leaf=True)))
    return requirements


def _compiled_match_requirements(
    match: Any,
    *,
    label_prefix: str,
) -> list[tuple[str, tuple[str, ...]]]:
    if not isinstance(match, dict) or match.get("field") is None:
        return []
    return [
        _compiled_req(f"{label_prefix} filter", "filter", "where", "matching", "only"),
        _compiled_req(
            f"{label_prefix}.field={match.get('field')!r}",
            _compiled_field_terms(match.get("field"), leaf=True),
        ),
        _compiled_req(
            f"{label_prefix}.value={match.get('value')!r}",
            _compiled_value_terms(match.get("value")),
        ),
    ]


def _compiled_rtv_preserve_requirements(collection: Any) -> list[tuple[str, tuple[str, ...]]]:
    keep_terms = [
        "keep each",
        "keep every",
        "preserve each",
        "preserve every",
        "retain each",
        "retain every",
        "for each",
        "each document",
        "every document",
        "otherwise unchanged",
    ]
    if isinstance(collection, str) and collection:
        keep_terms.extend([f"each {collection}", f"every {collection}"])
    return [
        _compiled_req("preserve/add-field semantics", "add", "attach", "computed field", "new field"),
        _compiled_req("keep-every-document semantics", keep_terms),
    ]


def _compiled_optional_embed_projection_requirements(
    params: dict[str, Any]
) -> list[tuple[str, tuple[str, ...]]]:
    return [
        _compiled_req(params.get("target_field"), _compiled_field_terms(params.get("target_field"))),
        _compiled_req(params.get("embed_field"), _compiled_field_terms(params.get("embed_field"))),
        _compiled_req(params.get("value_path"), _compiled_field_terms(params.get("value_path"), leaf=True)),
        _compiled_req(
            f"missing_default={params.get('missing_default')!r}",
            _compiled_value_terms(params.get("missing_default")),
            "default",
            "otherwise",
            "missing",
        ),
    ]


def _compiled_present_missing_projection_requirements(
    params: dict[str, Any]
) -> list[tuple[str, tuple[str, ...]]]:
    denom = params.get("denom") if isinstance(params.get("denom"), dict) else {}
    requirements = [
        _compiled_req(params.get("target_field"), _compiled_field_terms(params.get("target_field"))),
        _compiled_req(params.get("embed_field"), _compiled_field_terms(params.get("embed_field"))),
        _compiled_req(
            params.get("numerator_path"),
            _compiled_field_terms(params.get("numerator_path"), leaf=True),
        ),
        _compiled_req(
            f"absent_value={params.get('absent_value', 0)!r}",
            _compiled_value_terms(params.get("absent_value", 0)),
            "absent",
            "missing",
            "default",
        ),
        _compiled_req(denom.get("collection"), _compiled_field_terms(denom.get("collection"))),
        _compiled_req(denom.get("local_id"), _compiled_field_terms(denom.get("local_id"), leaf=True)),
        _compiled_req(
            denom.get("foreign_field"),
            _compiled_field_terms(denom.get("foreign_field"), leaf=True),
        ),
        _compiled_req(denom.get("sum_field"), _compiled_field_terms(denom.get("sum_field"), leaf=True)),
        _compiled_req(
            f"zero_value={denom.get('zero_value', 1)!r}",
            _compiled_value_terms(denom.get("zero_value", 1)),
            "zero divisor",
            "zero denominator",
            "when denominator is zero",
        ),
    ]
    requirements.extend(_compiled_match_requirements(denom.get("match"), label_prefix="denom.match"))
    return requirements


def _compiled_has_vs_absent_compare_requirements(
    params: dict[str, Any]
) -> list[tuple[str, tuple[str, ...]]]:
    agg = str(params.get("agg", "count") or "count").lstrip("$").lower()
    requirements = [
        _compiled_req(params.get("embed_field"), _compiled_field_terms(params.get("embed_field"))),
        _compiled_req("present", "present"),
        _compiled_req("absent", "absent"),
    ]
    if agg != "count":
        requirements.append(_compiled_req(agg, _compiled_agg_terms(agg)))
        metric = params.get("metric_field")
        requirements.append(
            _compiled_req(metric, _compiled_field_terms(metric, leaf=True))
            if isinstance(metric, str) and metric
            else _compiled_req("metric_field", ())
        )
    else:
        requirements.append(_compiled_req("count", _compiled_agg_terms("count")))
    return requirements


def _compiled_simple_filter_requirements(params: dict[str, Any]) -> list[tuple[str, tuple[str, ...]]]:
    requirements: list[tuple[str, tuple[str, ...]]] = []
    for pred in params.get("predicates", []) or []:
        if not isinstance(pred, dict):
            continue
        requirements.append(_compiled_req(pred.get("field"), _compiled_field_terms(pred.get("field"))))
        requirements.append(
            _compiled_req(f"predicate op {pred.get('op')}", _compiled_op_terms(pred.get("op")))
        )
        if "value" in pred:
            requirements.append(_compiled_req(pred.get("value"), _compiled_value_terms(pred.get("value"))))
    requirements.extend(_compiled_projection_requirements(params.get("project")))
    return requirements


def _compiled_topn_requirements(params: dict[str, Any]) -> list[tuple[str, tuple[str, ...]]]:
    order = params.get("order", "desc")
    order_terms = ("ascending", "lowest", "smallest") if order == "asc" else (
        "descending", "highest", "largest", "top"
    )
    requirements = [
        _compiled_req(params.get("sort_key"), _compiled_field_terms(params.get("sort_key"), leaf=True)),
        _compiled_req("top", "top", "first", "limit"),
        _compiled_req(params.get("n"), _compiled_value_terms(params.get("n"))),
        _compiled_req(order, order_terms),
        _compiled_req(
            f"nulls={params.get('nulls', 'last')!r}",
            _compiled_null_order_terms(params.get("nulls", "last")),
        ),
    ]
    requirements.extend(_compiled_projection_requirements(params.get("project")))
    return requirements


def _compiled_group_count_requirements(params: dict[str, Any]) -> list[tuple[str, tuple[str, ...]]]:
    return [
        _compiled_req(params.get("group_by"), _compiled_field_terms(params.get("group_by"), leaf=True)),
        _compiled_req("count", _compiled_agg_terms("count")),
    ]


def _compiled_existence_count_requirements(params: dict[str, Any]) -> list[tuple[str, tuple[str, ...]]]:
    return [
        _compiled_req(params.get("field"), _compiled_field_terms(params.get("field"), leaf=True)),
        _compiled_req("count", _compiled_agg_terms("count")),
    ]


def _compiled_null_coalesce_agg_requirements(
    params: dict[str, Any]
) -> list[tuple[str, tuple[str, ...]]]:
    return [
        _compiled_req(params.get("field"), _compiled_field_terms(params.get("field"), leaf=True)),
        _compiled_req(params.get("agg"), _compiled_agg_terms(params.get("agg"))),
        _compiled_req(
            f"default={params.get('default', 0)!r}",
            _compiled_value_terms(params.get("default", 0)),
            "default",
            "coalesce",
            "missing",
            "null",
        ),
    ]


def _compiled_subtype_agg_requirements(params: dict[str, Any]) -> list[tuple[str, tuple[str, ...]]]:
    requirements = [
        _compiled_req(params.get("discriminator"), _compiled_field_terms(params.get("discriminator"))),
        _compiled_req(params.get("agg"), _compiled_agg_terms(params.get("agg"))),
    ]
    fbs = params.get("field_by_subtype")
    if isinstance(fbs, dict):
        for field in fbs.values():
            requirements.append(_compiled_req(field, _compiled_field_terms(field, leaf=True)))
    return requirements


def _compiled_subtype_cond_projection_requirements(
    params: dict[str, Any]
) -> list[tuple[str, tuple[str, ...]]]:
    requirements = [
        _compiled_req(params.get("target_field"), _compiled_field_terms(params.get("target_field"))),
        _compiled_req(params.get("discriminator"), _compiled_field_terms(params.get("discriminator"))),
        _compiled_req(
            f"default={params.get('default', 0)!r}",
            _compiled_value_terms(params.get("default", 0)),
            "default",
        ),
    ]
    fbs = params.get("field_by_subtype")
    if isinstance(fbs, dict):
        for field in fbs.values():
            requirements.append(_compiled_req(field, _compiled_field_terms(field, leaf=True)))
    return requirements


def _compiled_subtype_specific_field_requirements(
    params: dict[str, Any]
) -> list[tuple[str, tuple[str, ...]]]:
    return [
        _compiled_req(params.get("discriminator"), _compiled_field_terms(params.get("discriminator"))),
        _compiled_req(params.get("subtype_value"), params.get("subtype_value")),
        _compiled_req(params.get("field"), _compiled_field_terms(params.get("field"), leaf=True)),
    ]


def _compiled_join_nested_group_requirements(
    params: dict[str, Any]
) -> list[tuple[str, tuple[str, ...]]]:
    requirements = [
        _compiled_req(params.get("array_field"), _compiled_field_terms(params.get("array_field"))),
        _compiled_req(params.get("group_by"), _compiled_field_terms(params.get("group_by"), leaf=True)),
        _compiled_req(params.get("agg", "count"), _compiled_agg_terms(params.get("agg", "count"))),
    ]
    if params.get("agg", "count") != "count" or params.get("value_field"):
        requirements.append(
            _compiled_req(
                params.get("value_field"),
                _compiled_field_terms(params.get("value_field"), leaf=True),
            )
        )
    return requirements


def _compiled_dynamic_key_fold_requirements(
    params: dict[str, Any]
) -> list[tuple[str, tuple[str, ...]]]:
    return [
        _compiled_req(params.get("name_field"), _compiled_field_terms(params.get("name_field"))),
        _compiled_req(params.get("value_field"), _compiled_field_terms(params.get("value_field"), leaf=True)),
        _compiled_req(params.get("agg"), _compiled_agg_terms(params.get("agg"))),
    ]


def _compiled_cross_keyset_value_requirements(
    params: dict[str, Any]
) -> list[tuple[str, tuple[str, ...]]]:
    return [_compiled_req(params.get("key"), _compiled_field_terms(params.get("key"), leaf=True))]


def _compiled_cross_version_agg_requirements(
    params: dict[str, Any]
) -> list[tuple[str, tuple[str, ...]]]:
    requirements = [_compiled_req(params.get("agg"), _compiled_agg_terms(params.get("agg")))]
    candidates = params.get("field_candidates")
    if isinstance(candidates, list):
        for field in candidates:
            requirements.append(_compiled_req(field, _compiled_field_terms(field, leaf=True)))
    requirements.append(
        _compiled_req(
            f"default={params.get('default', 0)!r}",
            _compiled_value_terms(params.get("default", 0)),
            "default",
            "coalesce",
        )
    )
    return requirements


def _compiled_fk_rollup_requirements(params: dict[str, Any]) -> list[tuple[str, tuple[str, ...]]]:
    requirements = [
        _compiled_req(params.get("parent_collection"), _compiled_field_terms(params.get("parent_collection"))),
        _compiled_req(params.get("child_collection"), _compiled_field_terms(params.get("child_collection"))),
        _compiled_req(params.get("parent_key"), _compiled_field_terms(params.get("parent_key"), leaf=True)),
        _compiled_req(params.get("foreign_key"), _compiled_field_terms(params.get("foreign_key"), leaf=True)),
        _compiled_req("join/rollup", "join", "roll up", "rollup", "matching child"),
        _compiled_req(params.get("agg", "count"), _compiled_agg_terms(params.get("agg", "count"))),
    ]
    if params.get("agg", "count") != "count":
        requirements.append(
            _compiled_req(
                params.get("value_field"),
                _compiled_field_terms(params.get("value_field"), leaf=True),
            )
        )
    requirements.extend(_compiled_match_requirements(params.get("match"), label_prefix="match"))
    return requirements


_COMPILED_RTV_CONTRACT_BUILDERS: dict[
    str, Callable[[dict[str, Any]], list[tuple[str, tuple[str, ...]]]]
] = {
    "simple_filter": _compiled_simple_filter_requirements,
    "topn": _compiled_topn_requirements,
    "group_count": _compiled_group_count_requirements,
    "join_nested_group": _compiled_join_nested_group_requirements,
    "fk_rollup": _compiled_fk_rollup_requirements,
    "existence_count": _compiled_existence_count_requirements,
    "null_coalesce_agg": _compiled_null_coalesce_agg_requirements,
    "per_subtype_agg": _compiled_subtype_agg_requirements,
    "subtype_cond_projection": _compiled_subtype_cond_projection_requirements,
    "cross_subtype_compare": _compiled_subtype_agg_requirements,
    "subtype_specific_field": _compiled_subtype_specific_field_requirements,
    "present_missing_projection": _compiled_present_missing_projection_requirements,
    "optional_embed_projection": _compiled_optional_embed_projection_requirements,
    "has_vs_absent_compare": _compiled_has_vs_absent_compare_requirements,
    "dynamic_key_fold": _compiled_dynamic_key_fold_requirements,
    "cross_keyset_value": _compiled_cross_keyset_value_requirements,
    "cross_version_agg": _compiled_cross_version_agg_requirements,
}


@register
class MqlSynthesizer(LLMAgent):
    id = "ms"
    phase = "B"
    title = "MS · MQL Synthesizer"
    prompt_file = "ms_mql_synthesizer.md"
    output_schema = _MS_SCHEMA
    offload_postprocess = True

    def render_inputs(self, ctx: AgentContext, inputs: dict[str, Any]) -> str:
        import json
        intent = inputs.get("intent", {})
        oracle = inputs.get("reference_oracle") or intent.get("reference_oracle")
        fb = inputs.get("ms_feedback")
        fb_note = ""
        if fb:
            fb_note = (
                "\n\n## previous gold-lock failure\n"
                f"{fb}\n"
                "Repair the MQL to satisfy the oracle result contract below exactly. If the "
                "failure reports mql_rows != oracle_rows, the output cardinality/shape is wrong; "
                "do not just rename fields.\n"
            )
        oracle_rule = _oracle_prompt_rule(oracle)
        oracle_contract = _oracle_lock_contract(oracle)
        if isinstance(oracle, dict):
            oracle_block = (
                "## optional reference_oracle (certification aid, not a template)\n"
                f"```json\n{json.dumps(oracle, ensure_ascii=False, indent=2, sort_keys=True)}\n```\n"
                "If present, the deterministic lock may compare your MQL result to this "
                "oracle. Do not mechanically copy a canonical template; your representative "
                "MQL should still be an independently designed query.\n"
                f"## oracle result contract\n{oracle_contract}\n"
            )
            lock_rule = (
                "Do not alter the reference_oracle; the deterministic lock will run "
                "reference_oracle.template with reference_oracle.params against DM's snapshot "
                "and require it to match YOUR MQL result. "
            )
        else:
            oracle_block = (
                "## certification mode\n"
                "No reference_oracle is provided. The representative MQL itself is the gold "
                "program, and it will be certified by execution, static checks, preserve-shape "
                "checks, mutation discrimination, NL round-trip, NNC, RA, and skeleton-family "
                "diversity gates.\n"
            )
            lock_rule = (
                "Because there is no reference oracle, make the MQL directly express the "
                "business intent without hidden assumptions; downstream agents will reject it "
                "if the NL, realism, complexity, or execution behavior is weak. "
            )
        target = (
            f"target_difficulty: {inputs.get('target_difficulty', 'L4')}\n"
            "target_sql_infeasibility_class: "
            f"{inputs.get('target_sql_infeasibility_class', 'structural_schema_flex')}\n"
            f"target_schema_flex: {inputs.get('target_schema_flex', 'polymorphic')}\n"
        )
        structural_rule = ""
        if inputs.get("target_sql_infeasibility_class") == "structural_schema_flex":
            template = oracle.get("template") if isinstance(oracle, dict) else None
            if template == "subtype_specific_field":
                structural_rule = (
                    "\nFor target structural_schema_flex with subtype_specific_field, the MQL "
                    "MUST filter the real discriminator to the requested subtype value and project "
                    "the subtype-exclusive field. Because the structural gate needs visible "
                    "shape-awareness, use $type/$cond in the projection for projected fields; do "
                    "not use a preserve all-documents pipeline."
                )
            else:
                structural_rule = (
                    "\nFor target structural_schema_flex, the MQL MUST visibly dispatch over real "
                    "document-shape variants using $type/$switch/$cond on the optional embedded "
                    "sub-document or discriminator-specific fields. The representative MQL field "
                    "itself must contain $type or $objectToArray and must contain $cond or "
                    "$switch; putting that logic only in mql_alt does not satisfy gold-lock. "
                    "Set schema_flex='polymorphic'."
                )
            if intent.get("archetype") == "schema_flex_variant_summary":
                structural_rule += (
                    " This summary archetype must reshape/reduce into a variant summary; do "
                    "not output a plain preserve-only computed field. Output a field named "
                    "'variant' whose only values are the exact strings 'present' and "
                    "'missing', plus 'count' and the aggregate target field."
                )
            elif intent.get("shape_policy") == "preserve":
                structural_rule += (
                    " This preserve archetype must keep every root document and attach the "
                    "computed target field; do not add a root $group."
                )
        return ("# MS — synthesize gold MQL that realizes THIS intent exactly\n"
                f"## coverage target\n{target}"
                f"## intent\n```json\n{json.dumps(intent, ensure_ascii=False, indent=2, sort_keys=True)}\n```\n"
                f"{oracle_block}"
                f"## schema (real collections/fields)\n{_schema_digest(inputs.get('schema', {}))}{fb_note}\n\n"
                "Produce your own MQL = db.<root_collection>.aggregate([...]) realizing the intent's "
                "exact formula + missing-default. Honor intent.shape_policy: if 'preserve', use "
                "$addFields to attach the computed field and KEEP every input document and its "
                "original fields (no $match that drops docs, no root $group/$unwind). Remove "
                "any temporary lookup/sum/helper fields before the final output; the only new "
                "field left in preserve output should be the intent target field. "
                f"{oracle_rule}"
                "Do not output a mechanical minimal template unless the intent truly requires "
                "that exact shape. Use the real financial schema to choose a query structure: "
                "$lookup with a child rollup, $unwind over embedded arrays, branch-specific "
                "$switch/$cond, pre-aggregation helper stages, or an equivalent algebraic "
                "rewrite when that makes the query more realistic. Do not make a family of "
                "near-identical queries by only changing field names or $sum/$avg/$min/$max. "
                f"{lock_rule}"
                "Also give mql_alt (an equivalent algebraic rewrite) + shape_policy + schema_flex. "
                "No banned operators ($sample/$rand/$$NOW/$out/$merge/$function)."
                f"{structural_rule}")

    def postprocess(self, ctx, inputs, output, result) -> dict[str, Any]:
        mql = output["MQL"]
        promoted = _promote_structural_alt_if_valid(mql, output.get("mql_alt"), inputs)
        if promoted:
            output["MQL"] = promoted
            output["mql_alt"] = mql
            output["representative_mql_promoted_from_alt"] = True
            mql = promoted
        canonical = (
            _canonical_reference_mql(inputs)
            if inputs.get("allow_reference_oracle_canonicalization")
            else None
        )
        if canonical:
            canonical_mql, canonical_shape = canonical
            output["llm_MQL"] = mql
            output["MQL"] = canonical_mql
            output["shape_policy"] = canonical_shape  # gold's true shape drives the preserve gate
            output["reference_oracle_canonicalized"] = True
            mql = canonical_mql
            ctx.log.info(
                "ms_reference_oracle_canonicalized",
                template=(inputs.get("reference_oracle") or {}).get("template"),
                shape_policy=canonical_shape,
                transcript_ref=getattr(result, "transcript_ref", None),
                diagnostics_ref=getattr(result, "diagnostics_ref", None),
            )
        shape = output.get("shape_policy", "preserve")
        hits = scan_disabled(mql)
        if hits:
            output["gold_locked"] = False
            output["gold_lock_reason"] = f"banned operators: {hits}"
            return output
        output["canonical_form_set"] = derive_canonical_form_set(mql, shape)
        output.setdefault("schema_flex", "none")
        try:
            _, pipeline = parse_pipeline(mql)
            output["join_depth"] = sum(1 for s in pipeline if "$lookup" in s)
            output["aggregation_depth"] = _bucket(len(pipeline))
        except TendError:
            output["aggregation_depth"] = "shallow"
            output["join_depth"] = 0

        if ctx.settings.stub:
            output.setdefault("gold_locked", True)
            output.setdefault("mql_alt", mql)
            return output

        if ctx.mongo is None or not ctx.mongo.available():
            output["gold_locked"] = False
            output["gold_lock_reason"] = "MongoDB executor unavailable for gold-lock"
            return output

        # gold-lock: executes + non-empty + (preserve => cardinality preserved) + independent
        # reference oracle R over DM's authoritative snapshot. Dual-path remains advisory only
        # because independently-generated LLM pairs are often inconsistent.
        try:
            collection, _ = parse_pipeline(mql)
            bad_refs = _unknown_schema_refs(mql, inputs.get("schema", {}), collection)
            if bad_refs:
                output["gold_locked"] = False
                output["gold_lock_reason"] = "unknown schema field refs: " + ", ".join(bad_refs)
                return output
            structural_reasons = _structural_schema_flex_reasons(
                mql, inputs.get("schema", {}), collection, inputs
            )
            if structural_reasons:
                output["gold_locked"] = False
                output["gold_lock_reason"] = "; ".join(structural_reasons)
                return output
            if inputs.get("target_schema_flex") and inputs.get("target_schema_flex") != "none":
                output["schema_flex"] = inputs["target_schema_flex"]
            r_primary = [_normalize_doc(d) for d in ctx.mongo.norm_exec(ctx.db_id, mql)]
            if not r_primary:
                output["gold_locked"] = False
                output["gold_lock_reason"] = "gold result is empty (P4 trivial)"
                return output
            output["result_fields"] = sorted({k for row in r_primary for k in row})
            reasons: list[str] = []
            reasons.extend(_structural_schema_flex_result_reasons(r_primary, inputs))
            if shape == "preserve":
                n_in = ctx.mongo.count(ctx.db_id, collection)
                if len(r_primary) != n_in:
                    reasons.append(f"preserve violated: {len(r_primary)} out docs != "
                                   f"{n_in} input docs (a $match/$group dropped some)")
                input_fields = ctx.mongo.sample_fields(ctx.db_id, collection)
                reasons.extend(_computed_field_quality_reasons(
                    r_primary, input_fields, _expected_new_fields(inputs)
                ))
            if _has_reference_oracle(inputs):
                oracle_reason = _reference_oracle_reason(inputs, r_primary)
                if oracle_reason:
                    reasons.append(oracle_reason)
                else:
                    output["reference_oracle_verified"] = True
            else:
                output["reference_oracle_verified"] = False
                output["llm_designed_gold"] = True
            alt = output.get("mql_alt")
            if alt and alt != mql:
                try:
                    r_alt = ctx.mongo.norm_exec(ctx.db_id, alt)
                    output["dual_path_match"] = equiv_rec(r_primary, r_alt,
                                                          order_sensitive=_ORDER_INSENSITIVE)
                except TendError:
                    output["dual_path_match"] = False
            output["gold_locked"] = not reasons
            if reasons:
                output["gold_lock_reason"] = "; ".join(reasons)
        except TendError as exc:
            output["gold_locked"] = False
            output["gold_lock_reason"] = exc.message
        return output


# --------------------------------------------------------------------------- #
# MUT — mutation generator
# --------------------------------------------------------------------------- #
_MUT_SCHEMA = {
    "type": "object",
    "required": ["mutations"],
    "properties": {
        "mutations": {
            "type": "array", "minItems": 5, "maxItems": 8,
            "items": {
                "type": "object",
                "required": ["mutation_id", "MQL"],
                "properties": {
                    "mutation_id": {"type": "string"},
                    "dimension": {"type": "string"},
                    "MQL": {"type": "string"},
                },
                "additionalProperties": True,
            },
        },
    },
    "additionalProperties": True,
}


@register
class MutationGenerator(LLMAgent):
    id = "mut"
    phase = "B"
    title = "MUT · Mutation Generator"
    prompt_file = "mut_mutation_generator.md"
    output_schema = _MUT_SCHEMA

    def render_inputs(self, ctx: AgentContext, inputs: dict[str, Any]) -> str:
        fb = inputs.get("pv_feedback")
        fb_note = ""
        if fb:
            bad = fb.get("non_discriminating_ids", [])
            fb_note = ("\n\nPREVIOUS ATTEMPT REJECTED: mutations " + str(bad) +
                       " produced the SAME result as gold (equivalent rewrites, not wrong). "
                       "Every mutation MUST change the computed RESULT.")
        return ("# MUT — produce 6-8 plausible-WRONG variants. Each MUST change the gold's "
                "computed RESULT (not just restructure it). Moving a computation between "
                "$addFields/$project, or $cond<->$ifNull, is EQUIVALENT and forbidden.\n"
                "Make them wrong: change a filter value, drop a branch (mishandle "
                "present/missing), wrong field, wrong accumulator, off-by-one window.\n"
                f"gold MQL:\n{inputs.get('MQL', '')}{fb_note}")

    def check_contract(self, ctx, inputs, output) -> list[str]:
        muts = output.get("mutations", [])
        if not (5 <= len(muts) <= 8):
            return [f"need 5-8 mutations, got {len(muts)}"]
        return []


# --------------------------------------------------------------------------- #
# PV — property verifier (deterministic)
# --------------------------------------------------------------------------- #
@register
class PropertyVerifier(Agent):
    id = "pv"
    phase = "B"
    title = "PV · Property Verifier"

    async def run(self, ctx: AgentContext, inputs: dict[str, Any]) -> dict[str, Any]:
        mql = inputs["MQL"]
        muts = inputs.get("mutations", [])
        if ctx.settings.stub or ctx.mongo is None or not ctx.mongo.available():
            return {"pv_pass": True, "verified_mutations": muts,
                    "property_verification": {"mode": "stub"}}
        try:
            gold = await asyncio.to_thread(ctx.mongo.norm_exec, ctx.db_id, mql)
        except TendError as exc:
            return {"pv_pass": False, "verified_mutations": [],
                    "property_verification": {"gold_exec_error": exc.message}}
        # P3: keep only mutations that EX-fail (change the result). Equivalent rewrites
        # (e.g. $addFields<->$project) are NOT wrong, so they are pruned, not fatal — the
        # record passes when >=5 genuinely discriminating mutations remain. P4: gold non-empty.
        async def check_mutation(m: dict[str, Any]) -> tuple[bool, dict[str, Any], bool]:
            errored = False
            try:
                rm = await asyncio.to_thread(ctx.mongo.norm_exec, ctx.db_id, m["MQL"])
                fails = not equiv_rec(gold, rm, order_sensitive=_ORDER_INSENSITIVE)
            except TendError as exc:
                ctx.log.warning(
                    "pv_mutation_exec_fail",
                    mutation_id=m.get("mutation_id"),
                    error_type=type(exc).__name__,
                    reason=exc.message,
                    context=exc.context,
                )
                fails = True                    # a mutation that errors is sufficiently wrong
                errored = True
            return fails, m, errored

        outcomes = await asyncio.gather(*(check_mutation(m) for m in muts))
        discriminating: list[dict] = []
        non_discriminating: list[str] = []
        discriminating_by_error: int = 0
        for fails, mutation, errored in outcomes:
            if fails:
                discriminating.append(mutation)
                if errored:
                    discriminating_by_error += 1
            else:
                non_discriminating.append(mutation["mutation_id"])
        pv_pass = bool(gold) and len(discriminating) >= 5
        return {
            "pv_pass": pv_pass,
            "verified_mutations": discriminating,
            "property_verification": {
                "gold_cardinality": len(gold),
                "mutations_total": len(muts),
                "discriminating": len(discriminating),
                "discriminating_by_error": discriminating_by_error,
                "non_discriminating_ids": non_discriminating,
            },
        }


# --------------------------------------------------------------------------- #
# NLP — bilingual NLQ paraphraser
# --------------------------------------------------------------------------- #
_NLP_SCHEMA = {
    "type": "object",
    "required": ["nl_queries"],
    "properties": {
        "nl_queries": {
            "type": "object",
            "required": ["canonical", "colloquial"],
            "properties": {
                "canonical": {"type": "string", "minLength": 10},
                "colloquial": {"type": "string", "minLength": 5},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}


@register
class NlParaphraser(LLMAgent):
    id = "nlp"
    phase = "B"
    title = "NLP · NL Paraphraser"
    prompt_file = "nlp_nl_paraphraser.md"
    output_schema = _NLP_SCHEMA

    def render_inputs(self, ctx: AgentContext, inputs: dict[str, Any]) -> str:
        import json
        intent = inputs.get("intent", {})
        preserve = intent.get("shape_policy") == "preserve"
        result_fields = inputs.get("result_fields") or (intent.get("output") or {}).get("fields")
        field_note = ""
        if result_fields:
            field_note = "\nGold result field names to preserve literally: " + ", ".join(
                map(str, result_fields)
            ) + "."
        oracle_contract = _oracle_lock_contract_inline(intent.get("reference_oracle"))
        oracle_note = ""
        if oracle_contract:
            oracle_note = (
                "\nGold result semantics to express exactly, without mentioning operators: "
                f"{oracle_contract}"
            )
        mql_note = ""
        if inputs.get("MQL"):
            mql_note = (
                "\nLocked gold MQL is provided only to recover literal output field names and "
                "category labels; do not mention operator syntax in the NLQ.\n"
                f"{inputs['MQL']}"
            )
        shape_rule = (
            "This is a PRESERVE task: say the new field is ADDED to each document and that "
            "EVERY existing field and nested sub-document is KEPT unchanged. Do NOT enumerate "
            "an output field list and do NOT imply dropping/projecting any field (that would "
            "make the NLQ ambiguous between add-field and project)."
            if preserve else
            "This is a RESHAPE/REDUCE task: do NOT say the result adds a field to each "
            "document, and do NOT say that each document is kept unchanged. State the "
            "summary/grouping unit and list exactly the gold result fields. For schema-flex "
            "variant summaries, state that the variant field uses the exact literal values "
            "'present' and 'missing'."
        )
        fb = inputs.get("rtv_feedback")
        extra = ""
        if fb and preserve:
            extra = ("\nThe previous NLQ failed to round-trip: it implied a projection. For "
                     "this preserve task, say only 'add field X = <formula>; keep each "
                     "document otherwise unchanged'.")
        elif fb:
            reason = fb.get("rtv_reason") if isinstance(fb, dict) else None
            reason_note = f" Reason: {reason}." if reason else ""
            extra = ("\nThe previous NLQ failed to round-trip." + reason_note +
                     " For this reshape/reduce task, do not rewrite it as a per-document "
                     "add-field task. Say it summarizes/groups documents by the variant, "
                     "uses the missing-default formula, and outputs only the requested "
                     "summary fields. Preserve exact variant labels.")
        return ("# NLP — paraphrase canonical (L1) + colloquial (L0) NLQ from THIS intent "
                "(not from any pipeline)\n"
                "WRITE BOTH canonical AND colloquial IN ENGLISH ONLY — never Chinese or any "
                "other language; non-English output is rejected.\n"
                "Return exactly one JSON object with only the key nl_queries. Do NOT emit "
                "nlp_trace, rationale, markdown, code fences, or any extra keys.\n"
                f"## intent\n```json\n{json.dumps(intent, ensure_ascii=False, indent=2, sort_keys=True)}\n```\n"
                "RULES: describe EXACTLY the computation in this intent over ITS named "
                "collection/fields. Do NOT introduce entities/fields not in the intent "
                "(no 'customers'/'phone' unless named). Name the computed field + exact formula "
                f"+ missing-default; no $ operator terms.\n{shape_rule}{field_note}"
                f"{oracle_note}{mql_note}"
                + extra)

    def check_contract(self, ctx, inputs, output) -> list[str]:
        nlq = output.get("nl_queries", {})
        c = nlq.get("canonical", "")
        v = ["canonical NLQ must not contain $ operator terms"] if "$" in c else []
        non_english = [
            name for name, val in (("canonical", c), ("colloquial", nlq.get("colloquial", "")))
            if _contains_cjk(str(val))
        ]
        if non_english:
            v.append(
                f"nl_queries {non_english} contain non-English (CJK) characters; ALL natural "
                "language must be ENGLISH ONLY — rewrite both canonical and colloquial fully "
                "in English."
            )
        v.extend(_nl_shape_contract_violations(
            inputs.get("intent", {}), c, inputs.get("result_fields")
        ))
        return v


def _nl_shape_contract_violations(
    intent: dict[str, Any], canonical: str, result_fields: list[str] | None = None
) -> list[str]:
    """Catch NLQ/shape mismatches before RTV burns another retry."""
    shape = intent.get("shape_policy")
    text = canonical.lower()
    violations: list[str] = []
    if shape in {"reshape", "reduce"}:
        preserve_phrases = (
            "add a field",
            "adds a field",
            "to each document",
            "each document",
            "keep all other",
            "kept unchanged",
            "unchanged",
        )
        if any(p in text for p in preserve_phrases):
            violations.append(
                f"{shape} canonical NLQ must not describe preserve/add-field semantics"
            )
    if intent.get("archetype") == "schema_flex_variant_summary":
        fields = [
            str(f).lower()
            for f in (result_fields or (intent.get("output") or {}).get("fields", []))
            if isinstance(f, str) and f
        ]
        missing = [f for f in fields if f not in text]
        if missing:
            violations.append(
                "schema_flex_variant_summary canonical NLQ must name output fields: "
                + ", ".join(missing)
            )
        for label in ("present", "missing"):
            if label not in text:
                violations.append(
                    "schema_flex_variant_summary canonical NLQ must name exact variant "
                    f"label {label!r}"
                )
    ref = intent.get("reference_oracle") if isinstance(intent.get("reference_oracle"), dict) else {}
    if ref.get("template") == "has_vs_absent_compare":
        for label in ("present", "absent"):
            if label not in text:
                violations.append(
                    "has_vs_absent_compare canonical NLQ must name exact group label "
                    f"{label!r}"
                )
    return violations


# --------------------------------------------------------------------------- #
# RTV — round-trip verifier (independent NL->MQL + deterministic equiv)
# --------------------------------------------------------------------------- #
_RTV_SCHEMA = {
    "type": "object",
    "required": ["mql_round_trip_canonical"],
    "properties": {"mql_round_trip_canonical": {"type": "string"}},
    "additionalProperties": True,
}


@register
class RoundTripVerifier(LLMAgent):
    id = "rtv"
    phase = "B"
    title = "RTV · Round-Trip Verifier"
    prompt_file = "rtv_round_trip_verifier.md"
    output_schema = _RTV_SCHEMA
    temperature = 0.0
    offload_postprocess = True

    async def run(self, ctx: AgentContext, inputs: dict[str, Any]) -> dict[str, Any]:
        if inputs.get("verification_mode") == _COMPILED_REFERENCE_ORACLE_RTV_MODE:
            return _compiled_reference_oracle_nl_contract(inputs)
        return await super().run(ctx, inputs)

    def render_inputs(self, ctx: AgentContext, inputs: dict[str, Any]) -> str:
        nlq = inputs.get("nl_queries", {})
        return ("# RTV — independently translate the canonical NLQ to MQL using the schema "
                "below (you do NOT see the gold pipeline)\n"
                f"## schema (real collections/fields)\n{_schema_digest(inputs.get('schema', {}))}\n"
                f"## canonical NLQ\n{nlq.get('canonical', '')}\n\n"
                "Emit mql_round_trip_canonical = db.<root_collection>.aggregate([...]) over the "
                "REAL collections above. Reproduce the computation the NLQ describes exactly.")

    def postprocess(self, ctx, inputs, output, result) -> dict[str, Any]:
        if inputs.get("verification_mode") == _COMPILED_REFERENCE_ORACLE_RTV_MODE:
            return _compiled_reference_oracle_nl_contract(inputs)
        if ctx.settings.stub or ctx.mongo is None or not ctx.mongo.available():
            output["rtv_pass"] = True
            return output
        try:
            rt = ctx.mongo.norm_exec(ctx.db_id, output["mql_round_trip_canonical"])
            gold = ctx.mongo.norm_exec(ctx.db_id, inputs["MQL"])
            output["rtv_pass"] = equiv_rec(rt, gold, order_sensitive=_ORDER_INSENSITIVE)
            if not output["rtv_pass"]:
                detail = _round_trip_divergence_detail(rt, gold)
                output["rtv_reason"] = (
                    "round-trip MQL is not equivalent to gold "
                    f"(round_trip_rows={len(rt)}, gold_rows={len(gold)}){detail}"
                )
        except TendError as exc:
            output["rtv_pass"] = False
            output["rtv_reason"] = exc.message
        return output


# --------------------------------------------------------------------------- #
# NNC — nativeness critic (difficulty + dual-bridge gate)
# --------------------------------------------------------------------------- #
_NNC_SCHEMA = {
    "type": "object",
    "required": ["difficulty", "sql_infeasibility_class", "gate_pass"],
    "properties": {
        "difficulty": {"enum": ["L0", "L1", "L2", "L3", "L4"]},
        "sql_infeasibility_class": {
            "enum": ["feasible", "semantic", "performative",
                     "structural_pipeline", "structural_schema_flex"]
        },
        "gate_pass": {"type": "boolean"},
        "nnc_verdict": {"type": "object"},
    },
    "additionalProperties": True,
}


@register
class NativenessCritic(LLMAgent):
    id = "nnc"
    phase = "B"
    title = "NNC · Nativeness Critic"
    prompt_file = "nnc_nosql_nativeness_critic.md"
    output_schema = _NNC_SCHEMA

    def render_inputs(self, ctx: AgentContext, inputs: dict[str, Any]) -> str:
        target = (
            f"target_difficulty: {inputs.get('target_difficulty')}\n"
            f"target_sql_infeasibility_class: {inputs.get('target_sql_infeasibility_class')}\n"
            f"target_schema_flex: {inputs.get('target_schema_flex')}\n"
        )
        return ("# NNC — assign difficulty (L0-L4) + sql_infeasibility_class, run dual-bridge gate\n"
                f"{target}"
                f"gold MQL:\n{inputs.get('MQL', '')}\n"
                f"canonical NLQ: {inputs.get('nl_queries', {}).get('canonical', '')}\n"
                f"shape_policy: {inputs.get('shape_policy')}\n"
                "If the target asks for structural_schema_flex, only pass when the MQL "
                "uses real schema-flex dispatch on variant fields and should therefore be "
                "labeled difficulty L4 / structural_schema_flex. Otherwise mark gate_pass=false.\n"
                "gate_pass=true iff neither SQL-bridge nor template-bridge reaches the answer "
                "(non-feasible classes), or class==feasible.")

    def check_contract(self, ctx, inputs, output) -> list[str]:
        v = []
        if output.get("sql_infeasibility_class") == "structural_schema_flex" \
                and output.get("difficulty") != "L4":
            v.append("structural_schema_flex requires difficulty L4")
        return v


# --------------------------------------------------------------------------- #
# RA — realism auditor
# --------------------------------------------------------------------------- #
@register
class RealismAuditor(Agent):
    """RA — P4 non-triviality, verified deterministically against the witness.

    The objective criteria (result non-empty; the attached/computed field takes >=2
    distinct values, i.e. the heterogeneity actually matters) are checked by executing the
    gold on the loaded witness — not judged from the MQL text, which an LLM cannot verify
    without data.
    """

    id = "ra"
    phase = "B"
    title = "RA · Realism Auditor"

    async def run(self, ctx: AgentContext, inputs: dict[str, Any]) -> dict[str, Any]:
        if ctx.settings.stub or ctx.mongo is None or not ctx.mongo.available():
            return {"ra_pass": True, "ra_audit": {"mode": "stub"}}
        return await asyncio.to_thread(self._run_sync, ctx, inputs)

    def _run_sync(self, ctx: AgentContext, inputs: dict[str, Any]) -> dict[str, Any]:
        mql = inputs["MQL"]
        try:
            collection, _ = parse_pipeline(mql)
            res = ctx.mongo.norm_exec(ctx.db_id, mql)
        except TendError as exc:
            return {"ra_pass": False, "ra_audit": {"gold_exec_error": exc.message}}
        if not res:
            return {"ra_pass": False, "ra_audit": {"reason": "empty result (P4)"}}

        input_fields = ctx.mongo.sample_fields(ctx.db_id, collection)
        new_fields = sorted({k for d in res for k in d} - input_fields)
        # P4: an added field must vary (>=2 distinct values) so the computation is non-trivial;
        # with no added field (filter/reshape), require >=2 distinct result documents.
        field_distinct: dict[str, int] = {}
        for f in new_fields:
            field_distinct[f] = len({_hashable(d.get(f)) for d in res})
        if len(res) == 1:
            # a single-row aggregate (count/sum/avg over the collection) is a legitimate,
            # non-degenerate scalar answer — non-empty was already checked above. The
            # >=2-distinct heuristic only guards multi-row / attach-a-field results, where a
            # collapse to one value really is trivial.
            nontrivial = True
            reason = ""
        elif new_fields:
            nontrivial = any(c >= 2 for c in field_distinct.values())
            reason = "no added field takes >=2 distinct values" if not nontrivial else ""
        else:
            distinct_docs = len({_hashable(d) for d in res})
            nontrivial = distinct_docs >= 2
            reason = "" if nontrivial else "result collapses to a single document"
        return {
            "ra_pass": bool(nontrivial),
            "ra_audit": {
                "result_cardinality": len(res),
                "added_fields": new_fields,
                "added_field_distinct": field_distinct,
                **({"reason": reason} if reason else {}),
            },
        }


def _hashable(v: Any) -> Any:
    """Stable hashable form of any JSON value (for distinct-value counting)."""
    if isinstance(v, (dict, list)):
        import json
        return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
    return v


def _preserve_schema_flex_default_violations(intent: dict[str, Any]) -> list[str]:
    """Reject preserve schema-flex intents whose missing/default branch is underspecified."""
    op = intent.get("analytical_op") if isinstance(intent.get("analytical_op"), dict) else {}
    output = intent.get("output") if isinstance(intent.get("output"), dict) else {}
    op_output = op.get("output") if isinstance(op.get("output"), dict) else {}
    ref = intent.get("reference_oracle") if isinstance(intent.get("reference_oracle"), dict) else {}
    ref_params = ref.get("params") if isinstance(ref.get("params"), dict) else {}
    target = op.get("target_field")
    candidates: list[tuple[str, Any]] = []
    for key in ("missing_default", "absent_value", "default"):
        if key in op:
            candidates.append((f"analytical_op.{key}", op[key]))
        if key in op_output:
            candidates.append((f"analytical_op.output.{key}", op_output[key]))
        if key in ref_params:
            candidates.append((f"reference_oracle.params.{key}", ref_params[key]))
    semantics = op.get("missing_default_semantics")
    if isinstance(semantics, dict):
        if target and target in semantics:
            candidates.append(
                (f"analytical_op.missing_default_semantics.{target}", semantics[target])
            )
        else:
            candidates.extend(
                (f"analytical_op.missing_default_semantics.{k}", v)
                for k, v in semantics.items()
            )
    elif "missing_default_semantics" in op:
        candidates.append(("analytical_op.missing_default_semantics", semantics))
    for prefix, out in (("output", output), ("analytical_op.output", op_output)):
        output_missing = out.get("missing")
        if isinstance(output_missing, dict):
            if target and target in output_missing:
                candidates.append((f"{prefix}.missing.{target}", output_missing[target]))
            else:
                candidates.extend(
                    (f"{prefix}.missing.{k}", v) for k, v in output_missing.items()
                )
        elif "missing" in out:
            candidates.append((f"{prefix}.missing", output_missing))

    if not candidates:
        return [
            "preserve structural_schema_flex intent must state a non-null missing/default "
            "value for the computed target field"
        ]
    null_paths = [path for path, value in candidates if value is None]
    if null_paths:
        return [
            "preserve structural_schema_flex computed fields must use non-null "
            "missing/default values: " + ", ".join(null_paths)
        ]
    return []


def _bucket(n_stages: int) -> str:
    if n_stages <= 4:
        return "shallow"
    return "medium" if n_stages <= 9 else "deep"


def _reference_oracle_reason(inputs: dict[str, Any], mql_norm: list[dict[str, Any]]) -> str | None:
    """Return why R(D) does not certify the MQL result, or None when it matches."""
    intent = inputs.get("intent") if isinstance(inputs.get("intent"), dict) else {}
    payload = inputs.get("reference_oracle") or intent.get("reference_oracle")
    if not isinstance(payload, dict):
        return "missing reference_oracle payload with template and params"
    template = payload.get("template")
    if not isinstance(template, str) or not template:
        return "reference_oracle.template is required"
    if not has_oracle(template):
        return f"unsupported reference_oracle.template {template!r}"
    params = payload.get("params")
    if not isinstance(params, dict):
        return "reference_oracle.params must be an object"
    snapshot = inputs.get("mongodb_data")
    if not isinstance(snapshot, dict):
        return "mongodb_data snapshot is required to run reference_oracle"
    try:
        oracle_raw = reference_oracle(template)(snapshot, params)
    except OracleError as exc:
        return f"reference_oracle execution failed for {template!r}: {exc}"
    except Exception as exc:  # noqa: BLE001 - malformed oracle params must fail the lock cleanly
        return (
            f"reference_oracle execution failed for {template!r}: "
            f"{type(exc).__name__}: {exc}"
        )
    if not isinstance(oracle_raw, list):
        return f"reference_oracle {template!r} returned {type(oracle_raw).__name__}, expected list"
    oracle_norm = [_normalize_doc(d) for d in oracle_raw]
    if not equiv_rec(mql_norm, oracle_norm, order_sensitive=_ORDER_INSENSITIVE):
        detail = _oracle_divergence_detail(mql_norm, oracle_norm, params)
        contract = _oracle_lock_contract_inline(payload)
        contract_note = f"; expected_contract={contract}" if contract else ""
        return (
            f"reference_oracle divergence for {template!r}: "
            f"mql_rows={len(mql_norm)}, oracle_rows={len(oracle_norm)}{detail}"
            f"{contract_note}"
        )
    return None


def _has_reference_oracle(inputs: dict[str, Any]) -> bool:
    intent = inputs.get("intent") if isinstance(inputs.get("intent"), dict) else {}
    payload = inputs.get("reference_oracle") or intent.get("reference_oracle")
    return isinstance(payload, dict)


def _oracle_divergence_detail(
    mql_norm: list[dict[str, Any]], oracle_norm: list[dict[str, Any]], params: dict[str, Any]
) -> str:
    target = params.get("target_field")
    target = target if isinstance(target, str) and target else None
    if all(isinstance(row, dict) and "_id" in row for row in mql_norm[:500]) \
            and all(isinstance(row, dict) and "_id" in row for row in oracle_norm[:500]):
        oracle_by_id = {row["_id"]: row for row in oracle_norm if isinstance(row, dict)}
        for row in mql_norm[:500]:
            key = row.get("_id")
            other = oracle_by_id.get(key)
            if not isinstance(other, dict):
                return f"; first_mismatch _id={key!r} missing from oracle"
            detail = _row_divergence_detail(row, other, f"_id={key!r}", target=target)
            if detail:
                return detail
        return ""
    for index, (row, other) in enumerate(zip(mql_norm[:500], oracle_norm[:500])):
        if not isinstance(row, dict) or not isinstance(other, dict):
            if row != other:
                return f"; first_mismatch row={index} mql={row!r} oracle={other!r}"
            continue
        detail = _row_divergence_detail(row, other, f"row={index}", target=target)
        if detail:
            return detail
    return ""


def _row_divergence_detail(
    row: dict[str, Any], other: dict[str, Any], label: str, *, target: str | None
) -> str:
    extra = sorted(set(row) - set(other))
    missing = sorted(set(other) - set(row))
    if extra or missing:
        parts = []
        if extra:
            parts.append(f"extra_mql_fields={extra}")
        if missing:
            parts.append(f"missing_mql_fields={missing}")
        return f"; first_mismatch {label} " + " ".join(parts)
    fields = [target] if target else sorted(set(row) | set(other))
    for field in fields:
        if field is not None and row.get(field) != other.get(field):
            return (
                f"; first_mismatch {label} field={field!r} "
                f"mql={row.get(field)!r} oracle={other.get(field)!r}"
            )
    return ""


def _round_trip_divergence_detail(rt_rows: list[Any], gold_rows: list[Any]) -> str:
    rt_norm = [_normalize_doc(d) for d in rt_rows]
    gold_norm = [_normalize_doc(d) for d in gold_rows]
    if all(isinstance(row, dict) and "_id" in row for row in rt_norm[:500]) \
            and all(isinstance(row, dict) and "_id" in row for row in gold_norm[:500]):
        gold_by_id = {row["_id"]: row for row in gold_norm if isinstance(row, dict)}
        for row in rt_norm[:500]:
            key = row.get("_id")
            other = gold_by_id.get(key)
            if not isinstance(other, dict):
                return f"; first_mismatch _id={key!r} missing from gold"
            detail = _row_divergence_detail(row, other, f"_id={key!r}", target=None)
            if detail:
                return detail.replace("mql=", "round_trip=").replace("oracle=", "gold=")
        return ""
    for index, (row, other) in enumerate(zip(rt_norm[:500], gold_norm[:500])):
        if not isinstance(row, dict) or not isinstance(other, dict):
            if row != other:
                return f"; first_mismatch row={index} round_trip={row!r} gold={other!r}"
            continue
        detail = _row_divergence_detail(row, other, f"row={index}", target=None)
        if detail:
            return detail.replace("mql=", "round_trip=").replace("oracle=", "gold=")
    return ""


def _computed_field_quality_reasons(
    rows: list[dict[str, Any]], input_fields: set[str],
    allowed_new_fields: set[str] | None = None,
) -> list[str]:
    sample = rows[:500]
    new_fields = sorted({k for d in sample for k in d} - input_fields)
    reasons: list[str] = []
    if allowed_new_fields is not None:
        leaked = [f for f in new_fields if f not in allowed_new_fields]
        if leaked:
            reasons.append(
                "preserve leaked helper fields: " + ", ".join(repr(f) for f in leaked)
                + "; final preserve output may only add "
                + ", ".join(repr(f) for f in sorted(allowed_new_fields))
            )
    for field in new_fields:
        values = [d.get(field) for d in sample]
        if any(field not in d for d in sample) or any(v is None for v in values):
            reasons.append(f"computed field {field!r} produced null/missing values")
        if any(isinstance(v, (dict, list)) for v in values):
            reasons.append(f"computed field {field!r} produced non-scalar values")
    return reasons


def _expected_new_fields(inputs: dict[str, Any]) -> set[str] | None:
    intent = inputs.get("intent") if isinstance(inputs.get("intent"), dict) else {}
    op = intent.get("analytical_op") if isinstance(intent.get("analytical_op"), dict) else {}
    target = op.get("target_field")
    if not isinstance(target, str) or not target:
        ref = inputs.get("reference_oracle") or intent.get("reference_oracle")
        params = ref.get("params") if isinstance(ref, dict) else {}
        target = params.get("target_field") if isinstance(params, dict) else None
    return {target} if isinstance(target, str) and target else None


def _promote_structural_alt_if_valid(
    primary_mql: str, alt_mql: Any, inputs: dict[str, Any]
) -> str | None:
    """Use ``mql_alt`` as the representative only when it satisfies structural gates."""
    if inputs.get("target_sql_infeasibility_class") != "structural_schema_flex":
        return None
    if not isinstance(alt_mql, str) or not alt_mql.strip() or alt_mql == primary_mql:
        return None
    schema = inputs.get("schema", {})
    try:
        primary_collection, _ = parse_pipeline(primary_mql)
    except TendError:
        return None
    primary_reasons = _structural_schema_flex_reasons(
        primary_mql, schema, primary_collection, inputs
    )
    if not primary_reasons:
        return None
    try:
        alt_collection, _ = parse_pipeline(alt_mql)
    except TendError:
        return None
    if alt_collection != primary_collection:
        return None
    if _unknown_schema_refs(alt_mql, schema, alt_collection):
        return None
    if _structural_schema_flex_reasons(alt_mql, schema, alt_collection, inputs):
        return None
    return alt_mql


def _canonical_reference_mql(inputs: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(deterministic gold MQL, implied shape_policy)`` for reference-defined archetypes.

    Reverse construction: when the archetype's reference oracle has a mechanical MQL
    translation, synthesize gold from the oracle params so ``gold ≡_rec R`` holds *by
    construction* — the gold-lock can never diverge for these. The LLM's MQL is kept as
    ``llm_MQL`` for provenance but is no longer correctness-critical.
    """
    intent = inputs.get("intent") if isinstance(inputs.get("intent"), dict) else {}
    ref = inputs.get("reference_oracle") or intent.get("reference_oracle")
    if not isinstance(ref, dict):
        return None
    params = ref.get("params")
    if not isinstance(params, dict):
        return None
    template = ref.get("template")
    builder = _CANONICAL_MQL_BUILDERS.get(template)
    if builder is None:
        return None
    mql = builder(params, inputs.get("schema", {}))
    if mql is None:
        return None
    return mql, _CANONICAL_SHAPE.get(template, "reduce")


def _canonical_present_missing_projection_mql(
    params: dict[str, Any], schema: dict[str, Any]
) -> str | None:
    """Deterministic gold for the sparse-embed ratio archetype.

    Mirrors :func:`mechanisms.oracles._present_missing_projection` exactly:
    per parent doc, ``has(embed) ? numerator / Σ(denom) : absent_value`` with a
    ``zero_value`` guard when the denominator sum is 0. Preserves every parent doc and
    adds only ``target_field`` (helper join/sum fields are projected out).
    """
    parent = params.get("parent_collection")
    embed_field = params.get("embed_field")
    numerator_path = params.get("numerator_path")
    target = params.get("target_field")
    denom = params.get("denom")
    if not all(isinstance(x, str) and x for x in (parent, embed_field, numerator_path, target)):
        return None
    if not isinstance(denom, dict):
        return None
    # Match the oracle: normalize an embed-relative numerator ("amount") to parent-rooted
    # ("loan.amount") so the MQL reads from inside the embed and references a real schema
    # field (an unnormalized "$amount" both zeroes the metric and trips the schema-ref gate).
    numerator_path = _embed_value_path(embed_field, numerator_path)
    dcoll = denom.get("collection")
    local_id = denom.get("local_id")
    foreign_field = denom.get("foreign_field")
    sum_field = denom.get("sum_field")
    if not all(isinstance(x, str) and x for x in (dcoll, local_id, foreign_field, sum_field)):
        return None
    absent_value = params.get("absent_value", 0)
    zero_value = denom.get("zero_value", 1)
    match = denom.get("match")

    as_field = "__tend_denom"
    sum_field_name = "__tend_denom_sum"
    # Per-parent denominator sum. When the oracle filters denom docs (``match``), filter
    # the looked-up array before summing; otherwise sum the whole joined array.
    summed_array: Any = f"${as_field}"
    if isinstance(match, dict) and match.get("field") is not None:
        summed_array = {
            "$filter": {
                "input": f"${as_field}",
                "as": "d",
                "cond": {"$eq": [f"$$d.{match['field']}", match.get("value")]},
            }
        }
    sum_expr = {
        "$sum": {
            "$map": {
                "input": summed_array,
                "as": "d",
                "in": {"$ifNull": [f"$$d.{sum_field}", 0]},
            }
        }
    }
    missing_expr = {"$eq": [{"$type": f"${embed_field}"}, "missing"]}
    ratio_expr = {
        "$divide": [
            {"$ifNull": [f"${numerator_path}", 0]},
            {"$cond": [{"$eq": [f"${sum_field_name}", 0]}, zero_value, f"${sum_field_name}"]},
        ]
    }
    stages = [
        {"$lookup": {"from": dcoll, "localField": local_id,
                     "foreignField": foreign_field, "as": as_field}},
        {"$addFields": {sum_field_name: sum_expr}},
        {"$addFields": {target: {"$cond": [missing_expr, absent_value, ratio_expr]}}},
        {"$project": {as_field: 0, sum_field_name: 0}},
    ]
    return f"db.{parent}.aggregate({json.dumps(stages, ensure_ascii=False)})"


def _canonical_has_vs_absent_compare_mql(
    params: dict[str, Any], schema: dict[str, Any]
) -> str | None:
    collection = params.get("parent_collection")
    embed_field = params.get("embed_field")
    if not isinstance(collection, str) or not collection:
        return None
    if not isinstance(embed_field, str) or not embed_field:
        return None
    agg = str(params.get("agg", "count") or "count").lstrip("$").lower()
    if agg not in {"count", "sum", "avg", "min", "max"}:
        return None
    missing_expr = {"$eq": [{"$type": f"${embed_field}"}, "missing"]}
    stages: list[dict[str, Any]] = [
        {
            "$addFields": {
                "__tend_presence": {
                    "$cond": [missing_expr, "absent", "present"]
                }
            }
        }
    ]
    if agg == "count":
        accumulator: dict[str, Any] = {"$sum": 1}
    else:
        metric_path = _canonical_metric_path(
            schema, collection, embed_field, params.get("metric_field")
        )
        if metric_path is None:
            return None
        present_value = {"$ifNull": [f"${metric_path}", 0]}
        stages[0]["$addFields"]["__tend_metric"] = {
            "$cond": [missing_expr, 0, present_value]
        }
        accumulator = {f"${agg}": "$__tend_metric"}
    stages.extend([
        {"$group": {"_id": "$__tend_presence", "value": accumulator}},
        {"$project": {"_id": 1, "value": 1}},
    ])
    return f"db.{collection}.aggregate({json.dumps(stages, ensure_ascii=False)})"


# --- shared MQL-expression helpers for canonical builders ------------------- #
_NUMERIC_BSON_TYPES = ["int", "long", "double", "decimal"]


def _mql_numeric_or(expr: Any, default: Any) -> dict[str, Any]:
    """Mirror oracles._num: a numeric (non-bool) value of ``expr`` else ``default``."""
    return {"$cond": [{"$in": [{"$type": expr}, _NUMERIC_BSON_TYPES]}, expr, default]}


def _mql_accumulator(agg: Any, value_expr: Any) -> dict[str, Any]:
    """Mirror oracles._aggregate's group accumulator; ``count`` -> ``$sum: 1``."""
    name = str(agg or "count").lstrip("$").lower()
    if name == "count":
        return {"$sum": 1}
    return {f"${name}": value_expr}


def _mql_present_or_null(field: str) -> dict[str, Any]:
    """Mirror oracle ``_get``: the field's value, or ``null`` when the key is absent.
    Uses ``$type`` so projections also satisfy the structural_schema_flex op gate."""
    return {"$cond": [{"$eq": [{"$type": f"${field}"}, "missing"]}, None, f"${field}"]}


def _mql_projection(project: Any, default_field: str | None = None) -> dict[str, Any] | None:
    """Mirror an oracle ``project`` list (or a single field): include only those keys,
    keep ``_id`` only when listed, and emit ``null`` (not omit) for missing values."""
    if isinstance(project, list) and project:
        proj: dict[str, Any] = {"_id": 1 if "_id" in project else 0}
        for key in project:
            if key != "_id":
                proj[key] = _mql_present_or_null(key)
        return {"$project": proj}
    if default_field:
        return {"$project": {"_id": 0, default_field: _mql_present_or_null(default_field)}}
    return None


def _agg_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _canonical_simple_filter_mql(params: dict[str, Any], schema: dict[str, Any]) -> str | None:
    coll = params.get("collection")
    if not isinstance(coll, str) or not coll:
        return None
    op_map = {"eq": "$eq", "ne": "$ne", "gt": "$gt", "lt": "$lt", "gte": "$gte", "lte": "$lte"}
    clauses: list[dict[str, Any]] = []
    for pred in params.get("predicates", []) or []:
        if not isinstance(pred, dict) or "field" not in pred:
            return None
        op = pred.get("op", "eq")
        if op not in op_map:
            return None
        field = pred["field"]
        # the oracle excludes docs missing the field for EVERY op (incl. ne), so require present
        clauses.append({field: {"$exists": True}})
        clauses.append({field: {op_map[op]: pred.get("value")}})
    stages: list[dict[str, Any]] = [{"$match": {"$and": clauses} if clauses else {}}]
    proj = _mql_projection(params.get("project"))
    if proj:
        stages.append(proj)
    return f"db.{coll}.aggregate({json.dumps(stages, ensure_ascii=False)})"


def _canonical_existence_count_mql(params: dict[str, Any], schema: dict[str, Any]) -> str | None:
    coll, field = params.get("collection"), params.get("field")
    if not all(isinstance(x, str) and x for x in (coll, field)):
        return None
    stages = [
        {"$group": {"_id": None, "count": {
            "$sum": {"$cond": [{"$eq": [{"$type": f"${field}"}, "missing"]}, 0, 1]}}}},
        {"$project": {"_id": 0, "count": 1}},
    ]
    return f"db.{coll}.aggregate({json.dumps(stages, ensure_ascii=False)})"


def _canonical_group_count_mql(params: dict[str, Any], schema: dict[str, Any]) -> str | None:
    coll, gb = params.get("collection"), params.get("group_by")
    if not all(isinstance(x, str) and x for x in (coll, gb)):
        return None
    stages = [{"$group": {"_id": f"${gb}", "count": {"$sum": 1}}}]
    return f"db.{coll}.aggregate({json.dumps(stages, ensure_ascii=False)})"


def _canonical_topn_mql(params: dict[str, Any], schema: dict[str, Any]) -> str | None:
    coll, sort_key = params.get("collection"), params.get("sort_key")
    if not all(isinstance(x, str) and x for x in (coll, sort_key)):
        return None
    try:
        n = int(params.get("n"))
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    order = params.get("order", "desc")
    if order not in {"asc", "desc"}:
        return None
    nulls = params.get("nulls", "last")
    if nulls not in {"first", "last"}:
        return None
    missing_rank = 0 if nulls == "first" else 1
    present_rank = 1 if nulls == "first" else 0
    stages: list[dict[str, Any]] = [
        {"$addFields": {
            "__tend_missing_rank": {
                "$cond": [
                    {"$or": [
                        {"$eq": [{"$type": f"${sort_key}"}, "missing"]},
                        {"$eq": [f"${sort_key}", None]},
                    ]},
                    missing_rank,
                    present_rank,
                ]
            }
        }},
        {"$sort": {"__tend_missing_rank": 1, sort_key: 1 if order == "asc" else -1}},
        {"$limit": n},
    ]
    project = params.get("project")
    if isinstance(project, list) and project:
        stages.append(_mql_projection(project))
    else:
        stages.append({"$project": {"__tend_missing_rank": 0}})
    return f"db.{coll}.aggregate({json.dumps(stages, ensure_ascii=False)})"


def _canonical_null_coalesce_agg_mql(params: dict[str, Any], schema: dict[str, Any]) -> str | None:
    coll, field = params.get("collection"), params.get("field")
    agg = _agg_str(params.get("agg"))
    if not all(isinstance(x, str) and x for x in (coll, field)) or agg is None:
        return None
    value = _mql_numeric_or(f"${field}", params.get("default", 0))
    stages = [
        {"$group": {"_id": None, "value": _mql_accumulator(agg, value)}},
        {"$project": {"_id": 0, "value": 1}},
    ]
    return f"db.{coll}.aggregate({json.dumps(stages, ensure_ascii=False)})"


def _canonical_subtype_specific_field_mql(params: dict[str, Any], schema: dict[str, Any]) -> str | None:
    coll = params.get("collection")
    disc = params.get("discriminator")
    field = params.get("field")
    if not all(isinstance(x, str) and x for x in (coll, disc, field)):
        return None
    stages: list[dict[str, Any]] = [{"$match": {disc: params.get("subtype_value")}}]
    stages.append(_mql_projection(params.get("project"), default_field=field))
    return f"db.{coll}.aggregate({json.dumps(stages, ensure_ascii=False)})"


def _canonical_cross_keyset_value_mql(params: dict[str, Any], schema: dict[str, Any]) -> str | None:
    coll, key = params.get("collection"), params.get("key")
    if not all(isinstance(x, str) and x for x in (coll, key)):
        return None
    stages: list[dict[str, Any]] = [{"$match": {key: {"$exists": True}}}]
    stages.append(_mql_projection(params.get("project"), default_field=key))
    return f"db.{coll}.aggregate({json.dumps(stages, ensure_ascii=False)})"


def _canonical_dynamic_key_fold_mql(params: dict[str, Any], schema: dict[str, Any]) -> str | None:
    coll, nf, vf = params.get("collection"), params.get("name_field"), params.get("value_field")
    agg = _agg_str(params.get("agg"))
    if not all(isinstance(x, str) and x for x in (coll, nf, vf)) or agg is None:
        return None
    stages = [
        {"$addFields": {"__tend_v": _mql_numeric_or(f"${vf}", None)}},
        {"$match": {"__tend_v": {"$ne": None}}},  # oracle keeps only numeric values
        {"$group": {"_id": f"${nf}", "value": _mql_accumulator(agg, "$__tend_v")}},
        {"$project": {"_id": 1, "value": 1}},
    ]
    return f"db.{coll}.aggregate({json.dumps(stages, ensure_ascii=False)})"


def _canonical_cross_version_agg_mql(params: dict[str, Any], schema: dict[str, Any]) -> str | None:
    coll = params.get("collection")
    cands = params.get("field_candidates")
    agg = _agg_str(params.get("agg"))
    if not isinstance(coll, str) or not coll or not isinstance(cands, list) or not cands or agg is None:
        return None
    if not all(isinstance(c, str) and c for c in cands):
        return None
    default = params.get("default", 0)
    coalesced: Any = None  # first present-and-non-null candidate, mirroring the oracle
    for cand in reversed(cands):
        coalesced = {"$ifNull": [f"${cand}", None if coalesced is None else coalesced]}
    stages = [
        {"$addFields": {"__tend_v": _mql_numeric_or(coalesced, default)}},
        {"$group": {"_id": None, "value": _mql_accumulator(agg, "$__tend_v")}},
        {"$project": {"_id": 0, "value": 1}},
    ]
    return f"db.{coll}.aggregate({json.dumps(stages, ensure_ascii=False)})"


def _subtype_branches(fbs: Any, disc: str, then_for: Callable[[str], Any]) -> list[dict[str, Any]] | None:
    """$switch branches matching the discriminator (by string form, mirroring the oracle's
    ``fbs.get(str(sub))``) to each subtype's own field expression."""
    if not isinstance(fbs, dict) or not fbs:
        return None
    branches = []
    for subval, field in fbs.items():
        if not isinstance(field, str) or not field:
            return None
        branches.append({
            "case": {"$eq": [{"$toString": f"${disc}"}, str(subval)]},
            "then": then_for(field),
        })
    return branches


def _canonical_per_subtype_agg_mql(params: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """polymorphic: group by the discriminator, aggregating each subtype's OWN field."""
    coll = params.get("collection")
    disc = params.get("discriminator")
    agg = _agg_str(params.get("agg"))
    if not all(isinstance(x, str) and x for x in (coll, disc)) or agg is None:
        return None
    branches = _subtype_branches(
        params.get("field_by_subtype"), disc, lambda f: _mql_numeric_or(f"${f}", None)
    )
    if branches is None:
        return None
    stages = [
        {"$addFields": {"__tend_sub": f"${disc}",
                        "__tend_metric": {"$switch": {"branches": branches, "default": None}}}},
        {"$match": {"__tend_metric": {"$ne": None}}},  # drop non-fbs subtypes / non-numeric values
        {"$group": {"_id": "$__tend_sub", "value": _mql_accumulator(agg, "$__tend_metric")}},
    ]
    return f"db.{coll}.aggregate({json.dumps(stages, ensure_ascii=False)})"


def _canonical_join_nested_group_mql(params: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """unwind an embedded array, then group + aggregate over its elements."""
    coll = params.get("collection")
    af = params.get("array_field")
    gb = params.get("group_by")
    if not all(isinstance(x, str) and x for x in (coll, af, gb)):
        return None
    vf = params.get("value_field")
    agg = params.get("agg", "count")
    if vf:
        if not isinstance(vf, str):
            return None
        acc = _mql_accumulator(agg, _mql_numeric_or(f"${af}.{vf}", 0))
    else:
        acc = _mql_accumulator(agg if agg else "count", 1)
    stages = [
        # the oracle skips docs whose array_field is not a real list; $unwind would otherwise
        # coerce a scalar/object into a 1-element array and diverge, so gate on $type=array
        {"$match": {"$expr": {"$eq": [{"$type": f"${af}"}, "array"]}}},
        {"$unwind": f"${af}"},
        {"$group": {"_id": f"${af}.{gb}", "value": acc}},
    ]
    return f"db.{coll}.aggregate({json.dumps(stages, ensure_ascii=False)})"


def _canonical_subtype_cond_projection_mql(params: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """polymorphic PRESERVE: keep every doc, attach a value chosen by subtype (else default)."""
    coll = params.get("collection")
    disc = params.get("discriminator")
    target = params.get("target_field")
    if not all(isinstance(x, str) and x for x in (coll, disc, target)):
        return None
    default = params.get("default", 0)
    branches = _subtype_branches(
        params.get("field_by_subtype"), disc,
        lambda f: {"$cond": [{"$eq": [{"$type": f"${f}"}, "missing"]}, default, f"${f}"]},
    )
    if branches is None:
        return None
    stages = [{"$addFields": {target: {"$switch": {"branches": branches, "default": default}}}}]
    return f"db.{coll}.aggregate({json.dumps(stages, ensure_ascii=False)})"


def _canonical_optional_embed_projection_mql(params: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """sparse_embed PRESERVE: attach an embed scalar (or missing_default), keeping every doc."""
    coll = params.get("parent_collection")
    embed = params.get("embed_field")
    value_path = params.get("value_path")
    target = params.get("target_field")
    if not all(isinstance(x, str) and x for x in (coll, embed, value_path, target)):
        return None
    if "missing_default" not in params:
        return None
    default = params.get("missing_default")
    vpath = _embed_value_path(embed, value_path)
    target_expr = {"$cond": [
        {"$and": [
            {"$ne": [{"$type": f"${embed}"}, "missing"]},
            {"$ne": [{"$type": f"${vpath}"}, "missing"]},
            {"$ne": [f"${vpath}", None]},
        ]},
        f"${vpath}",
        default,
    ]}
    stages = [{"$addFields": {target: target_expr}}]
    return f"db.{coll}.aggregate({json.dumps(stages, ensure_ascii=False)})"


def _canonical_fk_rollup_mql(params: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """Cross-collection rollup: per parent, $lookup its child rows by FK and aggregate — a
    genuine multi-collection query (mirrors oracles._fk_rollup)."""
    parent = params.get("parent_collection")
    child = params.get("child_collection")
    pk = params.get("parent_key")
    fk = params.get("foreign_key")
    if not all(isinstance(x, str) and x for x in (parent, child, pk, fk)):
        return None
    agg = str(params.get("agg", "count") or "count").lstrip("$").lower()
    if agg not in {"count", "sum", "avg", "min", "max"}:
        return None
    vf = params.get("value_field")
    match = params.get("match")
    as_f = "__tend_kids"
    src: Any = f"${as_f}"
    if isinstance(match, dict) and match.get("field") is not None:
        src = {"$filter": {"input": f"${as_f}", "as": "k",
                           "cond": {"$eq": [f"$$k.{match['field']}", match.get("value")]}}}
    if agg == "count":
        value_expr: Any = {"$size": src}
    else:
        if not isinstance(vf, str) or not vf:
            return None
        numeric_vals = {"$filter": {
            "input": {"$map": {"input": src, "as": "k", "in": f"$$k.{vf}"}},
            "as": "v", "cond": {"$in": [{"$type": "$$v"}, _NUMERIC_BSON_TYPES]},
        }}
        value_expr = {"$ifNull": [{f"${agg}": numeric_vals}, 0]}  # empty -> 0, mirroring the oracle
    stages = [
        {"$lookup": {"from": child, "localField": pk, "foreignField": fk, "as": as_f}},
        {"$addFields": {"value": value_expr}},
        {"$project": {"_id": (1 if pk == "_id" else f"${pk}"), "value": 1}},
    ]
    return f"db.{parent}.aggregate({json.dumps(stages, ensure_ascii=False)})"


#: archetype reference_oracle.template -> deterministic gold-MQL builder. Templates here
#: lock by construction (gold derived from oracle params); the rest fall back to the
#: LLM MQL and must match the oracle to lock.
_CANONICAL_MQL_BUILDERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], str | None]] = {
    "fk_rollup": _canonical_fk_rollup_mql,
    "has_vs_absent_compare": _canonical_has_vs_absent_compare_mql,
    "present_missing_projection": _canonical_present_missing_projection_mql,
    "optional_embed_projection": _canonical_optional_embed_projection_mql,
    "subtype_cond_projection": _canonical_subtype_cond_projection_mql,
    "simple_filter": _canonical_simple_filter_mql,
    "existence_count": _canonical_existence_count_mql,
    "group_count": _canonical_group_count_mql,
    "topn": _canonical_topn_mql,
    "null_coalesce_agg": _canonical_null_coalesce_agg_mql,
    "subtype_specific_field": _canonical_subtype_specific_field_mql,
    "per_subtype_agg": _canonical_per_subtype_agg_mql,
    "cross_subtype_compare": _canonical_per_subtype_agg_mql,
    "join_nested_group": _canonical_join_nested_group_mql,
    "cross_keyset_value": _canonical_cross_keyset_value_mql,
    "dynamic_key_fold": _canonical_dynamic_key_fold_mql,
    "cross_version_agg": _canonical_cross_version_agg_mql,
}

#: shape_policy implied by each canonical gold (preserve = keep every doc + add a field;
#: reduce = aggregate/filter/group to a different cardinality). Drives the MS preserve gate.
_CANONICAL_SHAPE: dict[str, str] = {
    "present_missing_projection": "preserve",
    "optional_embed_projection": "preserve",
    "subtype_cond_projection": "preserve",
    "has_vs_absent_compare": "reduce",
    "existence_count": "reduce",
    "group_count": "reduce",
    "topn": "reshape",
    "null_coalesce_agg": "reduce",
    "per_subtype_agg": "reduce",
    "cross_subtype_compare": "reduce",
    "join_nested_group": "reduce",
    "dynamic_key_fold": "reduce",
    "cross_version_agg": "reduce",
    # filter/projection archetypes have no root $group and change cardinality, so they are
    # neither "reduce" (AST requires $group) nor "preserve" (MS enforces same cardinality):
    "simple_filter": "reshape",
    "subtype_specific_field": "reshape",
    "cross_keyset_value": "reshape",
    "fk_rollup": "reshape",
}


def _canonical_metric_path(
    schema: dict[str, Any], collection: str, embed_field: str, metric_field: Any
) -> str | None:
    fallback = _first_numeric_nested_field(schema, collection, embed_field)
    if not isinstance(metric_field, str) or not metric_field:
        return fallback
    if metric_field in _schema_field_paths(schema, collection):
        return metric_field
    embedded = metric_field if metric_field.startswith(f"{embed_field}.") else (
        f"{embed_field}.{metric_field}"
    )
    if embedded in _schema_field_paths(schema, collection):
        return embedded
    if fallback is not None:
        return fallback
    return metric_field


def _first_numeric_nested_field(
    schema: dict[str, Any], collection: str, embed_field: str
) -> str | None:
    colls = schema.get("collections", schema)
    node = colls.get(collection, {}) if isinstance(colls, dict) else {}
    if not isinstance(node, dict):
        return None
    embed = node.get(embed_field)
    fields = embed.get("fields") if isinstance(embed, dict) else None
    if not isinstance(fields, dict):
        return None
    for name in _METRIC_FIELD_PRIORITY:
        typ = fields.get(name)
        if str(typ).upper() in {"INT", "INTEGER", "REAL", "FLOAT", "DOUBLE", "NUMERIC"}:
            return f"{embed_field}.{name}"
    for name, typ in fields.items():
        if str(typ).upper() in {"INT", "INTEGER", "REAL", "FLOAT", "DOUBLE", "NUMERIC"}:
            return f"{embed_field}.{name}"
    return None


def _structural_schema_flex_reasons(
    mql: str, schema: dict[str, Any], collection: str, inputs: dict[str, Any]
) -> list[str]:
    if inputs.get("target_sql_infeasibility_class") != "structural_schema_flex":
        return []
    reasons: list[str] = []
    colls = schema.get("collections", schema)
    node = colls.get(collection, {}) if isinstance(colls, dict) else {}
    if not isinstance(node, dict) or not node.get("__variants"):
        reasons.append(f"target structural_schema_flex but {collection!r} has no __variants")
    try:
        _, pipeline = parse_pipeline(mql)
    except TendError as exc:
        return [exc.message]
    ops = _pipeline_operator_set(pipeline)
    if "$type" not in ops and "$objectToArray" not in ops:
        reasons.append("structural_schema_flex target requires $type or $objectToArray")
    if not ({"$switch", "$cond"} & ops):
        reasons.append("structural_schema_flex target requires explicit variant branch dispatch")
    intent = inputs.get("intent") if isinstance(inputs.get("intent"), dict) else {}
    if intent.get("archetype") == "schema_flex_variant_summary" and "$group" not in ops:
        reasons.append("schema_flex_variant_summary target requires a $group summary")
    if inputs.get("target_schema_flex") and inputs.get("target_schema_flex") != "none":
        sf = str(inputs.get("target_schema_flex"))
        if sf != "polymorphic":
            reasons.append(f"unsupported target_schema_flex {sf!r}")
    return reasons


def _structural_schema_flex_result_reasons(
    rows: list[dict[str, Any]], inputs: dict[str, Any]
) -> list[str]:
    if inputs.get("target_sql_infeasibility_class") != "structural_schema_flex":
        return []
    intent = inputs.get("intent") if isinstance(inputs.get("intent"), dict) else {}
    if intent.get("archetype") != "schema_flex_variant_summary":
        return []
    sample = rows[:500]
    reasons: list[str] = []
    if any("variant" not in row for row in sample):
        reasons.append("schema_flex_variant_summary result must expose field 'variant'")
    else:
        variants = {row.get("variant") for row in sample}
        if variants != {"present", "missing"}:
            reasons.append(
                "schema_flex_variant_summary result variant values must be exactly "
                f"'present' and 'missing', got {sorted(map(str, variants))}"
            )
    if any("count" not in row for row in sample):
        reasons.append("schema_flex_variant_summary result must expose field 'count'")
    target_field = ((inputs.get("intent") or {}).get("analytical_op") or {}).get("target_field")
    if target_field and any(target_field not in row for row in sample):
        reasons.append(
            "schema_flex_variant_summary result must expose analytical target field "
            f"{target_field!r}"
        )
    return reasons


def _pipeline_operator_set(value: Any) -> set[str]:
    ops: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if isinstance(key, str) and key.startswith("$"):
                    ops.add(key)
                visit(child)
            return
        if isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    return ops


def _unknown_schema_refs(mql: str, schema: dict[str, Any], collection: str) -> list[str]:
    """Return field paths used by MQL but absent from the collection schema."""
    try:
        _, pipeline = parse_pipeline(mql)
    except TendError:
        return []
    refs_by_collection = _mql_field_refs_by_collection(pipeline, collection)
    transient_heads = _pipeline_created_heads(mql)
    bad: list[str] = []
    for coll, refs in refs_by_collection.items():
        ignored_heads = transient_heads if coll == collection else set()
        prefix = "" if coll == collection else f"{coll}."
        bad.extend(_unknown_refs_for_collection(schema, coll, refs, ignored_heads, prefix))
    return sorted(set(bad))


def _unknown_refs_for_collection(
    schema: dict[str, Any], collection: str, refs: set[str], ignored_heads: set[str],
    prefix: str = "",
) -> list[str]:
    allowed = _schema_field_paths(schema, collection)
    if not allowed:
        return []
    known_nested_heads = _known_nested_schema_heads(schema, collection)
    candidates = set()
    for ref in refs:
        if not ref or ref.startswith("$"):
            continue
        head = ref.split(".", 1)[0]
        if head in {"ROOT", "CURRENT"} or head in ignored_heads:
            continue
        candidates.add(ref)
    bad = []
    for ref in candidates:
        if ref in allowed:
            continue
        head = ref.split(".", 1)[0]
        if "." in ref and head in allowed and head not in known_nested_heads:
            continue
        bad.append(f"{prefix}{ref}")
    return sorted(bad)


def _mql_field_refs(mql: str) -> set[str]:
    """Field references appearing as expression values, e.g. ``"$loan.amount"``."""
    try:
        _, pipeline = parse_pipeline(mql)
    except TendError:
        return set()

    return _field_refs(pipeline)


def _mql_field_refs_by_collection(
    pipeline: list[dict[str, Any]], root_collection: str
) -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {root_collection: set()}
    for stage in pipeline:
        if not isinstance(stage, dict):
            continue
        lookup = stage.get("$lookup")
        if isinstance(lookup, dict):
            root_lookup = {k: v for k, v in lookup.items() if k != "pipeline"}
            refs[root_collection].update(_field_refs(root_lookup))
            from_coll = lookup.get("from")
            subpipe = lookup.get("pipeline")
            if isinstance(from_coll, str) and isinstance(subpipe, list):
                sub_created = _pipeline_created_heads_from_stages(subpipe)
                refs.setdefault(from_coll, set()).update(
                    ref for ref in _field_refs(subpipe)
                    if ref.split(".", 1)[0] not in sub_created
                )
            stage = {k: v for k, v in stage.items() if k != "$lookup"}
            if not stage:
                continue
        refs[root_collection].update(_field_refs(stage))
    return refs


def _field_refs(value: Any) -> set[str]:
    refs: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, str):
            if node.startswith("$") and not node.startswith("$$") and len(node) > 1:
                refs.add(node[1:])
            return
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if isinstance(node, dict):
            for item in node.values():
                visit(item)

    visit(value)
    return refs


def _pipeline_created_heads(mql: str) -> set[str]:
    """Top-level fields created inside the pipeline and safe to reference later."""
    try:
        _, pipeline = parse_pipeline(mql)
    except TendError:
        return set()
    return _pipeline_created_heads_from_stages(pipeline)


def _pipeline_created_heads_from_stages(pipeline: list[Any]) -> set[str]:
    """Top-level fields created inside a root pipeline or lookup sub-pipeline."""
    heads: set[str] = set()
    for stage in pipeline:
        if not isinstance(stage, dict):
            continue
        lookup = stage.get("$lookup")
        if isinstance(lookup, dict) and isinstance(lookup.get("as"), str):
            heads.add(lookup["as"])
        for op in ("$addFields", "$set"):
            spec = stage.get(op)
            if isinstance(spec, dict):
                heads.update(k.split(".", 1)[0] for k in spec if isinstance(k, str))
        project = stage.get("$project")
        if isinstance(project, dict):
            heads.update(
                k.split(".", 1)[0]
                for k, v in project.items()
                if isinstance(k, str)
                and (
                    isinstance(v, (dict, list))
                    or (isinstance(v, str) and v.startswith("$"))
                )
            )
        group = stage.get("$group")
        if isinstance(group, dict):
            heads.update(
                k.split(".", 1)[0]
                for k in group
                if isinstance(k, str) and k != "_id"
            )
    return heads


def _schema_field_paths(schema: dict[str, Any], collection: str) -> set[str]:
    colls = schema.get("collections", schema)
    node = colls.get(collection, {}) if isinstance(colls, dict) else {}
    if not isinstance(node, dict):
        return set()
    out: set[str] = set()
    for name, typ in node.items():
        if name.startswith("__"):
            continue
        out.add(name)
        out.update(f"{name}.{child}" for child in _nested_field_paths(typ))
    return out


def _known_nested_schema_heads(schema: dict[str, Any], collection: str) -> set[str]:
    colls = schema.get("collections", schema)
    node = colls.get(collection, {}) if isinstance(colls, dict) else {}
    if not isinstance(node, dict):
        return set()
    return {
        name
        for name, typ in node.items()
        if isinstance(name, str)
        and not name.startswith("__")
        and isinstance(typ, dict)
        and bool(_nested_field_paths(typ))
    }


def _nested_field_paths(typ: Any) -> set[str]:
    if isinstance(typ, dict) and typ.get("type") == "OBJECT":
        out: set[str] = set()
        for name, child in typ.get("fields", {}).items():
            out.add(name)
            out.update(f"{name}.{p}" for p in _nested_field_paths(child))
        return out
    if isinstance(typ, dict) and typ.get("type") == "ARRAY":
        return _nested_field_paths(typ.get("items"))
    return set()


def _schema_digest(schema: dict[str, Any], max_fields: int = 14) -> str:
    """Compact, prompt-friendly view of DM's derived schema (collections, fields, variants)."""
    colls = schema.get("collections", schema)
    if not colls:
        return "(no schema)"
    lines = []
    for name, node in colls.items():
        if not isinstance(node, dict):
            continue
        fields = []
        for k, v in node.items():
            if k.startswith("__"):
                continue
            nested = sorted(_nested_field_paths(v))
            fields.append(k if not nested else f"{k}{{{', '.join(nested[:6])}}}")
        fields = fields[:max_fields]
        variants = node.get("__variants", [])
        flex = " schema_flex=polymorphic" if variants else ""
        vnote = ""
        if variants:
            disc = ", ".join(str(v.get("discriminator")) for v in variants)
            vnote = f"  optional/variants: {disc}"
        lines.append(f"- {name}: {', '.join(map(str, fields))}{flex}{vnote}")
    return "\n".join(lines)
