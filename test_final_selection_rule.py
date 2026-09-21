"""final 选择排序键与 MAE 政策（统一 Q 协议）。"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline_entry
from pipeline_entry import candidate_rank


class FinalRankingTest(unittest.TestCase):
    def test_lower_q_wins(self):
        self.assertLess(
            candidate_rank(5, {"severe": 0.0, "soft": 3.0}),
            candidate_rank(4, {"severe": 2.0, "soft": 1.0}),
        )

    def test_ties_on_q_are_broken_by_mean_severe(self):
        # 两个候选 Q 相同（5.0）：V4 severe=2，V6 severe=0
        self.assertEqual(2.5 * 2 + 0, 2.5 * 0 + 5)
        self.assertLess(
            candidate_rank(6, {"severe": 0.0, "soft": 5.0}),
            candidate_rank(4, {"severe": 2.0, "soft": 0.0}),
        )

    def test_full_tie_prefers_the_earlier_version(self):
        counts = {"severe": 1.0, "soft": 1.0}
        self.assertLess(candidate_rank(3, counts), candidate_rank(6, dict(counts)))


class MaePolicyTest(unittest.TestCase):
    """MAE 只记录并告警，不得改变 final 选择。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original = os.getcwd()
        self.addCleanup(os.chdir, self.original)
        os.chdir(self.temp.name)

        for name in (
            "test_scoring_results4.json",
            "test_scoring_results4_rerun.json",
            "test_scoring_results6.json",
            "test_scoring_results6_rerun.json",
        ):
            Path(name).write_text("[]", encoding="utf-8")

        self.counts = {
            "test_scoring_results4.json": {"severe": 0, "soft": 3, "total": 3},
            "test_scoring_results4_rerun.json": {"severe": 0, "soft": 3, "total": 3},
            "test_scoring_results6.json": {"severe": 2, "soft": 1, "total": 3},
            "test_scoring_results6_rerun.json": {"severe": 2, "soft": 1, "total": 3},
        }

    def test_selection_ignores_mae_and_only_warns(self):
        def fake_counts(path):
            return self.counts[path]

        def fake_mae(path):
            # V6 的 MAE 明显更差（3.0 vs 0.0）
            return 0.0 if "results4" in path else 3.0

        captured = io.StringIO()
        with mock.patch(
            "pipeline_entry.b_bias_counts_from_scoring_results",
            side_effect=fake_counts,
        ), mock.patch("pipeline_entry.scoring_mae", side_effect=fake_mae):
            with contextlib.redirect_stdout(captured):
                selected = pipeline_entry.run_validation_and_select(
                    (4, 6), "origin_scoring_results.json", "cap_reached"
                )

        output = captured.getvalue()
        self.assertIn("MAE rebounded", output)
        self.assertIn("did not affect final selection", output.replace("\n", " "))
        self.assertEqual(selected, 4, "Q 更小者胜出；MAE 更差也不改变选择")

        report = json.loads(
            Path(pipeline_entry.VALIDATION_REPORT).read_text(encoding="utf-8")
        )
        self.assertEqual(report["selected_iteration"], 4)
        self.assertEqual(report["mae_policy"], "record_only; warn_on_rebound")
        self.assertIn("2.5", report["ranking"])
        self.assertEqual([item["iteration"] for item in report["candidates"]], [4, 6])
        self.assertEqual(
            [item["mae_mean"] for item in report["candidates"]],
            [0.0, 3.0],
            "MAE 必须逐候选记录，即使更差",
        )
        self.assertEqual(
            [len(item["runs"]) for item in report["candidates"]],
            [2, 2],
            "每候选固定两次独立评测",
        )


class SecondEvalAlwaysRunsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original = os.getcwd()
        self.addCleanup(os.chdir, self.original)
        os.chdir(self.temp.name)

    def test_second_eval_runs_even_when_the_first_looks_terrible(self):
        Path("test_scoring_results5.json").write_text("[]", encoding="utf-8")
        calls = []

        def fake_run(iteration, origin_file, paths=pipeline_entry.LEGACY_PATHS,
                     eval_essays="test_essays.json", rerun=False):
            calls.append((iteration, rerun))
            Path("test_scoring_results5_rerun.json").write_text("[]", encoding="utf-8")
            return {"severe": 9, "soft": 9, "total": 18}

        with mock.patch("pipeline_entry.run_test_iteration", side_effect=fake_run):
            performed = pipeline_entry.ensure_eval_runs(5, "origin.json")

        self.assertEqual(performed, 1)
        self.assertEqual(calls, [(5, True)])


if __name__ == "__main__":
    unittest.main()
