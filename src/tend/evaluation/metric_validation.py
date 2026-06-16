"""Metric-validation harness (experiment design §1.3, M1/M2) + retro-score core.

The bounded EXC headline (``EXC_SURPLUS_BOUND``) is a frozen design parameter; this module
makes that freeze AUDITABLE rather than asserted, and is the offline scorer for
budget-exhausted agents.

M1 — gold self-scoring: every gold pipeline, scored against itself, must earn EXC=1. A
failure means a benchmark defect (unparseable gold, banned operator, empty gold result),
not a metric artifact. The end-to-end leg lives in ``scripts/validate_metric.py`` (it needs
witness execution); the per-result invariant is :func:`gold_self_scores`.

M2 — null/shortcut probes × β sweep: the metric operates on result rows, so the surplus
bound is validated at the RESULT level (deterministic, no Mongo). For one gold result we
synthesize benign-surplus predictions (a leftover ``_id``; two helper columns) and excess
predictions (a 3-surplus; a full-document dump), plus pure-null probes (empty / constant).
:func:`select_min_beta` returns the smallest β that accepts every benign family and rejects
every excess/null probe — which must come out to ``EXC_SURPLUS_BOUND`` (=2).

Pure-diagnostic: nothing here changes a witness, a gold, or the headline scored on D.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..execution.mongo import equiv_rec_values_superset
from .metrics import EXC_SURPLUS_BOUND, exf1

# β candidates: 0 (=strict EX), 1, 2, 3, None (=unbounded Spider 2.0 EXC_spider).
BETA_SWEEP: tuple[int | None, ...] = (0, 1, 2, 3, None)


@dataclass(frozen=True, slots=True)
class ResultProbe:
    name: str
    rows: list[dict[str, Any]]
    family: str  # "benign" (must pass) | "excess" | "null" (must fail)
    surplus: int | None  # per-row extra values vs gold (None = pure-null, not surplus-based)


def _with_surplus(gold_rows: list[dict[str, Any]], extra: int) -> list[dict[str, Any]]:
    """Each gold row plus ``extra`` sentinel top-level columns (distinct, non-colliding)."""
    out: list[dict[str, Any]] = []
    for i, row in enumerate(gold_rows):
        base = dict(row) if isinstance(row, dict) else {"__v": row}
        for j in range(extra):
            base[f"__surplus_{i}_{j}"] = f"__cf_surplus__{i}_{j}"
        out.append(base)
    return out


def build_result_probes(gold_rows: list[dict[str, Any]]) -> list[ResultProbe]:
    """Synthesize the benign / excess / null probe predictions for one gold result.

    Benign families model MongoDB's two mechanical surplus channels (a leftover synthetic
    ``$group`` ``_id``; a retained helper/sort key). Excess families model a 3rd surplus
    and a projection-free document dump. Null probes are the empty result and a constant.
    """
    n = len(gold_rows)
    return [
        ResultProbe("id_leak", _with_surplus(gold_rows, 1), "benign", 1),
        ResultProbe("double_helper", _with_surplus(gold_rows, 2), "benign", 2),
        ResultProbe("triple_surplus", _with_surplus(gold_rows, 3), "excess", 3),
        ResultProbe("full_doc_dump", _with_surplus(gold_rows, 8), "excess", 8),
        ResultProbe("empty_result", [], "null", None),
        ResultProbe(
            "constant_scalar",
            [{"__const": 0} for _ in range(n)] if n else [{"__const": 0}],
            "null",
            None,
        ),
    ]


def probe_accepts(
    gold_rows: list[dict[str, Any]],
    probe: ResultProbe,
    *,
    order_sensitive: bool,
    beta: int | None,
) -> bool:
    """Whether the headline EXC at surplus bound ``beta`` accepts this probe's result."""
    return equiv_rec_values_superset(
        gold_rows, probe.rows, order_sensitive=order_sensitive, max_surplus=beta
    )


@dataclass
class BetaSweepRecord:
    """Per-record β-sweep outcome: for each β, did every probe behave as its family demands?"""

    db_id: str
    record_id: Any
    gold_rows: int
    # beta -> {probe_name: accepted}
    acceptance: dict[str, dict[str, bool]]
    # beta -> bool: benign all-pass AND excess+null all-fail
    correct: dict[str, bool]


def _beta_key(beta: int | None) -> str:
    return "inf" if beta is None else str(beta)


