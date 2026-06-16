"""Pure console-formatting helpers for the TEND CLI run summaries.

These functions only read their arguments and ``print`` — they hold no state and
make no I/O beyond stdout — so they live apart from the runtime wiring in
``cli.py`` to keep that module focused on orchestration.
"""
from __future__ import annotations

from .evaluation import EvaluationOutput
from .evaluation.metrics import HEADLINE_METRIC


def _count_by(items: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key))
        counts[value] = counts.get(value, 0) + 1
    return counts


def _print_run_refs(rt) -> None:
    print(f"  run dir   : {rt.settings.run_dir}")
    print("  run files : run.log | milestones.jsonl | errors.jsonl | cost_summary.jsonl")


def _print_primary_session_refs(*groups: list[dict]) -> None:
    refs: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            for ref in _item_primary_session_refs(item):
                if ref not in seen:
                    refs.append(ref)
                    seen.add(ref)
    if not refs:
        return
    suffix = "" if len(refs) <= 5 else f" | ... {len(refs) - 5} more"
    print(f"  session refs : {' | '.join(refs[:5])}{suffix}")


def _item_primary_session_refs(item: dict) -> list[str]:
    refs: list[str] = []
    single = item.get("agent_session_ref") or item.get("session_ref")
    if isinstance(single, str) and single:
        refs.append(single)
    for key in ("agent_session_refs", "session_refs", "transcript_refs"):
        value = item.get(key)
        if isinstance(value, list):
            refs.extend(ref for ref in value if isinstance(ref, str) and ref)
    phase_refs = item.get("phase_session_refs")
    if isinstance(phase_refs, dict):
        for value in phase_refs.values():
            if isinstance(value, list):
                refs.extend(ref for ref in value if isinstance(ref, str) and ref)
    return refs


def _print_summary(rt, artifacts, records, summary, out_dir) -> None:
    print("\n" + "=" * 64)
    print(f"TEND construct · run {rt.settings.run_id} · "
          f"{'STUB' if rt.settings.stub else 'LIVE ' + rt.settings.llm.model}")
    print(f"  Phase A dbs : {len(artifacts)}  -> {sorted(artifacts)}")
    for db_id, art in sorted(artifacts.items()):
        coll = art.mongodb_data
        print(f"    {db_id:24} sig={art.world_signature[:20]}.. "
              f"collections={len(coll)} query_bearing={art.query_bearing}")
    print(f"  Phase B records : {len(records)}")
    for r in records[:5]:
        print(f"    #{r['record_id']} {r['db_id']} {r['difficulty']} "
              f"{r.get('sql_infeasibility_class')}")
    print(f"  anomalies : {summary.get('anomaly_total', 0)} {summary.get('anomalies_by_kind', {})}")
    _print_run_refs(rt)
    print(f"  output : {out_dir}")
    print("=" * 64)


def _print_solve_summary(
    rt,
    predictions,
    failures,
    summary,
    out_path,
    failures_path,
    evaluation: EvaluationOutput | None = None,
    *,
    evaluate: bool = True,
    skip_reason: str | None = None,
) -> None:
    print("\n" + "=" * 64)
    print(f"TEND solve · run {rt.settings.run_id} · "
          f"{'STUB' if rt.settings.stub else 'LIVE ' + rt.settings.llm.model}")
    print(f"  predictions : {len(predictions)}")
    for pred in predictions[:5]:
        print(f"    #{pred.get('record_id')} {pred.get('db_id')} "
              f"mql={str(pred.get('MQL', ''))[:96]}")
    print(f"  failures    : {len(failures)}")
    for item in failures[:5]:
        print(f"    #{item.get('record_id')} {item.get('db_id')} "
              f"{item.get('error_code')}: {str(item.get('message', ''))[:96]}")
    print(f"  anomalies : {summary.get('anomaly_total', 0)} {summary.get('anomalies_by_kind', {})}")
    _print_run_refs(rt)
    _print_primary_session_refs(predictions, failures)
    print(f"  output : {out_path}")
    print(f"  failures output : {failures_path}")
    _print_evaluation_block(evaluation, evaluate=evaluate, skip_reason=skip_reason)
    print("=" * 64)


