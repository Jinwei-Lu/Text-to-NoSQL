"""Unit tests for SA-6 evaluator (metrics, fingerprint, aggregation, disclosure)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tend.config import REPO_ROOT, SCHEMAS_ROOT
from tend.core import AST_check
from tend.core.models import CanonicalFormSet
from tend.evaluate.disclosure import check_disclosure_artifacts, disclosure_complete
from tend.evaluate.disjointness import verify_six_pool_disjoint
from tend.evaluate.fingerprint import compute_fingerprint, parse_failure_fingerprint
from tend.evaluate.leaderboard import build_leaderboard_payload, validate_leaderboard_payload
from tend.evaluate.metrics import (
    exact_match,
    parse_failure_fingerprint as metrics_parse_failure,
    query_field_coverage,
    query_intent_match,
    query_structure_match,
)
from tend.evaluate.panel import empirical_difficulty_bucket, stub_panel_pr
from tend.evaluate.slice_aggregate import SIX_AXES, aggregate_slices, bucket_join_depth
from tend.cli.evaluate import (
    assert_solver_path_allowed,
    load_allow_list,
    narrow_record_for_solver,
)

GOLD_QUERY = (
    "db.conductor.aggregate(["
    " { $unwind: { path: \"$orchestra\", preserveNullAndEmptyArrays: false } },"
    " { $unwind: { path: \"$orchestra.performance\", preserveNullAndEmptyArrays: false } },"
    " { $setWindowFields: { partitionBy: \"$_id\", sortBy: { \"orchestra.performance.Performance_ID\": 1 },"
    " output: { moving_avg_attendance: { $avg: { $ifNull: [\"$orchestra.performance.Attendance\", 0] },"
    " window: { documents: [-2, 0] } } } } },"
    " { $group: { _id: \"$_id\", Name: { $first: { $ifNull: [\"$Name\", \"(unknown)\"] } },"
    " last_window_avg: { $last: \"$moving_avg_attendance\" } } },"
    " { $facet: { per_conductor: [ { $project: { _id: 0, Name: 1, last_window_avg: 1 } } ],"
    " global_median: [ { $sort: { last_window_avg: 1 } }, { $group: { _id: null, vals: { $push: \"$last_window_avg\" } } },"
    " { $project: { _id: 0, median: { $arrayElemAt: [\"$vals\", { $floor: { $divide: [{ $size: \"$vals\" }, 2] } }] } } } ] } },"
    " { $project: { kept: { $filter: { input: \"$per_conductor\", as: \"c\", cond: { $gt: [\"$$c.last_window_avg\", { $arrayElemAt: [\"$global_median.median\", 0] }] } } } } },"
    " { $unwind: \"$kept\" },"
    " { $project: { _id: 0, Name: \"$kept.Name\", last_window_avg: \"$kept.last_window_avg\" } }"
    " ])"
)

STRUCTURAL_FAIL_QUERY = "db.conductor.aggregate([{ $group: { _id: null, total: { $sum: 1 } } }])"

CFS = CanonicalFormSet(
    must_contain=("$setWindowFields", "$facet", "$ifNull"),
    must_not_contain=(),
    must_contain_at_root=("$setWindowFields", "$facet"),
    must_not_contain_at_root=(),
)


@pytest.fixture
def orchestra_record() -> dict:
    path = REPO_ROOT / "proposals" / "fixtures" / "orchestra" / "record.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_canonical_fingerprints_from_proposal() -> None:
    """Reproduce 05 §5 canonical scenarios (structure / verbatim / equivalent)."""
    gold_rows = [{"Name": "A", "last_window_avg": 2.0}]
    ast_fail = AST_check(STRUCTURAL_FAIL_QUERY, CFS)

    structural = compute_fingerprint(
        STRUCTURAL_FAIL_QUERY,
        GOLD_QUERY,
        [],
        gold_rows,
        CFS,
        ast_result=ast_fail,
    )
    assert structural[:2] == (0, 0)
    assert structural[2] == query_field_coverage(STRUCTURAL_FAIL_QUERY, GOLD_QUERY)
    assert structural[3:] == (0, 0, 0, 0)

    verbatim = compute_fingerprint(GOLD_QUERY, GOLD_QUERY, gold_rows, gold_rows, CFS)
    assert verbatim == (1, 1, 1, 1, 1, 1, 1)

    equiv_query = GOLD_QUERY.replace("$facet", "$facet /*rewritten*/", 1)
    equiv = compute_fingerprint(equiv_query, GOLD_QUERY, gold_rows, gold_rows, CFS)
    assert equiv[0] == 0
    assert equiv[2] == 1
    assert equiv[3:] == (1, 1, 1, 1)

    assert parse_failure_fingerprint() == metrics_parse_failure()


def test_parse_failure_all_zeros() -> None:
    fp = compute_fingerprint("not valid mql", GOLD_QUERY, [], [], CFS)
    assert fp == (0, 0, 0, 0, 0, 0, 0)


def test_metrics_gold_self_match() -> None:
    assert exact_match(GOLD_QUERY, GOLD_QUERY) == 1
    assert query_structure_match(GOLD_QUERY, GOLD_QUERY) == 1
    assert query_intent_match(GOLD_QUERY, CFS) == 1


def test_six_axes_and_bucket() -> None:
    assert bucket_join_depth(2) == "2"
    assert bucket_join_depth(4) == "3+"
    assert len(SIX_AXES) == 6

    records = [
        {
            "record_id": 1,
            "domain_id": "arts",
            "join_depth": 1,
            "aggregation_depth": "deep",
            "schema_pattern": "embed",
            "schema_flex": "none",
            "difficulty": "L4",
        }
    ]
    fingerprints = [{"record_id": 1, "fp": (1, 1, 1, 1, 1, 1, 1)}]
    slices = aggregate_slices(fingerprints, records)
    assert "domain" in slices
    assert slices["domain"]["arts"]["EM"] == 1.0


def test_disjointness_six_pools() -> None:
    report = verify_six_pool_disjoint()
    assert report["violations"] == []
    if report.get("shared_model_mode"):
        assert report["model_count"] >= 1
    else:
        assert report["model_count"] >= 20


def test_solver_narrow_face() -> None:
    allow_list = load_allow_list()
    assert "audit/**" in allow_list["tier1_forbidden_glob"]
    with pytest.raises(PermissionError):
        assert_solver_path_allowed("audit/orchestra/1001/qir.yaml")
    narrow = narrow_record_for_solver(
        {
            "record_id": 1001,
            "MQL": "secret",
            "canonical_form_set": {},
            "nl_queries": {"canonical": "q"},
            "mutations_ref": "x",
        }
    )
    assert "MQL" not in narrow
    assert "mutations_ref" not in narrow


def test_leaderboard_schema_valid(orchestra_record: dict) -> None:
    records = [orchestra_record]
    fingerprints = [{"record_id": 1001, "fp": (1, 1, 1, 1, 1, 1, 1)}]
    panel_pr = stub_panel_pr(records)
    payload = build_leaderboard_payload(
        submission_id="tend-test-eval",
        solver_id="unit-test",
        release_tag="tend-release-dev0",
        fingerprints=fingerprints,
        records=records,
        panel_pr_meta=panel_pr,
        solver_llm_backbones=[
            {"model_id": "stub", "vendor": "tend", "version_pin": "dev0"},
        ],
        eval_dir=Path("out/eval-test"),
        panel_stub=True,
    )
    validate_leaderboard_payload(payload)


def test_disclosure_mvp_dev0(tmp_path: Path, orchestra_record: dict) -> None:
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    (eval_dir / "fingerprints.csv").write_text("record_id,em,qsm,qfc,ex,efm,evm,qim\n", encoding="utf-8")
    (eval_dir / "per_record_metrics.csv").write_text("record_id\n", encoding="utf-8")
    (eval_dir / "slices").mkdir()
    (eval_dir / "nnc_histogram.json").write_text("{}", encoding="utf-8")
    (eval_dir / "ra_pass_rate.json").write_text('{"pass_rate": 1.0}', encoding="utf-8")
    (eval_dir / "world_signature_digest.txt").write_text("sha256:00", encoding="utf-8")

    leaderboard = build_leaderboard_payload(
        submission_id="tend-test",
        solver_id="unit",
        release_tag="tend-release-dev0",
        fingerprints=[{"record_id": 1001, "fp": (1, 1, 1, 1, 1, 1, 1)}],
        records=[orchestra_record],
        panel_pr_meta=stub_panel_pr([orchestra_record]),
        solver_llm_backbones=[{"model_id": "m", "vendor": "v", "version_pin": "p"}],
        eval_dir=eval_dir,
        panel_stub=True,
    )
    (eval_dir / "leaderboard.json").write_text(json.dumps(leaderboard), encoding="utf-8")
    (eval_dir / "_meta.json").write_text(json.dumps({"panel_stub": True}), encoding="utf-8")

    checks = check_disclosure_artifacts(eval_dir, leaderboard=leaderboard, panel_stub=True)
    assert disclosure_complete(checks, require_panel=False)


def test_empirical_difficulty_buckets() -> None:
    assert empirical_difficulty_bucket(0.9) == "easy"
    assert empirical_difficulty_bucket(0.6) == "medium"
    assert empirical_difficulty_bucket(0.3) == "hard"
    assert empirical_difficulty_bucket(0.1) == "expert"


def test_allow_list_file_exists() -> None:
    assert (SCHEMAS_ROOT / "solver_allow_list.json").exists()
