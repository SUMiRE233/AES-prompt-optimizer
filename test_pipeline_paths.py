"""运行路径解析与旧入口守卫的回归测试。

`RunPaths` 的 legacy 模式必须与改动前的模块级路径函数逐字一致——
这是「CV 改造被搁置但基础设施保留」的可验证前提。
"""

import contextlib
import unittest
from unittest import mock

import pipeline_entry
from pipeline_entry import (
    LEGACY_PATHS,
    RunPaths,
    badcase_path,
    prompt_meta_path,
    prompt_path,
    test_scoring_path,
    train_scoring_path,
)


class RunPathsRegressionTest(unittest.TestCase):
    def test_legacy_paths_match_module_level_helpers(self):
        for iteration in (0, 1, 4, 6):
            with self.subTest(iteration=iteration):
                self.assertEqual(
                    LEGACY_PATHS.prompt_meta(iteration), prompt_meta_path(iteration)
                )
                self.assertEqual(
                    LEGACY_PATHS.prompt(iteration), prompt_path(iteration)
                )
                self.assertEqual(
                    LEGACY_PATHS.train_scoring(iteration),
                    train_scoring_path(iteration),
                )
                self.assertEqual(
                    LEGACY_PATHS.eval_scoring(iteration),
                    test_scoring_path(iteration),
                )
                self.assertEqual(
                    LEGACY_PATHS.badcase(iteration), badcase_path(iteration)
                )

    def test_legacy_mode_keeps_artifacts_in_the_working_directory(self):
        self.assertEqual(LEGACY_PATHS.train_scoring(2), "train_scoring_results2.json")
        self.assertEqual(LEGACY_PATHS.eval_scoring(2), "test_scoring_results2.json")
        self.assertEqual(LEGACY_PATHS.badcase(2), "aes_badcases2.json")
        self.assertEqual(LEGACY_PATHS.prompt_meta(0), "origin_prompt_meta.md")

    def test_redirected_mode_isolates_artifacts(self):
        paths = RunPaths("somewhere/fold3")
        self.assertEqual(
            paths.train_scoring(2).replace("\\", "/"),
            "somewhere/fold3/train_scoring_results2.json",
        )
        self.assertEqual(
            paths.eval_scoring(2).replace("\\", "/"),
            "somewhere/fold3/eval_scoring_results2.json",
        )
        # V0 的 prompt 是共享的 origin，不随运行目录复制
        self.assertEqual(paths.prompt_meta(0), "origin_prompt_meta.md")
        self.assertEqual(paths.prompt(0), "origin_prompt.md")


class ProtocolMixGuardTest(unittest.TestCase):
    """CV 协议标记存在时，旧入口必须拒绝，否则会把留出集用作选版依据。"""

    def test_run_pipeline_refuses_when_a_cv_marker_is_present(self):
        with mock.patch(
            "pipeline_entry.cv_protocol_marker_present", return_value=True
        ):
            with self.assertRaisesRegex(SystemExit, "报告留出"):
                pipeline_entry.run_pipeline("origin_scoring_results.json")

    def test_guard_message_points_to_the_alternative_entry(self):
        with mock.patch(
            "pipeline_entry.cv_protocol_marker_present", return_value=True
        ):
            with self.assertRaises(SystemExit) as caught:
                pipeline_entry.run_pipeline("origin_scoring_results.json")
        self.assertIn("cv_runner.py", str(caught.exception))

    def test_marker_helper_reads_the_cv_manifest(self):
        with mock.patch("pipeline_entry.os.path.exists", return_value=True) as probe:
            self.assertTrue(pipeline_entry.cv_protocol_marker_present())
        probe.assert_called_once_with("cv_folds.json")

    def test_run_pipeline_proceeds_without_the_marker(self):
        seen = []

        patches = [
            mock.patch("pipeline_entry.cv_protocol_marker_present", return_value=False),
            mock.patch("pipeline_entry.assert_clean_working_directory"),
            mock.patch("pipeline_entry.extract_samples"),
            mock.patch("pipeline_entry.ensure_origin_prompt"),
            mock.patch(
                "pipeline_entry.write_run_manifest",
                return_value="run_manifest_test.json",
            ),
            mock.patch(
                "pipeline_entry.run_scoring",
                side_effect=lambda *a, **k: seen.append("scoring"),
            ),
            mock.patch("pipeline_entry.run_badcase_mining"),
            mock.patch("pipeline_entry.save_protocol_state"),
            mock.patch(
                "pipeline_entry.version_review",
                return_value={
                    "iteration": 0,
                    "contract_ok": True,
                    "contract_violations": [],
                },
            ),
            mock.patch("pipeline_entry.print_version_review"),
            mock.patch("pipeline_entry.append_version_review"),
            mock.patch("pipeline_entry.record_version_in_manifest"),
            mock.patch("pipeline_entry.optimize_or_stop"),
            mock.patch("pipeline_entry.run_train_iteration"),
            mock.patch(
                "pipeline_entry.version_mean_counts",
                return_value={"severe": 5.0, "soft": 5.0, "runs": 1, "q": 17.5},
            ),
            mock.patch(
                "pipeline_entry.decide_iteration",
                return_value=pipeline_entry.IterationDecision(
                    "stop", "cap_reached", (0, 1)
                ),
            ),
            mock.patch(
                "pipeline_entry.resolve_decision",
                side_effect=lambda decision, iteration, origin_file, state: (
                    decision,
                    state,
                    [],
                ),
            ),
            mock.patch("pipeline_entry.run_validation_and_select", return_value=0),
            mock.patch("pipeline_entry.save_final_prompt"),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            final_meta = pipeline_entry.run_pipeline("origin_scoring_results.json")

        self.assertTrue(seen)
        self.assertEqual(final_meta, pipeline_entry.prompt_meta_path(0))


if __name__ == "__main__":
    unittest.main()
