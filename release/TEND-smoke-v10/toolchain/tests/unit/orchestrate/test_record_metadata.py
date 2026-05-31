"""Unit tests for record metadata derivation."""

from __future__ import annotations

from tend.orchestrate.record_metadata import (
    aggregation_depth_from_mql,
    derive_record_axes,
    join_depth_from_mql,
)


def test_join_depth_counts_lookup_and_graphlookup() -> None:
    mql = 'db.coll.aggregate([{"$match": {}}, {"$graphLookup": {"from": "x"}}, {"$lookup": {}}])'
    assert join_depth_from_mql(mql) == 2


def test_aggregation_depth_buckets() -> None:
    shallow = 'db.coll.aggregate([{"$match": {}}])'
    assert aggregation_depth_from_mql(shallow) == "shallow"
    medium = ", ".join(f'{{"$match": {{"i": {i}}}}}' for i in range(6))
    assert aggregation_depth_from_mql(f"db.coll.aggregate([{medium}])") == "medium"


def test_derive_record_axes_uses_context_schema_pattern() -> None:
    mql = 'db.coll.aggregate([{"$lookup": {"from": "other", "localField": "a", "foreignField": "b", "as": "x"}}])'
    axes = derive_record_axes(mql, {}, {"schema_pattern": "reference"})
    assert axes["join_depth"] == 1
    assert axes["schema_pattern"] == "reference"
    assert axes["aggregation_depth"] in {"shallow", "medium", "deep"}
