"""Model-facing prompt assembly for the SAG solver.

The system prompt carries the complete induced path card as the decoding
hypothesis space, the closed collection enum, and the output contracts (verbatim
values, _id suppression, top-N limits, exact projection/sort adherence). The text
is mechanism, ported verbatim from the validated prototype — edits here change
measured EX and must be re-benchmarked.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Iterable

from bson.json_util import RELAXED_JSON_OPTIONS, dumps as bson_dumps

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .induction import GroundingIndex
    from .world import WorldAccess

_PRESENCE_LINE = (
    " Presence-wrapped fields: leaves shaped {presence_state, value} store the datum at "
    "`.value` and its availability at `.presence_state` ('present'/'missing'); when the "
    "question says \"defaulting to X if absent\", $ifNull the wrapped `.value` (or the "
    "stated source) to X."
)

_RAW_TRUNCATION_MARKER = "\n...[document prefix truncated]"


@dataclass(frozen=True)
class RawDocumentReceipt:
    """Audit record for one raw-document prefix shown to the decoder."""

    collection: str
    ordinal: int
    full_sha256: str
    visible_prefix_sha256: str
    raw_bytes: int
    visible_prefix_bytes: int
    truncated: bool

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RawDocumentContext:
    """Bounded, non-induced database context for the grounding knockout.

    ``document_text`` is transport-only relaxed Extended JSON.  It deliberately
    carries no derived paths, types, field lists, value summaries, or dynamic-key
    labels.
    """

    db_id: str
    collections: tuple[str, ...]
    document_text: str
    receipts: tuple[RawDocumentReceipt, ...]
    source: str
    docs_per_collection: int
    prefix_bytes: int

    @property
    def stats(self) -> dict[str, int]:
        return {
            "collections": len(self.collections),
            "raw_documents": len(self.receipts),
            "raw_context_chars": len(self.document_text),
            "raw_visible_bytes": sum(r.visible_prefix_bytes for r in self.receipts),
            "raw_truncated_documents": sum(int(r.truncated) for r in self.receipts),
        }


def _front_documents(world: "WorldAccess", collection: str, n: int) -> list[dict[str, Any]]:
    """Read the natural-order front of a collection without spread sampling."""
    if world.can_execute:
        docs = world.aggregate(collection, [{"$limit": int(n)}], max_time_ms=20_000)
    else:
        docs = world.sample_docs(collection, int(n))
    return [doc for doc in docs[: int(n)] if isinstance(doc, dict)]


def build_raw_document_context(
    world: "WorldAccess", *, docs_per_collection: int = 3, prefix_bytes: int = 12_288
) -> RawDocumentContext:
    """Freeze the first raw documents of every collection as bounded UTF-8 prefixes.

    The full relaxed-Extended-JSON serialization is hashed before truncation.  The
    visible hash covers exactly the valid UTF-8 prefix shown to the model (the
    human-readable truncation marker is not part of that hash).
    """
    if docs_per_collection < 1 or prefix_bytes < 1:
        raise ValueError("docs_per_collection and prefix_bytes must be >= 1")
    collections = tuple(sorted(world.list_collections()))
    blocks: list[str] = []
    receipts: list[RawDocumentReceipt] = []
    for collection in collections:
        rendered: list[str] = [f"Collection `{collection}`:"]
        docs = _front_documents(world, collection, docs_per_collection)
        if not docs:
            rendered.append("(no documents in the bounded front sample)")
        for ordinal, doc in enumerate(docs, start=1):
            raw_text = bson_dumps(
                doc,
                json_options=RELAXED_JSON_OPTIONS,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            raw = raw_text.encode("utf-8")
            bounded = raw[:prefix_bytes]
            # Never expose an invalid partial UTF-8 code point.
            visible_text = bounded.decode("utf-8", errors="ignore")
            visible = visible_text.encode("utf-8")
            truncated = len(visible) < len(raw)
            receipts.append(
                RawDocumentReceipt(
                    collection=collection,
                    ordinal=ordinal,
                    full_sha256=hashlib.sha256(raw).hexdigest(),
                    visible_prefix_sha256=hashlib.sha256(visible).hexdigest(),
                    raw_bytes=len(raw),
                    visible_prefix_bytes=len(visible),
                    truncated=truncated,
                )
            )
            rendered.append(
                f"Document {ordinal} (raw relaxed Extended JSON prefix):\n"
                + visible_text
                + (_RAW_TRUNCATION_MARKER if truncated else "")
            )
        blocks.append("\n".join(rendered))
    return RawDocumentContext(
        db_id=str(world.db_id),
        collections=collections,
        document_text="\n\n".join(blocks),
        receipts=tuple(receipts),
        source="mongo" if world.can_execute else "local",
        docs_per_collection=int(docs_per_collection),
        prefix_bytes=int(prefix_bytes),
    )


def system_prompt(index: "GroundingIndex | RawDocumentContext") -> str:
    if isinstance(index, RawDocumentContext):
        colls = list(index.collections)
        return (
            f"You translate a natural-language question into a MongoDB aggregation over a read-only "
            f'database. Output STRICT JSON: {{"collection": <one of the listed collections>, "pipeline": [...]}}.\n\n'
            f"The database `{index.db_id}` has EXACTLY these {len(colls)} collections: {colls}. There are NO other "
            f"collections and NO separate relational tables; related data is EMBEDDED inside documents. "
            f"Use the following bounded prefixes of the first {index.docs_per_collection} raw documents "
            f"from each collection as the database context. A truncated prefix does not imply that later "
            f"fields are absent.\n\n{index.document_text}\n\n"
            f"Rules: keep the `_id` KEY out of the output rows unless asked; honor 'top/first/up to "
            f"N' as $limit N; follow the exact projection fields, sort keys and tie-break order stated "
            f"in the question; string matches are EXACT and case-sensitive — copy stored values "
            f"verbatim. Output stored values AS-IS: never translate or re-label them unless the "
            f"question explicitly defines a label mapping (then use the question's exact label "
            f"strings); when the question asks 'whether ...' or for an indicator, output a boolean."
        )
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


def response_schema(
    collections: "GroundingIndex | RawDocumentContext | Iterable[str]",
) -> dict[str, Any]:
    """Return the unchanged response contract from only collection names.

    Accepting context objects remains backward compatible, while raw-context
    callers do not need to fabricate a :class:`GroundingIndex`.
    """
    values = (
        list(collections.collections)
        if hasattr(collections, "collections")
        else [str(collection) for collection in collections]
    )
    return {
        "type": "object",
        "required": ["collection", "pipeline"],
        "properties": {
            "collection": {"type": "string", "enum": values},
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
