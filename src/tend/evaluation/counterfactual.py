"""Counterfactual-witness probe (experiment design §1.3, M3).

A bounded-tolerant EXC pass means a prediction's result matched the gold's on the ONE
frozen witness ``D``. That can still be a coincidence: a wrong pipeline may land on the
right rows because the witness happens not to contain a distinguishing document. This
module builds a counterfactual witness ``D'`` by injecting **gold-invariant distractors**
— schema-conformant documents whose presence does NOT change ``NormExec(gold, D)`` — and
then asks whether a headline-passing prediction still agrees with the gold on ``D'``. A
disagreement is a caught false positive (a Spider-style test-suite/distinguishing-database
check, applied post-hoc to predictions that already scored 1).

Distractor generation is self-validating and needs NO access to the gold predicate
semantics: candidate documents are mutated copies of sampled witness rows with every leaf
scalar pushed to a sentinel out-of-domain value (and a fresh ``_id``), and only the subset
that leaves the gold result byte-identical is kept. A record whose gold reads the whole
collection (e.g. ``count all``) admits no safe distractor; that is reported, not hidden —
the flip rate is over records where the probe actually applies.

Pure-diagnostic: this never feeds back into a solver, never changes a release witness, and
never overrides the headline EXC scored on ``D``.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..execution.ast_check import parse_pipeline
from ..execution.mongo import equiv_rec_values
from ..execution.signature import world_signature

_DATE_RE = re.compile(r"^\d{4}[-/]\d{2}")
_SENTINEL_STR = "__CF_DISTRACTOR__"
_NUMBER_OFFSET = 10**9
_SENTINEL_DATE = "2999-12-31"


class CounterfactualExecutor(Protocol):
    """Same surface the evaluator uses; lets the probe reuse the live executor."""

    def load_witness(self, db_id: str, collections: dict[str, list[dict[str, Any]]]) -> None:
        ...

    def norm_exec(self, db_id: str, mql: str) -> list[dict[str, Any]]:
        ...


@dataclass(frozen=True, slots=True)
class CounterfactualWitness:
    db_id: str
    collections: dict[str, list[dict[str, Any]]]  # D' = D + kept distractors
    base_signature: str
    cf_signature: str
    distractors_injected: int
    distractors_kept: int
    applicable: bool  # at least one gold-invariant distractor survived
    note: str = ""


@dataclass(frozen=True, slots=True)
class FlipVerdict:
    applicable: bool  # the probe ran (pred passed on D AND a safe distractor exists)
    flipped: bool  # pred agreed with gold on D but disagreed on D'
    passed_on_base: bool  # NormExec(pred, D)  ≡ NormExec(gold, D)
    passed_on_cf: bool  # NormExec(pred, D') ≡ NormExec(gold, D')
    note: str = ""


def _order_sensitive(mql: str) -> bool:
    """Gold enforces row order iff its pipeline sorts/limits at the root (mirrors metrics)."""
    try:
        _coll, pipeline = parse_pipeline(mql)
    except Exception:  # noqa: BLE001 - a malformed gold is handled by the caller
        return False
    return any(
        isinstance(stage, dict)
        and any(op in {"$sort", "$limit", "$skip", "$setWindowFields"} for op in stage)
        for stage in pipeline
    )


def _mutate_leaf(value: Any, salt: int) -> Any:
    """Push a scalar to a sentinel out-of-domain value of the SAME JSON type."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        base = _NUMBER_OFFSET + salt
        return base if isinstance(value, int) else float(base) + 0.5
    if isinstance(value, str):
        if _DATE_RE.match(value):
            return _SENTINEL_DATE
        return f"{_SENTINEL_STR}{salt}"
    return value


def _mutate_doc(doc: Any, salt: int) -> Any:
    """Deep copy with every leaf scalar mutated; keys/structure preserved (schema-conformant)."""
    if isinstance(doc, dict):
        return {k: _mutate_doc(v, salt) for k, v in doc.items()}
    if isinstance(doc, list):
        return [_mutate_doc(v, salt) for v in doc]
    return _mutate_leaf(doc, salt)


def _candidate_distractors(
    rows: list[dict[str, Any]], collection: str, per_collection: int
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows[:per_collection]):
        shadow = _mutate_doc(copy.deepcopy(row), salt=i)
        if isinstance(shadow, dict):
            # Fresh, collision-proof identity so the distractor is genuinely a new entity.
            shadow["_id"] = f"{_SENTINEL_STR}{collection}_{i}"
        out.append(shadow)
    return out


