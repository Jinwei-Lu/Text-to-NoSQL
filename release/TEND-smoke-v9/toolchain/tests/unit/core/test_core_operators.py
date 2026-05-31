"""Core operator unit tests from proposals/01 §II-7."""

from __future__ import annotations

import re

import pytest

from tend.core.ast_check import AST_check, disabled_operator_scanner
from tend.core.ex_verdict import EX_verdict
from tend.core.equiv import equiv_rec
from tend.core.normexec import NormExec
from tend.core.signatures import world_signature
from tend.errors import BOT


def strip_root_stage(mql: str, stage: str) -> str:
    return re.sub(rf"\{{\s*{re.escape(stage)}\s*:", "{ $group:", mql, count=1)


def test_AST_check_orchestra_1001_pass(orchestra_record):
    assert AST_check(orchestra_record["MQL"], orchestra_record["canonical_form_set"]) == "pass"


def test_AST_check_missing_facet_fail(orchestra_record):
    q_bad = strip_root_stage(orchestra_record["MQL"], "$facet")
    assert AST_check(q_bad, orchestra_record["canonical_form_set"]) != "pass"


def test_disabled_operator_sample_fail():
    q = 'db.c.aggregate([{"$sample": {"size": 1}}])'
    assert disabled_operator_scanner(q) is True


def test_disabled_operator_now_fail():
    q = 'db.c.aggregate([{"$match": {"t": "$$NOW"}}])'
    assert disabled_operator_scanner(q) is True


def test_NormExec_gold_non_bot(orchestra_record, orchestra_snapshot):
    result = NormExec(orchestra_record["MQL"], orchestra_snapshot)
    assert not isinstance(result, BOT)


def test_equiv_rec_null_vs_missing():
    assert equiv_rec({"a": None}, {}, order_sensitive=False) is False


def test_equiv_rec_float_tolerance():
    assert equiv_rec(1.0, 1.0 + 1e-12, order_sensitive=False) is True


def test_world_signature_stable(orchestra_snapshot):
    sig = world_signature(orchestra_snapshot)
    assert sig.startswith("sha256:")
    assert world_signature(orchestra_snapshot) == sig


def test_EX_verdict_gold_member(orchestra_record, orchestra_snapshot):
    assert EX_verdict(orchestra_record["MQL"], orchestra_record, orchestra_snapshot) is True


def test_EX_verdict_mutation_fail(orchestra_record, orchestra_snapshot, orchestra_mutations):
    for mut in orchestra_mutations:
        assert EX_verdict(mut["MQL"], orchestra_record, orchestra_snapshot) is False
