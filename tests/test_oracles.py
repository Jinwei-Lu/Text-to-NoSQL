"""Tests for the reference oracles R (Session A: tend.mechanisms.oracles).

Pure Python (no BIRD, no MongoDB): each oracle is the naive answer-definition MS gold-locks
against, so these pin its semantics — including the canonical financial/1001 present/missing
pattern and the empty-vs-missing distinction.
"""
from __future__ import annotations

import pytest

from tend.mechanisms import reference_oracle
from tend.mechanisms.oracles import OracleError


def test_present_missing_projection_financial_pattern():
    snap = {
        "account": [{"_id": 1, "loan": {"amount": 100}}, {"_id": 2},
                    {"_id": 3, "loan": {"amount": 60}}],
        "trans": [
            {"account_id": 1, "type": "PRIJEM", "amount": 40},
            {"account_id": 1, "type": "PRIJEM", "amount": 10},
            {"account_id": 1, "type": "VYDAJ", "amount": 999},
            {"account_id": 3, "type": "VYDAJ", "amount": 5},
        ],
    }
    out = reference_oracle("present_missing_projection")(snap, {
        "parent_collection": "account", "embed_field": "loan",
        "numerator_path": "loan.amount", "target_field": "ratio", "absent_value": 0,
        "denom": {"collection": "trans", "local_id": "_id", "foreign_field": "account_id",
                  "match": {"field": "type", "value": "PRIJEM"}, "sum_field": "amount",
                  "zero_value": 1},
    })
    ratios = {d["_id"]: d["ratio"] for d in out}
    assert ratios == {1: 2.0, 2: 0, 3: 60.0}      # has/has-no-loan; zero-credit -> /1
    assert len(out) == 3                           # preserve: every account kept


def test_per_subtype_agg_uses_subtype_own_field():
    snap = {"a": [
        {"t": "written", "written_score": 80},
        {"t": "written", "written_score": 90},
        {"t": "oral", "oral_score": 70},
    ]}
    out = reference_oracle("per_subtype_agg")(snap, {
        "collection": "a", "discriminator": "t",
        "field_by_subtype": {"written": "written_score", "oral": "oral_score"}, "agg": "avg"})
    vals = {d["_id"]: d["value"] for d in out}
    assert vals == {"written": 85.0, "oral": 70.0}


def test_existence_count_distinguishes_missing_from_null():
    snap = {"a": [{"x": 1}, {"x": None}, {}]}    # present, present(null), missing
    out = reference_oracle("existence_count")(snap, {"collection": "a", "field": "x"})
    assert out == [{"count": 2}]                  # null counts as present; missing does not


def test_null_coalesce_agg_defaults_missing():
    snap = {"a": [{"v": 10}, {}, {"v": None}]}
    out = reference_oracle("null_coalesce_agg")(snap, {
        "collection": "a", "field": "v", "default": 0, "agg": "sum"})
    assert out == [{"value": 10.0}]


def test_simple_filter_and_topn_and_group_count():
    snap = {"p": [{"n": "a", "price": 5}, {"n": "b", "price": 30}, {"n": "c", "price": 20}]}
    f = reference_oracle("simple_filter")(snap, {
        "collection": "p", "predicates": [{"field": "price", "op": "gt", "value": 10}],
        "project": ["n"]})
    assert {d["n"] for d in f} == {"b", "c"}
    t = reference_oracle("topn")(snap, {"collection": "p", "sort_key": "price",
                                        "order": "desc", "n": 1, "project": ["n"]})
    assert t == [{"n": "b"}]
    g = reference_oracle("group_count")(snap, {"collection": "p", "group_by": "n"})
    assert sorted(d["_id"] for d in g) == ["a", "b", "c"]


def test_unknown_template_raises():
    with pytest.raises(OracleError):
        reference_oracle("no_such_template")
