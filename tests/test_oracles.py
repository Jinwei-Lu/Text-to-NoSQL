"""Tests for the reference oracles R (Session A: tend.mechanisms.oracles).

Pure Python (no BIRD, no MongoDB): each oracle is the naive answer-definition MS gold-locks
against, so these pin its semantics — including the canonical financial/1001 present/missing
pattern and the empty-vs-missing distinction.
"""
from __future__ import annotations

import pytest

from tend.mechanisms import oracle_param_errors, reference_oracle
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


def test_present_missing_projection_allows_null_match_filter():
    snap = {
        "budget": [
            {"_id": 1, "expense": {"cost": 10, "link_to_member": "m1"}},
            {"_id": 2},
            {"_id": 3, "expense": {"cost": 9, "link_to_member": "m2"}},
        ],
        "income": [
            {"link_to_member": "m1", "amount": 2},
            {"link_to_member": "m1", "amount": 3},
            {"link_to_member": "m2", "amount": 0},
        ],
    }
    out = reference_oracle("present_missing_projection")(snap, {
        "parent_collection": "budget",
        "embed_field": "expense",
        "numerator_path": "expense.cost",
        "target_field": "expense_to_income_ratio",
        "absent_value": 0,
        "denom": {
            "collection": "income",
            "local_id": "expense.link_to_member",
            "foreign_field": "link_to_member",
            "match": None,
            "sum_field": "amount",
            "zero_value": 1,
        },
    })
    ratios = {d["_id"]: d["expense_to_income_ratio"] for d in out}
    assert ratios == {1: 2.0, 2: 0, 3: 9.0}


def test_optional_embed_projection_preserves_docs_with_non_null_default():
    snap = {
        "account": [
            {"_id": 1, "loan": {"amount": 100}},
            {"_id": 2},
            {"_id": 3, "loan": {"amount": None}},
        ]
    }
    out = reference_oracle("optional_embed_projection")(snap, {
        "parent_collection": "account",
        "embed_field": "loan",
        "value_path": "loan.amount",
        "target_field": "loan_amount",
        "missing_default": 0,
    })
    assert len(out) == 3
    assert {d["_id"]: d["loan_amount"] for d in out} == {1: 100, 2: 0, 3: 0}
    assert out[0]["loan"] == {"amount": 100}


def test_optional_embed_projection_accepts_parent_rooted_value_path():
    snap = {"account": [{"_id": 1, "loan": {"amount": 100}}, {"_id": 2}]}
    out = reference_oracle("optional_embed_projection")(snap, {
        "parent_collection": "account",
        "embed_field": "loan",
        "value_path": "loan.amount",
        "target_field": "loan_amount",
        "missing_default": 0,
    })
    assert {d["_id"]: d["loan_amount"] for d in out} == {1: 100, 2: 0}


def test_oracle_param_errors_catch_missing_nested_params():
    assert oracle_param_errors("present_missing_projection", {
        "parent_collection": "account",
        "embed_field": "loan",
        "numerator_path": "loan.amount",
        "target_field": "ratio",
        "denom": {"collection": "trans"},
    }) == [
        "reference_oracle.params.denom missing required keys: "
        "['local_id', 'foreign_field', 'sum_field']"
    ]


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


def test_topn_sorts_missing_and_null_last_by_default_in_ascending_order():
    snap = {"p": [
        {"n": "missing"},
        {"n": "null", "price": None},
        {"n": "low", "price": 5},
        {"n": "high", "price": 30},
    ]}
    out = reference_oracle("topn")(snap, {
        "collection": "p", "sort_key": "price", "order": "asc", "n": 4, "project": ["n"]})
    assert [d["n"] for d in out] == ["low", "high", "missing", "null"]


def test_topn_sorts_missing_and_null_last_by_default_in_descending_order():
    snap = {"p": [
        {"n": "missing"},
        {"n": "null", "price": None},
        {"n": "low", "price": 5},
        {"n": "high", "price": 30},
    ]}
    out = reference_oracle("topn")(snap, {
        "collection": "p", "sort_key": "price", "order": "desc", "n": 4, "project": ["n"]})
    assert [d["n"] for d in out] == ["high", "low", "missing", "null"]


def test_topn_can_sort_missing_and_null_first_when_requested():
    snap = {"p": [
        {"n": "missing"},
        {"n": "null", "price": None},
        {"n": "low", "price": 5},
        {"n": "high", "price": 30},
    ]}
    out = reference_oracle("topn")(snap, {
        "collection": "p",
        "sort_key": "price",
        "order": "desc",
        "nulls": "first",
        "n": 4,
        "project": ["n"],
    })
    assert [d["n"] for d in out] == ["missing", "null", "high", "low"]


def test_unknown_template_raises():
    with pytest.raises(OracleError):
        reference_oracle("no_such_template")
