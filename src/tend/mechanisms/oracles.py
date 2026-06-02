"""Reference oracles R — naive, auditable answer definitions for MS gold-lock (04 §04-2-4).

R is *not* gold: it is an independent Python reimplementation, keyed by archetype, that **defines
the answer**. MS locks gold by checking ``NormExec(gold, D) ≡_rec Norm(R(D))`` — so a systematic
bug in gold (wrong ``$ifNull`` default, dropped present/missing branch) is caught by R, which the
double-path triangulation alone cannot. The discipline (04 §04-2-4): only archetypes with a
*simple, auditable* R go in the catalog.

R operates directly on the witness ``snapshot`` (``{collection: [doc, ...]}`` of plain JSON-native
docs) — no MongoDB, no LLM — and returns a raw list of result docs. The caller (MS) passes both
R's output and ``NormExec(gold)`` through the same Norm before ``≡_rec``.

Param contract (the ``intent.reference_oracle`` payload each template expects) is documented per
oracle below; see COORDINATION.md for the cross-session contract Session B's MS should populate.
"""
from __future__ import annotations

from typing import Any, Callable

Snapshot = dict[str, list[dict[str, Any]]]
Oracle = Callable[[Snapshot, dict[str, Any]], list[dict[str, Any]]]

_MISSING = object()
_METRIC_FIELD_PRIORITY = (
    "amount", "balance", "payments", "value", "total", "sum", "cost", "price",
)


class OracleError(Exception):
    """Unknown template or invalid params for a reference oracle."""


# --------------------------------------------------------------------------- #
# helpers — present/missing-aware access on plain dicts
# --------------------------------------------------------------------------- #
def _get(doc: dict[str, Any], path: str) -> tuple[bool, Any]:
    """Dotted-path access. Returns ``(present, value)``; missing key -> ``(False, None)``.

    Distinguishes missing from explicit ``null`` (present, value=None) — the empty-vs-missing
    semantics the benchmark turns on (01 §01-4-3).
    """
    cur: Any = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return (False, None)
        cur = cur[part]
    return (True, cur)


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _aggregate(values: list[float], how: str) -> float:
    how = _normal_agg(how)
    if how == "count":
        return float(len(values))
    if not values:
        return 0.0
    if how == "sum":
        return sum(values)
    if how == "avg":
        return sum(values) / len(values)
    if how == "min":
        return min(values)
    if how == "max":
        return max(values)
    raise OracleError(f"unknown aggregation: {how}")


def _normal_agg(value: Any) -> str:
    return str(value or "count").lstrip("$").lower()


def _require(params: dict[str, Any], *keys: str) -> None:
    missing = [k for k in keys if k not in params]
    if missing:
        raise OracleError(f"oracle params missing required keys: {missing}")


def _docs(snapshot: Snapshot, collection: str) -> list[dict[str, Any]]:
    docs = snapshot.get(collection)
    if docs is None:
        raise OracleError(f"collection {collection!r} not in snapshot")
    return docs


def _sortable_value(value: Any) -> tuple[int, Any]:
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        return (1, float(value))
    if isinstance(value, str):
        return (2, value)
    return (3, repr(value))


# --------------------------------------------------------------------------- #
# oracles (one per archetype reference_template)
# --------------------------------------------------------------------------- #
def _simple_filter(snapshot: Snapshot, params: dict[str, Any]) -> list[dict[str, Any]]:
    """params: {collection, predicates:[{field, op, value}], project:[field,...]}."""
    _require(params, "collection")
    docs = _docs(snapshot, params["collection"])
    preds = params.get("predicates", [])
    project = params.get("project")
    out = []
    for d in docs:
        if all(_match(d, p) for p in preds):
            out.append({k: _get(d, k)[1] for k in project} if project else dict(d))
    return out


_OPS = {
    "eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
    "gt": lambda a, b: a is not None and a > b, "lt": lambda a, b: a is not None and a < b,
    "gte": lambda a, b: a is not None and a >= b, "lte": lambda a, b: a is not None and a <= b,
}


def _match(doc: dict[str, Any], pred: dict[str, Any]) -> bool:
    present, val = _get(doc, pred["field"])
    if not present:
        return False
    op = pred.get("op", "eq")
    if op not in _OPS:
        raise OracleError(f"unknown predicate op: {op}")
    return _OPS[op](val, pred.get("value"))


