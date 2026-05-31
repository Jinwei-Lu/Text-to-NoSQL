"""Tests for DM duplicate ``_id`` suffix policy."""

from __future__ import annotations

from tend.phase_a.dm import ensure_unique_document_ids


def test_duplicate_ids_get_row_suffix():
    docs = [
        {"_id": "gomezle01", "year": 2000},
        {"_id": "other", "year": 2001},
        {"_id": "gomezle01", "year": 2002},
    ]
    out = ensure_unique_document_ids(docs)
    assert len(out) == 3
    assert out[0]["_id"] == "gomezle01"
    assert out[1]["_id"] == "other"
    assert out[2]["_id"] == "gomezle01__row2"


def test_first_occurrence_unchanged():
    docs = [{"_id": "a"}, {"_id": "b"}, {"_id": "a"}]
    out = ensure_unique_document_ids(docs)
    assert out[0]["_id"] == "a"
    assert out[2]["_id"] == "a__row2"


def test_preserves_row_count_and_fields():
    docs = [{"_id": "x", "gp": 10}, {"_id": "x", "gp": 20}]
    out = ensure_unique_document_ids(docs)
    assert len(out) == 2
    assert out[0]["gp"] == 10
    assert out[1]["gp"] == 20
