"""LLM-backed NLQ/MQL semantic review for release datasets."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..execution.ast_check import parse_pipeline
from ..llm import LLMClient
from ..observability import RunLogger
from ..release_layout import resolve_release_dataset_layout
from .repair import _load_pairs, _write_release_files


@dataclass(frozen=True, slots=True)
class LLMReviewSummary:
    records: int
    calls_ok: int
    calls_failed: int
    canonical_mismatches: int
    colloquial_mismatches: int
    applied_updates: int
    out_dir: str
    paths: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "calls_ok": self.calls_ok,
            "calls_failed": self.calls_failed,
            "canonical_mismatches": self.canonical_mismatches,
            "colloquial_mismatches": self.colloquial_mismatches,
            "applied_updates": self.applied_updates,
            "out_dir": self.out_dir,
            "paths": self.paths,
        }


_TRACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "reason", "replacement_nlq"],
    "properties": {
        "verdict": {"type": "string", "enum": ["aligned", "mismatch"]},
        "reason": {"type": "string"},
        "replacement_nlq": {"type": ["string", "null"]},
    },
}

_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["record_id", "db_id", "canonical", "colloquial"],
    "properties": {
        "record_id": {"type": ["integer", "string"]},
        "db_id": {"type": "string"},
        "canonical": _TRACK_SCHEMA,
        "colloquial": _TRACK_SCHEMA,
    },
}

_SYSTEM_PROMPT = """You are auditing a benchmark record.

The MQL is the ground truth. Decide whether each NLQ fully matches the MQL semantics.
Check collection, result shape, match predicates, constants, numeric thresholds, boolean
logic, unwind/filter conditions, ifNull/fallback semantics, group keys, output fields,
sort order, and limit.

Return only a JSON object. Do not use markdown.

The JSON object MUST have exactly this shape:
{
  "record_id": <same record_id>,
  "db_id": "<same db_id>",
  "canonical": {
    "verdict": "aligned" or "mismatch",
    "reason": "<short reason>",
    "replacement_nlq": null or "<complete replacement canonical NLQ>"
  },
  "colloquial": {
    "verdict": "aligned" or "mismatch",
    "reason": "<short reason>",
    "replacement_nlq": null or "<complete replacement colloquial NLQ>"
  }
}

