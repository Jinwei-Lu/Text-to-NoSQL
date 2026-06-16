"""Two-sided alignment gate: A_path ∧ A_value, plus the row-limit contract.

A_path checks that everything the pipeline READS is representable in the induced
lattice: the collection exists, every field ref resolves to an induced path (or a
computed intermediate / concrete dynamic-key access), and ``$lookup`` is admissible
ONLY between real collections along a join edge actually witnessed in the data.
A_value checks that hard witnessed literals are used in comparison position with
their exact stored form against a witnessed path. Violations are model-facing
repair feedback — the strings are mechanism, ported verbatim from the validated
prototype (gate accepts all 1210 golds: zero false positives).

All probes fail OPEN (a probe error or an offline world never blocks a candidate).
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Iterable

from ...errors import ExecutionError
from .world import WorldAccess

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .induction import GroundingIndex

from .witness import EnforcedLiteral, vnorm

CMP_OPS = {"$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin"}
GENERIC = {
    "missing", "present", "null", "empty", "none", "unknown", "n/a", "yes", "no", "true", "false",
}
RE_ROWLIM = re.compile(r"(?i)\b(?:top|first|limit(?:ed)? to|at most)\s+(\d+)\b")

_EDGE_PROBE_GROUP_LIMIT = 5
_EDGE_PROBE_SCAN_LIMIT = 50


class ProbeCache:
    """Mutable per-(db, run) cache for join-edge witness probes."""

    def __init__(self) -> None:
        self.edges: dict[tuple[str, str, str, str], bool] = {}


# --------------------------------------------------------------------------- #
# pipeline introspection helpers (pure)
# --------------------------------------------------------------------------- #
def walk_values(o: Any) -> Iterable[Any]:
    if isinstance(o, dict):
        for v in o.values():
            yield from walk_values(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk_values(v)
    else:
        yield o


def field_refs(pipeline: list[dict[str, Any]]) -> list[str]:
    return [
        v[1:]
        for v in walk_values(pipeline)
        if isinstance(v, str) and v.startswith("$") and not v.startswith("$$")
    ]


def field_refs_own(pipeline: list[dict[str, Any]]) -> list[str]:
    """Field refs of the OUTER pipeline only — ``$lookup`` bodies are B-scoped."""
    refs: list[str] = []

    def rec(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "$lookup":
                    continue
                rec(v)
        elif isinstance(o, list):
            for x in o:
                rec(x)
        elif isinstance(o, str) and o.startswith("$") and not o.startswith("$$"):
            refs.append(o[1:])

    rec(pipeline)
    return refs


def lookup_specs(pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def rec(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "$lookup" and isinstance(v, dict):
                    out.append(v)
                rec(v)
        elif isinstance(o, list):
            for x in o:
                rec(x)

    rec(pipeline)
    return out


def pipe_strings(pipeline: list[dict[str, Any]]) -> list[str]:
    return [v for v in walk_values(pipeline) if isinstance(v, str) and not v.startswith("$")]


def cmp_strings(pipeline: list[dict[str, Any]]) -> list[str]:
    """Strings used in COMPARISON position ($match values / comparison operators) —
    distinct from output-position label strings ($cond/$switch then, $project)."""
    out: list[str] = []

    def from_val(v: Any) -> None:
        if isinstance(v, str) and not v.startswith("$"):
            out.append(v)
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, str) and not x.startswith("$"):
                    out.append(x)

    def rec(o: Any, inm: bool) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k in CMP_OPS:
                    from_val(v)
                    rec(v, False)
                elif k == "$match":
                    rec(v, True)
                elif k in ("$and", "$or", "$nor") and inm:
                    rec(v, True)
                elif inm and not k.startswith("$"):
                    if isinstance(v, str) and not v.startswith("$"):
                        out.append(v)
                    elif isinstance(v, dict):
                        rec(v, True)
                    elif isinstance(v, list):
                        from_val(v)
                else:
                    rec(v, False)
        elif isinstance(o, list):
            for x in o:
                rec(x, inm)

    rec(pipeline, False)
    return out


# --------------------------------------------------------------------------- #
# lattice membership
# --------------------------------------------------------------------------- #
def dyn_prefixed(index: "GroundingIndex", coll: str, ref: str) -> bool:
    parts = ref.split(".")
    dyn = index.dynamic_maps.get(coll, {})
    return any(".".join(parts[:i]) in dyn for i in range(1, len(parts) + 1))


def path_exists(index: "GroundingIndex", coll: str, ref: str) -> bool:
    parts = ref.split(".")
    valid = index.valid_paths.get(coll, frozenset())
    return any(
        ".".join(parts[:i]) in valid for i in range(len(parts), 0, -1)
    ) or dyn_prefixed(index, coll, ref)


# --------------------------------------------------------------------------- #
# join-edge witness probe (fail-open)
# --------------------------------------------------------------------------- #
def edge_witnessed(
    world: WorldAccess,
    index: "GroundingIndex",
    cache: ProbeCache,
    a: str,
    lf: str,
    b: str,
    ff: str,
    *,
    probe_timeout_ms: int = 4000,
) -> bool:
    """Is the join edge ``a.lf -> b.ff`` witnessed by actual matching values?"""
    key = (a, lf, b, ff)
    if key in cache.edges:
        return cache.edges[key]
    if lf.split(".")[0] not in index.top_fields.get(a, frozenset()):
        cache.edges[key] = True  # computed localField: cannot probe, fail open
        return True
    if not world.can_execute:
        cache.edges[key] = True  # offline world: cannot probe, fail open
        return True
    ok = False
    try:
        vals = [
            d["_id"]
            for d in world.aggregate(
                a,
                [
                    {"$match": {lf: {"$exists": True, "$ne": None}}},
                    {"$limit": _EDGE_PROBE_SCAN_LIMIT},
                    {"$group": {"_id": f"${lf}"}},
                    {"$limit": _EDGE_PROBE_GROUP_LIMIT},
                ],
                max_time_ms=probe_timeout_ms,
            )
        ]
        flat: list[Any] = []
        for v in vals:
            flat.extend(v) if isinstance(v, list) else flat.append(v)
        for v in [x for x in flat if x is not None][:_EDGE_PROBE_GROUP_LIMIT]:
            if world.find_one(b, {ff: v}, {"_id": 1}, max_time_ms=1500):
                ok = True
                break
    except ExecutionError:
        ok = True  # probe failure: fail open
    cache.edges[key] = ok
    return ok


# --------------------------------------------------------------------------- #
# A_path
# --------------------------------------------------------------------------- #
def a_path(
    world: WorldAccess,
    index: "GroundingIndex",
    cache: ProbeCache,
    coll: str,
    pipeline: list[dict[str, Any]],
    *,
    edge_probe_timeout_ms: int = 4000,
) -> list[str]:
    viol: list[str] = []
    colls = list(index.collections)
    if coll not in index.collections:
        return [f"collection `{coll}` does not exist; choose one of {colls}"]
    # joins are admissible ONLY along data-witnessed id links between real collections
    for spec in lookup_specs(pipeline):
        frm = spec.get("from")
        if frm not in index.collections:
            viol.append(
                f"$lookup target `{frm}` does not exist; the only collections "
                f"are {colls} and related data is already EMBEDDED — re-derive "
                f"within `{coll}`."
            )
            continue
        lf, ff = spec.get("localField"), spec.get("foreignField")
        if lf and ff:
            if not path_exists(index, frm, ff):
                viol.append(f"$lookup foreignField `{ff}` does not exist in `{frm}`")
            elif not edge_witnessed(
                world, index, cache, coll, lf, frm, ff, probe_timeout_ms=edge_probe_timeout_ms
            ):
                viol.append(
                    f"$lookup join edge `{coll}.{lf}` -> `{frm}.{ff}` is not "
                    f"witnessed in the data (no matching values exist); this "
                    f"join is structurally wrong — the data you want is likely "
                    f"embedded in `{coll}` already."
                )
    valid = index.valid_paths.get(coll, frozenset())
    top = index.top_fields.get(coll, frozenset())
    dyn = index.dynamic_maps.get(coll, {})
    for ref in set(field_refs_own(pipeline)):
        head = ref.split(".")[0]
        if head not in top:
            continue  # computed intermediate
        if dyn_prefixed(index, coll, ref):
            continue  # concrete dynamic key access is legal
        parts = ref.split(".")
        if not any(".".join(parts[:i]) in valid for i in range(len(parts), 0, -1)):
            hint = next(
                (
                    f" (note: `{dp}` is a dynamic-key map — use $objectToArray)"
                    for dp in dyn
                    if dp.split(".")[0] == head
                ),
                "",
            )
            viol.append(f"path `{ref}` does not exist in `{coll}`{hint}")
    return viol


# --------------------------------------------------------------------------- #
# A_value
# --------------------------------------------------------------------------- #
def a_value(
    index: "GroundingIndex",
    coll: str,
    pipeline: list[dict[str, Any]],
    enforce: dict[str, EnforcedLiteral],
) -> list[str]:
    """Witnessed hard literals: comparison uses must copy the exact stored form and
    target a witnessed path; a witnessed term the query neither reads nor mentions
    is flagged. Output-position label strings are exempt."""
    viol: list[str] = []
    allnorm = {vnorm(s): s for s in pipe_strings(pipeline)}
    cmpnorm = {vnorm(s): s for s in cmp_strings(pipeline)}
    refs = set(field_refs(pipeline))
    for lit, w in enforce.items():
        n = vnorm(lit)
        if n in GENERIC:
            continue
        my_paths = {p for c, p in w.paths if c == coll}
        used_cmp, used_any = n in cmpnorm, n in allnorm
        if used_cmp and len(w.exact) <= 3 and cmpnorm[n] not in w.exact:
            viol.append(
                f"comparison value '{cmpnorm[n]}' is stored as "
                f"{sorted(w.exact)[:2]} — copy the stored form verbatim "
                f"(exact case/spacing/underscores)."
            )
        if not my_paths:
            # NOT a violation: comparisons may legitimately yield zero matches inside
            # expressions ($filter/$cond counts); a truly wrong collection produces an
            # empty result and the runtime bisection feedback handles it with data.
            continue
        if len(my_paths) > 4:
            continue
        touched = any(
            p == r or p.startswith(r + ".") or r.startswith(p + ".")
            for p in my_paths
            for r in refs
        )
        if touched:
            continue
        hint = (
            " (dynamic KEY — literal key access or $objectToArray)"
            if "dynamic KEY" in w.kinds
            else ""
        )
        if used_cmp:
            viol.append(
                f"'{lit}' lives at {sorted(my_paths)[:3]}{hint} — your query "
                f"compares it against a different path; filter on a witnessed path."
            )
        elif not used_any:
            viol.append(
                f"the question's term '{lit}' matches data at "
                f"{sorted(my_paths)[:3]}{hint}, but your query never reads any "
                f"of those paths."
            )
    return viol


# --------------------------------------------------------------------------- #
# output contracts
# --------------------------------------------------------------------------- #
def limit_contract(nlq: str, pipeline: list[dict[str, Any]]) -> list[str]:
    """NLQ asks for top/first N rows but the pipeline has no ``$limit`` at all."""
    ns = sorted({int(m) for m in RE_ROWLIM.findall(nlq)})
    if not ns:
        return []
    if any(isinstance(s, dict) and "$limit" in s for s in pipeline):
        return []
    return [
        f"the question asks for a bounded number of rows ({ns}) but the pipeline "
        f"has no $limit stage — add the appropriate $limit."
    ]
