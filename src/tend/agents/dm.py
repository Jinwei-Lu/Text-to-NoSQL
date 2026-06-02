"""DM — deterministic data migrator (Phase A).

Not an LLM agent: it derives the authoritative document-aggregate layout from real BIRD FK structure
(:mod:`tend.construct.migrate`), materializes witness documents, computes the
``world_signature``, loads the witness into the working MongoDB, and emits a schema that
is consistent with the data by construction (Gate-SD), including optional-embed
``__variants`` (the present/missing heterogeneity).
"""
from __future__ import annotations

from typing import Any

from ..construct import build_plan, migrate
from ..errors import MigrationError
from ..execution import world_signature
from .base import Agent, AgentContext, register


@register
class DataMigrator(Agent):
    id = "dm"
    phase = "A"
    title = "DM · Data Migrator"

    async def run(self, ctx: AgentContext, inputs: dict[str, Any]) -> dict[str, Any]:
        db_id = inputs["db_id"]
        if ctx.source is None:
            raise MigrationError("DM requires a BIRD source", context={"db_id": db_id})

        plan = build_plan(
            ctx.source,
            db_id,
            ref_sample_cap=ctx.settings.migration_ref_sample_cap,
        )
        data = migrate(ctx.source, db_id, plan)
        if not data:
            raise MigrationError("migration produced no documents", context={"db_id": db_id})
        sig = world_signature(data)
        schema = self._derive_schema(db_id, plan, data)
        schema_less_features = _schema_less_feature_counts(schema)

        ctx.log.info("dm_migrated", db_id=db_id,
                     collections={c: len(d) for c, d in data.items()},
                     schema_less_features=schema_less_features,
                     world_signature=sig)

        # load into the working MongoDB so Phase B can NormExec against it
        if ctx.mongo is not None and ctx.mongo.available():
            ctx.mongo.load_witness(db_id, data)
        else:
            ctx.log.warning("dm_mongo_unavailable", db_id=db_id,
                            note="witness materialized but not loaded (no MongoDB)")

        return {
            "mongodb_data": data,
            "mongodb_schema": schema,
            "schema_authority": "dm_materialized",
            "world_signature": sig,
            "migration_log": {
                "db_id": db_id,
                "roots": plan.roots,
                "embeds": {p: [e.child for e in es] for p, es in plan.embeds.items()},
                "references": plan.references,
                "sampled": plan.sample_caps,
                "doc_counts": {c: len(d) for c, d in data.items()},
            },
        }

    def _derive_schema(self, db_id: str, plan, data: dict[str, list[dict]]) -> dict[str, Any]:
        """Schema consistent with the migrated data (field types + optional-embed variants)."""
        collections: dict[str, Any] = {}
        for coll, docs in data.items():
            field_counts: dict[str, int] = {}
            field_samples: dict[str, Any] = {}
            for d in docs:
                for k, v in d.items():
                    field_counts[k] = field_counts.get(k, 0) + 1
                    field_samples.setdefault(k, v)
            n = len(docs) or 1
            node: dict[str, Any] = {
                k: _field_type(field_samples.get(k)) for k in sorted(field_counts)
            }
            variants = []
            schema_less_features: list[dict[str, Any]] = []
            # optional embeds (object satellites) AND optional array projections (e.g. a parent
            # that only sometimes has child rows) both produce present/missing schema-flex
            # variants — covering more than one structure per db diversifies the schema-less set.
            optional_edges = list(plan.embeds.get(coll, [])) + [
                e for e in getattr(plan, "array_projections", {}).get(coll, [])
            ]
            for e in optional_edges:
                kind = "optional_embed" if e in plan.embeds.get(coll, []) else "optional_array"
                present = field_counts.get(e.child, 0)
                if 0 < present < len(docs):           # sparse optional -> schema_flex variant
                    schema_less_features.append(_schema_less_feature(
                        family=kind,
                        feature=e.child,
                        coverage=present / n,
                        evidence=f"{kind}: {e.child} present ({present}/{len(docs)})",
                    ))
                    variants.extend([
                        {
                            "discriminator": {e.child: "present"},
                            "fields": {e.child: _field_type(field_samples.get(e.child))},
                            "coverage": round(present / n, 4),
                            "source_signal": (
                                f"{kind}: {e.child} present ({present}/{len(docs)})"
                            ),
                        },
                        {
                            "discriminator": {e.child: "missing"},
                            "fields": {e.child: _field_type(field_samples.get(e.child))},
                            "coverage": round((len(docs) - present) / n, 4),
                            "source_signal": (
                                f"{kind}: {e.child} missing "
                                f"({len(docs) - present}/{len(docs)})"
                            ),
                        },
                    ])
            for field in _sparse_scalar_variant_fields(field_counts, field_samples, len(docs)):
                present = field_counts[field]
                schema_less_features.append(_schema_less_feature(
                    family="sparse_scalar",
                    feature=field,
                    coverage=present / n,
                    evidence=f"sparse_scalar: {field} present ({present}/{len(docs)})",
                ))
                variants.extend([
                    {
                        "discriminator": {field: "present"},
                        "fields": {field: _field_type(field_samples.get(field))},
                        "coverage": round(present / n, 4),
                        "source_signal": (
                            f"sparse_scalar: {field} present ({present}/{len(docs)})"
                        ),
                    },
                    {
                        "discriminator": {field: "missing"},
                        "fields": {field: _field_type(field_samples.get(field))},
                        "coverage": round((len(docs) - present) / n, 4),
                        "source_signal": (
                            f"sparse_scalar: {field} missing ({len(docs) - present}/{len(docs)})"
                        ),
                    },
                ])
            # Polymorphic variants are an independent schema-less family, not a fallback.
            # A collection can simultaneously have sparse/missing fields and discriminator
            # subtypes; keep both families visible so Phase B can build diverse targets.
            poly_variants = _polymorphic_variants(docs)
            variants.extend(poly_variants)
            for feature in _polymorphic_feature_manifest(poly_variants):
                schema_less_features.append(feature)
            if variants:
                node["__variants"] = variants
            for field, value in sorted(field_samples.items()):
                if isinstance(value, list):
                    lengths = _array_lengths(docs, field)
                    schema_less_features.append(_schema_less_feature(
                        family="nested_array",
                        feature=field,
                        coverage=field_counts.get(field, 0) / n,
                        evidence=f"nested_array: {field} present ({field_counts.get(field, 0)}/{len(docs)})",
                    ))
                    if len(set(lengths)) > 1:
                        schema_less_features.append(_schema_less_feature(
                            family="array_cardinality",
                            feature=field,
                            coverage=len(lengths) / n,
                            evidence=(
                                f"array_cardinality: {field} length varies "
                                f"(min={min(lengths)}, max={max(lengths)}, "
                                f"distinct={len(set(lengths))})"
                            ),
                        ))
            if schema_less_features:
                node["__schema_less_features"] = _dedupe_schema_less_features(
                    schema_less_features
                )
            collections[coll] = node
        return collections


