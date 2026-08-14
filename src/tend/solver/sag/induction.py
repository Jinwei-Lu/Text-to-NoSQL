"""Offline structure induction: the lattice, the path card, and the value index.

Schema-Data Duality: in a schema-less world the only structure is the structure the
data witnesses. One bounded sample pass per collection feeds (a) the path lattice
(every path with its dominant type, dynamic-key maps collapsed to ``<*>``) and
(b) the value index (every bounded string value and dynamic KEY, with its exact
stored form). The rendered "path card" is the complete decoding hypothesis space
handed to the model; the value index grounds NLQ literals (see ``witness.py``).

Faithful port of the validated prototype (financial 61/110, gate 0 false positives
on all 1210 golds). Threshold constants and rendering text are mechanism, not
style — do not "clean them up" without re-measuring.

Note: the prototype's ``live_value_exists``/``_VAL_CACHE`` helper was dead code
(defined, never called) and is intentionally not ported.
"""
from __future__ import annotations

import collections
import os as _os
import re
from dataclasses import dataclass, field
from typing import Any

from .witness import vnorm
from .world import WorldAccess

RE_DATE = re.compile(r"^\d{4}[-/]\d{2}")
RE_CODE = re.compile(r"^[A-Z][A-Z0-9_:]{2,}$")
RE_SHORT = re.compile(r"^[A-Z]$")
RE_FIELD = re.compile(r"^[a-z][a-z0-9_]*$")

_WALK_MAX_DEPTH = 7
_WALK_LIST_CAP = 8
_VALUE_WALK_LIST_CAP = 50
_VALUE_INDEX_LOC_CAP = 20
_VALUE_MIN_LEN, _VALUE_MAX_LEN = 2, 60


def keylooks_data(k: str) -> bool:
    """Does this object key look like a data value (date / code / single capital)?"""
    return bool(RE_DATE.match(k) or RE_CODE.match(k) or RE_SHORT.match(k))


RE_NUM = re.compile(r"^\d{1,4}$")
KEYS_V2 = _os.environ.get("TEND_SAG_KEYS_V2", "1").strip().lower() not in {"0", "false", "no"}


def _member_shape(v: Any) -> tuple:
    """Coarse shape of a map member, for asking whether siblings are parallel."""
    if isinstance(v, dict):
        return ("d",) + tuple(sorted(v)[:6])
    if isinstance(v, list):
        return ("l",) + (_member_shape(v[0])[:2] if v else ())
    return ("s", type(v).__name__)


def _vnorm_key(k: str) -> str:
    return k.strip().lower().replace("_", " ")


def classify_object(
    keysets: list[frozenset[str]],
    members: list[Any] | None = None,
    seen_values: frozenset[str] | None = None,
) -> tuple[str, list[str]]:
    """Classify an object node as a dynamic-key map vs a static document shape.

    ``keylooks_data`` recognises only three shapes — a separator-bearing date, an ALL-CAPS
    token of 3+, and a single capital. Measured against these databases that misses the
    benchmark's central shape: ``legality.by_format`` keyed by ``commander``/``duel``,
    ``activity.by_year`` keyed by a bare ``2010``, ``elements_by_symbol`` keyed by ``c``/``cl``,
    ``attributes_by_name`` keyed by ``intelligence``. All four are labelled a static document
    shape, which is what makes the model hardcode the key list and reach in by name instead of
    enumerating. 84% of this dataset's questions are on that shape.

    Under ``TEND_SAG_KEYS_V2`` three further signals apply, each computable from the sample
    already in hand and each a statement about the data rather than a preference:

    ``numeric``   the names are bare numbers — years, lap numbers, vote-type ids.
    ``uniform``   a document carries only part of the vocabulary AND every member has the same
                  shape. Schema fields differ in shape from one another; parallel data entries
                  do not. The conjunct matters: sparseness alone also describes an object with
                  a couple of optional fields, and that is not a data-named map.
    ``as_value``  most of the names also occur as stored values elsewhere in the collection.
                  A schema field name does not turn up as a value; a data-derived one does.

    All three were tuned offline against the six groups the failure read flagged most often;
    all six fire, and the optional-field false positive the sparseness test alone produced
    (``metrics_by_grade_span``'s members) is rejected by the shape conjunct.
    """
    union: set[str] = set().union(*keysets) if keysets else set()
    per_doc = [len(ks) for ks in keysets] or [0]
    avg = sum(per_doc) / len(per_doc)
    data_like = sum(1 for k in union if keylooks_data(k))
    field_like = sum(1 for k in union if RE_FIELD.match(k))
    prolif = len(union) > max(8, 3 * avg) and len(union) > 6
    datadom = (
        len(union) >= 2 and data_like >= max(2, 0.6 * len(union)) and data_like >= field_like
    )
    dyn = prolif or datadom
    if KEYS_V2 and not dyn and len(union) >= 2:
        numeric = sum(1 for k in union if RE_NUM.match(k)) >= max(2, 0.6 * len(union))
        shapes = {_member_shape(v) for v in (members or [])[:40] if v is not None}
        uniform = len(union) >= 3 and avg < 0.85 * len(union) and len(shapes) == 1
        as_value = bool(seen_values) and sum(
            1 for k in union if _vnorm_key(k) in seen_values
        ) >= max(2, 0.6 * len(union))
        dyn = numeric or uniform or as_value
    return ("dynmap", sorted(union)[:6]) if dyn else ("static", sorted(union))


