import unittest

from pipeline_entry import candidate_rank, comparison_iterations, decide_iteration


def counts(severe, soft):
    return {"severe": severe, "soft": soft, "total": severe + soft}


class IterationPolicyTests(unittest.TestCase):
    def test_severe_decrease_always_continues_before_cap(self):
        decision = decide_iteration([counts(18, 9), counts(13, 20)], 1)
        self.assertEqual(decision.action, "continue")
        self.assertEqual(decision.reason, "severe_decreased")

    def test_severe_rebound_gates_immediately(self):
        decision = decide_iteration([counts(13, 7), counts(14, 1)], 1)
        self.assertEqual(decision.action, "compare")
        self.assertEqual(decision.stable_iteration, 0)

    def test_one_soft_non_decrease_does_not_gate(self):
        history = [counts(18, 9), counts(13, 7), counts(13, 8)]
        self.assertEqual(decide_iteration(history, 2).action, "continue")

    def test_two_soft_non_decreases_gate_against_pre_stall_version(self):
        history = [counts(18, 9), counts(13, 7), counts(13, 8), counts(13, 8)]
        decision = decide_iteration(history, 3)
        self.assertEqual(decision.action, "compare")
        self.assertEqual(decision.reason, "soft_not_down_twice")
        self.assertEqual(decision.stable_iteration, 1)

    def test_soft_decrease_resets_patience(self):
        history = [counts(13, 7), counts(13, 8), counts(13, 6)]
        self.assertEqual(decide_iteration(history, 2).action, "continue")

    def test_v6_cap_compares_last_three_versions(self):
        history = [counts(18 - i, 9 - i) for i in range(7)]
        decision = decide_iteration(history, 6)
        self.assertEqual(decision.action, "compare")
        self.assertEqual(decision.reason, "safety_cap")
        self.assertEqual(comparison_iterations(decision, 6), (4, 5, 6))

    def test_non_cap_gate_compares_stable_and_last(self):
        history = [counts(13, 7), counts(14, 1)]
        decision = decide_iteration(history, 1)
        self.assertEqual(comparison_iterations(decision, 1), (0, 1))

    def test_candidate_rank_uses_weight_then_severe_then_earlier(self):
        self.assertLess(candidate_rank(2, counts(1, 5)), candidate_rank(1, counts(2, 0)))
        self.assertLess(candidate_rank(2, counts(1, 5)), candidate_rank(1, counts(1, 6)))
        self.assertLess(candidate_rank(1, counts(1, 5)), candidate_rank(2, counts(1, 5)))


if __name__ == "__main__":
    unittest.main()
