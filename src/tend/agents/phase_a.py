"""Phase A LLM agents: WP (workload profiler), SRA (rationale), SC (critic).

DM (data migrator) is deterministic and lives in :mod:`tend.agents.dm`. These three drive
the DataWorld context around DM's materialized artifact: WP profiles the real BIRD workload,
SRA records design rationale/context, and SC adversarially reviews DM's actual MongoDB
schema/data plus query-bearing evidence.
"""
from __future__ import annotations

from typing import Any

from .base import AgentContext, LLMAgent, register

# Schemas are intentionally permissive on extra keys (the methodology prompts emit rich
# rationale) but pin the fields the workflow + downstream agents consume.

_WP_SCHEMA = {
    "type": "object",
    "required": ["scenario_summary", "access_patterns", "design_constraints"],
    "properties": {
        "scenario_summary": {"type": "string", "minLength": 20},
        "access_patterns": {"type": "array"},
        "hot_fields": {"type": "array"},
        "co_location_signals": {"type": "array"},
        "join_depth_distribution": {"type": "object"},
        "design_constraints": {"type": "array"},
    },
    "additionalProperties": True,
}

_SRA_SCHEMA = {
    "type": "object",
    "required": ["mongodb_schema", "agent_design_rationale"],
    "properties": {
        "mongodb_schema": {"type": "object"},
        "agent_design_rationale": {"type": "object"},
    },
    "additionalProperties": True,
}

_SC_SCHEMA = {
    "type": "object",
    "required": ["verdict", "query_bearing"],
    "properties": {
        "verdict": {"enum": ["pass", "reject"]},
        "issues": {"type": "array"},
        "coverage_gaps": {"type": "array"},
        "suggested_fixes": {"type": "array"},
        "query_bearing": {"type": "boolean"},
    },
    "additionalProperties": True,
}


@register
class WorkloadProfiler(LLMAgent):
    """WP — profile the BIRD workload into access patterns + a scenario summary."""

    id = "wp"
    phase = "A"
    title = "WP · Workload Profiler"
    prompt_file = "wp_workload_profiler.md"
    output_schema = _WP_SCHEMA

    def render_inputs(self, ctx: AgentContext, inputs: dict[str, Any]) -> str:
        db_id = inputs["db_id"]
        schema = ctx.source.schema(db_id) if ctx.source else None
        workload = ctx.source.workload(db_id) if ctx.source else []
        lines = [f"# WP profiling for db_id={db_id}"]
        if schema:
            lines.append(f"domain: {schema.domain}; tables: {schema.tables}")
            lines.append("foreign_keys: " + "; ".join(
                f"{fk.child_table}.{fk.child_col}->{fk.parent_table}.{fk.parent_col}"
                for fk in schema.foreign_keys))
            lines.append(f"columns (n={len(schema.columns)}); enums: " + ", ".join(
                f"{c.table}.{c.name}" for c in schema.columns if c.has_enum)[:1500])
        lines.append(f"\n## workload sample ({len(workload)} queries)")
        for q in workload[:18]:
            lines.append(f"- [{q.difficulty}] {q.question}")
            if q.sql:
                lines.append(f"    SQL: {q.sql[:200]}")
        lines.append("\nProfile the real join/access patterns and write scenario_summary "
                     "(domain semantics + >=3 business-question patterns; NO SQL/MQL terms).")
        return "\n".join(lines)

    def check_contract(self, ctx, inputs, output) -> list[str]:
        v = []
        if "$" in output.get("scenario_summary", ""):
            v.append("scenario_summary must not contain $ operator terms")
        if len(output.get("access_patterns", [])) == 0:
            v.append("access_patterns must be non-empty (profile the real workload)")
        return v


@register
class SchemaRearchitect(LLMAgent):
    """SRA — record document-aggregate design rationale/context."""

    id = "sra"
    phase = "A"
    title = "SRA · Schema Re-architect"
    prompt_file = "sra_schema_rearchitect.md"
    output_schema = _SRA_SCHEMA

    def render_inputs(self, ctx: AgentContext, inputs: dict[str, Any]) -> str:
        db_id = inputs.get("db_id")
        wp = inputs.get("wp_output", {})
        fixes = inputs.get("sc_fixes")
        parts = [f"# SRA design for db_id={db_id}",
                 "WP scenario: " + str(wp.get("scenario_summary", ""))[:600],
                 "WP design_constraints: " + str(wp.get("design_constraints", []))[:800]]
        if ctx.source and db_id:
            sch = ctx.source.schema(db_id)
            parts.append("BIRD tables: " + ", ".join(sch.tables))
            parts.append("FKs: " + "; ".join(
                f"{fk.child_table}.{fk.child_col}->{fk.parent_table}" for fk in sch.foreign_keys))
        if fixes:
            parts.append("\n## SC requested fixes (revise accordingly):\n" + str(fixes))
        if inputs.get("dm_review_context"):
            parts.append("\n## DM artifact under review (authoritative; revise rationale only):\n"
                         + _clip(inputs["dm_review_context"]))
        parts.append("\nApply the 11-pattern menu (Stage A) then five-mechanism recovery "
                     "(Stage B, real signal only). Emit agent_design_rationale grounded in "
                     "the real BIRD signals. Any mongodb_schema you include is advisory only; "
                     "DM's materialized schema/data are authoritative downstream.")
        return "\n".join(parts)


@register
class SchemaCritic(LLMAgent):
    """SC — adversarial anti-pattern review + query-bearing pre-audit."""

    id = "sc"
    phase = "A"
    title = "SC · Schema Critic"
    prompt_file = "sc_schema_critic.md"
    output_schema = _SC_SCHEMA

    def render_inputs(self, ctx: AgentContext, inputs: dict[str, Any]) -> str:
        schema = inputs.get("mongodb_schema", inputs.get("schema", {}))
        data = inputs.get("mongodb_data", {})
        wp = inputs.get("wp_output", {})
        return (
            "# SC review\n"
            "Review DM's materialized MongoDB schema and witness data as authoritative. "
            "Use SRA rationale only as context; do not rely on a stale SRA schema over "
            "the DM artifacts. Check the 3 anti-patterns (unnecessary collections, "
            "excessive lookups, over-indexing), workload coverage, and decorative "
            "(non query-bearing) heterogeneity against the real query evidence.\n\n"
            f"## DM materialized schema\n```json\n{_clip(schema)}\n```\n"
            f"## DM witness data sample\n```json\n{_clip(_sample_data(data))}\n```\n"
            f"## DM migration log\n```json\n{_clip(inputs.get('migration_log', {}))}\n```\n"
            f"## SRA rationale/context\n```json\n{_clip(inputs.get('sra_rationale', {}))}\n```\n"
            f"## WP access patterns\n{_clip(wp.get('access_patterns', []))}\n"
            f"## Query-bearing evidence\n```json\n{_clip(inputs.get('query_evidence', []))}\n```"
        )


def _clip(obj: Any, n: int = 2500) -> str:
    import json
    s = json.dumps(obj, ensure_ascii=False, indent=2)
    return s if len(s) <= n else s[:n] + "\n... (truncated)"


def _sample_data(data: Any, per_collection: int = 3) -> Any:
    if not isinstance(data, dict):
        return data
    sample = {}
    for coll, docs in data.items():
        sample[coll] = docs[:per_collection] if isinstance(docs, list) else docs
    return sample