def COLLAPSE(seg: str) -> bool:
    """Segment is itself a data-valued key (date / composite code / long ALL-CAPS)."""
    base = seg.replace("[]", "")
    return bool(RE_DATE.match(base) or "::" in base or (RE_CODE.match(base) and len(base) >= 4))


@dataclass(frozen=True)
class LatticeNode:
    """One induced lattice node: dominant type + dynamic-key classification."""

    type: str
    count: int
    objkind: str | None = None
    keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class GroundingIndex:
    """Per-database immutable induction artifact shared by every record's solve."""

    db_id: str
    collections: tuple[str, ...]
    lattice: dict[str, dict[str, LatticeNode]]
    valid_paths: dict[str, frozenset[str]]
    top_fields: dict[str, frozenset[str]]
    dynamic_maps: dict[str, dict[str, tuple[str, ...]]]
    card_text: str
    value_index: dict[str, tuple[tuple[str, str, str, str], ...]]
    value_keys: tuple[str, ...]
    has_presence: bool
    source: str
    stats: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# lattice walk
# --------------------------------------------------------------------------- #
_MEMBER_SAMPLE_CAP = 40


def _walk(
    doc: Any,
    path: str,
    acc: dict[str, dict[str, Any]],
    depth: int = 0,
    values: set[str] | None = None,
) -> None:
    if depth > _WALK_MAX_DEPTH:
        return
    rec = acc.setdefault(path, {"types": collections.Counter(), "keysets": [], "members": []})
    rec["types"][type(doc).__name__] += 1
    if isinstance(doc, dict):
        rec["keysets"].append(frozenset(doc.keys()))
        if len(rec["members"]) < _MEMBER_SAMPLE_CAP:
            rec["members"].extend(list(doc.values())[:8])
        for k, v in doc.items():
            _walk(v, f"{path}.{k}", acc, depth + 1, values)
    elif isinstance(doc, list):
        for el in doc[:_WALK_LIST_CAP]:
            _walk(el, f"{path}[]", acc, depth + 1, values)
    elif values is not None and isinstance(doc, str) and 1 <= len(doc) <= 40:
        values.add(_vnorm_key(doc))


def _collapsed_form(path: str, dynset: frozenset[str]) -> str:
    """The display path, with every segment under a data-named group replaced by ``<*>``.

    Mirrors exactly what ``render_path_card`` does when it decides how to print a segment, so
    that a decision made here is the decision the reader sees.
    """
    norm: list[str] = []
    disp: list[str] = []
    for s in path.lstrip(".").split("."):
        base = s.replace("[]", "")
        arr = "[]" if s.endswith("[]") else ""
        parent = ".".join(norm)
        norm.append(base)
        disp.append(("<*>" + arr) if (parent in dynset or COLLAPSE(s)) else s)
    return ".".join(disp)


