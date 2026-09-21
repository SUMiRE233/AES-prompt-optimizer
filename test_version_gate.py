"""逐版本迭代前后对比与人工确认闸门（大纲 §10 第 4 步）。"""

import contextlib
import io
import unittest
from unittest import mock

import pipeline_entry
from pipeline_entry import (
    confirm_next_iteration,
    hold_message,
    version_delta,
    version_review,
)


def review(iteration, **overrides):
    base = {
        "iteration": iteration,
        "chars": 504,
        "top_headings": 1,
        "bold_sections": 5,
        "severe": 18,
        "soft": 9,
        "q": 99,
        "mae": 1.708,
        "contract_ok": True,
    }
    base.update(overrides)
    return base


class VersionDeltaTest(unittest.TestCase):
    def test_first_version_has_no_before_values(self):
        delta = version_delta(None, review(0))
        self.assertIsNone(delta["from_iteration"])
        for row in delta["rows"]:
            self.assertIsNone(row["before"])
            self.assertIsNone(row["change"])
        self.assertEqual(delta["rows"][0]["after"], 504)

    def test_changes_are_computed_for_every_field(self):
        previous = review(0)
        current = review(
            1,
            chars=1180,
            top_headings=1,
            bold_sections=5,
            severe=13,
            soft=7,
            q=72,
            mae=1.394,
        )
        delta = version_delta(previous, current)
        changes = {row["field"]: row["change"] for row in delta["rows"]}
        self.assertEqual(changes["chars"], 676)
        self.assertEqual(changes["top_headings"], 0)
        self.assertEqual(changes["bold_sections"], 0)
        self.assertEqual(changes["severe"], -5)
        self.assertEqual(changes["soft"], -2)
        self.assertEqual(changes["q"], -27)
        self.assertAlmostEqual(changes["mae"], -0.314, places=4)

    def test_missing_metric_yields_no_change(self):
        delta = version_delta(review(0), {"iteration": 1, "severe": 5})
        changes = {row["field"]: row["change"] for row in delta["rows"]}
        self.assertIsNone(changes["soft"])
        self.assertIsNone(changes["mae"])

    def test_render_marks_improvement_and_regression(self):
        previous = review(0)
        current = review(1, severe=13, soft=12, q=77, mae=2.0)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            pipeline_entry.print_version_delta(version_delta(previous, current))
        output = captured.getvalue()
        self.assertIn("V0 -> V1", output)
        self.assertIn("改进", output, "severe 下降应标为改进")
        self.assertIn("恶化", output, "soft 上升应标为恶化")
        self.assertIn("结构契约", output)

    def test_render_handles_origin_row(self):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            pipeline_entry.print_version_delta(version_delta(None, review(0)))
        self.assertIn("origin -> V0", captured.getvalue())


class ConfirmationGateTest(unittest.TestCase):
    def test_yes_continues(self):
        for answer in ("y", "Y", "yes", "是", "继续"):
            with self.subTest(answer=answer):
                with mock.patch("builtins.input", return_value=answer):
                    self.assertTrue(confirm_next_iteration(1))

    def test_empty_or_anything_else_stops(self):
        for answer in ("", "n", "no", "随便"):
            with self.subTest(answer=answer):
                with mock.patch("builtins.input", return_value=answer):
                    self.assertFalse(confirm_next_iteration(1))

    def test_eof_stops_instead_of_hanging(self):
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertFalse(confirm_next_iteration(3))

    def test_auto_continue_bypasses_the_gate(self):
        with mock.patch("builtins.input", side_effect=AssertionError("must not prompt")):
            self.assertTrue(confirm_next_iteration(2, auto_continue=True))

    def test_hold_message_points_at_the_resume_command(self):
        message = hold_message(4)
        self.assertIn("--resume-from 4", message)
        self.assertIn("b_version_review.json", message)


class VersionReviewTest(unittest.TestCase):
    def test_review_of_missing_artifacts_only_reports_the_prompt(self):
        result = version_review(99)
        self.assertEqual(result["iteration"], 99)
        self.assertNotIn("severe", result)

    def test_review_of_origin_reports_structure_and_contract(self):
        result = version_review(0)
        self.assertTrue(result["contract_ok"])
        self.assertEqual(result["bold_sections"], 5)
        self.assertEqual(result["top_headings"], 1)


if __name__ == "__main__":
    unittest.main()
