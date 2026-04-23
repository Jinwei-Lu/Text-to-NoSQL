from pathlib import Path
import json
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tend_construct.pipeline import build_dataset, validate_dataset


class ConstructPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.assets_root = self.repo_root / "assets" / "domain_templates"
        self.temp_dir = Path(tempfile.mkdtemp(prefix="tend-construct-tests-"))
        self.output_root = self.temp_dir / "TEND"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_build_dataset_and_validate(self) -> None:
        summary = build_dataset(
            output_root=self.output_root,
            assets_root=self.assets_root,
            dbs_per_template=1,
            records_per_db=2,
            validation_backend="stub",
        )
        self.assertGreaterEqual(summary["db_count"], 2)
        self.assertGreaterEqual(summary["record_count"], 4)

        train = json.loads((self.output_root / "train.json").read_text(encoding="utf-8"))
        test = json.loads((self.output_root / "test.json").read_text(encoding="utf-8"))
        tend = json.loads((self.output_root / "TEND.json").read_text(encoding="utf-8"))
        self.assertEqual(len(tend), len(train) + len(test))
        self.assertGreater(len(train), 0)
        self.assertGreater(len(test), 0)
        self.assertTrue((self.output_root / "persona_bank.json").exists())
        self.assertTrue((self.output_root / "intent_template_lattice.json").exists())
        self.assertTrue((self.output_root / "domain_catalog.json").exists())

        first_db_id = json.loads((self.output_root / "dataset_manifest.json").read_text(encoding="utf-8"))["dbs"][0]["db_id"]
        self.assertTrue((self.output_root / "mongodb_schema" / f"{first_db_id}.json").exists())
        self.assertTrue((self.output_root / "mongodb_data" / f"{first_db_id}.json").exists())
        self.assertTrue((self.output_root / "phenomena_registry" / f"{first_db_id}.json").exists())

        validation = validate_dataset(output_root=self.output_root, assets_root=self.assets_root)
        self.assertTrue(validation["ok"], validation["errors"])


if __name__ == "__main__":
    unittest.main()
