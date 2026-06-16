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


def classify_object(keysets: list[frozenset[str]]) -> tuple[str, list[str]]:
    """Classify an object node as a dynamic-key map vs a static document shape."""
    union: set[str] = set().union(*keysets) if keysets else set()
    per_doc = [len(ks) for ks in keysets] or [0]
    avg = sum(per_doc) / len(per_doc)
    data_like = sum(1 for k in union if keylooks_data(k))
    field_like = sum(1 for k in union if RE_FIELD.match(k))
    prolif = len(union) > max(8, 3 * avg) and len(union) > 6
    datadom = (
        len(union) >= 2 and data_like >= max(2, 0.6 * len(union)) and data_like >= field_like
    )
    return ("dynmap", sorted(union)[:6]) if (prolif or datadom) else ("static", sorted(union))


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
def _walk(doc: Any, path: str, acc: dict[str, dict[str, Any]], depth: int = 0) -> None:
    if depth > _WALK_MAX_DEPTH:
        return
    rec = acc.setdefault(path, {"types": collections.Counter(), "keysets": []})
    rec["types"][type(doc).__name__] += 1
    if isinstance(doc, dict):
        rec["keysets"].append(frozenset(doc.keys()))
        for k, v in doc.items():
            _walk(v, f"{path}.{k}", acc, depth + 1)
    elif isinstance(doc, list):
        for el in doc[:_WALK_LIST_CAP]:
            _walk(el, f"{path}[]", acc, depth + 1)


def induce_collection(docs: list[dict[str, Any]]) -> dict[str, LatticeNode]:
    """Induce the path lattice of one collection from its sampled documents."""
    acc: dict[str, dict[str, Any]] = {}
    for d in docs:
        _walk(d, "", acc, 0)
    lat: dict[str, LatticeNode] = {}
    for path, rec in acc.items():
        if not path:
            continue
        top = rec["types"].most_common(1)[0][0]
        objkind: str | None = None
        keys: tuple[str, ...] = ()
        if rec["keysets"] and top == "dict":
            kind, ex = classify_object(rec["keysets"])
            objkind = kind
            keys = tuple(ex)
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
) -> str:
    """Render the ENTIRE induced lattice: every path (dynmap children collapsed to
    ``<*>``), with type, array markers, dynmap markers + example keys + value shape.

    ``collapse=False`` backs the ``nocollapse`` card-mode ablation: concrete data
    keys render verbatim (no ``<*>``, no dynamic-key map affordance), so dynamic-key
    blowup hits the cap and the deepest paths get elided — the measured cost of
    dropping the collapse representation. Card TEXT only; gates/witnesses unchanged.
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
            lines.append(f"  {d}: {r['ty']}")
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
            if depth == 0 and k.startswith("_"):
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
        if card_mode == "toplevel":
            card_lat = {p: nd for p, nd in lat.items() if "." not in p.lstrip(".")}
            card_dyn = {np_: keys for np_, keys in dyn[coll].items() if "." not in np_}
            cards.append(
                render_path_card(coll, card_lat, card_dyn, shapes, cap=card_cap)
            )
        elif card_mode == "nocollapse":
            cards.append(
                render_path_card(
                    coll, lat, dyn[coll], shapes, cap=card_cap, collapse=False
                )
            )
        else:
            cards.append(render_path_card(coll, lat, dyn[coll], shapes, cap=card_cap))
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