def _topn(snapshot: Snapshot, params: dict[str, Any]) -> list[dict[str, Any]]:
    """params: {collection, sort_key, order(asc|desc), n, project?, nulls(first|last)}."""
    _require(params, "collection", "sort_key", "n")
    docs = list(_docs(snapshot, params["collection"]))
    key = params["sort_key"]
    order = params.get("order", "desc")
    if order not in {"asc", "desc"}:
        raise OracleError(f"unknown topn order: {order}")
    nulls = params.get("nulls", "last")
    if nulls not in {"first", "last"}:
        raise OracleError(f"unknown topn nulls policy: {nulls}")

    with_sort_key: list[tuple[tuple[int, Any], dict[str, Any]]] = []
    without_sort_key: list[dict[str, Any]] = []
    for d in docs:
        present, value = _get(d, key)
        if not present or value is None:
            without_sort_key.append(d)
        else:
            with_sort_key.append((_sortable_value(value), d))

    with_sort_key.sort(key=lambda item: item[0], reverse=order == "desc")
    ordered = [d for _, d in with_sort_key]
    if nulls == "first":
        ordered = without_sort_key + ordered
    else:
        ordered.extend(without_sort_key)
    top = ordered[: int(params["n"])]
    project = params.get("project")
    return [{k: _get(d, k)[1] for k in project} if project else dict(d) for d in top]


def _group_count(snapshot: Snapshot, params: dict[str, Any]) -> list[dict[str, Any]]:
    """params: {collection, group_by}. Returns [{_id: key, count: n}, ...]."""
    _require(params, "collection", "group_by")
    docs = _docs(snapshot, params["collection"])
    counts: dict[Any, int] = {}
    for d in docs:
        counts[_get(d, params["group_by"])[1]] = counts.get(_get(d, params["group_by"])[1], 0) + 1
    return [{"_id": k, "count": v} for k, v in counts.items()]


def _existence_count(snapshot: Snapshot, params: dict[str, Any]) -> list[dict[str, Any]]:
    """params: {collection, field}. Counts docs where ``field`` is present."""
    _require(params, "collection", "field")
    docs = _docs(snapshot, params["collection"])
    n = sum(1 for d in docs if _get(d, params["field"])[0])
    return [{"count": n}]


def _null_coalesce_agg(snapshot: Snapshot, params: dict[str, Any]) -> list[dict[str, Any]]:
    """params: {collection, field, default, agg(sum|avg|min|max)}. Coalesces missing/null."""
    _require(params, "collection", "field", "agg")
    docs = _docs(snapshot, params["collection"])
    default = params.get("default", 0)
    vals = []
    for d in docs:
        present, v = _get(d, params["field"])
        n = _num(v)
        vals.append(n if n is not None else float(default))
    return [{"value": _aggregate(vals, params["agg"])}]


def _per_subtype_agg(snapshot: Snapshot, params: dict[str, Any]) -> list[dict[str, Any]]:
    """params: {collection, discriminator, field_by_subtype:{value:field}, agg}.

    Groups by the real discriminator column and aggregates each subtype's *own* field.
    """
    _require(params, "collection", "discriminator", "field_by_subtype", "agg")
    docs = _docs(snapshot, params["collection"])
    disc = params["discriminator"]
    fbs = params["field_by_subtype"]
    buckets: dict[Any, list[float]] = {}
    for d in docs:
        sub = _get(d, disc)[1]
        field = fbs.get(str(sub)) or fbs.get(sub)
        if field is None:
            continue
        n = _num(_get(d, field)[1])
        if n is not None:
            buckets.setdefault(sub, []).append(n)
    return [{"_id": k, "value": _aggregate(v, params["agg"])} for k, v in buckets.items()]


