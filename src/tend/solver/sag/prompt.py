"""Model-facing prompt assembly for the SAG solver.

The system prompt carries the complete induced path card as the decoding
hypothesis space, the closed collection enum, and the output contracts (verbatim
values, _id suppression, top-N limits, exact projection/sort adherence). The text
is mechanism, ported verbatim from the validated prototype — edits here change
measured EX and must be re-benchmarked.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .induction import GroundingIndex

_PRESENCE_LINE = (
    " Presence-wrapped fields: leaves shaped {presence_state, value} store the datum at "
    "`.value` and its availability at `.presence_state` ('present'/'missing'); when the "
    "question says \"defaulting to X if absent\", $ifNull the wrapped `.value` (or the "
    "stated source) to X."
)


def system_prompt(index: "GroundingIndex") -> str:
    colls = list(index.collections)
    presence_line = _PRESENCE_LINE if index.has_presence else ""
    return (
        f"You translate a natural-language question into a MongoDB aggregation over a read-only "
        f'database. Output STRICT JSON: {{"collection": <one of the listed collections>, "pipeline": [...]}}.\n\n'
        f"The database `{index.db_id}` has EXACTLY these {len(colls)} collections: {colls}. There are NO other "
        f"collections and NO separate relational tables; related data is EMBEDDED inside documents, so "
        f"$lookup is almost never needed — it is admissible ONLY between the listed collections along an "
        f"id-link that actually exists in the data (never to any other name). Use ONLY paths from this "
        f"data-induced path map (it is complete — a path not listed does not exist):\n\n{index.card_text}\n\n"
        f"Rules: keep the `_id` KEY out of the output rows unless asked; honor 'top/first/up to "
        f"N' as $limit N; follow the exact "
        f"projection fields, sort keys and tie-break order stated in the question; string matches are "
        f"EXACT and case-sensitive — copy stored values verbatim. Output stored values AS-IS: never "
        f"translate or re-label them unless the question explicitly defines a label mapping (then use "
        f"the question's exact label strings); when the question asks 'whether ...' or for an "
        f"indicator, output a boolean.{presence_line}"
    )


def response_schema(index: "GroundingIndex") -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["collection", "pipeline"],
        "properties": {
            "collection": {"type": "string", "enum": list(index.collections)},
            "pipeline": {"type": "array", "items": {"type": "object"}},
        },
        "additionalProperties": False,
    }


def witness_block(lines: list[str]) -> str:
    if not lines:
        return ""
    return (
        "\n\nValue witnesses (terms of the question located in the ACTUAL data — "
        "filters on these terms must target a witnessed path, exact stored form):\n"
        + "\n".join(lines)
    )
