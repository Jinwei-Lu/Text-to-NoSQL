"""Constrained LLM baseline definitions.

The baselines are intentionally competent but bounded: they solve released records from
public NLQ/schema/data only, do not use gold MQL, and avoid the SMART solver's stronger
shape-specific planning plus per-stage execution feedback.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
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

# Permissive parse schema for step 1; normalize_react_think_output maps aliases afterward.
REACT_THINK_PARSE_SCHEMA: JsonMap = {
    "type": "object",
    "properties": {
        "thoughts": {"type": "array", "items": {"type": "string"}},
        "thought": {"type": "string"},
        "needed_observations": {"type": "array", "items": {"type": "string"}},
        "needed_observation": {"type": "string"},
        "observations": {"type": "array", "items": {"type": "string"}},
        "observation": {"type": "string"},
        "mql": {"type": "string"},
        "MQL": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "additionalProperties": True,
}


def normalize_react_think_output(raw: JsonMap) -> JsonMap:
    """Accept common react-lite step-1 key aliases without changing strategy shape."""
    thoughts = raw.get("thoughts")
    if not isinstance(thoughts, list):
        single = raw.get("thought")
        if isinstance(single, str) and single.strip():
            thoughts = [single]
        elif isinstance(single, list):
            thoughts = [str(item) for item in single]
        else:
            thoughts = []
    else:
        thoughts = [str(item) for item in thoughts]

    needed = raw.get("needed_observations")
    if not isinstance(needed, list):
        needed = []
        for alias in ("observations", "needed_observation", "observation"):
            alt = raw.get(alias)
            if isinstance(alt, list):
                needed = [str(item) for item in alt]
                break
            if isinstance(alt, str) and alt.strip():
                needed = [alt]
                break
    else:
        needed = [str(item) for item in needed]

    return {"thoughts": thoughts, "needed_observations": needed}


@dataclass(frozen=True, slots=True)
class BaselinePromptContext:
    record: JsonMap
    schema: JsonMap
    witness_digest: JsonMap
    schema_summary: JsonMap
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
    # Agentic baselines run a bounded `execute_mql` ReAct tool-call loop against a read-only
    # Mongo handle (self-acquiring structure) instead of the fixed `steps` prompt loop. They
    # still lack the reference SAG solver's mechanisms (induced path card, value witnesses,
    # alignment gate, execution-grounded repair, consistency vote).
    agentic: bool = False
    # The fair multi-step ReAct arms ("naive" / "informed") run the published JSON-action
    # loop with RAW first-N-row observations — the paper's comparison baselines.
    react_arm: str | None = None
    # Structural information channel shown to the model (drives disclosure and whether the
    # zero-witness preprocess probe may run):
    #   "sampled_docs"  — public sampled documents in the prompt (the legacy channel)
    #   "nlq_only"      — nothing but the question (floor + prior-contamination probe)
    #   "public_schema" — the sanitized released schema file instead of documents
    prompt_channel: str = "sampled_docs"
    # k>1 turns a non-agentic step strategy into a compute-matched self-consistency arm:
    # k independent decodes, candidates executed read-only and clustered by result
    # equivalence with the SAME rule as SAG v3 (largest cluster wins). Built via the
    # dynamic `sc<k>_<base_id>` selection, never as a static registry entry.
    consistency_k: int = 1


def baseline_ids() -> tuple[str, ...]:
    return tuple(_BASELINES)


def _self_consistency_spec(key: str) -> BaselineSpec | None:
    """Resolve a dynamic ``sc<k>_<base_id>`` self-consistency arm (e.g. ``sc3_data_rich_direct``).

    Compute-matched contrast for the solver's k-sample consistency: k independent decodes
    of the SAME single-shot strategy, clustered by result equivalence with SAG v3's rule.
    Only non-agentic step strategies are wrappable (the agent arms own their loop budget).
    """
    if not key.startswith("sc") or "_" not in key:
        return None
    k_text, _, base_id = key.partition("_")
    try:
        k = int(k_text[2:])
    except ValueError:
        return None
    base = _BASELINES.get(base_id)
    if base is None or base.agentic or base.react_arm or k < 2:
        return None
    return replace(
        base,
        id=key,
        title=f"{base.title} ×{k} self-consistency",
        description=(
            f"{base.description} Run {k}× independently; candidates execute read-only "
            "and the largest result-equivalence cluster wins (SAG v3's clustering rule)."
        ),
        limitations=(*base.limitations, f"k={k} result-space self-consistency"),
        consistency_k=k,
    )


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
        spec = _BASELINES.get(key) or _self_consistency_spec(key)
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
                "dynamic_forms": ["sc<k>_<non_agentic_baseline_id>"],
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
# Kept behind an env flag so the DEFAULT prompt is byte-identical to the one that produced
# every frozen baseline artifact -- those runs must stay reproducible. Only the fair arm
# opts in, and the prediction rows record which contract was used.
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


def _mql_user(
    ctx: BaselinePromptContext,
    state: JsonMap,
    *,
    extra: str = "",
) -> list[Message]:
    body = _base_user(ctx)
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
            ctx,
            "Direct NL-to-MQL baseline",
            "Make a one-shot translation from a small sample of documents. Do not "
            "explicitly reason through shape variants.",
        )},
        *_mql_user(ctx, state),
    ]


def _nlq_only_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    """Floor arm: nothing but the question — no documents, no schema, no probe.

    Whatever this arm scores measures prior knowledge of the world (e.g. BIRD-derived
    name guessability), so it doubles as the contamination probe of the experiment
    design (§3.2/§6.4)."""
    system = (
        "# NLQ-only floor baseline\n"
        "You are a baseline Text-to-NoSQL solver. Produce MongoDB aggregation syntax.\n"
        "You receive ONLY the natural language question: no schema, no sampled "
        "documents, no database access. You must guess the collection name, field "
        "paths, and stored value forms outright. Never use hidden gold queries or "
        "evaluation output.\n"
        "Return only the requested JSON object."
    )
    body = "\n".join(
        [
            "# Released task",
            f"db_id: {ctx.record.get('db_id')}",
            f"record_id: {ctx.record.get('record_id')}",
            "",
            "## Natural language question",
            ctx.nlq,
            "",
            "No schema, documents, or database access are provided.",
        ]
    )
    if state:
        body += "\n\n## Prior baseline state\n" + _json_block(state)
    body += (
        "\n\nReturn JSON with fields `MQL`, `rationale`, and `assumptions`. "
        "The MQL must be a single `db.<collection>.aggregate([...])` expression."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": body},
    ]


def _schema_direct_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    """Released-schema arm: the sanitized public schema file replaces sampled documents —
    the direct contrast between the benchmark's distributed schema and SAG's induced card."""
    system = (
        "# Released-schema direct baseline\n"
        "You are a baseline Text-to-NoSQL solver. Produce MongoDB aggregation syntax.\n"
        "You receive the released PUBLIC schema file of the database (sanitized) and "
        "the question — no sampled documents and no database access. Infer document "
        "shapes, dynamic-key maps, and value forms from the schema alone. Never use "
        "hidden gold queries or evaluation output.\n"
        "Return only the requested JSON object."
    )
    body = "\n".join(
        [
            "# Released task",
            f"db_id: {ctx.record.get('db_id')}",
            f"record_id: {ctx.record.get('record_id')}",
            "",
            "## Natural language question",
            ctx.nlq,
            "",
            "## Released public schema (sanitized)",
            _json_block(ctx.schema),
        ]
    )
    if state:
        body += "\n\n## Prior baseline state\n" + _json_block(state)
    body += (
        "\n\nReturn JSON with fields `MQL`, `rationale`, and `assumptions`. "
        "The MQL must be a single `db.<collection>.aggregate([...])` expression."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": body},
    ]


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


def _sql_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    body = _base_user(ctx)
    body += (
        "\n\nFirst express the intent as ordinary SQL over the structure you infer from "
        "the sampled documents. This SQL is an intermediate sketch only."
        "\n\nReturn JSON with fields `SQL` (the SQL sketch) and `notes` "
        "(assumptions/caveats)."
    )
    return [
        {"role": "system", "content": _system(
            ctx,
            "SQL pivot baseline step 1",
            "Prefer straightforward relational SQL even when it loses document-shape nuance.",
        )},
        {"role": "user", "content": body},
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

# DIN-SQL (Pourreza & Rafiei, NeurIPS 2023) exemplars, ported to MQL. The originals are
# hand-written Spider SQL; ours are FIXED, identical for every question, and drawn only
# from MongoDB's public Atlas sample databases -- never from a TEND database, question or
# reference answer. Nothing here is retrieved per question: this arm is not RAG.
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
    """Context for DIN-SQL modules 2-4.

    Module 1 reads the sampled documents and decides what matters; the later modules see
    that pruned structure instead of the full document dump. That is the paper's design,
    and it keeps these prompts small — passing the whole sample to every module produced
    ~96k-token requests and provider timeouts.
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


