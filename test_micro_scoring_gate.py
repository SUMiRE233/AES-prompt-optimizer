import json
import tempfile
import unittest
from pathlib import Path

from micro_scoring_gate import MicroScoringGate


def scoring_row(index, teacher, ai):
    return {
        "index": index,
        "name": f"essay-{index}",
        "page": f"page-{index}",
        "essay": f"text-{index}",
        "teacher": {
            "content": teacher[0],
            "expression": teacher[1],
            "structure": teacher[2],
            "technique": 0,
            "length": 0,
        },
        "AI": {
            "content": ai[0],
            "expression": ai[1],
            "structure": ai[2],
            "technique": 0,
            "length": 0,
        },
    }


class MicroScoringGateTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.paths = {
            name: root / name
            for name in (
                "baseline.json",
                "badcases.json",
                "injected.json",
                "essays.json",
                "manifest.json",
                "candidate.json",
                "eval.json",
            )
        }
        self.baseline = [
            scoring_row(10, (7, 7, 7), (5, 7, 7)),
            scoring_row(11, (6, 6, 6), (6, 6, 6)),
            scoring_row(12, (7, 7, 7), (5, 5, 5)),
        ]
        self.write("baseline.json", self.baseline)
        self.write(
            "badcases.json",
            {
                "B_bias": {
                    "severe": [{"data_index": 2, "severity": "severe"}],
                    "soft": [],
                }
            },
        )
        self.write(
            "injected.json",
            {
                "injected_rules": [
                    {
                        "major_iteration": 1,
                        "sub_iteration": 1,
                        "source_rule_index": 0,
                        "dimension": "content",
                        "evidence": {
                            "outlier_indices": [0, 2],
                            "normal_indices": [1],
                        },
                    }
                ]
            },
        )
        self.gate = MicroScoringGate(
            baseline_path=str(self.paths["baseline.json"]),
            badcase_path=str(self.paths["badcases.json"]),
            injected_rule_path=str(self.paths["injected.json"]),
            essays_output_path=str(self.paths["essays.json"]),
            manifest_output_path=str(self.paths["manifest.json"]),
            candidate_path=str(self.paths["candidate.json"]),
            eval_output_path=str(self.paths["eval.json"]),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def write(self, name, payload):
        with open(self.paths[name], "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def test_manifest_deduplicates_evidence_and_b_bias_samples(self):
        manifest = self.gate.build_manifest()

        self.assertEqual(manifest["counts"]["unique_samples"], 3)
        sample = next(item for item in manifest["samples"] if item["index"] == 12)
        self.assertEqual(sample["evidence_roles"], ["outlier"])
        self.assertEqual(sample["b_bias_severities"], ["severe"])

    def test_weighted_improvement_passes(self):
        self.gate.build_manifest()
        candidate = [
            scoring_row(10, (7, 7, 7), (7, 7, 7)),
            scoring_row(11, (6, 6, 6), (6, 6, 6)),
            scoring_row(12, (7, 7, 7), (6, 6, 6)),
        ]
        self.write("candidate.json", candidate)

        result = self.gate.evaluate()

        self.assertTrue(result["gate_passed"])
        self.assertGreater(result["score"]["weighted_average"], 0)
        self.assertEqual(result["violations"], [])

    def test_target_regression_rejects_candidate(self):
        self.gate.build_manifest()
        candidate = [
            scoring_row(10, (7, 7, 7), (4, 7, 7)),
            scoring_row(11, (6, 6, 6), (6, 6, 6)),
            scoring_row(12, (7, 7, 7), (7, 7, 7)),
        ]
        self.write("candidate.json", candidate)

        result = self.gate.evaluate()

        self.assertFalse(result["gate_passed"])
        self.assertTrue(
            any(item["type"] == "outlier_target_regression" for item in result["violations"])
        )

    def test_multiple_rules_merge_all_evidence(self):
        rules = [
            {
                "dimension": "content",
                "source_rule_index": 1,
                "evidence": {"outlier_indices": [0], "normal_indices": [1]},
            },
            {
                "dimension": "content",
                "source_rule_index": 2,
                "evidence": {"outlier_indices": [2], "normal_indices": [1]},
            },
        ]
        gate = MicroScoringGate(
            baseline_path=str(self.paths["baseline.json"]),
            badcase_path=str(self.paths["badcases.json"]),
            injected_rule_path=str(self.paths["injected.json"]),
            essays_output_path=str(self.paths["essays.json"]),
            manifest_output_path=str(self.paths["manifest.json"]),
            candidate_path=str(self.paths["candidate.json"]),
            eval_output_path=str(self.paths["eval.json"]),
            injected_rules=rules,
        )

        manifest = gate.build_manifest()

        self.assertEqual(manifest["counts"]["outliers"], 2)
        self.assertEqual(manifest["counts"]["normals"], 1)
        self.assertEqual(len(manifest["rules"]), 2)


if __name__ == "__main__":
    unittest.main()