def induce_collection(docs: list[dict[str, Any]]) -> dict[str, LatticeNode]:
    """Induce the path lattice of one collection from its sampled documents.

    Under ``TEND_SAG_KEYS_V2`` the data-named decision is taken **once per collapsed path**
    rather than once per concrete instance. Instances are the wrong unit: the sample holds
    `career_by_year.1955.finishes_by_position_order`, `…1956…`, and so on as separate nodes, and
    a year with three finishes does not look data-named on its own even though the group plainly
    is. The result was that the same logical group collapsed for some years and not others, so
    the summary showed one arbitrary concrete key — `finishes_by_position_order.12` — instead of
    a pattern. That is worse than either alternative: less information than enumerating, and no
    clearer than it. Measured on formula_1: 92 lines became 11, and the questions it should have
    helped went from 7 correct to 4.

    Two passes, because recognising one group changes which paths share a collapsed form and can
    reveal a second group one level deeper.
    """
    acc: dict[str, dict[str, Any]] = {}
    # the stored string values of this collection, so `classify_object` can ask whether a
    # group's field names also occur as values -- see its docstring
    values: set[str] = set()
    for d in docs:
        _walk(d, "", acc, 0, values)
    seen = frozenset(values)

    verdict: dict[str, tuple[str, tuple[str, ...]]] = {}
    for path, rec in acc.items():
        if path and rec["keysets"] and rec["types"].most_common(1)[0][0] == "dict":
            kind, ex = classify_object(rec["keysets"], rec.get("members"), seen)
            verdict[path] = (kind, tuple(ex))

    if KEYS_V2:
        for _ in range(2):
            dynset = frozenset(
                p.lstrip(".").replace("[]", "") for p, (k, _e) in verdict.items() if k == "dynmap"
            )
            groups: dict[str, list[str]] = {}
            for path in verdict:
                groups.setdefault(_collapsed_form(path, dynset), []).append(path)
            changed = False
            for members in groups.values():
                if len(members) < 2:
                    continue
                ks: list[frozenset[str]] = []
                mv: list[Any] = []
                for p in members:
                    ks.extend(acc[p]["keysets"])
                    mv.extend(acc[p].get("members") or [])
                kind, ex = classify_object(ks, mv, seen)
                for p in members:
                    if verdict[p][0] != kind:
                        changed = True
                    verdict[p] = (kind, tuple(ex))
            if not changed:
                break

    lat: dict[str, LatticeNode] = {}
    for path, rec in acc.items():
        if not path:
            continue
        top = rec["types"].most_common(1)[0][0]
        objkind: str | None = None
        keys: tuple[str, ...] = ()
        if path in verdict:
            objkind, keys = verdict[path]
        lat[path] = LatticeNode(
            type=top, count=sum(rec["types"].values()), objkind=objkind, keys=keys
        )
    return lat


def derive_path_sets(
    lattice: dict[str, LatticeNode],
) -> tuple[frozenset[str], frozenset[str], dict[str, tuple[str, ...]]]:
    """Derive (valid prefix paths, top-level fields, dynamic-map paths) for one collection."""
    paths: set[str] = set()
    top: set[str] = set()
    dyn: dict[str, tuple[str, ...]] = {}
    for p, nd in lattice.items():
        np_ = p.lstrip(".").replace("[]", "")
        if not np_:
            continue
        parts = np_.split(".")
        for i in range(1, len(parts) + 1):
            paths.add(".".join(parts[:i]))
        top.add(parts[0])
        if nd.objkind == "dynmap":
            dyn[np_] = nd.keys
    return frozenset(paths), frozenset(top), dyn


# --------------------------------------------------------------------------- #
# affordance rendering (dynmap value shapes)
# --------------------------------------------------------------------------- #
def _afford(v: Any, depth: int = 0) -> str:
    if isinstance(v, list):
        return f"ARRAY of [{_afford(v[0], depth + 1) if v else '?'}]"
    if isinstance(v, dict):
        if depth >= 2:
            return "OBJECT{" + ",".join(sorted(v.keys())[:8]) + "}"
        parts = []
        for k in sorted(v.keys())[:8]:
            sub = v[k]
            if isinstance(sub, list):
                parts.append(f"{k}:ARRAY of [{_afford(sub[0], depth + 1) if sub else '?'}]")
            elif isinstance(sub, dict):
                parts.append(f"{k}:{{{','.join(sorted(sub.keys())[:6])}}}")
            else:
                parts.append(k)
        return "OBJECT{" + ", ".join(parts) + "}"
    return type(v).__name__


