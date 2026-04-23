from pathlib import Path
import json
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tend_benchmark.evaluator import evaluate_bundle, export_solver_view

try:
    from pymongo import MongoClient
except ImportError:
    MongoClient = None


def local_mongo_available() -> bool:
    if MongoClient is None:
        return False
    try:
        client = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=1000)
        client.admin.command("ping")
        return True
    except Exception:  # noqa: BLE001
        return False


class EvaluatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.bundle_root = self.repo_root / "fixtures" / "TEND"
        self.gold_predictions = self.repo_root / "fixtures" / "predictions" / "gold.jsonl"
        self.bad_predictions = self.repo_root / "fixtures" / "predictions" / "bad.jsonl"
        self.temp_dir = Path(tempfile.mkdtemp(prefix="tend-benchmark-tests-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_export_solver_view_hides_gold_fields(self) -> None:
        out_path = self.temp_dir / "public_test.json"
        export_solver_view(self.bundle_root, out_path)
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload), 1)
        self.assertIn("nl", payload[0])
        self.assertNotIn("MQL", payload[0])
        self.assertNotIn("canonical_form_set", payload[0])

    def test_evaluate_gold_prediction(self) -> None:
        out_dir = self.temp_dir / "gold"
        rows = evaluate_bundle(self.bundle_root, self.gold_predictions, out_dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].ex, 1)
        self.assertEqual(rows[0].qim, 1)
        summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["overall"]["ex"], 1.0)

    def test_evaluate_bad_prediction(self) -> None:
        out_dir = self.temp_dir / "bad"
        rows = evaluate_bundle(self.bundle_root, self.bad_predictions, out_dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].ex, 0)
        self.assertEqual(rows[0].qim, 0)

    @unittest.skipUnless(local_mongo_available(), "local MongoDB is not available")
    def test_evaluate_gold_prediction_with_local_mongo(self) -> None:
        out_dir = self.temp_dir / "gold-local"
        rows = evaluate_bundle(
            self.bundle_root,
            self.gold_predictions,
            out_dir,
            backend_name="local-mongo",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].ex, 1)
        self.assertEqual(rows[0].efm, 1)
        self.assertEqual(rows[0].evm, 1)


if __name__ == "__main__":
    unittest.main()
