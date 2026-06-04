"""LLM-backed NLQ-first gold-query review and repair for release datasets."""
from __future__ import annotations

import asyncio
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ExecutionError
from ..execution.ast_check import all_ops, assert_no_disabled, derive_canonical_form_set, parse_pipeline
from ..execution.mongo import MongoExecutor
from ..execution.signature import (
    canonical_json,
    mql_signature,
    mql_skeleton_signature,
    mql_skeleton_summary,
)
from ..llm import LLMClient
from ..observability import RunLogger
from ..release_layout import resolve_release_dataset_layout
from .repair import _load_pairs, _refresh_native_metadata_after_mql_change, _write_release_files
from .validate import _load_native_manifests, _load_native_provenance, _validate_native_record


@dataclass(frozen=True, slots=True)
class LLMGoldReviewSummary:
    records: int
    calls_ok: int
    calls_failed: int
    invalid_reviews: int
    gold_valid: int
    not_gold: int
    candidate_mqls: int
    candidate_exec_ok: int
    candidate_exec_failed: int
    manual_required: int
    applied_updates: int
    out_dir: str
    paths: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "calls_ok": self.calls_ok,
            "calls_failed": self.calls_failed,
            "invalid_reviews": self.invalid_reviews,
            "gold_valid": self.gold_valid,
            "not_gold": self.not_gold,
            "candidate_mqls": self.candidate_mqls,
            "candidate_exec_ok": self.candidate_exec_ok,
            "candidate_exec_failed": self.candidate_exec_failed,
            "manual_required": self.manual_required,
            "applied_updates": self.applied_updates,
            "out_dir": self.out_dir,
            "paths": self.paths,
        }


_VERDICTS = (
    "gold_valid",
    "mql_needs_rewrite",
    "nlq_needs_rewrite",
    "not_gold_template",
    "reject",
)
_ACTIONS = (
    "keep_current",
    "replace_mql",
    "replace_both",
    "replace_nlq",
    "manual_only",
    "reject",
)
_TRACKS = ("canonical", "colloquial")
_MAX_SCHEMA_LIST_ITEMS = 24
_MAX_SAMPLE_DOCS = 3
_MAX_PROMPT_STRING_CHARS = 240
_MAX_PROMPT_LIST_ITEMS = 8
_MAX_PROMPT_DICT_ITEMS = 18
_MAX_PROMPT_DEPTH = 5

_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "record_id",
        "db_id",
        "verdict",
        "repair_action",
        "confidence",
        "pipeline_rigidity_score",
        "natural_intent",
        "current_mql_assessment",
        "corrected_mql",
        "corrected_canonical_nlq",
        "corrected_colloquial_nlq",
        "requires_human_review",
        "evidence",
    ],
    "properties": {
        "record_id": {"type": ["integer", "string"]},
        "db_id": {"type": "string"},
        "verdict": {"type": "string", "enum": list(_VERDICTS)},
        "repair_action": {"type": "string", "enum": list(_ACTIONS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "pipeline_rigidity_score": {"type": "integer", "minimum": 0, "maximum": 5},
        "natural_intent": {"type": "string", "minLength": 20},
        "current_mql_assessment": {"type": "string", "minLength": 20},
        "corrected_mql": {"type": ["string", "null"]},
        "corrected_canonical_nlq": {"type": ["string", "null"]},
        "corrected_colloquial_nlq": {"type": ["string", "null"]},
        "requires_human_review": {"type": "boolean"},
        "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
}

_SYSTEM_PROMPT = """You are repairing an NL2MQL benchmark record.

The current MQL is NOT ground truth. Treat the current NLQs and the current MQL as
suspect evidence. Your job is to decide whether this record is a genuine NLQ-first
gold query or a template/pipeline-caption artifact.

Return only a JSON object. Do not use markdown.

Gold standard:
- A gold NLQ should describe a realistic domain-facing information need.
- A gold MQL should be the MongoDB aggregation that answers that NLQ.
- Do not preserve a bad MQL merely because the NLQ was rewritten to match it.
- Do not preserve pipeline-caption language such as "unwind", "project", "convert
  object to key-value pairs", "native_context_bucket", "native_dynamic_entries",
  "native_matching_dynamic_entries", "native_key", "native_value", or schema_state
  unless the user question is explicitly about MongoDB internals.
- Corrected NLQs must not name the physical collection, raw dotted field paths,
  generated output aliases such as _id/above_threshold/observed, or exact pipeline
  staging. Avoid snake_case schema or alias tokens; use ordinary English phrases.
  They should use domain nouns such as members, events, schools, teams, molecules,
  accounts, districts, cards, seasons, or patients.
- Corrected MQL output should use user-facing field aliases where practical. Avoid
  returning grouped _id objects or generated helper aliases in a repaired gold query.
- If the current record is template/archetype-captioned, propose a repaired
  NLQ/MQL pair from the domain intent, using the same database and preferably the
  same broad collection/complexity when that remains meaningful.
- Corrected MQL must be a single read-only string of the form
  db.<collection>.aggregate([...]).
- Never use $sample, $rand, $$NOW, $out, $merge, or $function.
- If you are not confident the corrected MQL is semantically valid, set
  requires_human_review=true and repair_action="manual_only".

Required JSON shape:
{
  "record_id": <same record_id>,
  "db_id": "<same db_id>",
  "verdict": "gold_valid" | "mql_needs_rewrite" | "nlq_needs_rewrite" |
    "not_gold_template" | "reject",
  "repair_action": "keep_current" | "replace_mql" | "replace_both" |
    "replace_nlq" | "manual_only" | "reject",
  "confidence": <number from 0 to 1>,
  "pipeline_rigidity_score": <integer 0 to 5, where 5 is template/pipeline-caption>,
  "natural_intent": "<domain-facing intent you believe should define the gold query>",
  "current_mql_assessment": "<specific assessment of current MQL vs the intent>",
  "corrected_mql": null or "db.<collection>.aggregate([...])",
  "corrected_canonical_nlq": null or "<formal natural benchmark question>",
  "corrected_colloquial_nlq": null or "<realistic user request>",
  "requires_human_review": true or false,
  "evidence": ["<short concrete evidence strings>"]
}
"""

_ANTI_GOLD_TEXT_PATTERNS = (
    re.compile(r"\bnative_[A-Za-z0-9_]*\b", re.IGNORECASE),
    re.compile(r"\bschema_state\b", re.IGNORECASE),
    re.compile(r"\babove_threshold\b", re.IGNORECASE),
    re.compile(r"\bobserved\b", re.IGNORECASE),
    re.compile(r"\bobjectToArray\b", re.IGNORECASE),
    re.compile(r"\bunwind\b", re.IGNORECASE),
    re.compile(r"\bproject(?:ing|ion)?\b", re.IGNORECASE),
    re.compile(r"\bpipeline\b", re.IGNORECASE),
    re.compile(r"\bcollection\b", re.IGNORECASE),
    re.compile(r"\b[A-Za-z]+_[A-Za-z0-9_]*\b"),
    re.compile(r"\bkey[- ]value pairs?\b", re.IGNORECASE),
    re.compile(r"\bconvert(?:ing)? .* object\b", re.IGNORECASE),
    re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_ -]+){2,}\b"),
)


