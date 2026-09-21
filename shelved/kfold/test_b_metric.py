import unittest

from b_metric import (
    BiasMetrics,
    compare,
    dominates,
    label_floor,
    legacy_rank,
    measure,
    pareto_frontier,
    select_sequential,
)


DIMS = ("content", "expression", "structure")


def row(index, ai, teacher):
    return {
        "index": index,
        "AI": dict(zip(DIMS, ai)),
        "teacher": dict(zip(DIMS, teacher)),
    }


def metrics(severe, soft, aligned_count, score_count, mae_above_floor):
    return BiasMetrics(
        sample_count=score_count // 3,
        severe=severe,
        soft=soft,
        bias_score=5 * severe + soft,
        mean_abs_cross_dim_bias=0.0,
        aligned_count=aligned_count,
        exact_count=0,
        score_count=score_count,
        aligned_rate=aligned_count / score_count,
        exact_hit_rate=0.0,
        mae=mae_above_floor,
        mae_above_floor=mae_above_floor,
    )


class LabelFloorTest(unittest.TestCase):
    def test_integer_label_has_no_floor(self):
        self.assertEqual(label_floor(7.0), 0.0)

    def test_half_label_has_a_five_tenths_floor(self):
        self.assertEqual(label_floor(7.5), 0.5)


class MeasureTest(unittest.TestCase):
    def test_perfect_alignment_is_fully_aligned_and_exact(self):
        rows = [row(1, (6, 7, 5), (6, 7, 5))]
        result = measure(rows)
        self.assertEqual(result.aligned_count, 3)
        self.assertEqual(result.exact_count, 3)
        self.assertEqual(result.severe, 0)
        self.assertEqual(result.soft, 0)
        self.assertEqual(result.mae_above_floor, 0.0)

    def test_half_label_sample_at_its_floor_counts_as_aligned(self):
        rows = [row(1, (7, 7, 7), (7.5, 7.5, 7.5))]
        result = measure(rows)
        self.assertEqual(result.aligned_count, 3)
        self.assertEqual(result.exact_count, 0)
        self.assertEqual(result.mae, 0.5)
        self.assertEqual(result.mae_above_floor, 0.0)

    def test_consistent_cross_dimension_drift_is_severe(self):
        rows = [row(1, (4, 4, 4), (7, 7, 7))]
        result = measure(rows)
        self.assertEqual(result.severe, 1)
        self.assertEqual(result.bias_score, 5)
        # mean_abs_cross_dim_bias 是逐篇 |跨维均值| 的平均，恒为非负。
        self.assertAlmostEqual(result.mean_abs_cross_dim_bias, 3.0)

    def test_inconsistent_drift_is_not_counted_as_bias(self):
        rows = [row(1, (4, 8, 7), (7, 7, 7))]
        result = measure(rows)
        self.assertEqual(result.severe, 0)
        self.assertEqual(result.soft, 0)

    def test_empty_rows_are_rejected(self):
        with self.assertRaises(ValueError):
            measure([])


class CompareTest(unittest.TestCase):
    def test_strictly_better_candidate_is_accepted(self):
        incumbent = metrics(5, 7, 40, 108, 0.90)
        candidate = metrics(3, 6, 50, 108, 0.75)
        self.assertTrue(compare(candidate, incumbent).accepted)

    def test_worse_bias_score_is_rejected(self):
        incumbent = metrics(0, 3, 48, 36, 0.694)
        candidate = metrics(2, 1, 63, 36, 0.639)
        decision = compare(candidate, incumbent)
        self.assertFalse(decision.accepted)
        self.assertIn("bias_score 劣化", decision.reasons[0])

    def test_aligned_degradation_beyond_tolerance_is_rejected(self):
        incumbent = metrics(5, 7, 42, 108, 0.944)
        candidate = metrics(5, 7, 30, 108, 0.900)
        self.assertFalse(compare(candidate, incumbent).accepted)

    def test_single_cell_aligned_degradation_is_tolerated(self):
        # n=108 时 1 格 = 0.93%；0 容忍会把单格抖动判成退化。
        incumbent = metrics(6, 8, 42, 108, 0.944)
        candidate = metrics(5, 7, 41, 108, 0.898)
        decision = compare(candidate, incumbent)
        self.assertTrue(decision.accepted, decision.reasons)

    def test_no_strict_improvement_is_rejected(self):
        incumbent = metrics(5, 7, 40, 108, 0.90)
        candidate = metrics(5, 7, 40, 108, 0.90)
        decision = compare(candidate, incumbent)
        self.assertFalse(decision.accepted)
        self.assertIn("无任何分量严格改善", decision.reasons)

    def test_bias_score_is_a_composite_not_the_severe_count(self):
        # severe 上升但 soft 大幅下降时，bias_score 仍可改善。
        incumbent = metrics(3, 6, 40, 108, 0.90)   # Score 21
        candidate = metrics(4, 5, 41, 108, 0.85)   # Score 25 -> 劣化
        self.assertFalse(compare(candidate, incumbent).accepted)


class SelectionTest(unittest.TestCase):
    def test_legacy_rank_prefers_the_lower_severe_count(self):
        left = metrics(0, 3, 48, 36, 0.694)
        right = metrics(2, 1, 63, 36, 0.639)
        # 现规则把 V4 排在 V5 之前，尽管 V5 的 aligned 更高、MAE 更低。
        self.assertLess(legacy_rank(left, 4), legacy_rank(right, 5))

    def test_dominates_requires_not_worse_on_every_component(self):
        better = metrics(3, 6, 50, 108, 0.75)
        worse = metrics(5, 7, 40, 108, 0.90)
        self.assertTrue(dominates(better, worse))
        self.assertFalse(dominates(worse, better))

    def test_trade_off_pair_is_not_dominated(self):
        bias_best = metrics(0, 3, 48, 36, 0.694)
        resolution_best = metrics(1, 2, 63, 36, 0.611)
        self.assertFalse(dominates(bias_best, resolution_best))
        self.assertFalse(dominates(resolution_best, bias_best))

    def test_pareto_frontier_keeps_the_trade_off_set(self):
        table = {
            "V1": metrics(5, 3, 18, 36, 1.361),
            "V4": metrics(0, 3, 48, 36, 0.694),
            "V5": metrics(2, 1, 63, 36, 0.639),
            "V6": metrics(1, 2, 63, 36, 0.611),
        }
        self.assertEqual(pareto_frontier(table), ["V4", "V6"])

    def test_sequential_selection_keeps_a_monotone_chain(self):
        table = {
            "V1": metrics(5, 3, 18, 108, 1.361),
            "V2": metrics(3, 4, 30, 108, 1.083),
            "V3": metrics(3, 1, 40, 108, 0.972),
        }
        result = select_sequential(["V1", "V2", "V3"], table)
        self.assertEqual(result.selected, "V3")
        self.assertEqual(result.accepted_versions, ["V1", "V2", "V3"])


if __name__ == "__main__":
    unittest.main()
