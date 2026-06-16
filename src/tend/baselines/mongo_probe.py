"""Minimal read-only Mongo probe surface for the baselines.

The fairness preprocess exploration (single-shot arms at `--witness-k 0`) and the
react arm's witness preload self-acquire structure through bounded, REDACTED
read-only probes. This module carries exactly the two operations those paths
use — `list_collections` and `run_readonly_probe` — ported verbatim from the
retired SMART-EG tool layer (the rest of that layer died with the solver).
Raw rows never cross this boundary: samples are redacted to hashed shapes, and
zero-match probes return the attempted (field, literal) pairs as typed guidance.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from ..execution.ast_check import render_mql, scan_disabled
from .safety import (
    DEFAULT_DOC_LIMIT,
    MAX_DOC_LIMIT,
    bounded_limit,
    redact_value,
    stable_hash,
    summarize_redacted_value,
)


class ReadonlyMongoProbe:
    """Bounded, redacted read-only Mongo observations for baseline loops."""

    def __init__(self, mongo: Any, db_id: str) -> None:
        self.mongo = mongo
        self.db_id = db_id

    def list_collections(self, _request: Mapping[str, Any] | None = None) -> dict[str, Any]:
        collections = self.mongo.list_collections(self.db_id)
        clean: list[Any] = []
        for item in collections:
            if isinstance(item, dict) and item.get("collection"):
                clean.append(
                    {
                        "collection": str(item.get("collection", "")),
                        "estimated_document_count": int(item.get("estimated_document_count", 0)),
                    }
                )
            elif isinstance(item, str) and item:
                clean.append(item)
        return {
            "tool": "list_collections",
            "db_id": self.db_id,
            "collection_count": len(clean),
            "collections": clean,
        }

    def run_readonly_probe(
        self,
        mql: str | Mapping[str, Any],
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        request = mql if isinstance(mql, Mapping) else {}
        marker_pipeline = _probe_request_pipeline(request)
        mql_text = _probe_mql_text(request, mql)
        probe_limit = bounded_limit(
            request.get("limit", limit) if request else limit,
            default=DEFAULT_DOC_LIMIT,
            maximum=MAX_DOC_LIMIT,
        )
        disabled = _disabled_hits_in_mql(mql_text)
        if disabled:
            raise ValueError(
                f"readonly_probe_error=disabled_operator: disabled operator in readonly probe: {disabled}"
            )
        if hasattr(self.mongo, "run_readonly_probe"):
            raw_result = self.mongo.run_readonly_probe(self.db_id, mql_text, limit=probe_limit)
        else:
            raw_result = self.mongo.aggregate_readonly_bounded(
                self.db_id,
                mql_text,
                limit=probe_limit,
            )
        result = _redact_probe_result(raw_result)
        collection_name = _probe_collection_name(request, result, mql_text)
        mql_hash = stable_hash(mql_text)
        result_summary = _probe_result_summary(result)
        if collection_name:
            result.setdefault("collection", collection_name)
        result["tool"] = "run_readonly_probe"
        result["db_id"] = self.db_id
        result["mql_hash"] = mql_hash
        result["request"] = _probe_request_summary(
            request,
            mql,
            mql_text,
            probe_limit,
            mql_hash,
        )
        matched_value_markers = _positive_match_value_markers(marker_pipeline, result)
        if matched_value_markers:
            result["matched_value_markers"] = matched_value_markers
        unmatched_value_literals = _unmatched_value_literals(marker_pipeline, result)
        if unmatched_value_literals:
            result["unmatched_value_literals"] = unmatched_value_literals
            result["guidance"] = (
                "these literals matched 0 documents; enumerate the field's distinct values "
                "(execute_mql candidate [{\"$group\": {\"_id\": \"$<field>\"}}]) and pick the "
                "matching real value before re-running the $match."
            )
        result["rendered_mql"] = mql_text
        result["rendered_query"] = mql_text
        result["result_summary"] = result_summary
        redaction = result.get("redaction")
        if not isinstance(redaction, dict):
            redaction = {}
        redaction.setdefault("raw_rows", False)
        if "redacted_sample_shape" in result:
            redaction.setdefault("sample", "redacted_shape")
        result["redaction"] = redaction
        return result


def _redact_probe_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        out = dict(result)
    else:
        out = {"result": redact_value(result)}
    if "sample" in out:
        out["redacted_sample_shape"] = redact_value(out.pop("sample"))
    return out


def _probe_result_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    collection = result.get("collection")
    if collection:
        summary["collection"] = str(collection)
    count = result.get("count")
    if isinstance(count, int) and not isinstance(count, bool):
        summary["count"] = count
    if "redacted_sample_shape" in result:
        summary["sample_redacted"] = True
    if not summary:
        summary["keys"] = sorted(str(key) for key in result)[:8]
    return summary


def _probe_request_pipeline(request: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    if not request:
        return None
    candidate = request.get("MQL") or request.get("mql")
    if isinstance(candidate, str) and candidate.strip().startswith("["):
        try:
            return _parse_pipeline_json(candidate)
        except ValueError:
            return None
    pipeline = request.get("pipeline")
    if pipeline is None:
        pipeline = request.get("stages")
    if isinstance(pipeline, list) and all(isinstance(stage, dict) for stage in pipeline):
        return pipeline
    return None


def _positive_match_value_markers(
    pipeline: list[dict[str, Any]] | None,
    result: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if not pipeline or _probe_positive_count(result) <= 0:
        return []
    markers: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add_value(value: Any, *, field: str | None, operator: str) -> None:
        if value is None:
            return
        if isinstance(value, str) and value.startswith("$"):
            return
        if not isinstance(value, (str, int, float, bool)):
            return
        summary = summarize_redacted_value(value, expose_literal=True, include_proof=True)
        key = (
            str(field or ""),
            operator,
            str(summary.get("token") or summary.get("hash") or value),
        )
        if key in markers:
            return
        summary.update(
            {
                "source": "positive_match_probe",
                "operator": operator,
                "match_result_count": _probe_positive_count(result),
            }
        )
        if field:
            summary["field"] = field
        markers[key] = summary

    def visit_expr(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit_expr(item)
            return
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            if key == "$eq" and isinstance(child, list):
                field = next(
                    (
                        str(item).lstrip("$")
                        for item in child
                        if isinstance(item, str) and item.startswith("$") and not item.startswith("$$")
                    ),
                    None,
                )
                if field:
                    for item in child:
                        add_value(item, field=field, operator="$eq")
            elif key in {"$and", "$or"} and isinstance(child, list):
                for item in child:
                    visit_expr(item)
            else:
                visit_expr(child)

    def visit_match(match: Any) -> None:
        if not isinstance(match, dict):
            return
        for raw_key, child in match.items():
            key = str(raw_key)
            if key in {"$and", "$or"} and isinstance(child, list):
                for item in child:
                    visit_match(item)
                continue
            if key == "$expr":
                visit_expr(child)
                continue
            if key.startswith("$"):
                continue
            if isinstance(child, dict):
                eq_value = child.get("$eq")
                add_value(eq_value, field=key, operator="$eq")
                in_values = child.get("$in")
                if isinstance(in_values, list) and len(in_values) == 1:
                    add_value(in_values[0], field=key, operator="$in")
                continue
            add_value(child, field=key, operator="$eq")

    for stage in pipeline:
        match = stage.get("$match")
        if isinstance(match, dict):
            visit_match(match)
    return list(markers.values())


def _unmatched_value_literals(
    pipeline: list[dict[str, Any]] | None,
    result: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Harvest the (field, literal) grounding pairs a probe attempted via `$eq`/`$in`
    when it matched zero documents, so a fired value_grounding gate can steer the model
    toward enumerating the field's real distinct values instead of guessing again."""
    if not pipeline or _probe_positive_count(result) > 0:
        return []
    pairs: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add_value(value: Any, *, field: str | None, operator: str) -> None:
        if not field:
            return
        if isinstance(value, str) and value.startswith("$"):
            return
        if not isinstance(value, (str, int, float, bool)):
            return
        summary = summarize_redacted_value(value, expose_literal=True, include_proof=True)
        token = str(summary.get("token") or summary.get("hash") or value)
        key = (str(field), operator, token)
        if key in pairs:
            return
        entry: dict[str, Any] = {
            "field": str(field),
            "operator": operator,
            "value": summary,
        }
        if "literal" in summary:
            entry["literal"] = summary["literal"]
        pairs[key] = entry

    def visit_expr(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit_expr(item)
            return
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            if key == "$eq" and isinstance(child, list):
                field = next(
                    (
                        str(item).lstrip("$")
                        for item in child
                        if isinstance(item, str)
                        and item.startswith("$")
                        and not item.startswith("$$")
                    ),
                    None,
                )
                if field:
                    for item in child:
                        add_value(item, field=field, operator="$eq")
            elif key in {"$and", "$or"} and isinstance(child, list):
                for item in child:
                    visit_expr(item)
            else:
                visit_expr(child)

    def visit_match(match: Any) -> None:
        if not isinstance(match, dict):
            return
        for raw_key, child in match.items():
            key = str(raw_key)
            if key in {"$and", "$or"} and isinstance(child, list):
                for item in child:
                    visit_match(item)
                continue
            if key == "$expr":
                visit_expr(child)
                continue
            if key.startswith("$"):
                continue
            if isinstance(child, dict):
                add_value(child.get("$eq"), field=key, operator="$eq")
                in_values = child.get("$in")
                if isinstance(in_values, list):
                    for item in in_values:
                        add_value(item, field=key, operator="$in")
                continue
            add_value(child, field=key, operator="$eq")

    for stage in pipeline:
        match = stage.get("$match")
        if isinstance(match, dict):
            visit_match(match)
    return list(pairs.values())


def _probe_positive_count(result: Mapping[str, Any]) -> int:
    for key in ("result_count", "count", "row_count"):
        value = result.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def _probe_collection_name(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    mql_text: str,
) -> str:
    requested = request.get("collection") if request else None
    if requested:
        return str(requested)
    result_collection = result.get("collection")
    if result_collection:
        return str(result_collection)
    return _collection_from_rendered_mql(mql_text)


def _probe_request_summary(
    request: Mapping[str, Any],
    mql: Any,
    mql_text: str,
    limit: int,
    mql_hash: str,
) -> dict[str, Any]:
    collection = _probe_collection_name(request, {}, mql_text)
    summary: dict[str, Any] = {}
    if collection:
        summary["collection"] = collection
    summary["limit"] = limit
    summary["mql_hash"] = mql_hash
    summary["source"] = _probe_request_source(request, mql)
    return summary


def _probe_request_source(request: Mapping[str, Any], mql: Any) -> str:
    if not request:
        return "raw_mql" if isinstance(mql, str) else "unknown"
    candidate = request.get("MQL") or request.get("mql")
    if isinstance(candidate, str) and candidate.strip().startswith("["):
        return "collection_raw_pipeline"
    if isinstance(candidate, str) and candidate.strip():
        return "raw_mql"
    if request.get("pipeline") is not None or request.get("stages") is not None:
        return "collection_pipeline"
    return "unknown"


def _collection_from_rendered_mql(mql_text: str) -> str:
    stripped = mql_text.strip()
    if not stripped.startswith("db."):
        return ""
    remainder = stripped[3:]
    for marker in (".aggregate", ".find"):
        if marker in remainder:
            collection = remainder.split(marker, 1)[0]
            return collection.strip()
    return ""


def _disabled_hits_in_mql(mql: str) -> list[str]:
    try:
        return scan_disabled(mql)
    except Exception:
        return []


def _probe_mql_text(request: Mapping[str, Any], mql: Any) -> str:
    if not request:
        if isinstance(mql, str) and mql.strip():
            return mql
        raise ValueError(
            "readonly_probe_error=missing_query: readonly probe requires MQL or collection and pipeline"
        )

    candidate = request.get("MQL") or request.get("mql")
    collection = str(request.get("collection") or "").strip()
    pipeline = request.get("pipeline")
    if pipeline is None:
        pipeline = request.get("stages")

    if isinstance(candidate, str) and candidate.strip().startswith("["):
        pipeline = _parse_pipeline_json(candidate)
        candidate = None
    if isinstance(candidate, str) and candidate.strip():
        return candidate

    if not collection:
        raise ValueError(
            "readonly_probe_error=missing_collection: readonly probe collection is required when MQL is omitted"
        )
    if not isinstance(pipeline, list) or not all(isinstance(stage, dict) for stage in pipeline):
        raise ValueError(
            "readonly_probe_error=invalid_pipeline: readonly probe pipeline must be a list of stage objects"
        )
    return render_mql(collection, pipeline)


def _parse_pipeline_json(value: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "readonly_probe_error=invalid_pipeline_json: readonly probe raw pipeline string must be valid JSON"
        ) from exc
    if not isinstance(parsed, list) or not all(isinstance(stage, dict) for stage in parsed):
        raise ValueError(
            "readonly_probe_error=invalid_pipeline: readonly probe raw pipeline string must decode to stage objects"
        )
    return parsed
