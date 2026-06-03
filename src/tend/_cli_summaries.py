"""Pure console-formatting helpers for the TEND CLI run summaries.

These functions only read their arguments and ``print`` — they hold no state and
make no I/O beyond stdout — so they live apart from the runtime wiring in
``cli.py`` to keep that module focused on orchestration.
"""
from __future__ import annotations

from .evaluation import EvaluationOutput


def _count_by(items: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key))
        counts[value] = counts.get(value, 0) + 1
    return counts


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
    print(f"  logs   : {rt.settings.run_dir}/events.jsonl | anomalies.jsonl | progress.jsonl | llm/")
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
    print(f"  logs   : {rt.settings.run_dir}/events.jsonl | anomalies.jsonl | progress.jsonl | llm/")
    print(f"  output : {out_path}")
    if failures:
        print(f"  failures output : {failures_path}")
    _print_evaluation_block(evaluation, evaluate=evaluate)
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
    print(f"  logs   : {rt.settings.run_dir}/events.jsonl | anomalies.jsonl | progress.jsonl | llm/")
    print(f"  output : {out_path}")
    if failures:
        print(f"  failures output : {failures_path}")
    _print_evaluation_block(evaluation, evaluate=evaluate)
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
    print(f"  logs   : {rt.settings.run_dir}/events.jsonl | anomalies.jsonl | progress.jsonl | llm/")
    print(f"  output : {out_path}")
    print(f"  summary: {summary_path}")
    if failures:
        print(f"  failures output : {failures_path}")
    _print_evaluation_block(evaluation, evaluate=evaluate)
    print("=" * 64)


def _print_evaluation_block(
    evaluation: EvaluationOutput | None,
    *,
    evaluate: bool = True,
) -> None:
    if evaluation is None:
        if not evaluate:
            print("  evaluation : disabled (--no-eval)")
        else:
            print("  evaluation : skipped (no predictions)")
        return
    scores = evaluation.report.get("scores", {})
    print(f"  evaluation : {evaluation.status} EX={scores.get('EX', 0.0)} "
          f"EFM={scores.get('EFM', 0.0)} EVM={scores.get('EVM', 0.0)}")
    print(f"  eval report: {evaluation.paths.report_md}")