def _schema_less_feature_counts(schema: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in schema.values():
        if not isinstance(node, dict):
            continue
        features = node.get("__schema_less_features")
        if not isinstance(features, list):
            continue
        for feature in features:
            if not isinstance(feature, dict):
                continue
            family = str(feature.get("family") or "unknown")
            counts[family] = counts.get(family, 0) + 1
    return dict(sorted(counts.items()))


def _array_lengths(docs: list[dict], field: str) -> list[int]:
    return [
        len(doc[field])
        for doc in docs
        if isinstance(doc, dict) and isinstance(doc.get(field), list)
    ]


def _schema_less_feature(
    *, family: str, feature: str, coverage: float, evidence: str
) -> dict[str, Any]:
    return {
        "family": family,
        "feature": feature,
        "coverage": round(max(0.0, min(1.0, coverage)), 4),
        "evidence": evidence,
    }


def _dedupe_schema_less_features(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for feature in features:
        key = (str(feature.get("family")), str(feature.get("feature")))
        out.setdefault(key, feature)
    return sorted(out.values(), key=lambda f: (str(f.get("family")), str(f.get("feature"))))


def _polymorphic_feature_manifest(variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for variant in variants:
        disc = variant.get("discriminator") if isinstance(variant, dict) else None
        if not isinstance(disc, dict) or len(disc) != 1:
            continue
        field, _value = next(iter(disc.items()))
        signal = str(variant.get("source_signal", ""))
        if not signal.startswith("polymorphic:"):
            continue
        groups.setdefault(str(field), []).append(variant)
    return [
        _schema_less_feature(
            family="polymorphic",
            feature=field,
            coverage=sum(float(v.get("coverage") or 0) for v in variants),
            evidence=f"polymorphic: {field} has {len(variants)} structural subtypes",
        )
        for field, variants in sorted(groups.items())
    ]


def _sparse_scalar_variant_fields(
    field_counts: dict[str, int], field_samples: dict[str, Any], n_docs: int
) -> list[str]:
    """Top-level missing-key fields are real schema-less variants after NULL->missing.

    Keep a small deterministic cap so prompts stay readable on very sparse schemas while
    still exposing multiple present/missing axes to Phase B.
    """
    if n_docs <= 1:
        return []
    out: list[str] = []
    for field in sorted(field_counts):
        if field.startswith("_"):
            continue
        present = field_counts[field]
        if not (0 < present < n_docs):
            continue
        sample = field_samples.get(field)
        if isinstance(sample, (dict, list)):
            continue
        out.append(field)
        if len(out) >= 6:
            break
    return out


def _polymorphic_variants(docs: list[dict]) -> list[dict[str, Any]]:
    """Detect a value discriminator whose subtypes carry DIFFERENT top-level field sets and
    emit one schema-flex variant per discriminator value.

    Faithful to the data: a field belongs to a subtype only when the data actually populates
    it for that subtype (>=50%). Returns [] unless some subtype genuinely lacks a field that
    another has (true structural polymorphism, not just different values).
    """
    n = len(docs)
    if n < 20:
        return []
    # candidate discriminators: top-level STRING (categorical) fields present in >=90% of docs
    # with 2..6 distinct values. Restricting to strings avoids treating a low-cardinality
    # numeric metric (e.g. amount) as a subtype discriminator.
    present: dict[str, int] = {}
    vals: dict[str, set] = {}
    non_string: set[str] = set()
    for d in docs:
        for k, v in d.items():
            if k == "_id" or isinstance(v, (dict, list)):
                continue
            present[k] = present.get(k, 0) + 1
            if isinstance(v, bool) or not isinstance(v, str):
                non_string.add(k)
            else:
                s = vals.setdefault(k, set())
                if len(s) <= 12:
                    s.add(v)
    candidates = [
        k for k in sorted(present)
        if k not in non_string and present[k] >= 0.9 * n and 2 <= len(vals.get(k, set())) <= 6
    ]
    if not candidates:
        return []
    disc = min(candidates, key=lambda k: len(vals[k]))  # most discriminator-like (fewest values)
    # per-subtype field-presence
    buckets: dict[Any, dict[str, int]] = {}
    sizes: dict[Any, int] = {}
    samples: dict[Any, dict[str, Any]] = {}
    for d in docs:
        dv = d.get(disc)
        if dv is None:
            continue
        sizes[dv] = sizes.get(dv, 0) + 1
        fc = buckets.setdefault(dv, {})
        smp = samples.setdefault(dv, {})
        for f, v in d.items():
            fc[f] = fc.get(f, 0) + 1
            smp.setdefault(f, v)
    subtype_fields = {
        dv: {f for f, c in fc.items() if f not in ("_id", disc) and c >= 0.5 * sizes[dv]}
        for dv, fc in buckets.items()
    }
    union = set().union(*subtype_fields.values()) if subtype_fields else set()
    if not any(fs != union for fs in subtype_fields.values()):
        return []  # all subtypes share the same field set -> not structurally polymorphic
    return [
        {
            "discriminator": {disc: dv},
            "fields": {f: _field_type(samples[dv].get(f)) for f in sorted(subtype_fields[dv])},
            "coverage": round(sizes[dv] / n, 4),
            "source_signal": (
                f"polymorphic: {disc}={dv!r} fields={sorted(subtype_fields[dv])}"
            ),
        }
        for dv in sorted(buckets, key=str)
    ]


def _field_type(value: Any) -> str:
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT"
    if isinstance(value, float):
        return "REAL"
    if isinstance(value, dict):
        return {
            "type": "OBJECT",
            "fields": {k: _field_type(v) for k, v in sorted(value.items())},
        }
    if isinstance(value, list):
        item = next((v for v in value if v is not None), None)
        return {"type": "ARRAY", "items": _field_type(item) if item is not None else "OBJECT"}
    return "TEXT"
