"""DM — deterministic data migrator (Phase A).

Not an LLM agent: it derives the document-aggregate layout from real BIRD FK structure
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

        plan = build_plan(ctx.source, db_id)
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
            for e in plan.embeds.get(coll, []):
                present = field_counts.get(e.child, 0)
                if 0 < present < len(docs):           # sparse optional embed -> schema_flex
                    variants.extend([
                        {
                            "discriminator": {e.child: "present"},
                            "fields": {e.child: _field_type(field_samples.get(e.child))},
                            "coverage": round(present / n, 4),
                            "source_signal": (
                                f"optional_embed: {e.child} present ({present}/{len(docs)})"
                            ),
                        },
                        {
                            "discriminator": {e.child: "missing"},
                            "fields": {e.child: _field_type(field_samples.get(e.child))},
                            "coverage": round((len(docs) - present) / n, 4),
                            "source_signal": (
                                f"optional_embed: {e.child} missing "
                                f"({len(docs) - present}/{len(docs)})"
                            ),
                        },
                    ])
            if variants:
                node["__variants"] = variants
            collections[coll] = node
        return collections


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