def build_counterfactual_witness(
    executor: CounterfactualExecutor,
    db_id: str,
    base_collections: dict[str, list[dict[str, Any]]],
    gold_mql: str,
    *,
    per_collection: int = 4,
) -> CounterfactualWitness:
    """Inject gold-invariant distractors into every collection of ``base_collections``.

    A candidate distractor is KEPT only if adding it leaves ``NormExec(gold)`` byte-equal
    to the baseline result — so the kept set provably does not change the correct answer,
    yet exercises whether a prediction over-/under-selects on the larger world.
    """
    order_sensitive = _order_sensitive(gold_mql)
    base_signature = world_signature(base_collections)

    executor.load_witness(db_id, base_collections)
    try:
        gold_base = executor.norm_exec(db_id, gold_mql)
    except Exception as exc:  # noqa: BLE001 - a gold that won't execute can't be probed
        return CounterfactualWitness(
            db_id=db_id,
            collections=base_collections,
            base_signature=base_signature,
            cf_signature=base_signature,
            distractors_injected=0,
            distractors_kept=0,
            applicable=False,
            note=f"gold did not execute on base witness: {type(exc).__name__}",
        )

    cf = copy.deepcopy(base_collections)
    injected = 0
    kept = 0
    for collection, rows in base_collections.items():
        for cand in _candidate_distractors(rows, collection, per_collection):
            injected += 1
            cf[collection].append(cand)
            executor.load_witness(db_id, cf)
            try:
                gold_cf = executor.norm_exec(db_id, gold_mql)
                safe = equiv_rec_values(gold_cf, gold_base, order_sensitive=order_sensitive)
            except Exception:  # noqa: BLE001 - a candidate that breaks gold is unsafe
                safe = False
            if safe:
                kept += 1
            else:
                cf[collection].pop()  # revert: distractor changed the correct answer

    note = "" if kept else "no gold-invariant distractor survived (gold reads the full set)"
    return CounterfactualWitness(
        db_id=db_id,
        collections=cf,
        base_signature=base_signature,
        cf_signature=world_signature(cf),
        distractors_injected=injected,
        distractors_kept=kept,
        applicable=kept > 0,
        note=note,
    )


def counterfactual_flip(
    executor: CounterfactualExecutor,
    db_id: str,
    cf: CounterfactualWitness,
    pred_mql: str,
    gold_mql: str,
) -> FlipVerdict:
    """Does a headline-passing prediction survive the counterfactual witness?

    ``flipped=True`` means the prediction matched the gold on ``D`` but diverged on ``D'``
    — a false positive the single-witness EXC could not catch. The probe is ``applicable``
    only when a safe distractor exists AND the prediction actually passed on ``D``.
    """
    order_sensitive = _order_sensitive(gold_mql)

    def _exec(mql: str) -> list[dict[str, Any]] | None:
        try:
            return executor.norm_exec(db_id, mql)
        except Exception:  # noqa: BLE001 - an unexecutable candidate scores no agreement
            return None

    executor.load_witness(db_id, cf.collections)  # ensure cf state regardless of call order
    base_collections_signature = cf.base_signature

    # Recompute agreement on D (the probe must be anchored to a real headline pass).
    executor.load_witness(db_id, _strip_distractors(cf))
    pred_base, gold_base = _exec(pred_mql), _exec(gold_mql)
    passed_base = (
        pred_base is not None
        and gold_base is not None
        and equiv_rec_values(pred_base, gold_base, order_sensitive=order_sensitive)
    )

    if not cf.applicable or not passed_base:
        return FlipVerdict(
            applicable=False,
            flipped=False,
            passed_on_base=passed_base,
            passed_on_cf=False,
            note=(
                "no safe distractor" if not cf.applicable else "prediction did not pass on base"
            ),
        )

    executor.load_witness(db_id, cf.collections)
    pred_cf, gold_cf = _exec(pred_mql), _exec(gold_mql)
    passed_cf = (
        pred_cf is not None
        and gold_cf is not None
        and equiv_rec_values(pred_cf, gold_cf, order_sensitive=order_sensitive)
    )
    assert base_collections_signature  # signature is carried for the caller's ledger
    return FlipVerdict(
        applicable=True,
        flipped=not passed_cf,
        passed_on_base=True,
        passed_on_cf=passed_cf,
    )


def _strip_distractors(cf: CounterfactualWitness) -> dict[str, list[dict[str, Any]]]:
    """Reconstruct the base witness ``D`` by dropping injected distractor docs."""
    base: dict[str, list[dict[str, Any]]] = {}
    for collection, rows in cf.collections.items():
        base[collection] = [
            row
            for row in rows
            if not (
                isinstance(row, dict)
                and isinstance(row.get("_id"), str)
                and str(row.get("_id")).startswith(f"{_SENTINEL_STR}{collection}_")
            )
        ]
    return base


@dataclass
class FlipRateLedger:
    """Aggregate flip outcomes across a system's headline-passing predictions."""

    system_id: str
    applicable: int = 0
    flipped: int = 0
    skipped_no_distractor: int = 0
    skipped_not_passed: int = 0
    per_record: list[dict[str, Any]] = field(default_factory=list)

    def add(self, db_id: str, record_id: Any, verdict: FlipVerdict) -> None:
        if not verdict.applicable:
            if "distractor" in verdict.note:
                self.skipped_no_distractor += 1
            else:
                self.skipped_not_passed += 1
            return
        self.applicable += 1
        if verdict.flipped:
            self.flipped += 1
        self.per_record.append(
            {
                "db_id": db_id,
                "record_id": record_id,
                "flipped": verdict.flipped,
                "passed_on_cf": verdict.passed_on_cf,
            }
        )

    @property
    def flip_rate(self) -> float:
        return self.flipped / self.applicable if self.applicable else 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "system_id": self.system_id,
            "applicable": self.applicable,
            "flipped": self.flipped,
            "flip_rate": round(self.flip_rate, 6),
            "skipped_no_distractor": self.skipped_no_distractor,
            "skipped_not_passed": self.skipped_not_passed,
        }