The canonical and colloquial values MUST be objects, not strings. Use verdict "mismatch",
not "misaligned". For an aligned NLQ, replacement_nlq must be null. For a mismatch,
replacement_nlq must be a complete NLQ that preserves the original style but adds the
missing MQL semantics. Never change the MQL and never invent semantics not in the MQL.
"""


async def run_llm_nlq_review(
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
    workers: int = 8,
    apply: bool = False,
) -> LLMReviewSummary:
    """Run one LLM JSON-mode semantic review call per selected record."""

    layout = resolve_release_dataset_layout(dataset_dir)
    records = _load_records(layout.tend_path if layout.tend_path.exists() else layout.test_path)
    selected = _select_records(records, db_id=db_id, record_ids=record_ids, limit=limit)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "review_results.jsonl"
    report_path = out / "review_report.md"
    applied_path = out / "applied_updates.jsonl"

    sem = asyncio.Semaphore(max(1, workers))

    async def review_one(record: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            log = logger.bind(
                db_id=str(record.get("db_id") or ""),
                record_id=record.get("record_id"),
            )
            messages = _messages_for_record(record)
            completion = llm.complete(
                agent="nlq_mql_review",
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
            return {
                "status": "ok",
                "db_id": record.get("db_id"),
                "record_id": record.get("record_id"),
                "review": result.data,
                "transcript_ref": result.transcript_ref,
                "diagnostics_ref": result.diagnostics_ref,
                "model": result.model,
                "usage": result.usage,
            }

    rows: list[dict[str, Any]] = []
    for task in asyncio.as_completed([review_one(record) for record in selected]):
        try:
            rows.append(await task)
        except Exception as exc:  # noqa: BLE001 - preserve per-record failures in audit output
            rows.append({
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
            })

    rows.sort(key=lambda row: (str(row.get("db_id") or ""), str(row.get("record_id") or "")))
    results_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )

    applied_rows: list[dict[str, Any]] = []
    if apply:
        applied_rows = _apply_review_rows(records, rows)
        if applied_rows:
            _write_release_files(layout.root, records, _load_pairs(layout.root))
        applied_path.write_text(
            "".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                for row in applied_rows
            ),
            encoding="utf-8",
        )

    summary = _summary(rows, applied_rows=applied_rows, out=out)
    report_path.write_text(_render_report(summary, rows, applied_rows), encoding="utf-8")
    paths = {
        "results_jsonl": str(results_path),
        "report_md": str(report_path),
    }
    if apply:
        paths["applied_updates_jsonl"] = str(applied_path)
    return LLMReviewSummary(
        records=summary["records"],
        calls_ok=summary["calls_ok"],
        calls_failed=summary["calls_failed"],
        canonical_mismatches=summary["canonical_mismatches"],
        colloquial_mismatches=summary["colloquial_mismatches"],
        applied_updates=len(applied_rows),
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
        selected = [record for record in selected if int(record.get("record_id") or -1) in record_ids]
    if limit is not None:
        selected = selected[:max(0, int(limit))]
    return selected


def _messages_for_record(record: dict[str, Any]) -> list[dict[str, str]]:
    nlq = record.get("nl_queries") if isinstance(record.get("nl_queries"), dict) else {}
    mql = str(record.get("MQL") or "")
    payload = {
        "task": "Audit NLQ/MQL alignment. Return JSON only.",
        "required_json_shape": {
            "record_id": record.get("record_id"),
            "db_id": record.get("db_id"),
            "canonical": {
                "verdict": "aligned|mismatch",
                "reason": "short reason",
                "replacement_nlq": None,
            },
            "colloquial": {
                "verdict": "aligned|mismatch",
                "reason": "short reason",
                "replacement_nlq": None,
            },
        },
        "record_id": record.get("record_id"),
        "db_id": record.get("db_id"),
        "native_query_pattern": record.get("native_query_pattern"),
        "mql": mql,
        "mql_digest": _mql_digest(mql),
        "canonical_nlq": nlq.get("canonical", ""),
        "colloquial_nlq": nlq.get("colloquial", ""),
    }
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]


def _mql_digest(mql: str) -> dict[str, Any]:
    try:
        collection, pipeline = parse_pipeline(mql)
    except Exception as exc:  # noqa: BLE001 - prompt context should include parse failure
        return {"parse_error": f"{type(exc).__name__}: {exc}"}
    digest: dict[str, Any] = {"collection": collection, "stage_count": len(pipeline), "stages": []}
    for stage in pipeline:
        if not isinstance(stage, dict) or not stage:
            continue
        op, spec = next(iter(stage.items()))
        digest["stages"].append({"op": op, "spec": spec})
    return digest


def _apply_review_rows(records: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {(str(record.get("db_id") or ""), record.get("record_id")): record for record in records}
    applied: list[dict[str, Any]] = []
    for row in rows:
        if row.get("status") != "ok" or not isinstance(row.get("review"), dict):
            continue
        review = row["review"]
        record = by_key.get((str(review.get("db_id") or row.get("db_id") or ""), review.get("record_id")))
        if record is None:
            record = by_key.get((str(row.get("db_id") or ""), row.get("record_id")))
        if record is None:
            continue
        nlq = record.setdefault("nl_queries", {})
        if not isinstance(nlq, dict):
            continue
        for track in ("canonical", "colloquial"):
            track_review = review.get(track)
            if not isinstance(track_review, dict) or track_review.get("verdict") != "mismatch":
                continue
            replacement = track_review.get("replacement_nlq")
            if not isinstance(replacement, str) or not replacement.strip():
                continue
            before = str(nlq.get(track) or "")
            after = replacement.strip()
            if before == after:
                continue
            nlq[track] = after
            applied.append({
                "db_id": record.get("db_id"),
                "record_id": record.get("record_id"),
                "track": track,
                "before": before,
                "after": after,
                "reason": track_review.get("reason", ""),
            })
    return applied


def _summary(rows: list[dict[str, Any]], *, applied_rows: list[dict[str, Any]], out: Path) -> dict[str, Any]:
    ok_rows = [row for row in rows if row.get("status") == "ok"]
    canonical_mismatches = 0
    colloquial_mismatches = 0
    for row in ok_rows:
        review = row.get("review") if isinstance(row.get("review"), dict) else {}
        if isinstance(review.get("canonical"), dict) and review["canonical"].get("verdict") == "mismatch":
            canonical_mismatches += 1
        if isinstance(review.get("colloquial"), dict) and review["colloquial"].get("verdict") == "mismatch":
            colloquial_mismatches += 1
    return {
        "records": len(rows),
        "calls_ok": len(ok_rows),
        "calls_failed": len(rows) - len(ok_rows),
        "canonical_mismatches": canonical_mismatches,
        "colloquial_mismatches": colloquial_mismatches,
        "applied_updates": len(applied_rows),
        "out_dir": str(out),
    }


def _render_report(
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
    applied_rows: list[dict[str, Any]],
) -> str:
    lines = [
        "# LLM NLQ/MQL Review",
        "",
        "## Summary",
        "",
        f"- Records: {summary['records']}",
        f"- Calls OK: {summary['calls_ok']}",
        f"- Calls failed: {summary['calls_failed']}",
        f"- Canonical mismatches: {summary['canonical_mismatches']}",
        f"- Colloquial mismatches: {summary['colloquial_mismatches']}",
        f"- Applied updates: {len(applied_rows)}",
        "",
        "## Mismatches",
        "",
    ]
    mismatch_count = 0
    for row in rows:
        if row.get("status") != "ok":
            lines.extend([
                f"### ERROR {row.get('db_id')} {row.get('record_id')}",
                "",
                f"- {row.get('error_type')}: {row.get('error')}",
                "",
            ])
            continue
        review = row.get("review") if isinstance(row.get("review"), dict) else {}
        items = []
        for track in ("canonical", "colloquial"):
            track_review = review.get(track) if isinstance(review.get(track), dict) else {}
            if track_review.get("verdict") == "mismatch":
                items.append((track, track_review))
        if not items:
            continue
        mismatch_count += 1
        lines.extend([f"### {review.get('db_id')} {review.get('record_id')}", ""])
        for track, track_review in items:
            lines.extend([
                f"- Track: `{track}`",
                f"- Reason: {track_review.get('reason', '')}",
                f"- Replacement: {track_review.get('replacement_nlq', '')}",
                "",
            ])
    if mismatch_count == 0:
        lines.append("No mismatches reported.")
        lines.append("")
    return "\n".join(lines)


__all__ = ["LLMReviewSummary", "run_llm_nlq_review"]
