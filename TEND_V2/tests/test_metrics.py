from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tend_benchmark.metrics import ast_check, query_field_coverage, query_structure_match
from tend_benchmark.models import CanonicalFormSet


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


class MetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.canonical_form_set = CanonicalFormSet(
            must_contain=("$setWindowFields", "$facet", "$ifNull"),
            must_not_contain=(),
            must_contain_at_root=("$setWindowFields", "$facet"),
            must_not_contain_at_root=(),
        )

    def test_ast_check_accepts_gold_query(self) -> None:
        self.assertEqual(ast_check(GOLD_QUERY, self.canonical_form_set), "pass")

    def test_ast_check_rejects_missing_root_operator(self) -> None:
        bad_query = "db.conductor.aggregate([{ $group: { _id: null, total: { $sum: 1 } } }])"
        self.assertEqual(
            ast_check(bad_query, self.canonical_form_set),
            "fail:missing:$setWindowFields",
        )

    def test_qsm_and_qfc_match_gold(self) -> None:
        self.assertEqual(query_structure_match(GOLD_QUERY, GOLD_QUERY), 1)
        self.assertEqual(query_field_coverage(GOLD_QUERY, GOLD_QUERY), 1)


if __name__ == "__main__":
    unittest.main()