def _print_baseline_summary(
    rt,
    predictions,
    failures,
    summary,
    out_path,
    failures_path,
    evaluation: EvaluationOutput | None = None,
    *,
    evaluate: bool = True,
    skip_reason: str | None = None,
    baseline_outputs: dict | None = None,
) -> None:
    print("\n" + "=" * 64)
    print(f"TEND baselines · run {rt.settings.run_id} · "
          f"{'STUB' if rt.settings.stub else 'LIVE ' + rt.settings.llm.model}")
    print(f"  predictions : {len(predictions)}")
    by_baseline: dict[str, int] = {}
    for item in predictions:
        bid = str(item.get("baseline_id"))
        by_baseline[bid] = by_baseline.get(bid, 0) + 1
    print(f"  baselines : {by_baseline}")
    print(f"  failures  : {len(failures)}")
    for item in predictions[:5]:
        print(f"    {item.get('baseline_id')} #{item.get('record_id')} "
              f"{item.get('db_id')} status={item.get('status')} "
              f"mql={str(item.get('MQL', ''))[:80]}")
    for item in failures[:5]:
        print(f"    failure {item.get('baseline_id')} #{item.get('record_id')} "
              f"{item.get('error_code')}: {str(item.get('message', ''))[:80]}")
    print(f"  anomalies : {summary.get('anomaly_total', 0)} {summary.get('anomalies_by_kind', {})}")
    _print_run_refs(rt)
    _print_primary_session_refs(predictions, failures)
    print(f"  output : {out_path}")
    print(f"  failures output : {failures_path}")
    if baseline_outputs:
        print("  per-baseline outputs:")
        for baseline_id, paths in sorted(baseline_outputs.items()):
            pred_path = paths.get("predictions") if isinstance(paths, dict) else None
            fail_path = paths.get("failures") if isinstance(paths, dict) else None
            print(f"    {baseline_id}: {pred_path} | {fail_path}")
    _print_evaluation_block(evaluation, evaluate=evaluate, skip_reason=skip_reason)
    print("=" * 64)


def _print_ablation_summary(
    rt,
    predictions,
    failures,
    summary,
    out_path,
    failures_path,
    summary_path,
    evaluation: EvaluationOutput | None = None,
    *,
    evaluate: bool = True,
    skip_reason: str | None = None,
) -> None:
    print("\n" + "=" * 64)
    print(f"TEND ablation · run {rt.settings.run_id} · "
          f"{'STUB' if rt.settings.stub else 'LIVE ' + rt.settings.llm.model}")
    print(f"  predictions : {len(predictions)}")
    print(f"  ablations : {_count_by(predictions, 'ablation_id')}")
    print(f"  failures  : {len(failures)}")
    for item in predictions[:5]:
        print(f"    {item.get('ablation_id')} #{item.get('record_id')} "
              f"{item.get('db_id')} attempts={item.get('attempts')} "
              f"mql={str(item.get('MQL', ''))[:80]}")
    for item in failures[:5]:
        print(f"    failure {item.get('ablation_id')} #{item.get('record_id')} "
              f"{item.get('error_code')}: {str(item.get('message', ''))[:80]}")
    print(f"  anomalies : {summary.get('anomaly_total', 0)} {summary.get('anomalies_by_kind', {})}")
    _print_run_refs(rt)
    _print_primary_session_refs(predictions, failures)
    print(f"  output : {out_path}")
    print(f"  summary: {summary_path}")
    print(f"  failures output : {failures_path}")
    _print_evaluation_block(evaluation, evaluate=evaluate, skip_reason=skip_reason)
    print("=" * 64)


def _print_evaluation_block(
    evaluation: EvaluationOutput | None,
    *,
    evaluate: bool = True,
    skip_reason: str | None = None,
) -> None:
    if evaluation is None:
        reason = skip_reason or ("disabled" if not evaluate else "no_predictions")
        if reason == "disabled":
            print("  evaluation : disabled (--no-eval)")
        elif reason == "no_release_dataset":
            print("  evaluation : skipped (NLQ+DB mode has no release evaluation dataset)")
        elif reason == "no_predictions":
            print("  evaluation : skipped (no predictions)")
        else:
            print(f"  evaluation : skipped ({reason})")
        return
    headline = evaluation.report.get("headline")
    if isinstance(headline, dict) and headline.get("mode") == "per_system":
        print(f"  evaluation : {evaluation.status} per-system {HEADLINE_METRIC}")
        systems = headline.get("systems") if isinstance(headline.get("systems"), dict) else {}
        for system_id, payload in list(systems.items())[:8]:
            scores = payload.get("scores", {}) if isinstance(payload, dict) else {}
            deltas = (
                payload.get("delta_vs_reference")
                if isinstance(payload, dict) and isinstance(payload.get("delta_vs_reference"), dict)
                else {}
            )
            delta = deltas.get(HEADLINE_METRIC)
            suffix = f" delta_vs_reference={delta}" if delta is not None else ""
            print(
                f"    {system_id}: {HEADLINE_METRIC}={scores.get(HEADLINE_METRIC, 0.0)} "
                f"EXF1={scores.get('EXF1', 0.0)}{suffix}"
            )
        print(f"  eval report: {evaluation.paths.report_md}")
        return
    scores = evaluation.report.get("scores", {})
    print(f"  evaluation : {evaluation.status} {HEADLINE_METRIC}={scores.get(HEADLINE_METRIC, 0.0)} "
          f"EXF1={scores.get('EXF1', 0.0)}")
    print(f"  eval report: {evaluation.paths.report_md}")