_ID_EXAMPLE_SCAN = 50
_ID_EXAMPLE_CAP = 3
_ID_FIELD_EXAMPLE_CAP = 2
_ID_FIELD_MAX_DOTS = 1
RE_ID_FIELD = re.compile(r"(?:^|\.)[A-Za-z0-9]*_?(?:id|uuid|code|key)$", re.I)


def _scalar_at(doc: Any, path: str) -> Any:
    cur = doc
    for seg in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
    if isinstance(cur, bool) or not isinstance(cur, (str, int, float)):
        return None
    return cur


def _id_field_examples(
    docs: list[dict[str, Any]], valid_paths: frozenset[str]
) -> dict[str, tuple[Any, ...]]:
    """Example stored values for shallow id-like sibling fields.

    An entity's identifier is often carried twice in different FORMS — ``_id`` as
    ``'patient:2110'`` next to ``identity.patient_id`` as ``2110``, or (toxicology)
    as the identical string. Which one a question wants is decidable only from the
    values, so the card carries a couple of each. Names alone are not enough and
    the wrong choice fails every row.
    """
    out: dict[str, tuple[Any, ...]] = {}
    for path in valid_paths:
        if path == "_id" or path.count(".") > _ID_FIELD_MAX_DOTS:
            continue
        if not RE_ID_FIELD.search(path):
            continue
        vals: list[Any] = []
        for d in docs[:_ID_EXAMPLE_SCAN]:
            v = _scalar_at(d, path)
            if v is not None and v not in vals:
                vals.append(v)
            if len(vals) >= _ID_FIELD_EXAMPLE_CAP:
                break
        if vals:
            out[path] = tuple(vals)
    return out


def _id_examples(docs: list[dict[str, Any]]) -> tuple[Any, ...]:
    """Example stored ``_id`` values for the card's document-key line.

    Scalars only: an ObjectId or a compound key renders as noise, and the point of
    the line is to let the model judge whether ``_id`` is the identifier the question
    names (``patient:2110`` vs a sibling surrogate like ``card_id = 1``).
    """
    out: list[Any] = []
    for d in docs[:_ID_EXAMPLE_SCAN]:
        v = d.get("_id") if isinstance(d, dict) else None
        if isinstance(v, (str, int, float)) and not isinstance(v, bool) and v not in out:
            out.append(v)
        if len(out) >= _ID_EXAMPLE_CAP:
            break
    return tuple(out)


def _dynmap_value_shape(docs: list[dict[str, Any]], dynpath: str) -> str:
    """Example value affordance for a dynamic-key map, from the in-memory sample.

    Mirrors the prototype's live ``find_one`` + segment traversal exactly: the
    FIRST document where the path resolves decides — a non-empty dict yields its
    first value's affordance, anything else (empty dict, scalar) yields ``"?"``.
    Array-nested or absent paths yield ``"?"``.
    """
    segs = dynpath.split(".")
    for doc in docs:
        cur: Any = doc
        ok = True
        for seg in segs:
            if isinstance(cur, dict) and seg in cur:
                cur = cur[seg]
            else:
                ok = False
                break
        if not ok:
            continue
        if not isinstance(cur, dict) or not cur:
            return "?"
        return "value = " + _afford(next(iter(cur.values())))
    return "?"


