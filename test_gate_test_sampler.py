import json
import tempfile
import unittest
from pathlib import Path

from gate_test_sampler import GateTestSampler


def score_row(index, content_ai):
    return {
        "index": index,
        "essay": f"synthetic-{index}",
        "teacher": {"content": 5, "expression": 5, "structure": 5},
        "AI": {"content": content_ai, "expression": 5, "structure": 5},
    }


class GateTestSamplerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.train = root / "train.json"
        self.badcases = root / "badcases.json"
        self.injected = root / "injected.json"
        self.essays = root / "essays.json"
        self.manifest = root / "manifest.json"
        # Deliberately shuffled: position must never be treated as identity.
        self.rows = [
            score_row(300, 5),
            score_row(100, 3),
            score_row(400, 5),
            score_row(200, 7),
        ]
        self.train.write_text(json.dumps(self.rows), encoding="utf-8")
        self.injected.write_text(
            json.dumps({"injected_rules": [{"dimension": "content"}]}),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def sampler(self, badcases, count=2):
        self.badcases.write_text(json.dumps(badcases), encoding="utf-8")
        return GateTestSampler(
            train_path=str(self.train),
            badcase_path=str(self.badcases),
            injected_rule_path=str(self.injected),
            output_essays_path=str(self.essays),
            output_manifest_path=str(self.manifest),
            improved_dimension="content",
            badcase_count=count,
            normal_count=1,
        )

    def test_global_index_and_both_directions_are_selected(self):
        sampler = self.sampler(
            {
                "E_residual": {
                    "content": {
                        "severe": [],
                        "soft": [
                            {"index": 100, "direction": "strict", "z_score": -2},
                            {"index": 200, "direction": "lenient", "z_score": 2},
                            {"index": 300, "direction": "lenient", "z_score": 1.8},
                        ],
                    }
                }
            }
        )

        manifest = sampler.run()

        badcases = [s for s in manifest["samples"] if s["sample_type"] == "badcase"]
        self.assertEqual({s["direction"] for s in badcases}, {"strict", "lenient"})
        self.assertEqual({s["index"] for s in badcases}, {100, 300})
        self.assertEqual(manifest["identity"], "global_index")
        self.assertNotIn("source_data_index", manifest["samples"][0])

    def test_legacy_position_is_converted_to_global_index(self):
        sampler = self.sampler(
            {
                "E_residual": {
                    "content": {
                        "severe": [],
                        "soft": [{"data_index": 1, "direction": "strict", "z_score": -2}],
                    }
                }
            },
            count=1,
        )

        manifest = sampler.run()

        badcase = next(s for s in manifest["samples"] if s["sample_type"] == "badcase")
        self.assertEqual(badcase["index"], 100)


if __name__ == "__main__":
    unittest.main()
