import argparse
import unittest
from unittest.mock import Mock

from etype_iteration_runner import ETypeIterationRunner, required_improvement_count


def runner_args():
    return argparse.Namespace(analysis_dir="etype_analysis", execute=True)


class IterationRunnerFlowTest(unittest.TestCase):
    def test_half_of_five_requires_three_improvements(self):
        self.assertEqual(required_improvement_count(5, 0.5), 3)

    def build_runner(self, micro_passed=True, regular_passed=True):
        runner = ETypeIterationRunner(runner_args())
        runner.run_badcase_mining = Mock()
        runner.run_contrastive_analysis = Mock(return_value="consolidated.json")
        runner.run_rule_injection = Mock()
        runner.preprocess_output_prompt = Mock()
        runner.run_micro_scoring = Mock(return_value=micro_passed)
        runner.run_scoring = Mock(return_value=regular_passed)
        runner.commit_rule_injection = Mock()
        runner.rollback_rule_injection = Mock()
        runner.promote_to_final = Mock()
        runner.save_run_report = Mock()
        return runner

    def test_micro_rejection_rolls_back_and_stops(self):
        runner = self.build_runner(micro_passed=False)

        runner.run()

        runner.rollback_rule_injection.assert_called_once()
        runner.run_scoring.assert_not_called()
        runner.commit_rule_injection.assert_not_called()
        runner.promote_to_final.assert_not_called()

    def test_regular_gate_rejection_rolls_back_and_stops(self):
        runner = self.build_runner(regular_passed=False)

        runner.run()

        runner.rollback_rule_injection.assert_called_once()
        runner.commit_rule_injection.assert_not_called()
        runner.promote_to_final.assert_not_called()

    def test_all_required_gates_pass_before_commit(self):
        runner = self.build_runner()

        runner.run()

        runner.rollback_rule_injection.assert_not_called()
        runner.commit_rule_injection.assert_called_once()
        runner.promote_to_final.assert_called_once()


if __name__ == "__main__":
    unittest.main()
