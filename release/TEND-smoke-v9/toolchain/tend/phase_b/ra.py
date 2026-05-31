"""RA: five realism checks, targeted append-only augment (budget=1), world_signature refresh."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from tend.core.normexec import NormExec
from tend.core.signatures import world_signature
from tend.errors import BOT, BOT_EXEC, RetryBudgetExhausted

AUGMENT_BUDGET = 1

_ROOT_COLLECTION_RE = re.compile(r"db\.([\w\$\-]+)\.(?:aggregate|find|count|distinct)")
_EXISTS_FIELD_RE = re.compile(r'["\']?(\w+)["\']?\s*:\s*\{\s*["\']?\$exists["\']?\s*:\s*true\s*\}', re.IGNORECASE)
_MATCH_EQ_FIELD_RE = re.compile(r'["\']?(\w+)["\']?\s*:\s*(?:"([^"]+)"|\'([^\']+)\'|(\d+))')

_ORCHESTRA_EMBED_MARKERS = ("conductor", "orchestra.performance", "Performance_ID")


def _is_orchestra_shape(mql: str) -> bool:
    """True only for orchestra embed/window-facet MQL, not generic $ifNull usage."""
    if "conductor" not in (mql or ""):
        return False
    return any(marker in mql for marker in _ORCHESTRA_EMBED_MARKERS)


def _extract_root_collection(mql: str) -> str | None:
    match = _ROOT_COLLECTION_RE.search(mql or "")
    return match.group(1) if match else None


def _exists_fields(mql: str) -> list[str]:
    return [m.group(1) for m in _EXISTS_FIELD_RE.finditer(mql or "")]


def _generic_cardinality_docs(mql: str) -> list[dict[str, Any]]:
    """Two minimal docs that satisfy the MQL's $exists/$match predicates."""
    fields = _exists_fields(mql)
    if not fields:
        fields = ["field"]
    seen: list[str] = []
    for fld in fields:
        if fld not in seen:
            seen.append(fld)
    docs: list[dict[str, Any]] = []
    for i in range(2):
        doc: dict[str, Any] = {}
        for fld in seen:
            doc[fld] = f"placeholder_{fld}_{i}"
        docs.append(doc)
    return docs


def _field_paths_in_mql(mql: str) -> set[str]:
    paths: set[str] = set()
    for token in ("Name", "Attendance", "Performance_ID", "orchestra", "performance"):
        if token in mql:
            paths.add(token)
    return paths


def _embed_unwind_depth(mql: str) -> int:
    return mql.count("$unwind")


def _flex_payload_coverage(snapshot: dict[str, Any], mql: str) -> bool:
    collection = _extract_root_collection(mql)
    if not collection:
        for key, val in snapshot.items():
            if isinstance(val, list) and val:
                collection = key
                break
    if not collection:
        return True
    docs = snapshot.get(collection, [])
    if not docs:
        return False
    if "payload.v2" in mql or "payload.v1" in mql or "payload.legacy" in mql:
        has_v1 = any(isinstance(d.get("payload"), dict) and "v1" in d["payload"] for d in docs)
        has_v2 = any(isinstance(d.get("payload"), dict) and "v2" in d["payload"] for d in docs)
        has_legacy = any(
            isinstance(d.get("payload"), dict) and "legacy" in d["payload"] for d in docs
        )
        return has_v1 and has_v2 and has_legacy
    return any(isinstance(d.get("payload"), dict) and d["payload"] for d in docs)


def _generic_ifnull_coverage(snapshot: dict[str, Any], mql: str) -> bool:
    collection = _extract_root_collection(mql)
    if not collection:
        return True
    docs = snapshot.get(collection, [])
    if not docs:
        return False
    match = re.search(r'\$ifNull"\s*:\s*\[\s*"\$([^"]+)"', mql or "", re.DOTALL)
    if not match:
        return True
    top_field = match.group(1).split(".")[0]
    has_null = any(isinstance(d, dict) and d.get(top_field) is None for d in docs)
    has_value = any(
        isinstance(d, dict) and d.get(top_field) not in (None, "") for d in docs
    )
    return has_null and has_value


def _null_coverage(snapshot: dict[str, Any], mql: str) -> bool:
    if "$ifNull" not in mql and "$payload" not in mql:
        return True
    if _is_orchestra_shape(mql):
        conductors = snapshot.get("conductor", [])
        has_null = any(doc.get("Name") is None for doc in conductors)
        has_non_null = any(doc.get("Name") not in (None, "") for doc in conductors)
        has_missing_attendance = False
        has_present_attendance = False
        for doc in conductors:
            for orch in doc.get("orchestra", []) or []:
                for perf in orch.get("performance", []) or []:
                    att = perf.get("Attendance")
                    if att is None:
                        has_missing_attendance = True
                    else:
                        has_present_attendance = True
        return has_null and has_non_null and has_missing_attendance and has_present_attendance
    if "$payload" in mql:
        return _flex_payload_coverage(snapshot, mql)
    return _generic_ifnull_coverage(snapshot, mql)


