import tempfile
import unittest
from pathlib import Path

from run_manifest import build_manifest, key_value


class RunManifestTest(unittest.TestCase):
    def test_manifest_records_stable_hash_model_threshold_and_repeat(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sample = Path(temp_dir) / "sample.json"
            sample.write_text('{"synthetic": true}', encoding="utf-8")

            first = build_manifest(
                [sample],
                models={"scoring": "offline-model-id"},
                thresholds={"b_score": 3},
                repeat=2,
            )
            second = build_manifest(
                [sample],
                models={"scoring": "offline-model-id"},
                thresholds={"b_score": 3},
                repeat=2,
            )

        self.assertEqual(first["files"][0]["sha256"], second["files"][0]["sha256"])
        self.assertEqual(first["repeat"], 2)
        self.assertEqual(first["models"]["scoring"], "offline-model-id")
        self.assertEqual(first["thresholds"]["b_score"], 3)

    def test_secret_like_manifest_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            key_value(["AES_API_KEY=do-not-record"])


if __name__ == "__main__":
    unittest.main()