async def run_llm_gold_query_review(
    dataset_dir: str | Path,
    *,
    llm: LLMClient,
    logger: RunLogger,
    executor: MongoExecutor,
    out_dir: str | Path,
    db_id: str | None = None,
    record_ids: set[int] | None = None,
    limit: int | None = None,
    model: str | None = "deepseek-v4-flash",
    reasoning_effort: str | None = "max",
    thinking: str | None = "enabled",
    first_token_timeout_s: float = 6.0,
    call_timeout_s: float = 900.0,
    workers: int = 2500,
    apply: bool = False,
    allow_nlq_only_apply: bool = False,
    auto_apply_min_confidence: float = 0.82,
    quality_repair_retries: int = 1,
    candidate_repair_retries: int = 0,
    retry_invalid: bool = False,
    resume: bool = True,
    include_current_exec: bool = True,
) -> LLMGoldReviewSummary:
    """Run one NLQ-first gold review call per selected record."""

    layout = resolve_release_dataset_layout(dataset_dir)
    records = _load_records(layout.tend_path if layout.tend_path.exists() else layout.test_path)
    selected = _select_records(records, db_id=db_id, record_ids=record_ids, limit=limit)
    selected_keys = {_record_key(record) for record in selected}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "gold_review_results.jsonl"
    candidate_validation_path = out / "candidate_validation.jsonl"
    applied_path = out / "applied_updates.jsonl"
    manual_ids_path = out / "manual_required_record_ids.txt"
    summary_path = out / "summary.json"
    report_path = out / "gold_review_report.md"

    if not resume and results_path.exists():
        results_path.unlink()
    row_by_key = _load_existing_rows(results_path, selected_keys=selected_keys) if resume else {}
    completed_statuses = {"ok"} if retry_invalid else {"ok", "invalid"}
    pending = [
        record
        for record in selected
        if row_by_key.get(_record_key(record), {}).get("status") not in completed_statuses
    ]
    schemas = _load_schema_summaries(layout, selected)
    current_exec_records = selected if candidate_repair_retries > 0 else pending
    current_exec = (
        _current_execution_summaries(layout, current_exec_records, executor=executor, logger=logger)
        if include_current_exec and current_exec_records
        else {}
    )
    records_by_key = {_record_key(record): record for record in selected}
    sem = asyncio.Semaphore(max(1, workers))

    async def review_one(record: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            log = logger.bind(
                db_id=str(record.get("db_id") or ""),
                record_id=record.get("record_id"),
            )
            messages = _messages_for_record(
                record,
                schema_summary=schemas.get(str(record.get("db_id") or ""), {}),
                current_exec=current_exec.get(_record_key(record)),
            )
            result = None
            review: dict[str, Any] = {}
            validation_errors: list[str] = []
            quality_attempts = 0
            try:
                for quality_attempt in range(max(0, quality_repair_retries) + 1):
                    quality_attempts = quality_attempt
                    completion = llm.complete(
                        agent="gold_query_review",
                        logger=log,
                        messages=messages,
                        schema=_REVIEW_SCHEMA,
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
                    review = result.data
                    validation_errors = _review_validation_errors(record, review)
                    if not validation_errors:
                        break
                    if quality_attempt >= max(0, quality_repair_retries):
                        break
                    messages = messages + [
                        {"role": "assistant", "content": json.dumps(review, ensure_ascii=False)},
                        {"role": "user", "content": _quality_repair_prompt(validation_errors)},
                    ]
                return {
                    "status": "ok" if not validation_errors else "invalid",
                    "db_id": record.get("db_id"),
                    "record_id": record.get("record_id"),
                    "review": review,
                    "validation_errors": validation_errors,
                    "quality_repair_attempts": quality_attempts,
                    "transcript_ref": result.transcript_ref,
                    "diagnostics_ref": result.diagnostics_ref,
                    "model": result.model,
                    "usage": result.usage,
                }
            except Exception as exc:  # noqa: BLE001 - preserve per-record failure
                return {
                    "status": "error",
                    "db_id": record.get("db_id"),
                    "record_id": record.get("record_id"),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }

    async def feedback_repair_one(
        row: dict[str, Any],
        candidate: dict[str, Any] | None,
        manual_reason: str,
        attempt: int,
    ) -> dict[str, Any]:
        record = records_by_key.get(_row_key(row))
        if record is None:
            return {
                "status": "error",
                "db_id": row.get("db_id"),
                "record_id": row.get("record_id"),
                "error_type": "MissingRecord",
                "error": "selected record not found for feedback repair",
            }
        async with sem:
            log = logger.bind(
                db_id=str(record.get("db_id") or ""),
                record_id=record.get("record_id"),
                feedback_attempt=attempt,
            )
            messages = _messages_for_record(
                record,
                schema_summary=schemas.get(str(record.get("db_id") or ""), {}),
                current_exec=current_exec.get(_record_key(record)),
            ) + [
                {
                    "role": "assistant",
                    "content": json.dumps(row.get("review") or {}, ensure_ascii=False),
                },
                {
                    "role": "user",
                    "content": _candidate_feedback_prompt(
                        row,
                        candidate,
                        manual_reason=manual_reason,
                        attempt=attempt,
                    ),
                },
            ]
            result = None
            review: dict[str, Any] = {}
            validation_errors: list[str] = []
            quality_attempts = 0
            try:
                for quality_attempt in range(max(0, quality_repair_retries) + 1):
                    quality_attempts = quality_attempt
                    completion = llm.complete(
                        agent="gold_query_candidate_repair",
                        logger=log,
                        messages=messages,
                        schema=_REVIEW_SCHEMA,
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
                    review = result.data
                    validation_errors = _review_validation_errors(record, review)
                    if not validation_errors:
                        break
                    if quality_attempt >= max(0, quality_repair_retries):
                        break
                    messages = messages + [
                        {"role": "assistant", "content": json.dumps(review, ensure_ascii=False)},
                        {"role": "user", "content": _quality_repair_prompt(validation_errors)},
                    ]
                return {
                    "status": "ok" if not validation_errors else "invalid",
                    "db_id": record.get("db_id"),
                    "record_id": record.get("record_id"),
                    "review": review,
                    "validation_errors": validation_errors,
                    "quality_repair_attempts": quality_attempts,
                    "feedback_repair_attempt": attempt,
                    "previous_manual_reason": manual_reason,
                    "transcript_ref": result.transcript_ref,
                    "diagnostics_ref": result.diagnostics_ref,
                    "model": result.model,
                    "usage": result.usage,
                }
            except Exception as exc:  # noqa: BLE001 - preserve per-record failure
                return {
                    "status": "error",
                    "db_id": record.get("db_id"),
                    "record_id": record.get("record_id"),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "feedback_repair_attempt": attempt,
                    "previous_manual_reason": manual_reason,
                }

    tasks = [asyncio.create_task(review_one(record)) for record in pending]
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
        for key in sorted(selected_keys, key=lambda item: (item[0], str(item[1])))
        if key in row_by_key
    ]
    candidate_rows = _validate_candidate_mqls(
        layout,
        selected_rows,
        records_by_key=records_by_key,
        executor=executor,
        logger=logger,
    )

    for attempt in range(1, max(0, candidate_repair_retries) + 1):
        feedback_targets = _feedback_repair_targets(
            selected_rows,
            candidate_rows,
            allow_nlq_only_apply=allow_nlq_only_apply,
            min_confidence=auto_apply_min_confidence,
        )
        if not feedback_targets:
            break
        tasks = [
            asyncio.create_task(feedback_repair_one(row, candidate, reason, attempt))
            for row, candidate, reason in feedback_targets
        ]
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
            for key in sorted(selected_keys, key=lambda item: (item[0], str(item[1])))
            if key in row_by_key
        ]
        candidate_rows = _validate_candidate_mqls(
            layout,
            selected_rows,
            records_by_key=records_by_key,
            executor=executor,
            logger=logger,
        )
    _write_jsonl(candidate_validation_path, candidate_rows)

    applied_rows: list[dict[str, Any]] = []
    if apply:
        applied_rows = _apply_safe_repairs(
            records,
            selected_rows,
            candidate_rows,
            selected_keys=selected_keys,
            allow_nlq_only_apply=allow_nlq_only_apply,
            min_confidence=auto_apply_min_confidence,
        )
        if applied_rows:
            _write_release_files(layout.root, records, _load_pairs(layout.root))
    _write_jsonl(applied_path, applied_rows)

    manual_rows = _manual_required_rows(
        selected_rows,
        candidate_rows,
        applied_rows=applied_rows,
        allow_nlq_only_apply=allow_nlq_only_apply,
        min_confidence=auto_apply_min_confidence,
    )
    manual_ids_path.write_text(
        "".join(f"{row.get('db_id')},{row.get('record_id')}\n" for row in manual_rows),
        encoding="utf-8",
    )
    summary_dict = _summary(
        selected_rows,
        candidate_rows,
        applied_rows=applied_rows,
        manual_rows=manual_rows,
        out=out,
    )
    summary_path.write_text(
        json.dumps(summary_dict, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report_path.write_text(
        _render_report(summary_dict, selected_rows, candidate_rows, applied_rows, manual_rows),
        encoding="utf-8",
    )
    paths = {
        "results_jsonl": str(results_path),
        "candidate_validation_jsonl": str(candidate_validation_path),
        "applied_updates_jsonl": str(applied_path),
        "manual_required_record_ids": str(manual_ids_path),
        "summary_json": str(summary_path),
        "report_md": str(report_path),
    }
    return LLMGoldReviewSummary(
        records=summary_dict["records"],
        calls_ok=summary_dict["calls_ok"],
        calls_failed=summary_dict["calls_failed"],
        invalid_reviews=summary_dict["invalid_reviews"],
        gold_valid=summary_dict["gold_valid"],
        not_gold=summary_dict["not_gold"],
        candidate_mqls=summary_dict["candidate_mqls"],
        candidate_exec_ok=summary_dict["candidate_exec_ok"],
        candidate_exec_failed=summary_dict["candidate_exec_failed"],
        manual_required=summary_dict["manual_required"],
        applied_updates=summary_dict["applied_updates"],
        out_dir=str(out),
        paths=paths,
    )


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
    *,
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


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _load_schema_summaries(
    layout: Any,
    records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for current_db_id in sorted({str(record.get("db_id") or "") for record in records}):
        path = layout.mongodb_schema_dir / f"{current_db_id}.json"
        if not path.exists():
            summaries[current_db_id] = {"error": f"missing schema file: {path}"}
            continue
        schema = json.loads(path.read_text(encoding="utf-8"))
        audit = schema.get("structure_audit") if isinstance(schema.get("structure_audit"), dict) else {}
        summaries[current_db_id] = {
            "db_id": schema.get("db_id", current_db_id),
            "collections": schema.get("collections", {}),
            "source_tables": schema.get("source_tables", []),
            "structure_audit": {
                "collection_counts": audit.get("collection_counts", {}),
                "max_depth": audit.get("max_depth"),
                "dynamic_key_paths": _head(audit.get("dynamic_key_paths")),
                "nested_array_paths": _head(audit.get("nested_array_paths")),
                "dynamic_array_object_paths": _head(audit.get("dynamic_array_object_paths")),
                "array_object_dynamic_paths": _head(audit.get("array_object_dynamic_paths")),
                "presence_state_counts": audit.get("presence_state_counts", {}),
            },
        }
    return summaries


def _head(value: Any, *, limit: int = _MAX_SCHEMA_LIST_ITEMS) -> Any:
    if isinstance(value, list):
        return value[:limit]
    return value


def _current_execution_summaries(
    layout: Any,
    records: list[dict[str, Any]],
    *,
    executor: MongoExecutor,
    logger: RunLogger,
) -> dict[tuple[str, Any], dict[str, Any]]:
    out: dict[tuple[str, Any], dict[str, Any]] = {}
    by_db: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_db[str(record.get("db_id") or "")].append(record)
    for current_db_id, db_records in sorted(by_db.items()):
        collections = _load_mongodb_data(layout, current_db_id)
        if not isinstance(collections, dict):
            for record in db_records:
                out[_record_key(record)] = {"ok": False, "error": "mongodb data is not an object"}
            continue
        executor.load_witness(current_db_id, collections)
        for record in db_records:
            mql = str(record.get("MQL") or "")
            try:
                rows = executor.norm_exec(current_db_id, mql)
                out[_record_key(record)] = _exec_summary(rows)
            except Exception as exc:  # noqa: BLE001 - prompt evidence only
                logger.warning(
                    "gold_review_current_exec_failed",
                    db_id=current_db_id,
                    record_id=record.get("record_id"),
                    error_type=type(exc).__name__,
                    error=str(exc)[:300],
                )
                out[_record_key(record)] = {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:300],
                }
    return out


def _load_mongodb_data(layout: Any, db_id: str) -> Any:
    path = layout.mongodb_data_dir / f"{db_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _exec_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ok": True,
        "result_count": len(rows),
        "sample_docs": _compact_for_prompt(rows[:_MAX_SAMPLE_DOCS]),
        "shape": _shape_summary(rows[: min(len(rows), 20)]),
    }


def _compact_for_prompt(value: Any, *, depth: int = 0) -> Any:
    if depth >= _MAX_PROMPT_DEPTH:
        return f"<{_type_name(value)} truncated>"
    if isinstance(value, str):
        if len(value) <= _MAX_PROMPT_STRING_CHARS:
            return value
        return value[:_MAX_PROMPT_STRING_CHARS] + f"... <truncated {len(value)} chars>"
    if isinstance(value, list):
        head = [
            _compact_for_prompt(item, depth=depth + 1)
            for item in value[:_MAX_PROMPT_LIST_ITEMS]
        ]
        if len(value) > _MAX_PROMPT_LIST_ITEMS:
            head.append(f"<{len(value) - _MAX_PROMPT_LIST_ITEMS} more items>")
        return head
    if isinstance(value, dict):
        items = list(value.items())
        compacted = {
            str(key): _compact_for_prompt(child, depth=depth + 1)
            for key, child in items[:_MAX_PROMPT_DICT_ITEMS]
        }
        if len(items) > _MAX_PROMPT_DICT_ITEMS:
            compacted["<truncated_keys>"] = len(items) - _MAX_PROMPT_DICT_ITEMS
        return compacted
    return value


def _shape_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    paths: dict[str, Counter[str]] = {}
    for row in rows:
        _walk_shape(row, (), paths)
    return {
        path: dict(sorted(counts.items()))
        for path, counts in sorted(paths.items())
    }


def _walk_shape(value: Any, path: tuple[str, ...], paths: dict[str, Counter[str]]) -> None:
    if isinstance(value, dict):
        if path:
            paths.setdefault(".".join(path), Counter())["object"] += 1
        for key, child in value.items():
            _walk_shape(child, path + (str(key),), paths)
        return
    if isinstance(value, list):
        if path:
            paths.setdefault(".".join(path), Counter())["array"] += 1
        for item in value[:5]:
            _walk_shape(item, path + ("[]",), paths)
        return
    if path:
        paths.setdefault(".".join(path), Counter())[_type_name(value)] += 1


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return type(value).__name__


def _messages_for_record(
    record: dict[str, Any],
    *,
    schema_summary: dict[str, Any],
    current_exec: dict[str, Any] | None,
) -> list[dict[str, str]]:
    nlq = record.get("nl_queries") if isinstance(record.get("nl_queries"), dict) else {}
    mql = str(record.get("MQL") or "")
    payload = {
        "task": "NLQ-first gold-query audit and repair. The current MQL is suspect.",
        "record_id": record.get("record_id"),
        "db_id": record.get("db_id"),
        "difficulty": record.get("difficulty"),
        "shape_policy": record.get("shape_policy"),
        "native_query_pattern": record.get("native_query_pattern"),
        "native_feature_id": record.get("native_feature_id"),
        "mechanism": _native_mechanism(record),
        "current_nlq": {
            "canonical": nlq.get("canonical", ""),
            "colloquial": nlq.get("colloquial", ""),
        },
        "current_mql": mql,
        "current_mql_digest": _mql_digest(mql),
        "current_result_summary": current_exec,
        "schema_summary": schema_summary,
        "local_auto_apply_constraints": {
            "corrected_mql_must_execute": True,
            "corrected_mql_result_must_be_non_empty": True,
            "text_must_not_expose_native_helper_fields": True,
            "nlq_only_repair_is_not_automatically_trusted": True,
        },
    }
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]


def _native_mechanism(record: dict[str, Any]) -> Any:
    metadata = record.get("native_metadata")
    if isinstance(metadata, dict):
        return metadata.get("compiler") or metadata.get("mechanism")
    return record.get("mechanism")


def _mql_digest(mql: str) -> dict[str, Any]:
    try:
        collection, pipeline = parse_pipeline(mql)
    except Exception as exc:  # noqa: BLE001 - prompt context should include parse failure
        return {"parse_error": f"{type(exc).__name__}: {exc}"}
    return {
        "collection": collection,
        "stage_count": len(pipeline),
        "stage_ops": [_stage_op(stage) for stage in pipeline],
        "skeleton_summary": mql_skeleton_summary(mql),
        "pipeline": pipeline,
    }


def _stage_op(stage: Any) -> str:
    if not isinstance(stage, dict) or not stage:
        return "?"
    return ">".join(str(key) for key in stage)


def _review_validation_errors(record: dict[str, Any], review: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    expected_record_id = str(record.get("record_id"))
    expected_db_id = str(record.get("db_id") or "")
    if str(review.get("record_id")) != expected_record_id:
        errors.append(
            f"record_id mismatch: expected {expected_record_id}, got {review.get('record_id')}"
        )
    if str(review.get("db_id") or "") != expected_db_id:
        errors.append(f"db_id mismatch: expected {expected_db_id}, got {review.get('db_id')}")
    verdict = str(review.get("verdict") or "")
    action = str(review.get("repair_action") or "")
    if action == "keep_current" and verdict != "gold_valid":
        errors.append("keep_current is only valid with verdict=gold_valid")
    if verdict == "gold_valid" and action != "keep_current":
        errors.append("gold_valid must use repair_action=keep_current")
    if int(review.get("pipeline_rigidity_score") or 0) >= 4 and action == "keep_current":
        errors.append("high pipeline_rigidity_score cannot keep current query")
    if action in {"replace_mql", "replace_both"}:
        mql = review.get("corrected_mql")
        if not isinstance(mql, str) or not mql.strip():
            errors.append(f"{action} requires corrected_mql")
        else:
            try:
                assert_no_disabled(mql)
                parse_pipeline(mql)
            except Exception as exc:  # noqa: BLE001 - local validation report
                errors.append(f"corrected_mql static validation failed: {type(exc).__name__}: {exc}")
    else:
        mql = review.get("corrected_mql")
        if isinstance(mql, str) and mql.strip() and action not in {"manual_only", "reject"}:
            errors.append(f"corrected_mql present but repair_action={action}")
    if action in {"replace_both", "replace_nlq"}:
        for track, key in (
            ("canonical", "corrected_canonical_nlq"),
            ("colloquial", "corrected_colloquial_nlq"),
        ):
            text = review.get(key)
            if not isinstance(text, str) or len(text.strip()) < 40:
                errors.append(f"{action} requires substantial {track} NLQ")
            elif _anti_gold_text_violations(text):
                errors.append(
                    f"{track} corrected NLQ still exposes template/pipeline language: "
                    + "; ".join(_anti_gold_text_violations(text)[:4])
                )
    if action in {"replace_mql", "keep_current"}:
        for key in ("corrected_canonical_nlq", "corrected_colloquial_nlq"):
            if review.get(key) not in {None, ""}:
                errors.append(f"{key} present but repair_action={action}")
    return errors


def _anti_gold_text_violations(text: str) -> list[str]:
    return [pattern.pattern for pattern in _ANTI_GOLD_TEXT_PATTERNS if pattern.search(text)]


def _quality_repair_prompt(validation_errors: list[str]) -> str:
    return (
        "Your previous JSON had the right schema but failed local gold-quality validation. "
        "Return the same JSON object shape again. Fix these issues: "
        + json.dumps(validation_errors, ensure_ascii=False)
        + "\nDo not expose collection names, raw dotted field paths, _id, above_threshold, "
        "observed, snake_case schema or alias tokens, native_* helper names, schema_state, "
        "or pipeline mechanics in corrected NLQs. Avoid generated labels like "
        "'expense_count>=1'. If you cannot make a genuinely domain-facing repaired pair, set "
        "repair_action=\"manual_only\", corrected_mql=null, corrected_canonical_nlq=null, "
        "corrected_colloquial_nlq=null, and requires_human_review=true. "
        "Return only JSON."
    )


def _feedback_repair_targets(
    rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    *,
    allow_nlq_only_apply: bool,
    min_confidence: float,
) -> list[tuple[dict[str, Any], dict[str, Any] | None, str]]:
    candidate_by_key = {_row_key(row): row for row in candidate_rows}
    targets: list[tuple[dict[str, Any], dict[str, Any] | None, str]] = []
    for row in rows:
        candidate = candidate_by_key.get(_row_key(row))
        reason = _manual_reason(
            row,
            candidate,
            allow_nlq_only_apply=allow_nlq_only_apply,
            min_confidence=min_confidence,
        )
        if not reason:
            continue
        if reason in {
            "candidate_empty_result",
            "candidate_execution_failed",
            "candidate_native_validation_issues",
            "candidate_not_auto_apply_safe",
            "candidate_not_validated",
            "candidate_quality_warnings",
            "confidence_below_0.82",
            "llm_requires_human_review",
            "manual_only",
            "nlq_only_repair_blocked",
            "not_gold_without_safe_repair",
            "reject",
        } or reason.startswith("confidence_below_"):
            targets.append((row, candidate, reason))
    return targets


def _candidate_feedback_prompt(
    row: dict[str, Any],
    candidate: dict[str, Any] | None,
    *,
    manual_reason: str,
    attempt: int,
) -> str:
    feedback = {
        "attempt": attempt,
        "manual_reason": manual_reason,
        "previous_status": row.get("status"),
        "previous_validation_errors": row.get("validation_errors"),
        "candidate_validation": candidate,
    }
    return (
        "Your previous review did not produce an automatically safe gold NLQ/MQL pair. "
        "The current MQL is still suspect; do not solve this by writing an NLQ that merely "
        "describes the old pipeline. Use the feedback below to produce a better JSON object. "
        "Prefer repair_action=\"replace_both\" with a domain-facing canonical and colloquial "
        "question plus a corrected MongoDB aggregation. The corrected MQL must execute to a "
        "non-empty result, must not output native_* helper fields, schema_state, generated "
        "_id objects, above_threshold/observed aliases, or bucket labels such as field>=value. "
        "If the original record has a native_feature_id, the corrected MQL must still exercise "
        "the same native feature contract; for example nested event features need a "
        "shape-preserving $filter, dynamic-key features need $objectToArray over the feature "
        "path, and polymorphic features need a real discriminator branch. If you cannot repair "
        "this into a genuine domain-facing gold pair, return manual_only with null corrected "
        "fields and requires_human_review=true. Return only JSON.\n\nFeedback:\n"
        + json.dumps(feedback, ensure_ascii=False, indent=2)
    )


def _validate_candidate_mqls(
    layout: Any,
    rows: list[dict[str, Any]],
    *,
    records_by_key: dict[tuple[str, Any], dict[str, Any]],
    executor: MongoExecutor,
    logger: RunLogger,
) -> list[dict[str, Any]]:
    candidates = [
        row
        for row in rows
        if row.get("status") == "ok"
        and isinstance(row.get("review"), dict)
        and str(row["review"].get("repair_action") or "") in {"replace_mql", "replace_both"}
        and isinstance(row["review"].get("corrected_mql"), str)
    ]
    by_db: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        by_db[str(row.get("db_id") or "")].append(row)

    validation_rows: list[dict[str, Any]] = []
    db_ids = sorted(by_db)
    native_manifests = (
        _load_native_manifests(layout.native_feature_manifest_dir, db_ids)
        if layout.native_feature_manifest_dir.is_dir()
        else {}
    )
    native_provenance = (
        _load_native_provenance(layout.provenance_dir, db_ids)
        if layout.provenance_dir.is_dir()
        else {}
    )
    for current_db_id, db_rows in sorted(by_db.items()):
        collections = _load_mongodb_data(layout, current_db_id)
        if not isinstance(collections, dict):
            for row in db_rows:
                validation_rows.append({
                    "status": "error",
                    "db_id": row.get("db_id"),
                    "record_id": row.get("record_id"),
                    "error": "mongodb data is not an object",
                })
            continue
        executor.load_witness(current_db_id, collections)
        for row in db_rows:
            review = row["review"]
            mql = str(review.get("corrected_mql") or "")
            validation_rows.append(
                _validate_one_candidate(
                    current_db_id,
                    row.get("record_id"),
                    mql,
                    source_record=records_by_key.get(_row_key(row), {}),
                    manifest=native_manifests.get(current_db_id),
                    provenance=native_provenance.get(current_db_id),
                    layout=layout,
                    executor=executor,
                    logger=logger,
                )
            )
    return _sorted_rows(validation_rows)


def _validate_one_candidate(
    db_id: str,
    record_id: Any,
    mql: str,
    *,
    source_record: dict[str, Any],
    manifest: Any,
    provenance: dict[str, Any] | None,
    layout: Any,
    executor: MongoExecutor,
    logger: RunLogger,
) -> dict[str, Any]:
    try:
        assert_no_disabled(mql)
        collection, pipeline = parse_pipeline(mql)
        rows = executor.norm_exec(db_id, mql)
        warnings = _candidate_quality_warnings(mql, rows)
        native_issues = _candidate_native_issues(
            source_record,
            mql,
            pipeline,
            manifest=manifest,
            provenance=provenance,
            layout=layout,
        )
        return {
            "status": "ok",
            "db_id": db_id,
            "record_id": record_id,
            "collection": collection,
            "stage_count": len(pipeline),
            "result_count": len(rows),
            "non_empty": len(rows) > 0,
            "mql_signature": mql_signature(mql),
            "mql_skeleton_signature": mql_skeleton_signature(mql),
            "mql_skeleton_summary": mql_skeleton_summary(mql),
            "quality_warnings": warnings,
            "native_validation_issues": native_issues,
            "auto_apply_safe": len(warnings) == 0 and not native_issues and len(rows) > 0,
            "sample_docs": _compact_for_prompt(rows[:_MAX_SAMPLE_DOCS]),
        }
    except ExecutionError as exc:
        logger.warning(
            "gold_review_candidate_exec_failed",
            db_id=db_id,
            record_id=record_id,
            error=str(exc)[:300],
        )
        return {
            "status": "error",
            "db_id": db_id,
            "record_id": record_id,
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
        }
    except Exception as exc:  # noqa: BLE001 - static or execution failure
        return {
            "status": "error",
            "db_id": db_id,
            "record_id": record_id,
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
        }


def _candidate_native_issues(
    source_record: dict[str, Any],
    mql: str,
    pipeline: list[dict[str, Any]],
    *,
    manifest: Any,
    provenance: dict[str, Any] | None,
    layout: Any,
) -> list[str]:
    if not source_record.get("native_feature_id"):
        return []
    candidate_record = dict(source_record)
    candidate_record["MQL"] = mql
    candidate_record["mongo_native_constructs"] = sorted(all_ops(pipeline))
    metadata = candidate_record.get("native_metadata")
    if isinstance(metadata, dict):
        metadata = dict(metadata)
        metadata["mongo_native_constructs"] = candidate_record["mongo_native_constructs"]
        candidate_record["native_metadata"] = metadata
    return _validate_native_record(candidate_record, manifest, provenance, layout)


def _candidate_quality_warnings(mql: str, rows: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    lowered = mql.lower()
    if "native_" in lowered:
        warnings.append("candidate_mql_uses_native_helper_field")
    if "schema_state" in lowered:
        warnings.append("candidate_mql_uses_schema_state")
    sample_paths: set[str] = set()
    sample_values: list[str] = []
    for row in rows[:_MAX_SAMPLE_DOCS]:
        _collect_paths(row, (), sample_paths)
        _collect_string_values(row, sample_values)
    for path in sorted(sample_paths):
        lowered_path = path.lower()
        if "native_" in lowered_path:
            warnings.append(f"candidate_output_native_helper:{path}")
        if lowered_path in {"above_threshold", "observed"} or lowered_path.endswith(
            ".above_threshold"
        ):
            warnings.append(f"candidate_output_generated_metric:{path}")
        if lowered_path == "_id" or lowered_path.startswith("_id."):
            warnings.append(f"candidate_output_group_id:{path}")
    for value in sample_values[:40]:
        if _looks_like_generated_bucket_label(value):
            warnings.append(f"candidate_output_generated_bucket_label:{value[:80]}")
    return sorted(set(warnings))


def _collect_paths(value: Any, path: tuple[str, ...], out: set[str]) -> None:
    if isinstance(value, dict):
        if path:
            out.add(".".join(path))
        for key, child in value.items():
            _collect_paths(child, path + (str(key),), out)
        return
    if isinstance(value, list):
        if path:
            out.add(".".join(path))
        for item in value[:3]:
            _collect_paths(item, path + ("[]",), out)
        return
    if path:
        out.add(".".join(path))


def _collect_string_values(value: Any, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
        return
    if isinstance(value, dict):
        for child in value.values():
            _collect_string_values(child, out)
        return
    if isinstance(value, list):
        for item in value[:5]:
            _collect_string_values(item, out)


def _looks_like_generated_bucket_label(value: str) -> bool:
    if re.search(r"[A-Za-z0-9_. -]+(?:>=|<=|<|>)[A-Za-z0-9_. -]+", value):
        return True
    if re.search(r"\b[A-Za-z]+_[A-Za-z0-9_]*\b", value):
        return True
    return False


def _apply_safe_repairs(
    records: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    *,
    selected_keys: set[tuple[str, Any]],
    allow_nlq_only_apply: bool,
    min_confidence: float,
) -> list[dict[str, Any]]:
    by_record = {_record_key(record): record for record in records}
    candidate_by_key = {_row_key(row): row for row in candidate_rows}
    applied: list[dict[str, Any]] = []
    for row in rows:
        key = _row_key(row)
        if key not in selected_keys or row.get("status") != "ok":
            continue
        review = row.get("review") if isinstance(row.get("review"), dict) else {}
        if not _safe_to_apply(review, candidate_by_key.get(key), allow_nlq_only_apply, min_confidence):
            continue
        record = by_record.get(key)
        if record is None:
            continue
        before = _record_patch_snapshot(record)
        action = str(review.get("repair_action") or "")
        if action in {"replace_mql", "replace_both"}:
            new_mql = str(review.get("corrected_mql") or "").strip()
            record["MQL"] = new_mql
            shape_policy = str(record.get("shape_policy") or "reshape")
            record["canonical_form_set"] = derive_canonical_form_set(new_mql, shape_policy)
            record["mql_signature"] = mql_signature(new_mql)
            record["mql_skeleton_signature"] = mql_skeleton_signature(new_mql)
            record["mql_skeleton_summary"] = mql_skeleton_summary(new_mql)
            _refresh_native_metadata_after_mql_change(record)
        if action in {"replace_both", "replace_nlq"}:
            nlq = record.setdefault("nl_queries", {})
            if isinstance(nlq, dict):
                nlq["canonical"] = str(review.get("corrected_canonical_nlq") or "").strip()
                nlq["colloquial"] = str(review.get("corrected_colloquial_nlq") or "").strip()
        after = _record_patch_snapshot(record)
        if before != after:
            applied.append({
                "db_id": key[0],
                "record_id": key[1],
                "repair_action": action,
                "confidence": review.get("confidence"),
                "before": before,
                "after": after,
            })
    return applied


def _record_patch_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    nlq = record.get("nl_queries") if isinstance(record.get("nl_queries"), dict) else {}
    return {
        "MQL": record.get("MQL", ""),
        "canonical": nlq.get("canonical", ""),
        "colloquial": nlq.get("colloquial", ""),
        "mql_signature": record.get("mql_signature", ""),
        "mql_skeleton_signature": record.get("mql_skeleton_signature", ""),
        "mql_skeleton_summary": record.get("mql_skeleton_summary", ""),
        "canonical_form_set": record.get("canonical_form_set", {}),
        "mongo_native_constructs": record.get("mongo_native_constructs", []),
        "native_metadata": record.get("native_metadata", {}),
        "native_verification": record.get("native_verification", {}),
    }


def _safe_to_apply(
    review: dict[str, Any],
    candidate: dict[str, Any] | None,
    allow_nlq_only_apply: bool,
    min_confidence: float,
) -> bool:
    if review.get("requires_human_review") is True:
        return False
    try:
        confidence = float(review.get("confidence"))
    except (TypeError, ValueError):
        return False
    if confidence < min_confidence:
        return False
    action = str(review.get("repair_action") or "")
    if action in {"replace_mql", "replace_both"}:
        return (
            isinstance(candidate, dict)
            and candidate.get("status") == "ok"
            and candidate.get("non_empty") is True
            and candidate.get("auto_apply_safe") is True
        )
    if action == "replace_nlq":
        return allow_nlq_only_apply
    return False


def _manual_required_rows(
    rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    *,
    applied_rows: list[dict[str, Any]],
    allow_nlq_only_apply: bool,
    min_confidence: float,
) -> list[dict[str, Any]]:
    applied_keys = {(row.get("db_id"), row.get("record_id")) for row in applied_rows}
    candidate_by_key = {_row_key(row): row for row in candidate_rows}
    manual: list[dict[str, Any]] = []
    for row in rows:
        key = _row_key(row)
        if key in applied_keys:
            continue
        reason = _manual_reason(
            row,
            candidate_by_key.get(key),
            allow_nlq_only_apply=allow_nlq_only_apply,
            min_confidence=min_confidence,
        )
        if reason:
            manual.append({
                "db_id": row.get("db_id"),
                "record_id": row.get("record_id"),
                "reason": reason,
            })
    return _sorted_rows(manual)


def _manual_reason(
    row: dict[str, Any],
    candidate: dict[str, Any] | None,
    *,
    allow_nlq_only_apply: bool,
    min_confidence: float,
) -> str | None:
    if row.get("status") != "ok":
        return f"review_status={row.get('status')}"
    review = row.get("review") if isinstance(row.get("review"), dict) else {}
    action = str(review.get("repair_action") or "")
    verdict = str(review.get("verdict") or "")
    if verdict == "gold_valid":
        return None
    if review.get("requires_human_review") is True:
        return "llm_requires_human_review"
    try:
        confidence = float(review.get("confidence"))
    except (TypeError, ValueError):
        return "missing_confidence"
    if confidence < min_confidence:
        return f"confidence_below_{min_confidence}"
    if action in {"replace_mql", "replace_both"}:
        if not isinstance(candidate, dict):
            return "candidate_not_validated"
        if candidate.get("status") != "ok":
            return "candidate_execution_failed"
        if candidate.get("non_empty") is not True:
            return "candidate_empty_result"
        if candidate.get("auto_apply_safe") is not True:
            native_issues = candidate.get("native_validation_issues")
            if isinstance(native_issues, list) and native_issues:
                return "candidate_native_validation_issues"
            warnings = candidate.get("quality_warnings")
            if isinstance(warnings, list) and warnings:
                return "candidate_quality_warnings"
            return "candidate_not_auto_apply_safe"
        return None
    if action == "replace_nlq" and not allow_nlq_only_apply:
        return "nlq_only_repair_blocked"
    if action in {"manual_only", "reject"}:
        return action
    return "not_gold_without_safe_repair"


def _summary(
    rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    *,
    applied_rows: list[dict[str, Any]],
    manual_rows: list[dict[str, Any]],
    out: Path,
) -> dict[str, Any]:
    ok_rows = [row for row in rows if row.get("status") == "ok"]
    invalid_rows = [row for row in rows if row.get("status") == "invalid"]
    review_rows = [row.get("review") for row in ok_rows if isinstance(row.get("review"), dict)]
    verdict_counts = Counter(str(review.get("verdict") or "") for review in review_rows)
    action_counts = Counter(str(review.get("repair_action") or "") for review in review_rows)
    candidate_ok = [row for row in candidate_rows if row.get("status") == "ok"]
    return {
        "records": len(rows),
        "calls_ok": len(ok_rows),
        "calls_failed": len(rows) - len(ok_rows) - len(invalid_rows),
        "invalid_reviews": len(invalid_rows),
        "gold_valid": verdict_counts.get("gold_valid", 0),
        "not_gold": len(review_rows) - verdict_counts.get("gold_valid", 0),
        "candidate_mqls": len(candidate_rows),
        "candidate_exec_ok": len(candidate_ok),
        "candidate_exec_failed": len(candidate_rows) - len(candidate_ok),
        "candidate_empty_results": sum(1 for row in candidate_ok if row.get("non_empty") is not True),
        "candidate_not_auto_apply_safe": sum(
            1 for row in candidate_ok if row.get("auto_apply_safe") is not True
        ),
        "manual_required": len(manual_rows),
        "applied_updates": len(applied_rows),
        "verdict_counts": dict(sorted(verdict_counts.items())),
        "repair_action_counts": dict(sorted(action_counts.items())),
        "manual_reason_counts": dict(
            sorted(Counter(str(row.get("reason") or "") for row in manual_rows).items())
        ),
        "out_dir": str(out),
    }


def _render_report(
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    applied_rows: list[dict[str, Any]],
    manual_rows: list[dict[str, Any]],
) -> str:
    lines = [
        "# LLM Gold Query Review",
        "",
        "## Summary",
        "",
        f"- Records: {summary['records']}",
        f"- Calls OK: {summary['calls_ok']}",
        f"- Calls failed: {summary['calls_failed']}",
        f"- Invalid reviews: {summary['invalid_reviews']}",
        f"- Gold valid: {summary['gold_valid']}",
        f"- Not gold: {summary['not_gold']}",
        f"- Candidate MQLs: {summary['candidate_mqls']}",
        f"- Candidate exec OK: {summary['candidate_exec_ok']}",
        f"- Candidate exec failed: {summary['candidate_exec_failed']}",
        f"- Candidate empty results: {summary['candidate_empty_results']}",
        f"- Candidate not auto-apply safe: {summary['candidate_not_auto_apply_safe']}",
        f"- Applied updates: {summary['applied_updates']}",
        f"- Manual required: {summary['manual_required']}",
        "",
        "## Verdict Counts",
        "",
    ]
    for verdict, count in summary["verdict_counts"].items():
        lines.append(f"- `{verdict}`: {count}")
    lines.extend(["", "## Repair Action Counts", ""])
    for action, count in summary["repair_action_counts"].items():
        lines.append(f"- `{action}`: {count}")
    if summary["manual_reason_counts"]:
        lines.extend(["", "## Manual Required Reasons", ""])
        for reason, count in summary["manual_reason_counts"].items():
            lines.append(f"- `{reason}`: {count}")
    failed = [row for row in rows if row.get("status") != "ok"]
    if failed:
        lines.extend(["", "## Failed Or Invalid Reviews", ""])
        for row in failed[:40]:
            lines.append(
                f"- {row.get('status')} db={row.get('db_id')} "
                f"record={row.get('record_id')}: "
                f"{row.get('error') or row.get('validation_errors')}"
            )
        if len(failed) > 40:
            lines.append(f"- ... {len(failed) - 40} more")
    candidate_failed = [row for row in candidate_rows if row.get("status") != "ok"]
    if candidate_failed:
        lines.extend(["", "## Candidate Execution Failures", ""])
        for row in candidate_failed[:40]:
            lines.append(
                f"- db={row.get('db_id')} record={row.get('record_id')}: "
                f"{row.get('error_type')}: {row.get('error')}"
            )
        if len(candidate_failed) > 40:
            lines.append(f"- ... {len(candidate_failed) - 40} more")
    if applied_rows:
        lines.extend(["", "## Applied Updates", ""])
        for row in applied_rows[:40]:
            lines.append(
                f"- db={row.get('db_id')} record={row.get('record_id')} "
                f"action={row.get('repair_action')} confidence={row.get('confidence')}"
            )
        if len(applied_rows) > 40:
            lines.append(f"- ... {len(applied_rows) - 40} more")
    if manual_rows:
        lines.extend(["", "## Manual Required Sample", ""])
        for row in manual_rows[:60]:
            lines.append(
                f"- db={row.get('db_id')} record={row.get('record_id')}: {row.get('reason')}"
            )
        if len(manual_rows) > 60:
            lines.append(f"- ... {len(manual_rows) - 60} more")
    lines.append("")
    return "\n".join(lines)


__all__ = ["LLMGoldReviewSummary", "run_llm_gold_query_review"]
