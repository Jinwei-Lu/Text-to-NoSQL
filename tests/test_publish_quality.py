"""Focused tests for strict release quality auditing."""
from __future__ import annotations

from tend.publish.quality import (
    _declared_nlq_output_fields,
    _final_mql_output_fields,
    _nlq_alignment_issues,
)


def _record() -> dict[str, object]:
    return {"db_id": "financial", "record_id": 1}


def test_nlq_alignment_reports_missing_canonical_output_fields_from_project():
    pipeline = [
        {
            "$project": {
                "_id": 0,
                "account_id": 1,
                "region": "$district.region",
                "total": "$amount",
            },
        },
        {"$limit": 10},
    ]

    issues = _nlq_alignment_issues(
        _record(),
        track="canonical",
        text="On `account` return the top 10 documents; output fields account_id, total.",
        pipeline=pipeline,
    )

    issue = next(issue for issue in issues if issue.code == "NLQ_OUTPUT_FIELDS_MISSING")
    assert issue.severity == "error"
    assert issue.track == "canonical"
    assert issue.evidence["missing_fields"] == ["region"]
    assert issue.evidence["mql_output_fields"] == ["account_id", "region", "total"]
    assert issue.evidence["declared_fields"] == ["account_id", "total"]
    assert issue.evidence["stage_op"] == "$project"


def test_nlq_alignment_reports_missing_colloquial_output_fields_from_group():
    pipeline = [
        {
            "$group": {
                "_id": "$region",
                "account_count": {"$sum": 1},
                "total": {"$sum": "$amount"},
            },
        },
        {"$sort": {"total": -1}},
    ]

    issues = _nlq_alignment_issues(
        _record(),
        track="colloquial",
        text="Show aggregate groups sorted by total descending with fields _id, total.",
        pipeline=pipeline,
    )

    issue = next(issue for issue in issues if issue.code == "NLQ_OUTPUT_FIELDS_MISSING")
    assert issue.evidence["missing_fields"] == ["account_count"]
    assert issue.evidence["mql_output_fields"] == ["_id", "account_count", "total"]
    assert issue.evidence["stage_op"] == "$group"


def test_nlq_alignment_accepts_complete_project_output_fields_with_implicit_id():
    pipeline = [
        {"$project": {"account_id": 1, "region": 1}},
        {"$sort": {"account_id": 1}},
    ]

    issues = _nlq_alignment_issues(
        _record(),
        track="canonical",
        text="On `account` return documents; output fields _id, account_id, region.",
        pipeline=pipeline,
    )

    assert not [issue for issue in issues if issue.code == "NLQ_OUTPUT_FIELDS_MISSING"]


def test_nlq_output_field_check_requires_explicit_field_clause():
    pipeline = [{"$project": {"_id": 0, "account_id": 1, "region": 1}}]

    issues = _nlq_alignment_issues(
        _record(),
        track="canonical",
        text="List account documents.",
        pipeline=pipeline,
    )

    assert not [issue for issue in issues if issue.code == "NLQ_OUTPUT_FIELDS_MISSING"]


def test_declared_output_fields_parse_backticks_and_and_separator():
    parsed = _declared_nlq_output_fields(
        "colloquial",
        "Show groups with fields `_id`, total and account_count.",
    )

    assert parsed is not None
    fields, clause = parsed
    assert fields == {"_id", "account_count", "total"}
    assert clause == "`_id`, total and account_count."


def test_final_mql_output_fields_skips_exclusion_only_project():
    pipeline = [{"$project": {"_id": 0, "debug": 0}}]

    assert _final_mql_output_fields(pipeline) is None