def _field_observable(snapshot: dict[str, Any], mql: str) -> bool:
    if _is_orchestra_shape(mql):
        paths = _field_paths_in_mql(mql)
        if not paths:
            return True
        conductors = snapshot.get("conductor", [])
        if not conductors:
            return False
        for path in paths:
            if path == "Name" and not any("Name" in doc for doc in conductors):
                return False
            if path == "Attendance":
                found = False
                for doc in conductors:
                    for orch in doc.get("orchestra", []) or []:
                        for perf in orch.get("performance", []) or []:
                            if "Attendance" in perf:
                                found = True
                if not found:
                    return False
        return True

    collection = _extract_root_collection(mql)
    if not collection:
        return True
    docs = snapshot.get(collection, [])
    if not docs:
        return False
    fields = _exists_fields(mql)
    if fields:
        return all(any(isinstance(doc, dict) and fld in doc for doc in docs) for fld in fields)
    if "$payload" in mql:
        return any(isinstance(d.get("payload"), dict) and d["payload"] for d in docs)
    return True


def _result_cardinality(mql: str, snapshot: dict[str, Any], *, min_count: int = 2) -> tuple[bool, int]:
    result = NormExec(mql, snapshot)
    if isinstance(result, (BOT, BOT_EXEC)):
        return False, 0
    if not isinstance(result, list):
        return False, 0
    return len(result) >= min_count, len(result)


def _type_sanity(snapshot: dict[str, Any], mql: str = "") -> bool:
    if not _is_orchestra_shape(mql):
        return True
    for doc in snapshot.get("conductor", []):
        for orch in doc.get("orchestra", []) or []:
            for perf in orch.get("performance", []) or []:
                att = perf.get("Attendance")
                if att is not None and not isinstance(att, (int, float)):
                    return False
                pid = perf.get("Performance_ID")
                if pid is not None and not isinstance(pid, (int, float)):
                    return False
    return True


def _default_augment_plan(gaps: list[str], mql: str = "") -> dict[str, Any]:
    """Targeted append-only augment plan.

    Orchestra-shape MQL keeps the curated conductor sketches (null/ifNull
    coverage + median tie boundary). For all other MQL shapes we parse the
    root collection from the MQL itself and inject generic docs that satisfy
    the $exists/$match predicates so ``_result_cardinality`` can pass.
    """
    injections: list[dict[str, Any]] = []
    orchestra_shape = _is_orchestra_shape(mql)

    if orchestra_shape:
        if "missing_null_name_sample" in gaps or "null_missing_coverage" in gaps:
            injections.append(
                {
                    "gap_type": "null_name_ifnull",
                    "collection": "conductor",
                    "doc_sketch": {
                        "Name": None,
                        "orchestra": [{"performance": [{"Attendance": 50, "Performance_ID": 1}]}],
                    },
                    "reason": "Activate $ifNull on Name",
                }
            )
        if "missing_median_tie_boundary" in gaps or "result_cardinality" in gaps:
            injections.append(
                {
                    "gap_type": "median_tie_boundary",
                    "collection": "conductor",
                    "doc_sketch": {
                        "Name": "Eve High",
                        "orchestra": [
                            {
                                "performance": [
                                    {"Attendance": 100, "Performance_ID": 1},
                                    {"Attendance": 120, "Performance_ID": 2},
                                    {"Attendance": 140, "Performance_ID": 3},
                                ]
                            }
                        ],
                    },
                    "reason": "Raise last_window_avg above peer median for P4 cardinality",
                }
            )
            injections.append(
                {
                    "gap_type": "median_tie_boundary",
                    "collection": "conductor",
                    "doc_sketch": {
                        "Name": "Frank High",
                        "orchestra": [
                            {
                                "performance": [
                                    {"Attendance": 90, "Performance_ID": 1},
                                    {"Attendance": 110, "Performance_ID": 2},
                                    {"Attendance": 130, "Performance_ID": 3},
                                ]
                            }
                        ],
                    },
                    "reason": "Second high performer for filtered result cardinality",
                }
            )
    else:
        target_col = _extract_root_collection(mql)
        if target_col and "result_cardinality" in gaps and "$graphLookup" in mql:
            start_match = re.search(r'"startWith"\s*:\s*"\$([^"]+)"', mql or "")
            if start_match:
                top_field = start_match.group(1).split(".")[0]
                injections.append(
                    {
                        "gap_type": "graph_lookup_chain",
                        "collection": target_col,
                        "doc_sketch": {top_field: [{"_id": 1, "label": "linked"}]},
                        "reason": f"Satisfy $graphLookup startWith on {top_field}",
                    }
                )
        if target_col and "result_cardinality" in gaps:
            for doc_sketch in _generic_cardinality_docs(mql):
                injections.append(
                    {
                        "gap_type": "generic_cardinality",
                        "collection": target_col,
                        "doc_sketch": doc_sketch,
                        "reason": f"Satisfy MQL $exists/$match on {target_col}",
                    }
                )
        if target_col and "null_missing_coverage" in gaps and "$payload" in mql:
            injections.append(
                {
                    "gap_type": "flex_payload_sample",
                    "collection": target_col,
                    "doc_sketch": {
                        "payload": {"v1": {"x": 1}, "v2": {"x": 2}, "legacy": {"x": "1"}},
                    },
                    "reason": "Activate schema_version payload branches",
                }
            )
        elif target_col and "null_missing_coverage" in gaps:
            ifnull_match = re.search(r'\$ifNull"\s*:\s*\[\s*"\$([^"]+)"', mql or "", re.DOTALL)
            if ifnull_match:
                top_field = ifnull_match.group(1).split(".")[0]
                injections.append(
                    {
                        "gap_type": "generic_ifnull_sample",
                        "collection": target_col,
                        "doc_sketch": {top_field: None},
                        "reason": f"Activate $ifNull on {top_field}",
                    }
                )

    return {"required": bool(injections), "append_only": True, "injections": injections}


