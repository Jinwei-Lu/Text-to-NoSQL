"""Execution-verify the hand-authored gold TEND cases on real MongoDB.

These cases (tests/fixtures/manual/financial_cases.json) are the *expected-results oracle* for
the small-scale run test: each hand-authored gold MQL is verified against the real migrated
`financial` witness to (1) satisfy its thin cfs (AST_check), (2) be NormExec-equivalent to an
independent naive reference oracle R, (3) honor its shape_policy (preserve cardinality / group
key), (4) be discriminated from a plausible-wrong mutation (P3), and (5) pass the C1-C9 record
contract. Skipped when MongoDB or the BIRD source is unavailable.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_BIRD = Path("minidev/MINIDEV")
_CASES = Path("tests/fixtures/manual/financial_cases.json")


def _mongo_up() -> bool:
    try:
        from pymongo import MongoClient

        MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=1500).admin.command("ping")
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not (_BIRD.exists() and _CASES.exists() and _mongo_up()),
    reason="needs BIRD mini-dev + reachable MongoDB",
)


@pytest.fixture(scope="module")
def loaded():
    """Migrate financial, load it into an isolated working db, yield (executor, witness)."""
    from tend.config import Settings
    from tend.construct.migrate import build_plan, migrate
    from tend.execution.mongo import MongoExecutor
    from tend.observability import setup_logging
    from tend.source import BirdSource

    src = BirdSource(_BIRD)
    data = migrate(src, "financial", build_plan(src, "financial"))
    settings = Settings.from_env(run_id="pytest_manual")
    log = setup_logging(settings.run_dir, console=False)
    mongo = MongoExecutor(settings, log)
    mongo.load_witness("financial", data)
    yield mongo, data
    mongo.close()
    log.close()
    src.close()


def _cases():
    return json.loads(_CASES.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _cases(), ids=[c["name"] for c in _cases()])
def test_manual_gold_case(loaded, case):
    from tend.execution import ast_check, derive_canonical_form_set
    from tend.execution.mongo import _normalize_doc, equiv_rec
    from tend.mechanisms import reference_oracle
    from tend.publish import validate_record

    mongo, witness = loaded
    rec = case["record"]
    gold = rec["MQL"]
    cfs = rec["canonical_form_set"]

    # 1) the stored thin cfs matches what the deterministic deriver produces
    assert derive_canonical_form_set(gold, rec["shape_policy"]) == cfs

    # 2) AST_check — structural gold-class membership
    ok, hits = ast_check(gold, cfs)
    assert ok, f"gold fails its own cfs: {hits}"

    # 3) NormExec gold + shape_policy honored
    gold_norm = mongo.norm_exec("financial", gold)
    assert gold_norm, "gold result is empty (P4 trivial)"
    exp = case.get("expect", {})
    if exp.get("cardinality_equals_input"):
        assert len(gold_norm) == mongo.count("financial", exp["cardinality_equals_input"])
    if exp.get("group_key_preserved"):
        assert all(exp["group_key_preserved"] in d for d in gold_norm)

    # 4) reference oracle R ≡_rec gold (independent correctness anchor, P1)
    R = reference_oracle(case["oracle"]["template"])(witness, case["oracle"]["params"])
    r_norm = [_normalize_doc(d) for d in R]
    assert equiv_rec(gold_norm, r_norm, order_sensitive=False), \
        f"reference oracle disagrees with gold (gold n={len(gold_norm)}, R n={len(r_norm)})"

    # 5) plausible-wrong mutation EX-fails (P3 discriminative)
    mut_norm = mongo.norm_exec("financial", case["mutation"])
    assert not equiv_rec(gold_norm, mut_norm, order_sensitive=False), "mutation not discriminated"

    # 6) record contract C1-C9
    rec_full = dict(rec, world_signature="sha256:" + "0" * 64)  # signature checked elsewhere
    assert validate_record(rec_full) == []
