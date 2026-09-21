import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from etype_iteration_runner import ETypeIterationRunner


def scoring_row(index, teacher=(5, 5, 5), ai=(5, 5, 5)):
    return {
        "index": index,
        "essay": f"synthetic-{index}",
        "teacher": dict(zip(("content", "expression", "structure"), teacher)),
        "AI": dict(zip(("content", "expression", "structure"), ai)),
    }


def badcase_summary(content=(0, 0), expression=(0, 0), structure=(0, 0), b=(0, 0)):
    return {
        "statistics": {
            "B_bias": {"severe_count": b[0], "soft_count": b[1]},
            "E_residual": {
                "content": {"severe_count": content[0], "soft_count": content[1]},
                "expression": {"severe_count": expression[0], "soft_count": expression[1]},
                "structure": {"severe_count": structure[0], "soft_count": structure[1]},
            },
        }
    }


class IterationAggregateGateTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.paths = {name: root / name for name in (
            "baseline_scores.json",
            "candidate_train.json",
            "baseline_badcases.json",
            "candidate_badcases.json",
            "train_eval.json",
            "baseline_validation.json",
            "candidate_validation.json",
            "validation_eval.json",
        )}
        args = argparse.Namespace(
            analysis_dir=str(root / "analysis"),
            execute=True,
            train_output=str(self.paths["candidate_train.json"]),
            badcase_file=str(self.paths["baseline_badcases.json"]),
            train_badcase=str(self.paths["candidate_badcases.json"]),
            dimension="content",
            scoring_file=str(self.paths["baseline_scores.json"]),
            train_eval=str(self.paths["train_eval.json"]),
            validation_baseline=str(self.paths["baseline_validation.json"]),
            test_output=str(self.paths["candidate_validation.json"]),
            validation_eval=str(self.paths["validation_eval.json"]),
        )
        self.runner = ETypeIterationRunner(args)
        self.runner.rule_report = {"dimension": "content"}
        self.runner.discard_rejected_candidate = Mock(return_value=[])

    def tearDown(self):
        self.temp_dir.cleanup()

    def write(self, name, payload):
        self.paths[name].write_text(json.dumps(payload), encoding="utf-8")

    def test_full_train_gate_passes_only_with_target_reduction_and_stable_b(self):
        baseline = badcase_summary(content=(0, 3), b=(1, 2))
        candidate = badcase_summary(content=(0, 2), b=(1, 2))
        self.write("baseline_badcases.json", baseline)
        self.write("baseline_scores.json", [scoring_row(1, ai=(4, 5, 5))])
        self.write("candidate_train.json", [scoring_row(1)])

        with patch("etype_iteration_runner.AESBadcaseMiner") as miner:
            miner.return_value.run.return_value = candidate
            passed = self.runner.evaluate_full_train()

        self.assertTrue(passed)
        report = json.loads(self.paths["train_eval.json"].read_text(encoding="utf-8"))
        self.assertEqual(report["target_baseline"]["score"], 3)
        self.assertEqual(report["target_candidate"]["score"], 2)

    def test_full_train_gate_rejects_b_score_rebound(self):
        baseline = badcase_summary(content=(0, 3), b=(0, 2))
        candidate = badcase_summary(content=(0, 1), b=(1, 0))
        self.write("baseline_badcases.json", baseline)
        self.write("baseline_scores.json", [scoring_row(1, ai=(4, 5, 5))])
        self.write("candidate_train.json", [scoring_row(1)])

        with patch("etype_iteration_runner.AESBadcaseMiner") as miner:
            miner.return_value.run.return_value = candidate
            passed = self.runner.evaluate_full_train()

        self.assertFalse(passed)
        self.runner.discard_rejected_candidate.assert_called_once()

    def test_validation_gate_passes_when_b_and_target_guards_hold(self):
        self.write("baseline_validation.json", [scoring_row(1, ai=(4, 5, 5))])
        self.write("candidate_validation.json", [scoring_row(1)])

        passed = self.runner.evaluate_validation()

        self.assertTrue(passed)
        report = json.loads(self.paths["validation_eval.json"].read_text(encoding="utf-8"))
        self.assertLess(report["target_mae_candidate"], report["target_mae_baseline"])

    def test_validation_gate_rejects_return_of_severe_b_bias(self):
        self.write("baseline_validation.json", [scoring_row(1, ai=(4, 5, 5))])
        self.write("candidate_validation.json", [scoring_row(1, ai=(3, 3, 3))])

        passed = self.runner.evaluate_validation()

        self.assertFalse(passed)
        self.runner.discard_rejected_candidate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
