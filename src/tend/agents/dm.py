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

        ctx.log.info("dm_migrated", db_id=db_id,
                     collections={c: len(d) for c, d in data.items()},
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
            # polymorphic variants: a value discriminator whose subtypes carry DIFFERENT field
            # sets (real, e.g. trans VYBER lacks k_symbol/bank/account) — a second structural
            # heterogeneity axis beyond optional embeds, diversifying the schema-less set.
            if not variants:
                variants.extend(_polymorphic_variants(docs))
            if variants:
                node["__variants"] = variants
            collections[coll] = node
        return collections


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
