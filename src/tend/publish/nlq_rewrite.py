"""LLM-backed anti-template NLQ rewrite for release datasets."""
from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..execution.ast_check import parse_pipeline
from ..llm import LLMClient
from ..observability import RunLogger
from ..release_layout import resolve_release_dataset_layout
from .quality import _nlq_alignment_issues
from .repair import (
    _context_sources_for_nlq,
    _dynamic_sources_for_nlq,
    _field_refs_for_nlq,
    _first_limit,
    _group_keys_for_nlq,
    _load_pairs,
    _literal_constants_for_nlq,
    _output_fields_for_nlq,
    _predicate_parts_for_nlq,
    _sort_parts_for_nlq,
    _threshold_numbers_for_nlq,
    _write_release_files,
)


@dataclass(frozen=True, slots=True)
class LLMRewriteSummary:
    records: int
    calls_ok: int
    calls_failed: int
    invalid_rewrites: int
    applied_updates: int
    anti_template_violations: int
    out_dir: str
    paths: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "calls_ok": self.calls_ok,
            "calls_failed": self.calls_failed,
            "invalid_rewrites": self.invalid_rewrites,
            "applied_updates": self.applied_updates,
            "anti_template_violations": self.anti_template_violations,
            "out_dir": self.out_dir,
            "paths": self.paths,
        }


_REWRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "record_id",
        "db_id",
        "canonical_nlq",
        "colloquial_nlq",
        "semantic_preservation",
    ],
    "properties": {
        "record_id": {"type": ["integer", "string"]},
        "db_id": {"type": "string"},
        "canonical_nlq": {"type": "string", "minLength": 40},
        "colloquial_nlq": {"type": "string", "minLength": 40},
        "semantic_preservation": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "collection_preserved",
                "filters_preserved",
                "sort_and_limit_preserved",
                "grouping_preserved",
                "outputs_preserved",
                "notes",
            ],
            "properties": {
                "collection_preserved": {"type": "boolean"},
                "filters_preserved": {"type": "boolean"},
                "sort_and_limit_preserved": {"type": "boolean"},
                "grouping_preserved": {"type": "boolean"},
                "outputs_preserved": {"type": "boolean"},
                "notes": {"type": "string"},
            },
        },
    },
}

_SYSTEM_PROMPT = """You rewrite benchmark natural-language questions.

The MQL is the ground truth. Rewrite both NLQs so they remain fully faithful to the MQL but no longer look generated from a slot template.

Return only a JSON object. Do not use markdown.

Hard requirements:
- Keep the same record_id and db_id.
- Do not change, simplify, or add MQL semantics.
- Preserve the collection, limit, sort priority, filters, constants, numeric thresholds, boolean logic, dynamic-key or nested-array behavior, group keys, result shape, and output fields when those semantics exist.
- When a descending sort is paired with a limit, both NLQs must explicitly use a top/ranked/highest/most/maximum-style phrase and must name the descending sort metric with recognizable tokens. For synthetic metrics, include the readable words from the field name, such as "native dynamic key count", "above threshold", "observed", or "loan account share". If needed, include the exact field path in parentheses, such as "(ranked by native dynamic key count descending)". Replace dots and underscores with spaces so validators can see tokens such as "score", "native", "dynamic", "key", and "count". Do not rely on "first" alone.
- Include every numeric threshold and every answer-changing string constant verbatim somewhere in each NLQ.
- Write natural benchmark questions, not audit checklists.
- The canonical_nlq should be precise and formal, but still natural English.
- The colloquial_nlq should sound like a realistic user request, while keeping the same answer-changing constraints.
- Avoid a repeated global frame. Vary wording across records.

Forbidden template artifacts:
- Do not start with "On `".
- Do not start with "Show the top".
- Do not use the phrases "predicate fields", "output fields", "require constants", "apply numeric thresholds", "expand dynamic sources", "context source", "reference fields", "using constants", "with fields", or "aggregate groups".
- Do not expose native_query_pattern as a user-facing label.
- Avoid semicolon-separated slot lists.

Required JSON shape:
{
  "record_id": <same record_id>,
  "db_id": "<same db_id>",
  "canonical_nlq": "<complete rewritten canonical NLQ>",
  "colloquial_nlq": "<complete rewritten colloquial NLQ>",
  "semantic_preservation": {
    "collection_preserved": true,
    "filters_preserved": true,
    "sort_and_limit_preserved": true,
    "grouping_preserved": true,
    "outputs_preserved": true,
    "notes": "<short explanation>"
  }
}
"""