def sweep_record(
    gold_rows: list[dict[str, Any]],
    *,
    order_sensitive: bool,
    db_id: str = "",
    record_id: Any = None,
    betas: tuple[int | None, ...] = BETA_SWEEP,
) -> BetaSweepRecord:
    probes = build_result_probes(gold_rows)
    acceptance: dict[str, dict[str, bool]] = {}
    correct: dict[str, bool] = {}
    for beta in betas:
        bk = _beta_key(beta)
        acc = {
            p.name: probe_accepts(gold_rows, p, order_sensitive=order_sensitive, beta=beta)
            for p in probes
        }
        acceptance[bk] = acc
        benign_ok = all(acc[p.name] for p in probes if p.family == "benign")
        bad_rejected = all(not acc[p.name] for p in probes if p.family in ("excess", "null"))
        correct[bk] = benign_ok and bad_rejected
    return BetaSweepRecord(
        db_id=db_id,
        record_id=record_id,
        gold_rows=len(gold_rows),
        acceptance=acceptance,
        correct=correct,
    )


def select_min_beta(
    records: list[BetaSweepRecord], *, betas: tuple[int | None, ...] = BETA_SWEEP
) -> dict[str, Any]:
    """Smallest finite β that is ``correct`` on EVERY record (benign accept, excess/null reject).

    Returns the chosen β, whether it matches the frozen ``EXC_SURPLUS_BOUND``, and the
    per-β count of records the bound classifies correctly (so a wrong freeze is visible).
    """
    if not records:
        return {"chosen_beta": None, "matches_frozen_bound": False, "records": 0, "per_beta_correct": {}}
    per_beta_correct = {
        _beta_key(beta): sum(1 for r in records if r.correct[_beta_key(beta)]) for beta in betas
    }
    total = len(records)
    chosen: int | None = None
    for beta in betas:
        if beta is None:
            continue
        if per_beta_correct[_beta_key(beta)] == total:
            chosen = beta
            break
    return {
        "chosen_beta": chosen,
        "frozen_bound": EXC_SURPLUS_BOUND,
        "matches_frozen_bound": chosen == EXC_SURPLUS_BOUND,
        "records": total,
        "per_beta_correct": per_beta_correct,
    }


def gold_self_scores(
    gold_rows: list[dict[str, Any]], *, order_sensitive: bool, beta: int | None = EXC_SURPLUS_BOUND
) -> bool:
    """M1 invariant at the result level: a gold result is EXC-equivalent to itself."""
    return equiv_rec_values_superset(
        gold_rows, gold_rows, order_sensitive=order_sensitive, max_surplus=beta
    )


def gold_self_scores_exf1(gold_rows: list[dict[str, Any]]) -> bool:
    """M1 invariant for the graded companion: a gold result earns EXF1 = 1 against itself."""
    return exf1(gold_rows, gold_rows) == 1.0


def exf1_probe_scores(gold_rows: list[dict[str, Any]]) -> dict[str, float]:
    """EXF1 of every M2 probe family against the gold result.

    EXF1 uses the strict exact-row identity (no surplus tolerance), so unlike the
    headline it scores even the benign surplus families at 0 — that is by design (it is
    the graded distance companion, not a second headline) and this map makes the
    behaviour auditable: null probes in particular must never earn partial credit.
    """
    return {probe.name: exf1(probe.rows, gold_rows) for probe in build_result_probes(gold_rows)}


# --------------------------------------------------------------------------- #
# retro-score: last executed candidate from a budget-exhausted agent's session
# --------------------------------------------------------------------------- #
def last_candidate_mql(steps: list[dict[str, Any]]) -> str | None:
    """Extract the most recent candidate pipeline a ReAct/agentic arm actually proposed.

    Reads the step traces of a baseline_failure/ablation row (each ``output`` carries the
    arm's per-turn action). Used to ask 'did the agent have an answer it simply never
    submitted?' — a DIAGNOSTIC column only, never folded into the headline.
    """
    from ..execution.ast_check import render_mql

    for step in reversed(steps):
        output = step.get("output") if isinstance(step, dict) else None
        if not isinstance(output, dict):
            continue
        # react arms: {action, collection, ...}; the last assistant action with a pipeline.
        mql = output.get("submitted_mql") or output.get("MQL")
        if isinstance(mql, str) and mql.strip():
            return mql
        collection = output.get("collection")
        # agentic arm summarizes tool calls; pull the last execute_mql arguments.
        for summary in reversed(output.get("tool_summaries") or []):
            if summary.get("tool") == "execute_mql":
                args = summary.get("arguments") or {}
                coll = args.get("collection")
                pipe = args.get("pipeline")
                if isinstance(coll, str) and isinstance(pipe, list):
                    return render_mql(coll, pipe)
        if isinstance(collection, str) and collection:
            # react step trace keeps collection but not the raw pipeline in `output`;
            # without a pipeline there is no executable candidate to score.
            continue
    return None
