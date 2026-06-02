"""Deterministic executor for Codex-native migration recipes."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..errors import MigrationError
from .native_recipe import (
    NativeFeature,
    NativeFeatureManifest,
    NativeMigrationRecipe,
    NativeTransform,
    RecipeValidationResult,
    verify_native_recipe,
)


@dataclass
class NativeExecutionResult:
    data: dict[str, list[dict[str, Any]]]
    schema: dict[str, Any]
    manifest: NativeFeatureManifest
    provenance: dict[str, Any]
    world_signature: str
    validation: RecipeValidationResult | None = None


@dataclass
class _MaterializedDoc:
    doc: dict[str, Any]
    source_context: dict[str, dict[str, Any]]


@dataclass
class _ExecutionContext:
    source: Any
    db_id: str
    schema: Any
    rows_by_table: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def execute_native_recipe(
    source: Any,
    db_id: str,
    recipe: NativeMigrationRecipe,
    *,
    event_hook: Any = None,
) -> NativeExecutionResult:
    """Materialize MongoDB-native documents from a verified recipe.

    The recipe may come from an LLM, but execution is deterministic and fails closed
    on unsupported expressions.
    """
    schema = source.schema(db_id)
    validation = verify_native_recipe(recipe, schema)
    if not validation.ok:
        raise MigrationError(
            "native migration recipe failed verification",
            context={"db_id": db_id, "errors": validation.errors},
        )
    ctx = _ExecutionContext(source=source, db_id=db_id, schema=schema)
    data: dict[str, list[dict[str, Any]]] = {}
    materialized: dict[str, list[_MaterializedDoc]] = {}
    features: list[NativeFeature] = []
    provenance: dict[str, Any] = {}

    for collection_name, collection in sorted(recipe.collections.items()):
        docs = _materialize_collection(ctx, collection_name, collection.transforms)
        materialized[collection_name] = docs
        data[collection_name] = [item.doc for item in docs]
        for transform in collection.transforms:
            feature = _feature_for_transform(collection_name, transform)
            features.append(feature)
            provenance[feature.id] = _provenance_for_transform(transform)

    manifest = NativeFeatureManifest(db_id=db_id, features=features)
    inferred_schema = _infer_schema(data, manifest)
    signature_payload = {
        "db_id": db_id,
        "recipe": recipe.to_dict(),
        "data": data,
        "features": manifest.to_dict(),
    }
    world_signature = hashlib.sha256(
        json.dumps(signature_payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    if event_hook is not None:
        event_hook(
            "recipe_materialized",
            db_id=db_id,
            collection_count=len(data),
            document_count=sum(len(docs) for docs in data.values()),
            native_feature_count=len(features),
            world_signature=world_signature,
        )
    return NativeExecutionResult(
        data=data,
        schema=inferred_schema,
        manifest=manifest,
        provenance=provenance,
        world_signature=world_signature,
        validation=validation,
    )


def _materialize_collection(
    ctx: _ExecutionContext,
    collection_name: str,
    transforms: list[NativeTransform],
) -> list[_MaterializedDoc]:
    union = next((transform for transform in transforms if transform.type == "polymorphic_union"), None)
    if union is not None:
        docs = _materialize_polymorphic_union(ctx, union)
    else:
        docs = _materialize_parent_collection(ctx, transforms)
    for transform in transforms:
        if transform.type == "derived_tag_array":
            _apply_derived_tags(collection_name, docs, transform)
        elif transform.type == "dynamic_key_object":
            _apply_dynamic_key_object(ctx, docs, transform)
        elif transform.type == "nested_event_stream":
            _apply_nested_event_stream(ctx, docs, transform)
    return docs


def _materialize_polymorphic_union(
    ctx: _ExecutionContext,
    transform: NativeTransform,
) -> list[_MaterializedDoc]:
    raw = transform.raw
    discriminator = str(raw.get("discriminator") or "type")
    variants = raw.get("variants")
    if not isinstance(variants, dict):
        raise MigrationError(
            "polymorphic_union requires variants",
            context={"db_id": ctx.db_id, "transform_id": transform.id},
        )
    docs: list[_MaterializedDoc] = []
    for variant_name, variant_raw in variants.items():
        variant = variant_raw if isinstance(variant_raw, dict) else {}
        table = str(variant.get("source_table") or "")
        rows = _rows_for_table(ctx, table)
        for row in rows:
            source_context = {table: row}
            doc = {discriminator: str(variant_name)}
            fields = variant.get("fields") if isinstance(variant.get("fields"), dict) else {}
            for field_name, field_spec in fields.items():
                value = _eval_field_spec(ctx, field_spec, source_context, group_rows=None)
                if value is not None:
                    doc[str(field_name)] = value
            if "entity_id" in doc:
                doc["_id"] = doc["entity_id"]
            docs.append(_MaterializedDoc(doc=doc, source_context=source_context))
    return docs


def _materialize_parent_collection(
    ctx: _ExecutionContext,
    transforms: list[NativeTransform],
) -> list[_MaterializedDoc]:
    parent_table = ""
    for transform in transforms:
        if transform.type in {"dynamic_key_object", "nested_event_stream"}:
            parent_table = str(transform.raw.get("parent_table") or "")
            break
    if not parent_table:
        raise MigrationError(
            "native collection needs a parent table or polymorphic union",
            context={"db_id": ctx.db_id},
        )
    docs: list[_MaterializedDoc] = []
    pk = _primary_key(ctx.schema, parent_table)
    for row in _rows_for_table(ctx, parent_table):
        doc = {key: value for key, value in row.items() if value is not None}
        if pk and pk in row and row[pk] is not None:
            doc["_id"] = row[pk]
        docs.append(_MaterializedDoc(doc=doc, source_context={parent_table: row}))
    return docs


def _apply_dynamic_key_object(
    ctx: _ExecutionContext,
    docs: list[_MaterializedDoc],
    transform: NativeTransform,
) -> None:
    raw = transform.raw
    target_field = str(raw.get("target_field") or transform.id)
    parent_table = str(raw.get("parent_table") or "")
    child_table = str(raw.get("child_table") or "")
    left_ref, right_ref = _join_refs(raw.get("join"))
    if not left_ref or not right_ref:
        raise MigrationError(
            "dynamic_key_object requires a join",
            context={"db_id": ctx.db_id, "transform_id": transform.id},
        )
    key_spec = raw.get("key") if isinstance(raw.get("key"), dict) else {}
    values = raw.get("values") if isinstance(raw.get("values"), dict) else {}
    child_rows = _rows_for_table(ctx, child_table)

    for item in docs:
        parent_row = item.source_context.get(parent_table, {})
        parent_value = _value_from_ref(left_ref, {parent_table: parent_row})
        matching = [
            row
            for row in child_rows
            if _value_from_ref(right_ref, {child_table: row}) == parent_value
        ]
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for child_row in matching:
            source_context = dict(item.source_context)
            source_context[child_table] = child_row
            key = _eval_expr(ctx, str(key_spec.get("expr") or ""), source_context, group_rows=None)
            buckets[str(key)].append(child_row)
        out: dict[str, dict[str, Any]] = {}
        for key in sorted(buckets):
            group_rows = buckets[key]
            group_context = dict(item.source_context)
            group_context[child_table] = group_rows[0] if group_rows else {}
            out[key] = {}
            for value_name, value_spec in values.items():
                out[key][str(value_name)] = _eval_field_spec(
                    ctx,
                    value_spec,
                    group_context,
                    group_rows={child_table: group_rows},
                )
        if out:
            item.doc[target_field] = out


def _apply_nested_event_stream(
    ctx: _ExecutionContext,
    docs: list[_MaterializedDoc],
    transform: NativeTransform,
) -> None:
    raw = transform.raw
    target_field = str(raw.get("target_field") or transform.id)
    parent_table = str(raw.get("parent_table") or "")
    event_table = str(raw.get("event_source_table") or "")
    left_ref, right_ref = _join_refs(raw.get("join"))
    event_type_ref = str(raw.get("event_type_field") or "")
    event_time_ref = str(raw.get("event_time_field") or "")
    payload = raw.get("event_payload") if isinstance(raw.get("event_payload"), dict) else {}
    event_rows = _rows_for_table(ctx, event_table)

    for item in docs:
        parent_row = item.source_context.get(parent_table, {})
        parent_value = _value_from_ref(left_ref, {parent_table: parent_row})
        matching = [
            row
            for row in event_rows
            if _value_from_ref(right_ref, {event_table: row}) == parent_value
        ]
        events: list[dict[str, Any]] = []
        for row in matching:
            source_context = {**item.source_context, event_table: row}
            event = {
                "event_type": _value_from_ref(event_type_ref, source_context),
                "event_time": _value_from_ref(event_time_ref, source_context),
            }
            for field_name, ref in payload.items():
                value = _value_from_ref(str(ref), source_context)
                if value is not None:
                    event[str(field_name)] = value
            events.append(event)
        events.sort(key=lambda event: (str(event.get("event_time") or ""), json.dumps(event, sort_keys=True)))
        if events:
            item.doc[target_field] = events


def _apply_derived_tags(
    collection_name: str,
    docs: list[_MaterializedDoc],
    transform: NativeTransform,
) -> None:
    raw = transform.raw
    target_field = str(raw.get("target_field") or transform.id)
    tags = raw.get("tags") if isinstance(raw.get("tags"), dict) else {}
    for item in docs:
        out: list[str] = []
        for tag, spec_raw in tags.items():
            spec = spec_raw if isinstance(spec_raw, dict) else {}
            condition = str(spec.get("condition") or "")
            if condition and _eval_condition(condition, item.source_context):
                out.append(str(tag))
        if out:
            item.doc[target_field] = sorted(out)


def _rows_for_table(ctx: _ExecutionContext, table: str) -> list[dict[str, Any]]:
    if table in ctx.rows_by_table:
        return ctx.rows_by_table[table]
    if not table:
        return []
    conn = ctx.source.connection(ctx.db_id)
    pk = _primary_key(ctx.schema, table)
    order_by = f" ORDER BY {_quote_ident(pk)}" if pk else ""
    cur = conn.execute(f"SELECT * FROM {_quote_ident(table)}{order_by}")
    columns = [item[0] for item in cur.description]
    rows = [dict(zip(columns, values)) for values in cur.fetchall()]
    if not pk:
        rows.sort(key=lambda row: json.dumps(row, sort_keys=True, default=str))
    ctx.rows_by_table[table] = rows
    return rows


def _primary_key(schema: Any, table: str) -> str:
    keys = getattr(schema, "primary_keys", {}).get(table) or []
    return str(keys[0]) if len(keys) == 1 else ""


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _join_refs(join: Any) -> tuple[str, str]:
    if not isinstance(join, dict):
        return "", ""
    return str(join.get("left") or ""), str(join.get("right") or "")


def _eval_field_spec(
    ctx: _ExecutionContext,
    field_spec: Any,
    source_context: dict[str, dict[str, Any]],
    *,
    group_rows: dict[str, list[dict[str, Any]]] | None,
) -> Any:
    if isinstance(field_spec, str):
        return _value_from_ref(field_spec, source_context)
    if not isinstance(field_spec, dict):
        raise MigrationError(
            "unsupported native field spec",
            context={"db_id": ctx.db_id, "field_spec": repr(field_spec)},
        )
    if "source" in field_spec:
        return _value_from_ref(str(field_spec["source"]), source_context)
    if "expr" in field_spec:
        return _eval_expr(ctx, str(field_spec["expr"]), source_context, group_rows=group_rows)
    raise MigrationError(
        "unsupported native field spec",
        context={"db_id": ctx.db_id, "field_spec": field_spec},
    )


def _eval_expr(
    ctx: _ExecutionContext,
    expr: str,
    source_context: dict[str, dict[str, Any]],
    *,
    group_rows: dict[str, list[dict[str, Any]]] | None,
) -> Any:
    expr = expr.strip()
    if not expr:
        raise MigrationError("unsupported native expression", context={"db_id": ctx.db_id, "expr": expr})
    if _REF_RE.fullmatch(expr):
        return _value_from_ref(expr, source_context)
    if expr.startswith("concat(") and expr.endswith(")"):
        args = _split_args(expr[len("concat("):-1])
        return "".join(_eval_concat_arg(arg, source_context) for arg in args)
    if expr.startswith("month(") and expr.endswith(")"):
        value = _value_from_ref(expr[len("month("):-1].strip(), source_context)
        return _month_key(value)
    sum_match = _SUM_RE.fullmatch(expr)
    if sum_match:
        if group_rows is None:
            raise MigrationError(
                "sum expression requires grouped child rows",
                context={"db_id": ctx.db_id, "expr": expr},
            )
        table, column, condition = sum_match.group("table"), sum_match.group("column"), sum_match.group("condition")
        total = 0
        for row in group_rows.get(table, []):
            row_context = {**source_context, table: row}
            if condition is None or _eval_condition(condition, row_context):
                value = row.get(column)
                if value is not None:
                    total += value
        return total
    subtract_match = _SUBTRACT_RE.fullmatch(expr)
    if subtract_match:
        left = _value_from_ref(subtract_match.group("left"), source_context)
        right = _value_from_ref(subtract_match.group("right"), source_context)
        return left - right
    raise MigrationError(
        "unsupported native expression",
        context={"db_id": ctx.db_id, "expr": expr},
    )


def _eval_concat_arg(arg: str, source_context: dict[str, dict[str, Any]]) -> str:
    arg = arg.strip()
    if len(arg) >= 2 and arg[0] == arg[-1] == "'":
        return arg[1:-1]
    return str(_value_from_ref(arg, source_context))


def _eval_condition(condition: str, source_context: dict[str, dict[str, Any]]) -> bool:
    condition = condition.strip()
    match = _COMPARISON_RE.fullmatch(condition)
    if not match:
        return False
    left = _value_from_ref(match.group("left"), source_context)
    if left is None:
        return False
    right = _literal(match.group("right"))
    op = match.group("op")
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    try:
        left_num = float(left)
        right_num = float(right)
    except (TypeError, ValueError):
        return False
    if op == ">":
        return left_num > right_num
    if op == ">=":
        return left_num >= right_num
    if op == "<":
        return left_num < right_num
    if op == "<=":
        return left_num <= right_num
    return False


def _value_from_ref(ref: str, source_context: dict[str, dict[str, Any]]) -> Any:
    if "." not in ref:
        return None
    table, column = ref.split(".", 1)
    row = source_context.get(table)
    if row is None:
        return None
    return row.get(column)


def _literal(raw: str) -> Any:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _month_key(value: Any) -> str:
    text = str(value or "")
    return text[:7] if len(text) >= 7 else text


def _split_args(text: str) -> list[str]:
    args: list[str] = []
    current: list[str] = []
    in_quote = False
    for char in text:
        if char == "'":
            in_quote = not in_quote
            current.append(char)
        elif char == "," and not in_quote:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        args.append("".join(current).strip())
    return args


def _feature_for_transform(collection_name: str, transform: NativeTransform) -> NativeFeature:
    feature_id = f"{collection_name}.{transform.id}"
    target_field = str(transform.raw.get("target_field") or transform.id)
    if transform.type == "polymorphic_union":
        return NativeFeature(
            id=feature_id,
            type="polymorphic_collection",
            collection=collection_name,
            field=str(transform.raw.get("discriminator") or "type"),
            query_patterns=["subtype_field_dispatch"],
            required_constructs=["$switch"],
            provenance_refs=_source_refs_for_transform(transform),
            coverage={"variant_count": len(transform.raw.get("variants") or {})},
            extra={"recipe_transform_type": transform.type},
        )
    if transform.type == "dynamic_key_object":
        return NativeFeature(
            id=feature_id,
            type="dynamic_key_object",
            collection=collection_name,
            field=target_field,
            query_patterns=["dynamic_key_comparison"],
            required_constructs=["$objectToArray", "$filter"],
            provenance_refs=_source_refs_for_transform(transform),
        )
    if transform.type == "derived_tag_array":
        return NativeFeature(
            id=feature_id,
            type="derived_tag_array",
            collection=collection_name,
            field=target_field,
            query_patterns=["tag_combination"],
            required_constructs=["$setIntersection", "$size"],
            provenance_refs=_source_refs_for_transform(transform),
        )
    if transform.type == "nested_event_stream":
        return NativeFeature(
            id=feature_id,
            type="nested_event_stream",
            collection=collection_name,
            field=target_field,
            query_patterns=["nested_event_filter"],
            required_constructs=["$filter"],
            provenance_refs=_source_refs_for_transform(transform),
        )
    return NativeFeature(
        id=feature_id,
        type=transform.type,
        collection=collection_name,
        field=target_field,
        provenance_refs=_source_refs_for_transform(transform),
    )


def _provenance_for_transform(transform: NativeTransform) -> dict[str, Any]:
    return {
        "transform_id": transform.id,
        "transform_type": transform.type,
        "source_columns": _source_refs_for_transform(transform),
        "rule": "deterministic native recipe execution",
    }


def _source_refs_for_transform(transform: NativeTransform) -> list[str]:
    refs: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "provenance" and isinstance(item, list):
                    refs.update(str(ref) for ref in item)
                elif key in {
                    "source",
                    "left",
                    "right",
                    "event_type_field",
                    "event_time_field",
                } and isinstance(item, str):
                    refs.add(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            if _REF_RE.fullmatch(value):
                refs.add(value)

    visit(transform.raw)
    return sorted(refs)


def _infer_schema(data: dict[str, list[dict[str, Any]]], manifest: NativeFeatureManifest) -> dict[str, Any]:
    feature_by_collection: dict[str, list[str]] = defaultdict(list)
    for feature in manifest.features:
        feature_by_collection[feature.collection].append(feature.id)
    out: dict[str, Any] = {}
    for collection, docs in sorted(data.items()):
        fields = sorted({field for doc in docs for field in doc})
        out[collection] = {
            "fields": fields,
            "document_count": len(docs),
            "native_features": sorted(feature_by_collection.get(collection, [])),
        }
    return out


_REF_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*")
_SUM_RE = re.compile(
    r"sum\((?P<table>[A-Za-z_][A-Za-z0-9_]*)\.(?P<column>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s+where\s+(?P<condition>.+))?\)"
)
_SUBTRACT_RE = re.compile(r"(?P<left>[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\s*-\s*(?P<right>[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)")
_COMPARISON_RE = re.compile(
    r"(?P<left>[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?P<op>==|!=|>=|<=|>|<)\s*"
    r"(?P<right>'[^']*'|-?\d+(?:\.\d+)?)"
)