_BANNED_TEMPLATE_PHRASES = (
    "predicate fields",
    "output fields",
    "require constants",
    "apply numeric thresholds",
    "expand dynamic sources",
    "context source",
    "reference fields",
    "using constants",
    "with fields",
    "aggregate groups",
    "native_query_pattern",
)
_TRACKS = ("canonical", "colloquial")


async def run_llm_nlq_rewrite(
    dataset_dir: str | Path,
    *,
    llm: LLMClient,
    logger: RunLogger,
    out_dir: str | Path,
    db_id: str | None = None,
    record_ids: set[int] | None = None,
    limit: int | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    thinking: str | None = "enabled",
    first_token_timeout_s: float = 6.0,
    call_timeout_s: float = 900.0,
    workers: int = 2500,
    apply: bool = False,
    allow_partial_apply: bool = False,
    style_repair_retries: int = 1,
    resume: bool = True,
) -> LLMRewriteSummary:
    """Run one LLM JSON-mode anti-template rewrite call per selected record."""

    layout = resolve_release_dataset_layout(dataset_dir)
    records = _load_records(layout.tend_path if layout.tend_path.exists() else layout.test_path)
    selected = _select_records(records, db_id=db_id, record_ids=record_ids, limit=limit)
    selected_keys = {
        (str(record.get("db_id") or ""), record.get("record_id"))
        for record in selected
    }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "rewrite_results.jsonl"
    applied_path = out / "applied_updates.jsonl"
    anti_template_json_path = out / "anti_template_report.json"
    report_path = out / "rewrite_report.md"

    if not resume and results_path.exists():
        results_path.unlink()
    selected_key_set = selected_keys
    row_by_key = _load_existing_rows(results_path) if resume else {}
    pending = [
        record
        for record in selected
        if row_by_key.get(_record_key(record), {}).get("status") != "ok"
    ]
    sem = asyncio.Semaphore(max(1, workers))

    async def rewrite_one(record: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            log = logger.bind(
                db_id=str(record.get("db_id") or ""),
                record_id=record.get("record_id"),
            )
            messages = _messages_for_record(record)
            rewrite: dict[str, Any] = {}
            validation_errors: list[str] = []
            result = None
            style_attempts = 0
            for style_attempt in range(max(0, style_repair_retries) + 1):
                style_attempts = style_attempt
                completion = llm.complete(
                    agent="nlq_template_rewrite",
                    logger=log,
                    messages=messages,
                    schema=_REWRITE_SCHEMA,
                    response_format={"type": "json_object"},
                    model=model,
                    reasoning_effort=reasoning_effort,
                    thinking=thinking,
                    stream=True,
                    first_token_timeout_s=first_token_timeout_s,
                    json_repair_retries=1,
                )
                if call_timeout_s > 0:
                    result = await asyncio.wait_for(completion, timeout=call_timeout_s)
                else:
                    result = await completion
                rewrite = result.data
                validation_errors = _rewrite_validation_errors(record, rewrite)
                if not validation_errors:
                    break
                if style_attempt >= max(0, style_repair_retries):
                    break
                messages = messages + [
                    {
                        "role": "assistant",
                        "content": json.dumps(rewrite, ensure_ascii=False),
                    },
                    {
                        "role": "user",
                        "content": _style_repair_prompt(validation_errors),
                    },
                ]
            status = "ok" if not validation_errors else "invalid"
            return {
                "status": status,
                "db_id": record.get("db_id"),
                "record_id": record.get("record_id"),
                "rewrite": rewrite,
                "validation_errors": validation_errors,
                "style_repair_attempts": style_attempts,
                "transcript_ref": result.transcript_ref,
                "diagnostics_ref": result.diagnostics_ref,
                "model": result.model,
                "usage": result.usage,
            }

    async def guarded_rewrite(record: dict[str, Any]) -> dict[str, Any]:
        try:
            return await rewrite_one(record)
        except Exception as exc:  # noqa: BLE001 - preserve per-record failures
            return {
                "status": "error",
                "db_id": record.get("db_id"),
                "record_id": record.get("record_id"),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

    tasks = [asyncio.create_task(guarded_rewrite(record)) for record in pending]
    with results_path.open("a", encoding="utf-8") as fp:
        for task in asyncio.as_completed(tasks):
            row = await task
            row_by_key[_row_key(row)] = row
            fp.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            fp.flush()

    rows = _sorted_rows(row_by_key.values())
    _write_jsonl(results_path, rows)
    selected_rows = [
        row_by_key[key]
        for key in sorted(selected_key_set, key=lambda item: (item[0], str(item[1])))
        if key in row_by_key
    ]

    calls_failed = sum(1 for row in selected_rows if row.get("status") == "error")
    invalid_rewrites = sum(1 for row in selected_rows if row.get("status") == "invalid")
    applied_rows: list[dict[str, Any]] = []
    apply_allowed = apply and (allow_partial_apply or (calls_failed == 0 and invalid_rewrites == 0))
    if apply_allowed:
        applied_rows = _apply_rewrite_rows(
            records,
            selected_rows,
            selected_keys=selected_keys,
        )
        if applied_rows:
            _write_release_files(layout.root, records, _load_pairs(layout.root))
    _write_jsonl(applied_path, applied_rows)

    anti_template_report = build_anti_template_report(records)
    anti_template_json_path.write_text(
        json.dumps(anti_template_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary_dict = {
        "records": len(selected),
        "calls_ok": sum(1 for row in selected_rows if row.get("status") == "ok"),
        "calls_failed": calls_failed,
        "invalid_rewrites": invalid_rewrites,
        "applied_updates": len(applied_rows),
        "anti_template_violations": int(anti_template_report["violations"]),
        "apply_requested": apply,
        "apply_allowed": apply_allowed,
        "allow_partial_apply": allow_partial_apply,
        "resume": resume,
        "pending_records": len(pending),
        "out_dir": str(out),
    }
    report_path.write_text(
        _render_report(summary_dict, rows, applied_rows, anti_template_report),
        encoding="utf-8",
    )
    paths = {
        "results_jsonl": str(results_path),
        "applied_updates_jsonl": str(applied_path),
        "anti_template_report_json": str(anti_template_json_path),
        "report_md": str(report_path),
    }
    return LLMRewriteSummary(
        records=summary_dict["records"],
        calls_ok=summary_dict["calls_ok"],
        calls_failed=summary_dict["calls_failed"],
        invalid_rewrites=summary_dict["invalid_rewrites"],
        applied_updates=summary_dict["applied_updates"],
        anti_template_violations=summary_dict["anti_template_violations"],
        out_dir=str(out),
        paths=paths,
    )


def build_anti_template_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize visible template artifacts in current release NLQ text."""

    tracks: dict[str, dict[str, Any]] = {}
    total_violations = 0
    for track in _TRACKS:
        texts = [_record_nlq(record, track) for record in records]
        prefix_on = sum(text.startswith("On `") for text in texts)
        prefix_show_top = sum(text.startswith("Show the top") for text in texts)
        phrase_counts = {
            phrase: sum(phrase in text.lower() for text in texts)
            for phrase in _BANNED_TEMPLATE_PHRASES
        }
        semicolon_slot_lists = sum(text.count(";") >= 2 for text in texts)
        skeleton_counts = Counter(_skeleton(text) for text in texts)
        track_violations = (
            prefix_on
            + prefix_show_top
            + semicolon_slot_lists
            + sum(phrase_counts.values())
        )
        total_violations += track_violations
        tracks[track] = {
            "records": len(texts),
            "unique_texts": len(set(texts)),
            "starts_on_backtick": prefix_on,
            "starts_show_the_top": prefix_show_top,
            "semicolon_slot_lists": semicolon_slot_lists,
            "banned_phrase_counts": phrase_counts,
            "skeleton_unique": len(skeleton_counts),
            "top_skeletons": [
                {"count": count, "skeleton": skeleton}
                for skeleton, count in skeleton_counts.most_common(8)
            ],
            "violations": track_violations,
        }
    return {
        "records": len(records),
        "violations": total_violations,
        "tracks": tracks,
    }


def _load_records(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = raw.get("records", []) if isinstance(raw, dict) else raw
    return [record for record in records if isinstance(record, dict)]


def _select_records(
    records: list[dict[str, Any]],
    *,
    db_id: str | None,
    record_ids: set[int] | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    selected = records
    if db_id:
        selected = [record for record in selected if str(record.get("db_id") or "") == db_id]
    if record_ids:
        selected = [
            record
            for record in selected
            if int(record.get("record_id") or -1) in record_ids
        ]
    if limit is not None:
        selected = selected[:max(0, int(limit))]
    return selected


def _record_key(record: dict[str, Any]) -> tuple[str, Any]:
    return (str(record.get("db_id") or ""), record.get("record_id"))


def _row_key(row: dict[str, Any]) -> tuple[str, Any]:
    return (str(row.get("db_id") or ""), row.get("record_id"))


def _sorted_rows(rows: Any) -> list[dict[str, Any]]:
    return sorted(
        [row for row in rows if isinstance(row, dict)],
        key=lambda row: (str(row.get("db_id") or ""), str(row.get("record_id") or "")),
    )


def _load_existing_rows(
    path: Path,
    selected_keys: set[tuple[str, Any]] | None = None,
) -> dict[tuple[str, Any], dict[str, Any]]:
    if not path.exists():
        return {}
    rows: dict[tuple[str, Any], dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        key = _row_key(row)
        if selected_keys is None or key in selected_keys:
            rows[key] = row
    return rows


def _messages_for_record(record: dict[str, Any]) -> list[dict[str, str]]:
    nlq = record.get("nl_queries") if isinstance(record.get("nl_queries"), dict) else {}
    mql = str(record.get("MQL") or "")
    payload = {
        "task": "Rewrite both NLQs to remove template artifacts while preserving MQL semantics.",
        "record_id": record.get("record_id"),
        "db_id": record.get("db_id"),
        "native_query_pattern": record.get("native_query_pattern"),
        "current_canonical_nlq": nlq.get("canonical", ""),
        "current_colloquial_nlq": nlq.get("colloquial", ""),
        "mql": mql,
        "mql_semantic_atoms": _mql_semantic_atoms(mql),
        "forbidden_template_artifacts": {
            "prefixes": ["On `", "Show the top"],
            "phrases": list(_BANNED_TEMPLATE_PHRASES),
            "style": "No semicolon-separated audit slots or field-label inventories.",
        },
    }
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]


def _mql_semantic_atoms(mql: str) -> dict[str, Any]:
    try:
        collection, pipeline = parse_pipeline(mql)
    except Exception as exc:  # noqa: BLE001 - prompt context should include parse failure
        return {"parse_error": f"{type(exc).__name__}: {exc}"}
    return {
        "collection": collection,
        "limit": _first_limit(pipeline),
        "sort": _sort_parts_for_nlq(pipeline),
        "constants": _literal_constants_for_nlq(pipeline),
        "numeric_thresholds": _threshold_numbers_for_nlq(pipeline),
        "predicates": _predicate_parts_for_nlq(pipeline)[:20],
        "dynamic_or_nested_sources": _dynamic_sources_for_nlq(pipeline)[:16],
        "group_keys": _group_keys_for_nlq(pipeline)[:16],
        "context_sources": _context_sources_for_nlq(pipeline)[:8],
        "referenced_fields": _field_refs_for_nlq(pipeline)[:28],
        "final_output_fields": _output_fields_for_nlq(pipeline),
        "pipeline": pipeline,
    }


def _rewrite_validation_errors(record: dict[str, Any], rewrite: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    expected_record_id = str(record.get("record_id"))
    expected_db_id = str(record.get("db_id") or "")
    if str(rewrite.get("record_id")) != expected_record_id:
        errors.append(
            f"record_id mismatch: expected {expected_record_id}, got {rewrite.get('record_id')}"
        )
    if str(rewrite.get("db_id") or "") != expected_db_id:
        errors.append(f"db_id mismatch: expected {expected_db_id}, got {rewrite.get('db_id')}")
    old_nlq = record.get("nl_queries") if isinstance(record.get("nl_queries"), dict) else {}
    pipeline: list[dict[str, Any]] | None = None
    try:
        _collection, pipeline = parse_pipeline(str(record.get("MQL") or ""))
    except Exception as exc:  # noqa: BLE001 - parse failures are handled by release validation too
        errors.append(f"MQL parse failed during rewrite validation: {type(exc).__name__}: {exc}")
    seen: set[str] = set()
    for track, key in (("canonical", "canonical_nlq"), ("colloquial", "colloquial_nlq")):
        text = str(rewrite.get(key) or "").strip()
        if len(text) < 40:
            errors.append(f"{track} rewrite is too short")
        if text == str(old_nlq.get(track) or "").strip():
            errors.append(f"{track} rewrite did not change")
        if text in seen:
            errors.append(f"{track} rewrite duplicates another track")
        seen.add(text)
        for violation in anti_template_violations_for_text(text):
            errors.append(f"{track} template artifact: {violation}")
        if pipeline is not None:
            for issue in _nlq_alignment_issues(record, track=track, text=text, pipeline=pipeline):
                evidence = _compact_alignment_evidence(issue.evidence)
                errors.append(
                    f"{track} semantic audit {issue.code}: {issue.message}; evidence={evidence}"
                )
    preservation = rewrite.get("semantic_preservation")
    if isinstance(preservation, dict):
        for key in (
            "collection_preserved",
            "filters_preserved",
            "sort_and_limit_preserved",
            "grouping_preserved",
            "outputs_preserved",
        ):
            if preservation.get(key) is not True:
                errors.append(f"semantic_preservation.{key} is not true")
    return errors


def _compact_alignment_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in (
        "constant",
        "threshold",
        "sort_specs",
        "limit",
        "missing_fields",
        "mql_output_fields",
    ):
        if key in evidence:
            compact[key] = evidence[key]
    return compact


def anti_template_violations_for_text(text: str) -> list[str]:
    normalized = text.lower()
    violations: list[str] = []
    if text.startswith("On `"):
        violations.append("starts with On `")
    if text.startswith("Show the top"):
        violations.append("starts with Show the top")
    for phrase in _BANNED_TEMPLATE_PHRASES:
        if phrase in normalized:
            violations.append(f"contains banned phrase {phrase!r}")
    if text.count(";") >= 2:
        violations.append("contains semicolon-separated slot list")
    return violations


def _style_repair_prompt(validation_errors: list[str]) -> str:
    return (
        "Your previous JSON was semantically useful but failed local anti-template "
        "validation. Return the same JSON shape again with corrected canonical_nlq "
        "and colloquial_nlq. Do not change the intended MQL semantics. Remove these "
        "style defects: "
        + json.dumps(validation_errors, ensure_ascii=False)
        + "\nAvoid labels such as 'Output fields:' or 'with fields'. Use natural "
        "phrasing such as 'The answer should include ...' or 'Include ... in the result'. "
        "If the defect is NLQ_HIDDEN_TOP_BY, use a top/ranked/highest/most phrase "
        "and explicitly name the descending sort metric tokens from the evidence. "
        "Copy the sort_specs field path into a natural parenthetical when needed, "
        "but replace dots and underscores with spaces, for example "
        "'ranked by native dynamic key count descending' or "
        "'fixtures score for, highest first'. "
        "If the defect is a hidden threshold or constant, include that exact number "
        "or string literal verbatim in the NLQ. Do not use 'first' by itself for "
        "a limited sorted result. "
        "Return only the corrected JSON object."
    )


def _apply_rewrite_rows(
    records: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    selected_keys: set[tuple[str, Any]],
) -> list[dict[str, Any]]:
    by_key = {(str(record.get("db_id") or ""), record.get("record_id")): record for record in records}
    applied: list[dict[str, Any]] = []
    for row in rows:
        if row.get("status") != "ok" or not isinstance(row.get("rewrite"), dict):
            continue
        key = (str(row.get("db_id") or ""), row.get("record_id"))
        if key not in selected_keys:
            continue
        record = by_key.get(key)
        if record is None:
            continue
        nlq = record.setdefault("nl_queries", {})
        if not isinstance(nlq, dict):
            continue
        rewrite = row["rewrite"]
        for track, rewrite_key in (("canonical", "canonical_nlq"), ("colloquial", "colloquial_nlq")):
            before = str(nlq.get(track) or "")
            after = str(rewrite.get(rewrite_key) or "").strip()
            if not after or before == after:
                continue
            nlq[track] = after
            applied.append({
                "db_id": record.get("db_id"),
                "record_id": record.get("record_id"),
                "track": track,
                "before": before,
                "after": after,
            })
    return applied


def _record_nlq(record: dict[str, Any], track: str) -> str:
    nlq = record.get("nl_queries") if isinstance(record.get("nl_queries"), dict) else {}
    return str(nlq.get(track) or "")


def _skeleton(text: str) -> str:
    skeleton = re.sub(r"`[^`]+`", "`X`", text)
    skeleton = re.sub(r"\b\d+(?:\.\d+)?\b", "N", skeleton)
    skeleton = re.sub(r'"[^"]*"', '"X"', skeleton)
    skeleton = re.sub(r"'[^']*'", "'X'", skeleton)
    return skeleton[:260]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _render_report(
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
    applied_rows: list[dict[str, Any]],
    anti_template_report: dict[str, Any],
) -> str:
    lines = [
        "# LLM NLQ Anti-Template Rewrite",
        "",
        "## Summary",
        "",
        f"- Records: {summary['records']}",
        f"- Calls OK: {summary['calls_ok']}",
        f"- Calls failed: {summary['calls_failed']}",
        f"- Invalid rewrites: {summary['invalid_rewrites']}",
        f"- Applied updates: {len(applied_rows)}",
        f"- Apply requested: {summary['apply_requested']}",
        f"- Apply allowed: {summary['apply_allowed']}",
        f"- Anti-template violations after run: {anti_template_report['violations']}",
        "",
        "## Anti-Template Tracks",
        "",
    ]
    for track, data in anti_template_report["tracks"].items():
        lines.extend([
            f"### {track}",
            "",
            f"- Unique texts: {data['unique_texts']} / {data['records']}",
            f"- Starts `On \\``: {data['starts_on_backtick']}",
            f"- Starts `Show the top`: {data['starts_show_the_top']}",
            f"- Semicolon slot lists: {data['semicolon_slot_lists']}",
            f"- Violations: {data['violations']}",
            "",
        ])
    failed = [row for row in rows if row.get("status") != "ok"]
    if failed:
        lines.extend(["## Failed Or Invalid Rows", ""])
        for row in failed[:40]:
            lines.append(
                f"- {row.get('status')} db={row.get('db_id')} "
                f"record={row.get('record_id')}: "
                f"{row.get('error') or row.get('validation_errors')}"
            )
        if len(failed) > 40:
            lines.append(f"- ... {len(failed) - 40} more")
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "LLMRewriteSummary",
    "anti_template_violations_for_text",
    "build_anti_template_report",
    "run_llm_nlq_rewrite",
]