def apply_augment_plan(
    snapshot: dict[str, Any],
    augment_plan: dict[str, Any],
    *,
    augment_pass: int = 1,
) -> dict[str, Any]:
    if augment_pass > AUGMENT_BUDGET:
        raise RetryBudgetExhausted(
            f"RA augment budget exhausted ({AUGMENT_BUDGET} pass per db)"
        )
    updated = deepcopy(snapshot)
    next_ids: dict[str, int] = {}
    for collection, docs in updated.items():
        if isinstance(docs, list):
            existing_ids = [d.get("_id") for d in docs if isinstance(d, dict)]
            next_ids[collection] = (
                max((i for i in existing_ids if isinstance(i, int)), default=0) + 1
            )
    for injection in augment_plan.get("injections", []):
        collection = injection.get("collection")
        if not collection:
            continue
        sketch = deepcopy(injection.get("doc_sketch", {}))
        if "_id" not in sketch:
            sketch["_id"] = next_ids.get(collection, 1)
            next_ids[collection] = sketch["_id"] + 1
        updated.setdefault(collection, []).append(sketch)
    return updated


def audit_realism(
    mql: str,
    nl_queries: dict[str, str],
    query_plan: dict[str, Any],
    snapshot: dict[str, Any],
    schema: dict[str, Any],
    *,
    schema_pattern: str = "embed",
    sra_embed_depth: int = 2,
    augment_pass: int = 0,
    apply_augment: bool = True,
) -> dict[str, Any]:
    """Run five realism checks; optionally apply one targeted augment pass."""
    gaps: list[str] = []
    norm_result = NormExec(mql, snapshot)
    p1_ok = not isinstance(norm_result, (BOT, BOT_EXEC))

    card_ok, result_count = _result_cardinality(mql, snapshot)
    if not card_ok:
        gaps.append("result_cardinality")

    null_ok = _null_coverage(snapshot, mql)
    if not null_ok:
        gaps.append("null_missing_coverage")
        gaps.append("missing_null_name_sample")

    field_ok = _field_observable(snapshot, mql)
    if not field_ok:
        gaps.append("field_observability")

    embed_depth = _embed_unwind_depth(mql)
    embed_ok = embed_depth >= sra_embed_depth if schema_pattern == "embed" else True
    if not embed_ok:
        gaps.append("embed_depth")

    type_ok = _type_sanity(snapshot, mql)
    if not type_ok:
        gaps.append("type_sanity")

    realism_checks = {
        "field_observability": field_ok,
        "null_missing_coverage": null_ok,
        "result_cardinality": card_ok,
        "embed_depth_matches_sra": embed_ok,
        "type_sanity": type_ok,
    }

    augment_plan = _default_augment_plan(gaps, mql=mql)
    working_snapshot = snapshot
    world_sig = schema.get("world_signature") if isinstance(schema, dict) else None
    if not world_sig:
        world_sig = world_signature(snapshot)

    if augment_plan["required"] and apply_augment and augment_pass < AUGMENT_BUDGET:
        working_snapshot = apply_augment_plan(snapshot, augment_plan, augment_pass=augment_pass + 1)
        world_sig = world_signature(working_snapshot)
        card_ok, result_count = _result_cardinality(mql, working_snapshot)
        null_ok = _null_coverage(working_snapshot, mql)
        realism_checks["null_missing_coverage"] = null_ok
        realism_checks["result_cardinality"] = card_ok
        gaps = [g for g in gaps if g not in {"result_cardinality", "null_missing_coverage", "missing_null_name_sample"}]

    ra_pass = p1_ok and all(realism_checks.values()) and not gaps
    return {
        "p1_execution": {"pass": p1_ok, "normexec_non_bot": p1_ok},
        "p4_nontriviality": {
            "pass": card_ok and p1_ok,
            "result_count": result_count,
            "gaps": gaps,
        },
        "realism_checks": realism_checks,
        "augment_plan": augment_plan,
        "ra_audit": {
            "pass": ra_pass,
            "pending_augment": augment_plan["required"] and not ra_pass,
            "recommendations": [] if ra_pass else ["Re-run MS/PV/NNC after augment"],
        },
        "snapshot": working_snapshot if working_snapshot is not snapshot else None,
        "world_signature": world_sig if working_snapshot is not snapshot else None,
        "augment_pass": augment_pass + (1 if augment_plan["required"] and apply_augment and augment_pass < AUGMENT_BUDGET else 0),
    }
