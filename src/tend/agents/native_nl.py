"""Native NL generator for manifest-driven NL-MQL records."""
from __future__ import annotations

import json
from typing import Any

from .base import AgentContext, LLMAgent, register


_NATIVE_NL_SCHEMA = {
    "type": "object",
    "required": ["nl_queries"],
    "properties": {
        "nl_queries": {
            "type": "object",
            "required": ["canonical", "colloquial"],
            "properties": {
                "canonical": {"type": "string", "minLength": 8},
                "colloquial": {"type": "string", "minLength": 8},
            },
            "additionalProperties": False,
        }
    },
    "additionalProperties": True,
}


@register
class NativeNlGenerator(LLMAgent):
    """Generate user-facing NL for a deterministic native MQL intent."""

    id = "native_nl_generator"
    phase = "B"
    title = "Native NL Generator"
    prompt_file = "native_nl_generator.md"
    output_schema = _NATIVE_NL_SCHEMA

    async def run(self, ctx: AgentContext, inputs: dict[str, Any]) -> dict[str, Any]:
        if ctx.settings.stub:
            pattern = str(inputs.get("query_pattern") or inputs.get("native_query_pattern") or "")
            feature = inputs.get("native_feature") or {}
            feature_type = str(feature.get("type") or inputs.get("feature_type") or "native feature")
            field = str(feature.get("field") or inputs.get("feature_field") or "native field")
            target = _pattern_phrase(pattern, feature_type, field)
            return {
                "nl_queries": {
                    "canonical": f"Find records where {target}.",
                    "colloquial": f"Show me the items with {target}.",
                }
            }
        return await super().run(ctx, inputs)

    def render_inputs(self, ctx: AgentContext, inputs: dict[str, Any]) -> str:
        return (
            "# Native NL generation\n"
            "Write exactly two natural-language questions for the provided native MQL. "
            "The canonical form should be precise; the colloquial form should sound like "
            "a user request. Do not mention SQL, MongoDB operators, or implementation details.\n\n"
            "## Inputs\n"
            f"```json\n{json.dumps(inputs, ensure_ascii=False, indent=2, sort_keys=True, default=str)}\n```"
        )

    def check_contract(
        self, ctx: AgentContext, inputs: dict[str, Any], output: dict[str, Any]
    ) -> list[str]:
        nl = output.get("nl_queries")
        if not isinstance(nl, dict):
            return ["nl_queries must be an object"]
        keys = set(nl)
        if keys != {"canonical", "colloquial"}:
            return ["nl_queries must contain exactly canonical and colloquial"]
        violations = []
        for key, value in nl.items():
            if not isinstance(value, str) or len(value.split()) < 3:
                violations.append(f"{key} NL query is too short")
            if "SQL" in value.upper() or "$" in value:
                violations.append(f"{key} must not mention implementation details")
        return violations


def _pattern_phrase(pattern: str, feature_type: str, field: str) -> str:
    if pattern == "dynamic_key_comparison":
        return f"the dynamic {field} values satisfy the requested comparison"
    if pattern == "subtype_field_dispatch":
        return f"the {feature_type} subtype has the requested subtype-specific field"
    if pattern == "tag_combination":
        return "the derived tags include the requested combination"
    if pattern == "nested_event_filter":
        return "the nested event stream contains the requested event"
    return f"the {feature_type} condition on {field} is met"
