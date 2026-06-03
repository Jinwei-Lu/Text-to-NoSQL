"""MQL pipeline parsing, banned-operator scanning, and canonical_form_set logic.

The 6 banned operators ($sample/$rand/$$NOW/$out/$merge/$function) break deterministic,
read-only evaluation and are rejected at any pipeline depth. ``ast_check`` evaluates a
record's ``canonical_form_set`` (the thin RAR membership predicate); ``derive_canonical_
form_set`` produces one from a locked gold pipeline (idiom-invariants + output guard only).

MQL is a JS-flavored ``db.<coll>.aggregate([...])`` string with unquoted keys and single
quotes — json5 parses the pipeline array tolerantly.
"""
from __future__ import annotations

import re
from typing import Any

import json5

from ..errors import DisabledOperatorError, ResponseParseError

DISABLED_OPERATORS: frozenset[str] = frozenset(
    {"$sample", "$rand", "$out", "$merge", "$function"}
)
DISABLED_SYSTEM_VARS: frozenset[str] = frozenset({"$$NOW"})

#: structurally unavoidable for ANY correct idiom — the only ops a thin cfs may *require*
INVARIANT_STRUCTURAL_OPS: frozenset[str] = frozenset(
    {"$lookup", "$setWindowFields", "$facet", "$graphLookup", "$unionWith"}
)

_AGG_RE = re.compile(r"db\.([A-Za-z0-9_]+)\.aggregate\s*\(", re.DOTALL)


def _disabled_token_re(token: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(token)}(?![A-Za-z0-9_])")


