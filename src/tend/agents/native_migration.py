"""LLM-facing helpers for MongoDB-native construction.

Runtime native construction is database-design-code-first. These agents can explain or
surface a recipe, but checked-in per-database design modules remain authoritative.
"""
from __future__ import annotations

import json
from typing import Any

from ..construction.recipe import load_native_recipe, verify_native_recipe
from ..errors import ContractViolationError, MigrationError
from .base import AgentContext, LLMAgent, register


_NATIVE_MIGRATION_SCHEMA = {
    "type": "object",
    "required": ["db_id", "recipe_version", "design_goal", "collections"],
    "properties": {
        "db_id": {"type": "string", "minLength": 1},
        "recipe_version": {"type": "integer", "minimum": 1},
        "design_goal": {"type": "string", "minLength": 20},
        "collections": {"type": "object"},
    },
    "additionalProperties": True,
}


@register
class NativeMigrationDesigner(LLMAgent):
    """Design-assistance agent for native migration recipes."""

    id = "native_migration_designer"
    phase = "A"
    title = "Native Migration Designer"
    prompt_file = "native_migration_designer.md"
    output_schema = _NATIVE_MIGRATION_SCHEMA

    async def run(self, ctx: AgentContext, inputs: dict[str, Any]) -> dict[str, Any]:
        if ctx.settings.stub:
            db_id = str(inputs.get("db_id") or ctx.db_id or "")
            return _stub_recipe(ctx, db_id)
        return await super().run(ctx, inputs)

    def render_inputs(self, ctx: AgentContext, inputs: dict[str, Any]) -> str:
        db_id = str(inputs.get("db_id") or ctx.db_id or "")
        parts = [f"# Native migration recipe design for db_id={db_id}"]
        if ctx.source and db_id:
            schema = ctx.source.schema(db_id)
            parts.append(f"domain: {schema.domain}")
            parts.append("tables: " + ", ".join(schema.tables))
            parts.append(
                "foreign_keys: "
                + "; ".join(
                    f"{fk.child_table}.{fk.child_col}->{fk.parent_table}.{fk.parent_col}"
                    for fk in schema.foreign_keys
                )
            )
            parts.append("columns:")
            for table in schema.tables:
                columns = [
                    f"{col.name}:{col.type}"
                    for col in schema.columns
                    if col.table == table
                ]
                parts.append(f"- {table}: " + ", ".join(columns))
                try:
                    parts.append(f"  row_count: {ctx.source.row_count(db_id, table)}")
                except Exception:  # noqa: BLE001 - row counts are advisory prompt context
                    pass
                sample = _sample_rows(ctx, db_id, table)
                if sample:
                    parts.append("  sample: " + json.dumps(sample, ensure_ascii=False, default=str))
            workload = ctx.source.workload(db_id)
            parts.append(f"workload_examples ({len(workload)} total):")
            for query in workload[:12]:
                parts.append(f"- [{query.difficulty}] {query.question}")
                if query.evidence:
                    parts.append(f"  evidence: {query.evidence[:200]}")
        if inputs.get("design_constraints"):
            parts.append("\n## Constraints\n" + json.dumps(inputs["design_constraints"], ensure_ascii=False))
        parts.append(
            "\nReturn a recipe that uses MongoDB-native structures: polymorphic collections, "
            "dynamic keys, derived tags, attribute bags, versioned fields, or nested event streams. "
            "Every generated or transformed field must include provenance from real source columns."
        )
        return "\n".join(parts)

    def check_contract(
        self, ctx: AgentContext, inputs: dict[str, Any], output: dict[str, Any]
    ) -> list[str]:
        violations: list[str] = []
        try:
            recipe = load_native_recipe(output)
        except Exception as exc:  # noqa: BLE001 - contract diagnostics
            return [f"recipe is not parseable: {exc}"]
        native_types = {
            transform.type
            for collection in recipe.collections.values()
            for transform in collection.transforms
        }
        if not native_types.intersection(
            {
                "polymorphic_union",
                "dynamic_key_object",
                "attribute_bag",
                "derived_tag_array",
                "versioned_document",
                "nested_event_stream",
            }
        ):
            violations.append("recipe lacks MongoDB-native features")
        if native_types.issubset({"optional_embed", "reference_collection"}):
            violations.append("recipe only describes simple embedding/reference structure")
        if ctx.source and recipe.db_id:
            result = verify_native_recipe(recipe, ctx.source.schema(recipe.db_id))
            violations.extend(result.errors)
        return violations


def _stub_recipe(ctx: AgentContext, db_id: str) -> dict[str, Any]:
    try:
        from ..construction.designs.registry import build_native_recipe_for_db

        if ctx.source is not None:
            recipe = build_native_recipe_for_db(ctx.source, db_id)
            return recipe.to_dict()
    except (ImportError, MigrationError):
        pass
    if db_id == "financial":
        return _financial_stub_recipe()
    raise ContractViolationError(
        "native migration designer stub requires a registered native design",
        context={"db_id": db_id},
    )


def _financial_stub_recipe() -> dict[str, Any]:
    return {
        "db_id": "financial",
        "recipe_version": 1,
        "design_goal": "Build native financial entity and account activity documents.",
        "collections": {
            "financial_entities": {
                "purpose": "Polymorphic account and loan entities.",
                "source_tables": ["account", "loan"],
                "transforms": [
                    {
                        "id": "entity_union",
                        "type": "polymorphic_union",
                        "discriminator": "entity_type",
                        "variants": {
                            "account": {
                                "source_table": "account",
                                "fields": {
                                    "entity_id": {
                                        "expr": "concat('account:', account.account_id)",
                                        "provenance": ["account.account_id"],
                                    },
                                    "balance": {"source": "account.balance"},
                                },
                            },
                            "loan": {
                                "source_table": "loan",
                                "fields": {
                                    "entity_id": {
                                        "expr": "concat('loan:', loan.loan_id)",
                                        "provenance": ["loan.loan_id"],
                                    },
                                    "status": {"source": "loan.status"},
                                    "principal": {"source": "loan.amount"},
                                },
                            },
                        },
                    }
                ],
            },
            "account_activity": {
                "purpose": "Monthly account transaction activity.",
                "source_tables": ["account", "trans"],
                "transforms": [
                    {
                        "id": "activity_by_month",
                        "type": "dynamic_key_object",
                        "parent_table": "account",
                        "child_table": "trans",
                        "join": {"left": "account.account_id", "right": "trans.account_id"},
                        "target_field": "activity_by_month",
                        "key": {"expr": "month(trans.date)", "provenance": ["trans.date"]},
                        "values": {
                            "credit": {
                                "expr": "sum(trans.amount where trans.type == 'credit')",
                                "provenance": ["trans.amount", "trans.type"],
                            }
                        },
                    }
                ],
            },
        },
    }


def _sample_rows(ctx: AgentContext, db_id: str, table: str) -> list[dict[str, Any]]:
    try:
        conn = ctx.source.connection(db_id)
        cur = conn.execute(f"SELECT * FROM {_quote_ident(table)} LIMIT 3")
        columns = [item[0] for item in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    except Exception:  # noqa: BLE001 - prompt samples are advisory
        return []


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'
