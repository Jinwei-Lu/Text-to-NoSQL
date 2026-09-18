"""Constrained LLM baseline definitions.

The baselines of the final experiments: data-rich Direct (optionally with SAG's six output
conventions), the DIN-SQL-inspired MQL adaptation, SQL Pivot over the real relational DDL,
and the fair informed ReAct loop. They solve released records from public NLQ and sampled
data only, never see gold MQL, and have none of SAG's mechanisms.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from ..errors import SourceError
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

SQL_SCHEMA: JsonMap = {
    "type": "object",
    "required": ["SQL", "notes"],
    "properties": {
        "SQL": {"type": "string", "minLength": 8},
        "notes": {"type": "string"},
    },
    "additionalProperties": False,
}

@dataclass(frozen=True, slots=True)
class BaselinePromptContext:
    record: JsonMap
    witness_digest: JsonMap
    nlq: str
    # When true the baseline prompt carries the same six output conventions SAG's does.
    # Default False keeps every frozen baseline artifact byte-reproducible.
    output_contract: bool = False


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
    # The fair multi-step ReAct arm ("informed") runs the published JSON-action loop with
    # RAW first-N-row observations instead of the fixed `steps` prompt loop.
    react_arm: str | None = None
    # Structural information channel shown to the model (drives disclosure):
    #   "sampled_docs"             — public sampled documents in the prompt
    #   "relational_source_schema" — plus the real relational DDL of the BIRD source
    prompt_channel: str = "sampled_docs"


def baseline_ids() -> tuple[str, ...]:
    return tuple(_BASELINES)


def resolve_baselines(selection: str | list[str] | tuple[str, ...] | None) -> list[BaselineSpec]:
    if selection is None or selection == "all":
        return list(_BASELINES.values())
    parts = selection if isinstance(selection, (list, tuple)) else selection.split(",")
    keys = [str(part).strip() for part in parts if str(part).strip()]
    if not keys:
        raise SourceError(
            "empty baseline selection",
            context={
                "requested_baseline_selection": selection,
                "known_baseline_ids": list(_BASELINES),
            },
        )
    if keys == ["all"]:
        return list(_BASELINES.values())

    specs: list[BaselineSpec] = []
    unknown: list[str] = []
    for key in keys:
        spec = _BASELINES.get(key)
        if spec is None:
            unknown.append(key)
        else:
            specs.append(spec)
    if unknown:
        raise SourceError(
            "unknown baseline selection",
            context={
                "requested_baseline_ids": unknown,
                "known_baseline_ids": list(_BASELINES),
            },
        )
    return specs


# The six output conventions SAG's system prompt states, copied VERBATIM from
# `tend.solver.sag.prompt.system_prompt` (the `Rules:` sentence). The baseline prompts
# state none of them, and no disclosure field records that asymmetry -- so part of any
# measured SAG margin is instruction asymmetry rather than mechanism. None of the six
# belongs to a SAG mechanism claim; they are task conventions, and a baseline that is not
# told them is being compared unfairly.
#
# Identifying the affected records post hoc is not a substitute: only two of the six
# (boolean indicators, verbatim values) leave a detectable trace in the result, so an
# exclusion-based correction systematically undercounts. The honest correction is to give
# the baseline the same instructions and re-measure, which is what this enables.
#
# Kept behind an env flag (TEND_BASELINE_OUTPUT_CONTRACT=1) so the DEFAULT prompt is
# byte-identical to the one that produced every frozen baseline artifact. The prediction
# rows do not record the flag; the final "Direct with SAG's six output conventions" run
# set it for data_rich_direct.
#
# SAG's trailing presence-wrapped-field sentence is deliberately NOT copied: it is derived
# from SAG's induced index and describes structure the baseline is not given.
_OUTPUT_CONTRACT = (
    "Rules: keep the `_id` KEY out of the output rows unless asked; honor 'top/first/up to "
    "N' as $limit N; follow the exact projection fields, sort keys and tie-break order "
    "stated in the question; string matches are EXACT and case-sensitive — copy stored "
    "values verbatim. Output stored values AS-IS: never translate or re-label them unless "
    "the question explicitly defines a label mapping (then use the question's exact label "
    "strings); when the question asks 'whether ...' or for an indicator, output a boolean."
)


def _system(ctx: "BaselinePromptContext", title: str, constraints: str) -> str:
    contract = f"{_OUTPUT_CONTRACT}\n" if getattr(ctx, "output_contract", False) else ""
    return (
        f"# {title}\n"
        "You are a baseline Text-to-NoSQL solver. Produce MongoDB aggregation syntax.\n"
        "No schema is provided. Use only the released natural language question and the "
        "public sampled documents in the prompt; you must infer the collections, fields, "
        "document shape, dynamic-key maps, polymorphic variants, and value domains from "
        "those samples alone. Never use hidden gold queries or evaluation output.\n"
        f"{constraints}\n"
        f"{contract}"
        "Return only the requested JSON object."
    )


# Strict JSON-action contract for the fair multi-step ReAct arm (react_informed).
# Ported verbatim from the published fair-comparison harness.
REACT_ACTION_SCHEMA: JsonMap = {
    "type": "object",
    "required": ["action", "collection", "pipeline"],
    "properties": {
        "action": {"type": "string", "enum": ["execute_mql", "submit"]},
        "collection": {"type": "string"},
        "pipeline": {"type": "array", "items": {"type": "object"}},
    },
    "additionalProperties": False,
}


def build_react_system_prompt(
    db_id: str,
    *,
    steps: int,
    collection_names: list[str] | None = None,
) -> str:
    """System prompt of the fair ReAct arms (verbatim from the measured harness).

    ``collection_names`` is the informed arm's only extra information: the real
    collection-name list (no shapes, no paths). ``None`` degrades to the name-free
    prompt (the informed arm's no-Mongo-handle fallback).
    """
    extra = f" The database's collections are: {collection_names}." if collection_names else ""
    return (
        f"You answer a natural-language question by querying a read-only MongoDB "
        f"database named `{db_id}`.{extra} You interact in steps. At each step return "
        f'STRICT JSON {{"action": "execute_mql"|"submit", "collection": <name>, '
        f'"pipeline": [aggregation stages]}}.\n'
        f"- execute_mql runs the aggregation and shows you the first rows and the "
        f"total row count (use it to explore structure and test your query).\n"
        f"- submit returns your FINAL aggregation; its full result is your answer.\n"
        f"You have at most {steps} steps. The answer is graded by EXACT result-set "
        f"match (values, row order when sorted, exact strings). Suppress _id unless "
        f"asked; honor 'top/first N' with $limit N; copy stored values verbatim."
    )


def _json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n```"


def _base_user(ctx: BaselinePromptContext) -> str:
    lines = [
        "# Released task",
        f"db_id: {ctx.record.get('db_id')}",
        f"record_id: {ctx.record.get('record_id')}",
        "",
        "## Natural language question",
        ctx.nlq,
        "",
        "## Public sampled documents",
        "No schema is provided. These are sampled documents from the database; infer all "
        "structure (collections, fields, dynamic-key maps, polymorphic variants, value "
        "domains) from them.",
        _json_block(ctx.witness_digest),
    ]
    return "\n".join(lines)


# Step ids the runner mirrors into state; see `_mql_user`.
_STEP_SCOPED_KEYS = frozenset({"sql", "mql", "link", "classify", "generate", "correct"})


def _mql_user(
    ctx: BaselinePromptContext,
    state: JsonMap,
    *,
    extra: str = "",
) -> list[Message]:
    body = _base_user(ctx)
    # The runner keeps two copies of each step's output: the flat keys these arms have
    # always read, and a step-scoped copy for arms that need to name which step they mean.
    # Dump only the flat half, or every prompt would carry the same values twice.
    visible = {k: v for k, v in state.items() if k not in _STEP_SCOPED_KEYS}
    if visible:
        body += "\n\n## Prior baseline state\n" + _json_block(visible)
    if extra:
        body += "\n\n## Additional instruction\n" + extra
    body += (
        "\n\nReturn JSON with fields `MQL`, `rationale`, and `assumptions`. "
        "The MQL must be a single `db.<collection>.aggregate([...])` expression."
    )
    return [{"role": "user", "content": body}]


def _data_rich_direct_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    return [
        {"role": "system", "content": _system(
            ctx,
            "Data-rich direct baseline",
            "You are given a larger sample of documents than the direct baseline, but "
            "still no schema and no tools, examples, or execution feedback. Infer the "
            "structure from the larger sample.",
        )},
        *_mql_user(ctx, state),
    ]


LINK_SCHEMA: JsonMap = {
    "type": "object",
    "required": ["collections", "paths"],
    "properties": {
        "collections": {"type": "array", "items": {"type": "string"}},
        "paths": {"type": "array", "items": {"type": "string"}},
        "id_links": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}

CLASSIFY_SCHEMA: JsonMap = {
    "type": "object",
    "required": ["label", "sub_questions"],
    "properties": {
        "label": {"type": "string", "enum": ["easy", "non_nested_complex", "nested_complex"]},
        "sub_questions": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}

# Fixed examples for the DIN-SQL-inspired MQL adaptation. The predicted class selects one
# of three two-example blocks, so the library is fixed but the injected block is
# class-conditional. The examples were written from public MongoDB sample data and are not
# retrieved per question. Disjointness from TEND text is audited outside this loader.
_DINSQL_EXEMPLARS: dict | None = None


def _dinsql_exemplars(label: str) -> str:
    global _DINSQL_EXEMPLARS
    if _DINSQL_EXEMPLARS is None:
        import os as _os

        path = _os.path.join(
            _os.path.dirname(_os.path.abspath(__file__)), "assets",
            "dinsql_mql_exemplars.json",
        )
        with open(path, encoding="utf-8") as fh:
            _DINSQL_EXEMPLARS = json.load(fh)
    blocks = []
    for item in (_DINSQL_EXEMPLARS.get("exemplars") or {}).get(label, []):
        blocks.append(
            f"# database: {item['db']} (MongoDB public sample data)\n"
            f"# question: {item['question']}\n{item['query']}"
        )
    return "\n\n".join(blocks)


def _link_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    body = _base_user(ctx)
    body += (
        "\n\nSchema linking. List only the collections and document paths this question "
        "needs, plus any id-link edges you can see in the data (a field in one collection "
        "holding an identifier that appears in another). Do not write a query yet."
        "\n\nReturn JSON with fields `collections`, `paths`, `id_links`."
    )
    return [
        {"role": "system", "content": _system(
            ctx,
            "DIN-SQL module 1: schema linking",
            "Select the relevant structure only. Do not generate a query.",
        )},
        {"role": "user", "content": body},
    ]


def _dinsql_body(ctx: BaselinePromptContext, link: JsonMap, blocks: list[str]) -> str:
    """Context for the DIN-SQL-inspired modules 2-4.

    Later modules receive complete sampled documents for collections kept by module 1.
    Individual fields are not removed according to the returned path list. If no linked
    collection survives, the implementation falls back to the full witness digest.
    """
    kept = [str(c) for c in (link.get("collections") or [])]
    digest = ctx.witness_digest or {}
    pruned = {k: v for k, v in digest.items() if k in kept} or digest
    parts = [
        "# Released task",
        f"db_id: {ctx.record.get('db_id')}",
        f"record_id: {ctx.record.get('record_id')}",
        "",
        "## Natural language question",
        ctx.nlq,
        "",
        "## Linked structure (module 1: the collections, paths and id-links it kept)",
        json.dumps(link, ensure_ascii=False, indent=1),
        "",
        "## Sampled documents for the linked collections",
        "These are the same public samples the other baselines see, restricted to the "
        "collections module 1 kept. Collection names and paths must come from here.",
        _json_block(pruned),
    ]
    parts.extend(blocks)
    return "\n".join(parts)


def _classify_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    link = state.get("link") or {}
    body = _dinsql_body(ctx, link, [])
    body += (
        "\n\nClassify this question as `easy` (one collection, no nesting to traverse), "
        "`non_nested_complex` (one collection but multi-stage aggregation), or "
        "`nested_complex` (needs traversal of nested arrays/dynamic-key maps or a join "
        "across collections). For the two complex classes, decompose it into ordered "
        "sub-questions."
        "\n\nReturn JSON with fields `label` and `sub_questions`."
    )
    return [
        {"role": "system", "content": _system(
            ctx,
            "DIN-SQL module 2: classification and decomposition",
            "Classify and decompose only. Do not generate a query.",
        )},
        {"role": "user", "content": body},
    ]


def _dinsql_generate_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    link = state.get("link") or {}
    cls = state.get("classify") or {}
    label = str(cls.get("label") or "non_nested_complex")
    subs = cls.get("sub_questions") or []
    blocks = []
    if subs:
        blocks.append("\n## Sub-questions (module 2)\n" + "\n".join(f"- {s}" for s in subs))
    exemplars = _dinsql_exemplars(label)
    if exemplars:
        blocks.append(
            f"\n## Examples of MongoDB queries ({label}; other databases, style only)\n"
            f"{exemplars}"
        )
    body = _dinsql_body(ctx, link, blocks)
    body += (
        "\n\nWrite the MongoDB aggregation for the question, using only the linked paths."
        "\n\nReturn JSON with fields `MQL`, `rationale`, and `assumptions`. "
        "The MQL must be a single `db.<collection>.aggregate([...])` expression."
    )
    return [
        {"role": "system", "content": _system(
            ctx,
            f"DIN-SQL module 3: generation ({label})",
            "Follow the linked structure and the sub-questions. The examples come from "
            "other databases and show query style only.",
        )},
        {"role": "user", "content": body},
    ]


def _dinsql_correct_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    link = state.get("link") or {}
    draft = str((state.get("generate") or {}).get("MQL") or state.get("MQL") or "")
    body = _dinsql_body(ctx, link, [f"\n## Draft query (module 3)\n{draft}"])
    body += (
        "\n\nCheck the draft for missing filters, wrong paths, the wrong grouping level or "
        "unnecessary stages. Return the corrected query, or the draft unchanged if it is "
        "already right."
        "\n\nReturn JSON with fields `MQL`, `rationale`, and `assumptions`. "
        "The MQL must be a single `db.<collection>.aggregate([...])` expression."
    )
    return [
        {"role": "system", "content": _system(
            ctx,
            "DIN-SQL module 4: self-correction",
            "Revise only what is wrong. Do not rewrite a correct query.",
        )},
        {"role": "user", "content": body},
    ]


_RELATIONAL_SCHEMAS: dict[str, str] | None = None


def _relational_schema_for(db_id: str) -> str:
    """The REAL relational DDL of the BIRD source database (reviewer-requested arm).

    Loaded lazily from a repo asset extracted verbatim from the minidev sqlite files;
    raising on a missing db is correct — this arm is meaningless without the schema.
    """
    global _RELATIONAL_SCHEMAS
    if _RELATIONAL_SCHEMAS is None:
        import os as _os

        path = _os.path.join(
            _os.path.dirname(_os.path.abspath(__file__)), "assets",
            "bird_relational_schemas.json",
        )
        with open(path, encoding="utf-8") as fh:
            _RELATIONAL_SCHEMAS = json.load(fh)
    ddl = _RELATIONAL_SCHEMAS.get(str(db_id))
    if not ddl:
        raise SourceError(
            "no relational schema for db", context={"db_id": str(db_id)}
        )
    return ddl


def _sql_schema_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    db_id = str((ctx.record or {}).get("db_id") or "")
    body = _base_user(ctx)
    body += (
        "\n\nThe documents you saw were derived from a relational source database. "
        "Its REAL relational schema (verbatim DDL) is:\n\n"
        + _relational_schema_for(db_id)
        + "\n\nFirst express the intent as ordinary SQL over THIS relational schema. "
        "This SQL is an intermediate sketch only."
        "\n\nReturn JSON with fields `SQL` (the SQL sketch) and `notes` "
        "(assumptions/caveats)."
    )
    return [
        {"role": "system", "content": _system(
            ctx,
            "SQL pivot (real schema) baseline step 1",
            "Write straightforward relational SQL against the provided real schema.",
        )},
        {"role": "user", "content": body},
    ]


def _sql_to_mql_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    return [
        {"role": "system", "content": _system(
            ctx,
            "SQL pivot baseline step 2",
            "Translate the SQL sketch to MongoDB without adding new schema-flex analysis.",
        )},
        *_mql_user(
            ctx,
            state,
            extra="Translate the prior `SQL` sketch into MongoDB aggregation.",
        ),
    ]


_BASELINES: dict[str, BaselineSpec] = {
    "data_rich_direct": BaselineSpec(
        id="data_rich_direct",
        title="Data-rich direct",
        description="One-shot prompt with a larger sample of documents and no schema.",
        steps=(BaselineStep(
            "mql",
            "baseline_data_rich_direct_mql",
            "data-rich direct MQL",
            MQL_SCHEMA,
            _data_rich_direct_messages,
        ),),
        limitations=(
            "one-shot",
            "no schema",
            "larger sample only, no exploration",
            "no execution feedback",
            "no repair",
        ),
    ),
    "dinsql_mql": BaselineSpec(
        id="dinsql_mql",
        title="DIN-SQL-inspired MQL adaptation",
        description=(
            "Task-specific adaptation inspired by the four-stage DIN-SQL decomposition "
            "(Pourreza & Rafiei, NeurIPS 2023): document linking, document-oriented "
            "classification and decomposition, direct MQL generation, and self-correction. "
            "Generation receives two fixed examples selected by the predicted class, with "
            "no per-question retrieval. The relational schema channel, original class "
            "taxonomy, and SQL-specific NatSQL intermediate are replaced rather than "
            "ported unchanged."
        ),
        steps=(
            BaselineStep("link", "baseline_dinsql_link", "schema linking", LINK_SCHEMA, _link_messages),
            BaselineStep(
                "classify",
                "baseline_dinsql_classify",
                "classify and decompose",
                CLASSIFY_SCHEMA,
                _classify_messages,
            ),
            BaselineStep(
                "generate",
                "baseline_dinsql_generate",
                "generate MQL",
                MQL_SCHEMA,
                _dinsql_generate_messages,
            ),
            BaselineStep(
                "correct",
                "baseline_dinsql_correct",
                "self-correction",
                MQL_SCHEMA,
                _dinsql_correct_messages,
            ),
        ),
        limitations=(
            "fixed exemplars from public MongoDB sample data, no retrieval",
            "no NatSQL intermediate representation (SQL-specific)",
            "no execution feedback",
        ),
    ),
    "sql_pivot_schema": BaselineSpec(
        id="sql_pivot_schema",
        title="SQL pivot with the real relational schema",
        description=(
            "Reviewer-requested variant: step 1 drafts SQL against the REAL relational "
            "schema of the BIRD source database (verbatim DDL), step 2 translates that "
            "sketch to MQL. Isolates whether a genuine relational intermediate helps "
            "or hurts document-native reasoning, removing the schema-inference "
            "confound of plain sql_pivot."
        ),
        steps=(
            BaselineStep(
                "sql",
                "baseline_sql_pivot_schema_sql",
                "SQL sketch (real schema)",
                SQL_SCHEMA,
                _sql_schema_messages,
            ),
            BaselineStep(
                "mql",
                "baseline_sql_pivot_schema_mql",
                "SQL-to-MQL",
                MQL_SCHEMA,
                _sql_to_mql_messages,
            ),
        ),
        limitations=(
            "SQL bottleneck",
            "no schema-flex planner",
            "no execution feedback",
            "sees the relational SOURCE schema — a channel no other arm has",
        ),
        prompt_channel="relational_source_schema",
    ),
    "react_informed": BaselineSpec(
        id="react_informed",
        title="Fair ReAct (informed)",
        description=(
            "The fair ReAct loop plus the real collection-name list (no shapes, no "
            "paths) — isolates how much of the SAG gain is mere name disclosure."
        ),
        steps=(),
        limitations=(
            "classic ReAct exploration loop",
            "no induced structure",
            "collection names provided (names only)",
            "raw first-rows observations",
            "bounded step budget",
        ),
        react_arm="informed",
    ),
}


BASELINE_IDS = baseline_ids()