def _plan_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    body = _base_user(ctx)
    body += (
        "\n\nReturn a compact query plan, not MQL."
        "\n\nReturn JSON with fields `target_collection` (string), `steps` (array of "
        "strings), and `risks` (array of strings)."
    )
    return [
        {"role": "system", "content": _system(
            ctx,
            "Plan-then-query baseline step 1",
            "Create a concise plan. Do not perform execution, mutation, or per-stage checks.",
        )},
        {"role": "user", "content": body},
    ]


def _plan_to_mql_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    return [
        {"role": "system", "content": _system(
            ctx,
            "Plan-then-query baseline step 2",
            "Translate the plan to MQL exactly once. Do not self-debug.",
        )},
        *_mql_user(
            ctx,
            state,
            extra="Translate the prior plan into MongoDB aggregation.",
        ),
    ]


def _react_reason_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    body = _base_user(ctx)
    body += (
        "\n\nSimulate one ReAct thought step. The next step will provide the sampled "
        "documents as the single observation packet; no execution or gold-query "
        "observations are available."
    )
    return [
        {"role": "system", "content": _system(
            ctx,
            "Pure ReAct-lite baseline step 1",
            "Use exactly one thought/observation planning turn before final MQL.",
        )},
        {"role": "user", "content": body},
    ]