def _present_missing_projection(snapshot: Snapshot, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Attach a per-parent field from a sparse optional embed, preserving every parent doc.

    params: {
      parent_collection, embed_field,            # embed_field present => parent "has" it
      numerator_path,                             # dotted path under the parent (e.g. loan.amount)
      target_field,                               # new field name to attach
      absent_value (default 0),                   # value when embed missing
      denom: {collection, local_id, foreign_field, match:{field,value}, sum_field, zero_value(1)}
    }
    Mirrors the canonical financial/1001 archetype: has(loan) ? loan.amount / Σcredits : 0.
    """
    _require(params, "parent_collection", "embed_field", "numerator_path", "target_field", "denom")
    parents = _docs(snapshot, params["parent_collection"])
    target = params["target_field"]
    absent_value = params.get("absent_value", 0)
    # numerator_path may arrive embed-relative ("amount") or parent-rooted ("loan.amount");
    # normalize to parent-rooted so the metric is read from inside the embed, not a phantom
    # top-level field (which would silently zero every numerator).
    numerator_path = _embed_value_path(params["embed_field"], params["numerator_path"])
    denom_spec = params["denom"]
    # precompute per-parent denominator sums
    denom_docs = _docs(snapshot, denom_spec["collection"])
    sums: dict[Any, float] = {}
    match = denom_spec.get("match")
    mfield = match.get("field") if isinstance(match, dict) else None
    mval = match.get("value") if isinstance(match, dict) else None
    for dd in denom_docs:
        if mfield is not None and _get(dd, mfield)[1] != mval:
            continue
        fk = _get(dd, denom_spec["foreign_field"])[1]
        amt = _num(_get(dd, denom_spec["sum_field"])[1]) or 0.0
        sums[fk] = sums.get(fk, 0.0) + amt

    zero_value = float(denom_spec.get("zero_value", 1))
    out = []
    for p in parents:
        doc = dict(p)
        has_embed = _get(p, params["embed_field"])[0]
        if has_embed:
            num = _num(_get(p, numerator_path)[1]) or 0.0
            denom = sums.get(_get(p, denom_spec["local_id"])[1], 0.0)
            doc[target] = num / (denom if denom != 0 else zero_value)
        else:
            doc[target] = absent_value
        out.append(doc)
    return out


def _optional_embed_projection(snapshot: Snapshot, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Attach a scalar from an optional embed, preserving every parent document.

    params: {
      parent_collection, embed_field, value_path, target_field, missing_default
    }

    ``value_path`` may be either parent-rooted (``loan.amount``) or relative to
    ``embed_field`` (``amount``).

    This is the simple preserve-shape sparse-embed oracle. Use
    ``present_missing_projection`` only for the ratio form that needs a denominator
    collection.
    """
    _require(params, "parent_collection", "embed_field", "value_path",
             "target_field", "missing_default")
    parents = _docs(snapshot, params["parent_collection"])
    embed_field = params["embed_field"]
    value_path = _embed_value_path(embed_field, params["value_path"])
    target = params["target_field"]
    default = params["missing_default"]
    out = []
    for p in parents:
        doc = dict(p)
        has_embed = _get(p, embed_field)[0]
        present, value = _get(p, value_path)
        doc[target] = value if has_embed and present and value is not None else default
        out.append(doc)
    return out


def _embed_value_path(embed_field: str, value_path: str) -> str:
    if value_path == embed_field or value_path.startswith(f"{embed_field}."):
        return value_path
    return f"{embed_field}.{value_path}"


def _resolve_embed_metric_path(
    docs: list[dict[str, Any]], embed_field: str, metric_field: Any
) -> str | None:
    if not isinstance(metric_field, str) or not metric_field:
        return None
    if any(_get(d, metric_field)[0] for d in docs):
        return metric_field
    embedded = _embed_value_path(embed_field, metric_field)
    if embedded != metric_field and any(_get(d, embedded)[0] for d in docs):
        return embedded
    nested = _first_numeric_embed_path(docs, embed_field)
    if nested is not None:
        return nested
    return metric_field


def _first_numeric_embed_path(docs: list[dict[str, Any]], embed_field: str) -> str | None:
    for doc in docs:
        present, embed = _get(doc, embed_field)
        if not present or not isinstance(embed, dict):
            continue
        for key in _METRIC_FIELD_PRIORITY:
            if _num(embed.get(key)) is not None:
                return f"{embed_field}.{key}"
        for key, value in embed.items():
            if _num(value) is not None:
                return f"{embed_field}.{key}"
    return None


def _subtype_cond_projection(snapshot: Snapshot, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Preserve docs, attaching a value chosen by subtype (missing subtype field -> default).

    params: {collection, discriminator, field_by_subtype:{value:field}, target_field, default}.
    """
    _require(params, "collection", "discriminator", "field_by_subtype", "target_field")
    docs = _docs(snapshot, params["collection"])
    disc, fbs, target = params["discriminator"], params["field_by_subtype"], params["target_field"]
    default = params.get("default", 0)
    out = []
    for d in docs:
        doc = dict(d)
        sub = _get(d, disc)[1]
        field = fbs.get(str(sub)) or fbs.get(sub)
        present, val = _get(d, field) if field else (False, None)
        doc[target] = val if present else default
        out.append(doc)
    return out


def _subtype_specific_field(snapshot: Snapshot, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one subtype's exclusive field. params: {collection, discriminator, subtype_value,
    field, project?}."""
    _require(params, "collection", "discriminator", "subtype_value", "field")
    docs = _docs(snapshot, params["collection"])
    disc, sv, field = params["discriminator"], params["subtype_value"], params["field"]
    project = params.get("project")
    out = []
    for d in docs:
        if _get(d, disc)[1] == sv:
            out.append({k: _get(d, k)[1] for k in project} if project else {field: _get(d, field)[1]})
    return out


def _has_vs_absent_compare(snapshot: Snapshot, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Aggregate a metric for parents that have vs lack a sparse optional embed.

    params: {parent_collection, embed_field, metric_field?, agg(count default)}.
    """
    _require(params, "parent_collection", "embed_field")
    docs = _docs(snapshot, params["parent_collection"])
    agg = _normal_agg(params.get("agg", "count"))
    metric = (
        None if agg == "count"
        else _resolve_embed_metric_path(docs, params["embed_field"], params.get("metric_field"))
    )
    groups: dict[str, list[float]] = {"present": [], "absent": []}
    for d in docs:
        key = "present" if _get(d, params["embed_field"])[0] else "absent"
        if metric:
            n = _num(_get(d, metric)[1])
            groups[key].append(n if n is not None else 0.0)
        else:
            groups[key].append(1.0)
    return [{"_id": k, "value": _aggregate(v, agg)} for k, v in groups.items()]


def _dynamic_key_fold(snapshot: Snapshot, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Fold an EAV attribute bag: group by the attribute-name column, aggregate values.

    params: {collection, name_field, value_field, agg}.
    """
    _require(params, "collection", "name_field", "value_field", "agg")
    docs = _docs(snapshot, params["collection"])
    nf, vf = params["name_field"], params["value_field"]
    buckets: dict[Any, list[float]] = {}
    for d in docs:
        name = _get(d, nf)[1]
        n = _num(_get(d, vf)[1])
        if n is not None:
            buckets.setdefault(name, []).append(n)
    return [{"_id": k, "value": _aggregate(v, params["agg"])} for k, v in buckets.items()]


def _cross_keyset_value(snapshot: Snapshot, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Read a value only from docs whose (heterogeneous) keyset contains ``key``.

    params: {collection, key, project?}.
    """
    _require(params, "collection", "key")
    docs = _docs(snapshot, params["collection"])
    key, project = params["key"], params.get("project")
    out = []
    for d in docs:
        present, val = _get(d, key)
        if present:
            out.append({k: _get(d, k)[1] for k in project} if project else {key: val})
    return out


def _cross_version_agg(snapshot: Snapshot, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Aggregate a field renamed across versions by coalescing candidate names.

    params: {collection, field_candidates:[old,new,...], agg, default}.
    """
    _require(params, "collection", "field_candidates", "agg")
    docs = _docs(snapshot, params["collection"])
    cands, default = params["field_candidates"], params.get("default", 0)
    vals = []
    for d in docs:
        chosen = None
        for f in cands:
            present, v = _get(d, f)
            if present and v is not None:
                chosen = _num(v)
                break
        vals.append(chosen if chosen is not None else float(default))
    return [{"value": _aggregate(vals, params["agg"])}]


def _join_nested_group(snapshot: Snapshot, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Unwind an embedded array, then group+aggregate.

    params: {collection, array_field, group_by, agg(count default), value_field?}.
    """
    _require(params, "collection", "array_field", "group_by")
    docs = _docs(snapshot, params["collection"])
    af, gb = params["array_field"], params["group_by"]
    vf, agg = params.get("value_field"), params.get("agg", "count")
    buckets: dict[Any, list[float]] = {}
    for d in docs:
        arr = _get(d, af)[1]
        if not isinstance(arr, list):
            continue
        for elem in arr:
            if not isinstance(elem, dict):
                continue
            key = _get(elem, gb)[1]
            if vf:
                n = _num(_get(elem, vf)[1])
                buckets.setdefault(key, []).append(n if n is not None else 0.0)
            else:
                buckets.setdefault(key, []).append(1.0)
    return [{"_id": k, "value": _aggregate(v, agg)} for k, v in buckets.items()]


_REGISTRY: dict[str, Oracle] = {
    # baseline (mechanism="none")
    "simple_filter": _simple_filter,
    "topn": _topn,
    "group_count": _group_count,
    "join_nested_group": _join_nested_group,
    # sparse_scalar
    "existence_count": _existence_count,
    "null_coalesce_agg": _null_coalesce_agg,
    # polymorphic
    "per_subtype_agg": _per_subtype_agg,
    "subtype_cond_projection": _subtype_cond_projection,
    "cross_subtype_compare": _per_subtype_agg,        # same naive R; comparison is the consumer's
    "subtype_specific_field": _subtype_specific_field,
    # sparse_embed
    "present_missing_projection": _present_missing_projection,
    "optional_embed_projection": _optional_embed_projection,
    "has_vs_absent_compare": _has_vs_absent_compare,
    # dynamic_key
    "dynamic_key_fold": _dynamic_key_fold,
    "cross_keyset_value": _cross_keyset_value,
    # versioning
    "cross_version_agg": _cross_version_agg,
}

_REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "simple_filter": ("collection",),
    "topn": ("collection", "sort_key", "n"),
    "group_count": ("collection", "group_by"),
    "join_nested_group": ("collection", "array_field", "group_by"),
    "existence_count": ("collection", "field"),
    "null_coalesce_agg": ("collection", "field", "agg"),
    "per_subtype_agg": ("collection", "discriminator", "field_by_subtype", "agg"),
    "subtype_cond_projection": (
        "collection", "discriminator", "field_by_subtype", "target_field",
    ),
    "cross_subtype_compare": ("collection", "discriminator", "field_by_subtype", "agg"),
    "subtype_specific_field": ("collection", "discriminator", "subtype_value", "field"),
    "present_missing_projection": (
        "parent_collection", "embed_field", "numerator_path", "target_field", "denom",
    ),
    "optional_embed_projection": (
        "parent_collection", "embed_field", "value_path", "target_field", "missing_default",
    ),
    "has_vs_absent_compare": ("parent_collection", "embed_field"),
    "dynamic_key_fold": ("collection", "name_field", "value_field", "agg"),
    "cross_keyset_value": ("collection", "key"),
    "cross_version_agg": ("collection", "field_candidates", "agg"),
}

_REQUIRED_NESTED_PARAMS: dict[str, dict[str, tuple[str, ...]]] = {
    "present_missing_projection": {
        "denom": ("collection", "local_id", "foreign_field", "sum_field"),
    },
}


def oracle_param_errors(template: str, params: Any) -> list[str]:
    """Return static parameter-contract errors for a reference oracle template."""
    if not has_oracle(template):
        return [f"unsupported reference_oracle.template {template!r}"]
    if not isinstance(params, dict):
        return ["reference_oracle.params must be an object"]
    errors: list[str] = []
    missing = [k for k in _REQUIRED_PARAMS.get(template, ()) if k not in params]
    if missing:
        errors.append(f"reference_oracle.params missing required keys: {missing}")
    for parent_key, nested_keys in _REQUIRED_NESTED_PARAMS.get(template, {}).items():
        if parent_key not in params:
            continue
        nested = params[parent_key]
        if not isinstance(nested, dict):
            errors.append(f"reference_oracle.params.{parent_key} must be an object")
            continue
        nested_missing = [k for k in nested_keys if k not in nested]
        if nested_missing:
            errors.append(
                f"reference_oracle.params.{parent_key} missing required keys: "
                f"{nested_missing}"
            )
    return errors


def has_oracle(template: str) -> bool:
    return template in _REGISTRY


def reference_oracle(template: str) -> Oracle:
    """Return the naive reference implementation R for an archetype template name."""
    try:
        return _REGISTRY[template]
    except KeyError as exc:
        raise OracleError(
            f"no reference oracle for template {template!r}; "
            f"implemented={sorted(_REGISTRY)}"
        ) from exc
