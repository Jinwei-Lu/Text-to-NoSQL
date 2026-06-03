"""Constrained LLM baseline definitions.

The baselines are intentionally competent but bounded: they solve released records from
public NLQ/schema/data only, do not use gold MQL, and avoid the SMART solver's stronger
shape-specific planning plus per-stage execution feedback.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from ..llm import Message

JsonMap = dict[str, Any]
PromptBuilder = Callable[["BaselinePromptContext", JsonMap], list[Message]]


MQL_SCHEMA: JsonMap = {
    "type": "object",
    "required": ["MQL", "rationale"],
    "properties": {
        "MQL": {"type": "string", "minLength": 8},
        "rationale": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}

PLAN_SCHEMA: JsonMap = {
    "type": "object",
    "required": ["target_collection", "steps", "risks"],
    "properties": {
        "target_collection": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}

SQL_SCHEMA: JsonMap = {
    "type": "object",
    "required": ["SQL", "notes"],
    "properties": {
        "SQL": {"type": "string", "minLength": 8},
        "notes": {"type": "string"},
    },
    "additionalProperties": False,
}

REACT_SCHEMA: JsonMap = {
    "type": "object",
    "required": ["thoughts", "needed_observations"],
    "properties": {
        "thoughts": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "needed_observations": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class BaselinePromptContext:
    record: JsonMap
    schema: JsonMap
    witness_digest: JsonMap
    schema_summary: JsonMap
    nlq: str


@dataclass(frozen=True, slots=True)
class BaselineStep:
    id: str
    agent: str
    title: str
    schema: JsonMap
    build_messages: PromptBuilder


@dataclass(frozen=True, slots=True)
class BaselineSpec:
    id: str
    title: str
    description: str
    steps: tuple[BaselineStep, ...]
    limitations: tuple[str, ...]


def baseline_ids() -> tuple[str, ...]:
    return tuple(_BASELINES)


def resolve_baselines(selection: str | list[str] | tuple[str, ...] | None) -> list[BaselineSpec]:
    if selection is None or selection == "all":
        return list(_BASELINES.values())
    parts = selection if isinstance(selection, (list, tuple)) else selection.split(",")
    specs: list[BaselineSpec] = []
    unknown: list[str] = []
    for part in parts:
        key = str(part).strip()
        if not key:
            continue
        spec = _BASELINES.get(key)
        if spec is None:
            unknown.append(key)
        else:
            specs.append(spec)
    if unknown:
        raise KeyError(f"unknown baselines: {unknown}; known={list(_BASELINES)}")
    return specs


def _system(title: str, constraints: str) -> str:
    return (
        f"# {title}\n"
        "You are a baseline Text-to-NoSQL solver. Produce MongoDB aggregation syntax.\n"
        "Use only the released natural language question, public schema, and public sample "
        "digest provided in the prompt. Never use hidden gold queries or evaluation output.\n"
        f"{constraints}\n"
        "Return only the requested JSON object."
    )


def _json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n```"


def _base_user(
    ctx: BaselinePromptContext,
    *,
    include_full_schema: bool,
    include_witness: bool,
) -> str:
    schema_payload = ctx.schema if include_full_schema else ctx.schema_summary
    lines = [
        "# Released task",
        f"db_id: {ctx.record.get('db_id')}",
        f"record_id: {ctx.record.get('record_id')}",
        "",
        "## Natural language question",
        ctx.nlq,
        "",
        "## Public schema",
        _json_block(schema_payload),
    ]
    if include_witness:
        lines += ["", "## Public witness digest", _json_block(ctx.witness_digest)]
    return "\n".join(lines)


def _mql_user(
    ctx: BaselinePromptContext,
    state: JsonMap,
    *,
    include_full_schema: bool,
    include_witness: bool,
    extra: str = "",
) -> list[Message]:
    body = _base_user(
        ctx,
        include_full_schema=include_full_schema,
        include_witness=include_witness,
    )
    if state:
        body += "\n\n## Prior baseline state\n" + _json_block(state)
    if extra:
        body += "\n\n## Additional instruction\n" + extra
    body += (
        "\n\nReturn JSON with fields `MQL`, `rationale`, and `assumptions`. "
        "The MQL must be a single `db.<collection>.aggregate([...])` expression."
    )
    return [{"role": "user", "content": body}]


def _direct_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    return [
        {"role": "system", "content": _system(
            "Direct NL-to-MQL baseline",
            "Make a one-shot translation. Do not explicitly reason through shape variants.",
        )},
        *_mql_user(ctx, state, include_full_schema=False, include_witness=False),
    ]


def _schema_direct_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    return [
        {"role": "system", "content": _system(
            "Schema-aware direct baseline",
            "Use the public schema, but do not ask for tools, examples, or execution feedback.",
        )},
        *_mql_user(ctx, state, include_full_schema=True, include_witness=False),
    ]


def _sql_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    body = _base_user(ctx, include_full_schema=True, include_witness=False)
    body += (
        "\n\nFirst express the intent as ordinary SQL over the relational-style public "
        "schema. This SQL is an intermediate sketch only."
        "\n\nReturn JSON with fields `SQL` (the SQL sketch) and `notes` "
        "(assumptions/caveats)."
    )
    return [
        {"role": "system", "content": _system(
            "SQL pivot baseline step 1",
            "Prefer straightforward relational SQL even when it loses document-shape nuance.",
        )},
        {"role": "user", "content": body},
    ]


def _sql_to_mql_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    return [
        {"role": "system", "content": _system(
            "SQL pivot baseline step 2",
            "Translate the SQL sketch to MongoDB without adding new schema-flex analysis.",
        )},
        *_mql_user(
            ctx,
            state,
            include_full_schema=True,
            include_witness=False,
            extra="Translate the prior `SQL` sketch into MongoDB aggregation.",
        ),
    ]


def _plan_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    body = _base_user(ctx, include_full_schema=True, include_witness=False)
    body += (
        "\n\nReturn a compact query plan, not MQL."
        "\n\nReturn JSON with fields `target_collection` (string), `steps` (array of "
        "strings), and `risks` (array of strings)."
    )
    return [
        {"role": "system", "content": _system(
            "Plan-then-query baseline step 1",
            "Create a concise plan. Do not perform execution, mutation, or per-stage checks.",
        )},
        {"role": "user", "content": body},
    ]


def _plan_to_mql_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    return [
        {"role": "system", "content": _system(
            "Plan-then-query baseline step 2",
            "Translate the plan to MQL exactly once. Do not self-debug.",
        )},
        *_mql_user(
            ctx,
            state,
            include_full_schema=True,
            include_witness=False,
            extra="Translate the prior plan into MongoDB aggregation.",
        ),
    ]


def _react_reason_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    body = _base_user(ctx, include_full_schema=True, include_witness=False)
    body += (
        "\n\nSimulate one ReAct thought step. The next step will provide the full public "
        "schema and witness digest as the single observation packet; no execution or "
        "gold-query observations are available."
    )
    return [
        {"role": "system", "content": _system(
            "Pure ReAct-lite baseline step 1",
            "Use exactly one thought/observation planning turn before final MQL.",
        )},
        {"role": "user", "content": body},
    ]


def _react_mql_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    observation = {
        "available_public_observations": {
            "schema_summary": ctx.schema_summary,
            "witness_digest": ctx.witness_digest,
        }
    }
    merged = {**state, **observation}
    return [
        {"role": "system", "content": _system(
            "Pure ReAct-lite baseline step 2",
            "Use the single public observation packet. Do not run tools or execute MQL.",
        )},
        *_mql_user(
            ctx,
            merged,
            include_full_schema=True,
            include_witness=False,
            extra="Produce the final MQL after one ReAct-style thought and observation.",
        ),
    ]


def _draft_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    return [
        {"role": "system", "content": _system(
            "Static self-debug baseline step 1",
            "Draft MQL directly. You will receive only static syntax feedback later.",
        )},
        *_mql_user(ctx, state, include_full_schema=True, include_witness=False),
    ]


def _repair_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    return [
        {"role": "system", "content": _system(
            "Static self-debug baseline step 2",
            "Revise once using only static parser/operator feedback, not execution feedback.",
        )},
        *_mql_user(
            ctx,
            state,
            include_full_schema=True,
            include_witness=False,
            extra="Return the repaired MQL. Preserve the original intent.",
        ),
    ]


_BASELINES: dict[str, BaselineSpec] = {
    "direct": BaselineSpec(
        id="direct",
        title="Direct NL-to-MQL",
        description="One-shot prompt with NLQ and compact schema summary.",
        steps=(
            BaselineStep("mql", "baseline_direct_mql", "direct MQL", MQL_SCHEMA, _direct_messages),
        ),
        limitations=("one-shot", "compact schema only", "no data samples", "no repair"),
    ),
    "schema_direct": BaselineSpec(
        id="schema_direct",
        title="Schema-aware direct",
        description="One-shot prompt with full public schema.",
        steps=(BaselineStep(
            "mql",
            "baseline_schema_direct_mql",
            "schema direct MQL",
            MQL_SCHEMA,
            _schema_direct_messages,
        ),),
        limitations=("one-shot", "no witness data", "no execution feedback", "no repair"),
    ),
    "sql_pivot": BaselineSpec(
        id="sql_pivot",
        title="SQL pivot workflow",
        description="LLM drafts a SQL sketch, then translates that sketch to MQL.",
        steps=(
            BaselineStep("sql", "baseline_sql_pivot_sql", "SQL sketch", SQL_SCHEMA, _sql_messages),
            BaselineStep(
                "mql",
                "baseline_sql_pivot_mql",
                "SQL-to-MQL",
                MQL_SCHEMA,
                _sql_to_mql_messages,
            ),
        ),
        limitations=("SQL bottleneck", "no schema-flex planner", "no execution feedback"),
    ),
    "plan_then_mql": BaselineSpec(
        id="plan_then_mql",
        title="Plan then MQL",
        description="LLM writes a compact plan, then converts it to MQL once.",
        steps=(
            BaselineStep(
                "plan",
                "baseline_plan_then_mql_plan",
                "query plan",
                PLAN_SCHEMA,
                _plan_messages,
            ),
            BaselineStep(
                "mql",
                "baseline_plan_then_mql_mql",
                "plan-to-MQL",
                MQL_SCHEMA,
                _plan_to_mql_messages,
            ),
        ),
        limitations=("single plan", "no self-debug", "no per-stage checks"),
    ),
    "react_lite": BaselineSpec(
        id="react_lite",
        title="Pure ReAct-lite",
        description="One thought/observation turn followed by final MQL.",
        steps=(
            BaselineStep(
                "think",
                "baseline_react_lite_think",
                "ReAct thought",
                REACT_SCHEMA,
                _react_reason_messages,
            ),
            BaselineStep(
                "mql",
                "baseline_react_lite_mql",
                "ReAct final MQL",
                MQL_SCHEMA,
                _react_mql_messages,
            ),
        ),
        limitations=("one ReAct turn", "schema/sample observations only", "no execution tools"),
    ),
    "static_self_debug": BaselineSpec(
        id="static_self_debug",
        title="Static self-debug",
        description="LLM drafts MQL, receives static parser/operator feedback, repairs once.",
        steps=(
            BaselineStep(
                "draft",
                "baseline_static_self_debug_draft",
                "draft MQL",
                MQL_SCHEMA,
                _draft_messages,
            ),
            BaselineStep(
                "repair",
                "baseline_static_self_debug_repair",
                "static repair",
                MQL_SCHEMA,
                _repair_messages,
            ),
        ),
        limitations=("one repair", "static feedback only", "no execution feedback"),
    ),
}


BASELINE_IDS = baseline_ids()