# --------------------------------------------------------------------------- #
# path card rendering
# --------------------------------------------------------------------------- #
def render_path_card(
    coll: str,
    lattice: dict[str, LatticeNode],
    dyn: dict[str, tuple[str, ...]],
    value_shapes: dict[str, str],
    *,
    cap: int,
    collapse: bool = True,
    id_examples: tuple[Any, ...] = (),
    id_field_examples: dict[str, tuple[Any, ...]] | None = None,
) -> str:
    """Render the ENTIRE induced lattice: every path (dynmap children collapsed to
    ``<*>``), with type, array markers, dynmap markers + example keys + value shape.

    ``collapse=False`` backs the ``nocollapse`` card-mode ablation: concrete data
    keys render verbatim (no ``<*>``, no dynamic-key map affordance), so dynamic-key
    blowup hits the cap and the deepest paths get elided — the measured cost of
    dropping the collapse representation. Card TEXT only; gates/witnesses unchanged.

    ``_``-prefixed paths stay out of the generic row loop (internal keys are not part
    of the hypothesis space), but the top-level ``_id`` is rendered explicitly from
    ``id_examples`` and is exempt from the cap: in this world ``_id`` is a MEANINGFUL
    natural key (``patient:2110``, ``set:10E``, a print uuid) and is frequently the
    only carrier of the entity identifier a question asks for. Hiding it while the
    system prompt asserts the card is complete told the model the answer column did
    not exist — measured at 0.9% ``_id`` usage where the gold required it.
    """
    dynset = set(dyn)
    rows: dict[str, dict[str, Any]] = {}  # display path -> {ty, isdyn, np (concrete), keys}
    for p, nd in lattice.items():
        raw = p.lstrip(".")
        if not raw or raw.split(".")[0].startswith("_"):
            continue
        segs = raw.split(".")
        disp: list[str] = []
        norm: list[str] = []  # display keeps [] and <*>; norm keeps concrete keys
        for s in segs:
            base = s.replace("[]", "")
            arr = "[]" if s.endswith("[]") else ""
            norm.append(base)
            parent = ".".join(norm[:-1])
            collapsed = collapse and (parent in dynset or COLLAPSE(s))
            disp.append(("<*>" + arr) if collapsed else s)
        d = ".".join(disp)
        isdyn = collapse and nd.objkind == "dynmap"
        cur = rows.get(d)
        if cur is None:
            rows[d] = {
                "ty": (nd.objkind or nd.type) if collapse else nd.type,
                "isdyn": isdyn,
                "np": ".".join(norm),
                "keys": list(nd.keys),
            }
        elif isdyn and not cur["isdyn"]:  # a sibling instance proves it's a dynmap
            cur.update(ty="dynmap", isdyn=True, np=".".join(norm), keys=list(nd.keys))
    lines = [
        f"Collection `{coll}` — complete induced path map "
        + ("(`[]` = array element, `<*>` = dynamic DATA key):"
           if collapse else "(`[]` = array element):")
    ]
    if id_examples:
        lines.append(
            f"  _id: DOCUMENT KEY — a stored, readable field and usually this entity's "
            f"identifier, e.g. {list(id_examples[:3])}; read it as `$_id` when the "
            f"question asks for this entity's id/identifier/code"
        )
    keys = sorted(rows)
    dropped = 0
    if len(keys) > cap:  # keep the SHALLOWEST paths, drop the deepest
        keep = set(sorted(keys, key=lambda d: (d.count("."), d))[:cap])
        dropped = len(keys) - cap
        keys = [d for d in keys if d in keep]
    for d in keys:
        r = rows[d]
        if r["isdyn"]:
            shape = value_shapes.get(r["np"], "?")
            lines.append(
                f"  {d}: DYNAMIC-KEY MAP — keys are data values, e.g. "
                f"{r['keys'][:4]}; {shape}; "
                f"enumerate with $objectToArray -> {{k,v}}"
            )
        else:
            ex = (id_field_examples or {}).get(r["np"])
            lines.append(f"  {d}: {r['ty']}" + (f", e.g. {list(ex)}" if ex else ""))
    if dropped:
        lines.append(f"  ... ({dropped} deepest paths elided)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# value index (witness side of the same sample pass)
# --------------------------------------------------------------------------- #
def _vix_add(
    vidx: dict[str, set[tuple[str, str, str, str]]], val: Any, coll: str, path: str, kind: str
) -> None:
    if not isinstance(val, str):
        return
    v = val.strip()
    if not (_VALUE_MIN_LEN <= len(v) <= _VALUE_MAX_LEN):
        return
    n = vnorm(v)
    if not n:
        return
    vidx.setdefault(n, set())
    if len(vidx[n]) < _VALUE_INDEX_LOC_CAP:
        vidx[n].add((coll, path, kind, v))


def _vwalk(
    vidx: dict[str, set[tuple[str, str, str, str]]],
    doc: Any,
    coll: str,
    disp: str,
    norm: str,
    dynset: set[str],
    depth: int = 0,
) -> None:
    """``disp`` accumulates the display path (with ``<*>``); ``norm`` keeps concrete
    keys for the dynset membership test."""
    if depth > _WALK_MAX_DEPTH:
        return
    if isinstance(doc, dict):
        is_dyn = norm.lstrip(".").replace("[]", "") in dynset
        for k, v in doc.items():
            # `_id` carries real entity identifiers here, so NLQ literals must be able to
            # witness at it; other `_`-prefixed top-level keys stay internal.
            if depth == 0 and k.startswith("_") and k != "_id":
                continue
            if is_dyn or COLLAPSE(k):
                _vix_add(vidx, k, coll, (disp + ".<*>").lstrip("."), "dynamic KEY")
                _vwalk(vidx, v, coll, f"{disp}.<*>", f"{norm}.{k}", dynset, depth + 1)
            else:
                _vwalk(vidx, v, coll, f"{disp}.{k}", f"{norm}.{k}", dynset, depth + 1)
    elif isinstance(doc, list):
        for el in doc[:_VALUE_WALK_LIST_CAP]:
            _vwalk(vidx, el, coll, f"{disp}[]", f"{norm}[]", dynset, depth + 1)
    else:
        _vix_add(vidx, doc, coll, disp.lstrip("."), "value")


# --------------------------------------------------------------------------- #
# index builder
# --------------------------------------------------------------------------- #
def build_grounding_index(
    world: WorldAccess,
    *,
    sample_docs: int = 400,
    card_cap: int = 400,
    card_mode: str = "lattice",
) -> GroundingIndex:
    """Build the per-db grounding index in ONE bounded sample pass per collection.

    ``card_mode`` varies ONLY the rendered card text (the decoding hypothesis space
    shown to the model) for the card-representation ablations; the lattice, path
    sets, dynamic maps, and value index — everything the gates and witnesses use —
    are identical across modes:

    - ``lattice`` (solver default): the full collapsed lattice card.
    - ``toplevel``: top-level fields only (the early prototype's grounding).
    - ``nocollapse``: full depth but concrete keys, no ``<*>``/dynmap affordances.
    """
    colls = tuple(sorted(world.list_collections()))
    lattice: dict[str, dict[str, LatticeNode]] = {}
    valid: dict[str, frozenset[str]] = {}
    top: dict[str, frozenset[str]] = {}
    dyn: dict[str, dict[str, tuple[str, ...]]] = {}
    vidx: dict[str, set[tuple[str, str, str, str]]] = {}
    cards: list[str] = []
    for coll in colls:
        docs = world.sample_docs(coll, sample_docs)
        lat = induce_collection(docs)
        lattice[coll] = lat
        valid[coll], top[coll], dyn[coll] = derive_path_sets(lat)
        shapes = {np_: _dynmap_value_shape(docs, np_) for np_ in dyn[coll]}
        id_examples = _id_examples(docs)
        id_fields = _id_field_examples(docs, valid[coll])
        if card_mode == "toplevel":
            card_lat = {p: nd for p, nd in lat.items() if "." not in p.lstrip(".")}
            card_dyn = {np_: keys for np_, keys in dyn[coll].items() if "." not in np_}
            cards.append(
                render_path_card(
                    coll,
                    card_lat,
                    card_dyn,
                    shapes,
                    cap=card_cap,
                    id_examples=id_examples,
                    id_field_examples=id_fields,
                )
            )
        elif card_mode == "nocollapse":
            cards.append(
                render_path_card(
                    coll,
                    lat,
                    dyn[coll],
                    shapes,
                    cap=card_cap,
                    collapse=False,
                    id_examples=id_examples,
                    id_field_examples=id_fields,
                )
            )
        else:
            cards.append(
                render_path_card(
                    coll,
                    lat,
                    dyn[coll],
                    shapes,
                    cap=card_cap,
                    id_examples=id_examples,
                    id_field_examples=id_fields,
                )
            )
        dynset = set(dyn[coll])
        for d in docs:
            _vwalk(vidx, d, coll, "", "", dynset)
    card_text = "\n\n".join(cards)
    value_index = {n: tuple(sorted(hits)) for n, hits in vidx.items()}
    has_presence = any(p.endswith(".presence_state") for c in colls for p in valid[c])
    return GroundingIndex(
        db_id=world.db_id,
        collections=colls,
        lattice=lattice,
        valid_paths=valid,
        top_fields=top,
        dynamic_maps=dyn,
        card_text=card_text,
        value_index=value_index,
        value_keys=tuple(sorted(value_index)),
        has_presence=has_presence,
        source="mongo" if world.can_execute else "local",
        stats={
            "collections": len(colls),
            "path_count": sum(len(v) for v in valid.values()),
            "card_chars": len(card_text),
            "value_count": len(value_index),
        },
    )