def parse_pipeline(mql: str) -> tuple[str, list[dict[str, Any]]]:
    """Parse ``db.<coll>.aggregate([...])`` into ``(collection, pipeline_stages)``.

    Tolerant of JS syntax (unquoted keys, single quotes, trailing commas) via json5.
    """
    m = _AGG_RE.search(mql)
    if not m:
        raise ResponseParseError("not a db.<coll>.aggregate(...) pipeline",
                                 context={"preview": mql[:160]})
    collection = m.group(1)
    # find the matching bracket of the array argument
    start = mql.find("[", m.end() - 1)
    if start < 0:
        raise ResponseParseError("aggregate() missing array argument",
                                 context={"collection": collection})
    depth, end = 0, -1
    for i in range(start, len(mql)):
        c = mql[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        raise ResponseParseError("unbalanced [] in pipeline", context={"collection": collection})
    try:
        pipeline = json5.loads(mql[start : end + 1])
    except Exception as exc:  # noqa: BLE001 - json5 raises broad ValueError subclasses
        raise ResponseParseError("pipeline array is not parseable",
                                 context={"collection": collection,
                                          "preview": mql[start : start + 200]}) from exc
    if not isinstance(pipeline, list):
        raise ResponseParseError("pipeline is not a list", context={"collection": collection})
    return collection, pipeline


def _walk_keys(node: Any):
    """Yield every dict key (operator token) at any depth."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_keys(item)


def _walk_strings(node: Any):
    """Yield every string value at any depth."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _walk_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_strings(item)


def all_ops(pipeline: list[dict[str, Any]]) -> set[str]:
    return {k for k in _walk_keys(pipeline) if isinstance(k, str) and k.startswith("$")}


def root_ops(pipeline: list[dict[str, Any]]) -> set[str]:
    ops: set[str] = set()
    for stage in pipeline:
        if isinstance(stage, dict):
            ops.update(k for k in stage if isinstance(k, str) and k.startswith("$"))
    return ops


def disabled_tokens(pipeline: list[dict[str, Any]], *, raw_mql: str = "") -> set[str]:
    """Return disabled operators/system vars seen as keys or string tokens."""
    disabled = DISABLED_OPERATORS | DISABLED_SYSTEM_VARS
    hits = set(all_ops(pipeline) & disabled)
    for value in _walk_strings(pipeline):
        hits.update(token for token in disabled if _disabled_token_re(token).search(value))
    if raw_mql:
        hits.update(token for token in disabled if _disabled_token_re(token).search(raw_mql))
    return hits


def scan_disabled(mql: str) -> list[str]:
    """Return banned-operator hits (operators + $$NOW system var) found anywhere."""
    _, pipeline = parse_pipeline(mql)
    return sorted(disabled_tokens(pipeline, raw_mql=mql))


def assert_no_disabled(mql: str) -> None:
    hits = scan_disabled(mql)
    if hits:
        raise DisabledOperatorError("pipeline uses banned operators",
                                    context={"hits": hits})


def ast_check(mql: str, cfs: dict[str, Any]) -> tuple[bool, list[str]]:
    """Evaluate ``canonical_form_set`` membership. Returns ``(ok, reasons)``.

    cfs quadruple: must_contain / must_not_contain (any depth) and the *_at_root variants.
    """
    try:
        _, pipeline = parse_pipeline(mql)
    except ResponseParseError as exc:
        return False, [f"parse error: {exc}"]
    ops, rops = all_ops(pipeline), root_ops(pipeline)
    reasons: list[str] = []
    for op in sorted(disabled_tokens(pipeline, raw_mql=mql)):
        reasons.append(f"forbidden disabled token present {op}")
    for op in cfs.get("must_contain", []):
        if op not in ops:
            reasons.append(f"missing required op {op}")
    for op in cfs.get("must_not_contain", []):
        if op in ops:
            reasons.append(f"forbidden op present {op}")
    for op in cfs.get("must_contain_at_root", []):
        if op not in rops:
            reasons.append(f"missing required root op {op}")
    for op in cfs.get("must_not_contain_at_root", []):
        if op in rops:
            reasons.append(f"forbidden root op present {op}")
    return (not reasons), reasons


def static_mql_feedback(mql: str | None) -> list[dict[str, Any]]:
    """Static (non-execution) feedback on an MQL string.

    Returns a list of diagnostic dicts with at least ``severity`` and ``code`` keys.
    Covers three layers: blank/missing MQL, parse errors, and disabled-operator hits.
    """
    text = str(mql or "")
    if not text.strip():
        return [{"severity": "error", "code": "EMPTY_MQL", "message": "MQL is empty"}]
    try:
        collection, stages = parse_pipeline(text)
    except Exception as exc:  # noqa: BLE001 - parse errors are user-visible diagnostics
        return [{"severity": "error", "code": "PARSE_ERROR", "message": str(exc)}]
    feedback: list[dict[str, Any]] = [
        {"severity": "info", "code": "PARSE_OK", "collection": collection,
         "stages": len(stages)}
    ]
    disabled = scan_disabled(text)
    if disabled:
        feedback.append(
            {"severity": "error", "code": "DISABLED_OPERATOR", "operators": disabled}
        )
    return feedback


def derive_canonical_form_set(gold_mql: str, shape_policy: str) -> dict[str, list[str]]:
    """RAR thin cfs from a locked gold pipeline: idiom-invariants + output-space guard only.

    Never locks replaceable idiom ops ($addFields<->$project, $cond<->$switch<->$ifNull,
    $type<->$exists); structural discrimination is carried by the witness (L2/P3), not cfs.
    ``must_not_contain`` always carries the 6 banned operators (C6).
    """
    _, pipeline = parse_pipeline(gold_mql)
    ops, rops = all_ops(pipeline), root_ops(pipeline)
    must_contain = sorted(ops & INVARIANT_STRUCTURAL_OPS)
    must_contain_at_root = sorted(rops & INVARIANT_STRUCTURAL_OPS)
    must_not_contain = sorted(DISABLED_OPERATORS | DISABLED_SYSTEM_VARS)
    if shape_policy == "reduce":
        must_contain_at_root = sorted(set(must_contain_at_root) | {"$group"})
        must_not_contain_at_root: list[str] = []
    elif shape_policy == "preserve":
        must_not_contain_at_root = [
            op for op in ("$group", "$unwind")
            if op not in rops
        ]
    else:  # reshape
        must_not_contain_at_root = []
    return {
        "must_contain": must_contain,
        "must_not_contain": must_not_contain,
        "must_contain_at_root": must_contain_at_root,
        "must_not_contain_at_root": must_not_contain_at_root,
    }