def _react_mql_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    return [
        {"role": "system", "content": _system(
            ctx,
            "Pure ReAct-lite baseline step 2",
            "The sampled documents below are your single observation packet. Do not run "
            "tools or execute MQL.",
        )},
        *_mql_user(
            ctx,
            state,
            extra="Produce the final MQL after one ReAct-style thought and observation.",
        ),
    ]


def _draft_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    return [
        {"role": "system", "content": _system(
            ctx,
            "Static self-debug baseline step 1",
            "Draft MQL directly. You will receive only static syntax feedback later.",
        )},
        *_mql_user(ctx, state),
    ]


def _repair_messages(ctx: BaselinePromptContext, state: JsonMap) -> list[Message]:
    return [
        {"role": "system", "content": _system(
            ctx,
            "Static self-debug baseline step 2",
            "Revise once using only static parser/operator feedback, not execution feedback.",
        )},
        *_mql_user(
            ctx,
            state,
            extra="Return the repaired MQL. Preserve the original intent.",
        ),
    ]


_BASELINES: dict[str, BaselineSpec] = {
    "direct_nlq_only": BaselineSpec(
        id="direct_nlq_only",
        title="NLQ-only floor",
        description=(
            "One-shot prompt with NOTHING but the question: no documents, no schema, "
            "no probe. The structural-information floor and the prior-contamination "
            "probe (any score above zero quantifies world guessability)."
        ),
        steps=(
            BaselineStep(
                "mql",
                "baseline_direct_nlq_only_mql",
                "NLQ-only MQL",
                MQL_SCHEMA,
                _nlq_only_messages,
            ),
        ),
        limitations=(
            "one-shot",
            "no schema",
            "no sampled documents",
            "no database access",
            "no repair",
        ),
        prompt_channel="nlq_only",
    ),
    "schema_direct": BaselineSpec(
        id="schema_direct",
        title="Released-schema direct",
        description=(
            "One-shot prompt with the sanitized released schema file instead of "
            "sampled documents — does the benchmark's distributed schema match the "
            "induced card as a hypothesis space?"
        ),
        steps=(
            BaselineStep(
                "mql",
                "baseline_schema_direct_mql",
                "schema-direct MQL",
                MQL_SCHEMA,
                _schema_direct_messages,
            ),
        ),
        limitations=(
            "one-shot",
            "public schema file only (sanitized)",
            "no sampled documents",
            "no execution feedback",
            "no repair",
        ),
        prompt_channel="public_schema",
    ),
    "direct": BaselineSpec(
        id="direct",
        title="Direct NL-to-MQL",
        description="One-shot prompt with NLQ and a small sample of documents.",
        steps=(
            BaselineStep("mql", "baseline_direct_mql", "direct MQL", MQL_SCHEMA, _direct_messages),
        ),
        limitations=("one-shot", "no schema", "small document sample only", "no repair"),
    ),
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
    "dinsql_mql": BaselineSpec(
        id="dinsql_mql",
        title="DIN-SQL adapted to MQL",
        description=(
            "Reviewer-requested external pipeline (Pourreza & Rafiei, NeurIPS 2023) "
            "ported to MongoDB: schema linking, classification and decomposition, "
            "generation, self-correction. Exemplars are FIXED (identical for every "
            "question, no retrieval) and come only from MongoDB's public Atlas sample "
            "databases, never from a TEND database, question or reference answer. The "
            "SQL-specific NatSQL intermediate is dropped; MQL is generated directly."
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
                REACT_THINK_PARSE_SCHEMA,
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
