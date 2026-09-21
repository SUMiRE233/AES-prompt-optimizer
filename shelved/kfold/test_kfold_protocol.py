import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv_runner
import pipeline_entry
from b_cv_select import (
    DepthPoint,
    eval_scoring_path,
    find_depth_gaps,
    load_curve,
    load_folds,
    select_depth,
    validate_depth_set,
)
from b_metric import measure
from pipeline_entry import (
    LEGACY_PATHS,
    IterationDecision,
    RunPaths,
    badcase_path,
    prompt_meta_path,
    prompt_path,
    test_scoring_path,
    train_scoring_path,
)
from project_checks import CheckFailure, check_cv_protocol
from sample_extractor import EssayExtractor


DIMS = ("content", "expression", "structure")


def entry(index, teacher_total):
    per_dim = teacher_total / 3
    return {
        "index": index,
        "essay": f"essay-{index}",
        "teacher": {dim: per_dim for dim in DIMS},
    }


def scoring_row(index, ai, teacher):
    return {
        "index": index,
        "essay": f"essay-{index}",
        "teacher": dict(zip(DIMS, teacher)),
        "AI": dict(zip(DIMS, ai)),
    }


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class RunPathsTest(unittest.TestCase):
    def test_legacy_paths_match_existing_helpers(self):
        for iteration in (0, 1, 4):
            self.assertEqual(LEGACY_PATHS.prompt_meta(iteration), prompt_meta_path(iteration))
            self.assertEqual(LEGACY_PATHS.prompt(iteration), prompt_path(iteration))
            self.assertEqual(
                LEGACY_PATHS.train_scoring(iteration), train_scoring_path(iteration)
            )
            self.assertEqual(
                LEGACY_PATHS.eval_scoring(iteration), test_scoring_path(iteration)
            )
            self.assertEqual(LEGACY_PATHS.badcase(iteration), badcase_path(iteration))

    def test_legacy_run_dir_keeps_artifacts_in_place(self):
        self.assertEqual(LEGACY_PATHS.train_scoring(2), "train_scoring_results2.json")
        self.assertEqual(LEGACY_PATHS.eval_scoring(2), "test_scoring_results2.json")

    def test_fold_run_dir_isolates_artifacts(self):
        paths = RunPaths("cv_runs/fold3")
        self.assertEqual(
            Path(paths.train_scoring(2)),
            Path("cv_runs/fold3/train_scoring_results2.json"),
        )
        self.assertEqual(
            Path(paths.eval_scoring(2)),
            Path("cv_runs/fold3/eval_scoring_results2.json"),
        )
        # V0 的 prompt 是共享的 origin，不随折复制
        self.assertEqual(paths.prompt_meta(0), "origin_prompt_meta.md")
        self.assertEqual(paths.prompt(0), "origin_prompt.md")


class AssignmentTest(unittest.TestCase):
    def setUp(self):
        self.extractor = EssayExtractor()
        self.entries = [entry(index, 9 + index * 0.1) for index in range(48)]

    def test_assignment_sizes(self):
        result = self.extractor.assign(self.entries)
        self.assertEqual(len(result["report"]), 12)
        self.assertEqual(len(result["work"]), 36)
        self.assertEqual(len(result["folds"]), 4)
        for fold in result["folds"]:
            self.assertEqual(fold["eval_size"], 9)
            self.assertEqual(fold["train_size"], 27)

    def test_assignment_is_disjoint_and_complete(self):
        result = self.extractor.assign(self.entries)
        report = {int(item["index"]) for item in result["report"]}
        work = {int(item["index"]) for item in result["work"]}
        self.assertEqual(report & work, set())
        self.assertEqual(report | work, {int(item["index"]) for item in self.entries})

    def test_folds_partition_the_work_pool(self):
        result = self.extractor.assign(self.entries)
        work = {int(item["index"]) for item in result["work"]}
        covered = set()
        for fold in result["folds"]:
            train = set(fold["train_indices"])
            evaluation = set(fold["eval_indices"])
            self.assertEqual(train & evaluation, set())
            self.assertEqual(train | evaluation, work)
            self.assertEqual(evaluation & covered, set())
            covered |= evaluation
        self.assertEqual(covered, work)

    def test_report_holdout_spans_the_difficulty_range(self):
        result = self.extractor.assign(self.entries)
        ordered = sorted(self.entries, key=lambda item: item["index"])
        report = {int(item["index"]) for item in result["report"]}
        # 难度序上每第 4 篇被抽走，因此低难与高难两端都应出现。
        self.assertIn(0, report)
        self.assertIn(44, report)

    def test_assignment_is_deterministic(self):
        first = self.extractor.assign(self.entries)
        second = self.extractor.assign(list(reversed(self.entries)))
        self.assertEqual(
            [int(item["index"]) for item in first["report"]],
            [int(item["index"]) for item in second["report"]],
        )


class CurveTest(unittest.TestCase):
    """b_cv_select 的 `root` 参数语义 = cv_runs 目录本身。"""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "cv_runs"
        self.folds = [1, 2]

    def tearDown(self):
        self.temp.cleanup()

    def write_fold(self, fold, depth, ai_offset):
        rows = [
            scoring_row(index, [ai_offset] * 3, [6, 6, 6]) for index in range(9)
        ]
        write_json(
            self.root / f"fold{fold}" / f"eval_scoring_results{depth}.json", rows
        )

    def test_eval_scoring_path_points_into_fold_dir(self):
        path = Path(eval_scoring_path(2, 4, str(self.root)))
        self.assertEqual(path.relative_to(self.root), Path("fold2/eval_scoring_results4.json"))

    def test_curve_pools_folds_per_depth(self):
        for fold in self.folds:
            self.write_fold(fold, 0, 3)
            self.write_fold(fold, 1, 6)
        curve = load_curve([0, 1], self.folds, root=str(self.root))
        self.assertEqual(sorted(curve), [0, 1])
        # 每深度拼接 2 折 x 9 篇 = 18 篇
        self.assertEqual(curve[0].metrics.score_count, 18 * 3)
        self.assertEqual(curve[0].fold_sizes, [9, 9])
        # 深度 1 完全命中，深度 0 全体偏严 3 分
        self.assertGreater(curve[1].metrics.aligned_rate, curve[0].metrics.aligned_rate)

    def test_curve_skips_depths_missing_a_fold(self):
        self.write_fold(1, 0, 3)
        self.write_fold(1, 1, 3)
        self.write_fold(2, 0, 3)
        curve = load_curve([0, 1], self.folds, root=str(self.root))
        self.assertEqual(sorted(curve), [0])

    def test_depth_selection_uses_multi_component_rule(self):
        for fold in self.folds:
            self.write_fold(fold, 0, 3)
            self.write_fold(fold, 1, 6)
        curve = load_curve([0, 1], self.folds, root=str(self.root))
        depth, accepted, rejected = select_depth(curve)
        self.assertEqual(depth, 1)
        self.assertEqual(accepted, ["V0", "V1"])
        self.assertEqual(rejected, [])

    def test_empty_curve_is_rejected(self):
        with self.assertRaises(ValueError):
            select_depth({})

    def test_load_folds_reads_manifest(self):
        write_json(
            self.root / "cv_folds.json",
            {"folds": [{"fold": 1}, {"fold": 2}, {"fold": 3}]},
        )
        self.assertEqual(load_folds(str(self.root / "cv_folds.json")), [1, 2, 3])


class CvProtocolCheckTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.origin_rows = [
            {
                "index": index,
                "essay": f"essay-{index}",
                "teacher": {dim: 6 for dim in DIMS},
            }
            for index in range(6)
        ]
        self.origin = {row["index"]: row for row in self.origin_rows}

    def tearDown(self):
        self.temp.cleanup()

    def build(self, report=(0, 1), folds=((2, 3), (4, 5)), work=(2, 3, 4, 5)):
        write_json(
            self.root / "report_holdout_essays.json",
            [{"index": i, "essay": f"essay-{i}"} for i in report],
        )
        write_json(
            self.root / "work_pool_essays.json",
            [{"index": i, "essay": f"essay-{i}"} for i in work],
        )
        manifest = {
            "report_holdout_indices": list(report),
            "work_pool_indices": list(work),
            "folds": [],
        }
        for number, evaluation in enumerate(folds, start=1):
            train = [i for i in work if i not in evaluation]
            manifest["folds"].append(
                {"fold": number, "eval_indices": list(evaluation), "train_indices": train}
            )
            write_json(
                self.root / "fold_essays" / f"fold{number}_eval_essays.json",
                [{"index": i, "essay": f"essay-{i}"} for i in evaluation],
            )
            write_json(
                self.root / "fold_essays" / f"fold{number}_train_essays.json",
                [{"index": i, "essay": f"essay-{i}"} for i in train],
            )
        write_json(self.root / "cv_folds.json", manifest)

    def test_valid_protocol_passes(self):
        self.build()
        notes = check_cv_protocol(
            self.root, self.origin, self.root / "cv_folds.json"
        )
        self.assertEqual(notes, ["cv protocol: report=2, work=4, folds=2"])

    def test_report_and_work_overlap_is_rejected(self):
        self.build(work=(1, 2, 3, 4, 5, 6, 7))
        with self.assertRaises(CheckFailure):
            check_cv_protocol(self.root, self.origin, self.root / "cv_folds.json")

    def test_overlapping_fold_eval_sets_are_rejected(self):
        self.build(folds=((2, 3), (3, 4)))
        with self.assertRaises(CheckFailure):
            check_cv_protocol(self.root, self.origin, self.root / "cv_folds.json")

    def test_manifest_mismatch_is_rejected(self):
        self.build()
        manifest_path = self.root / "cv_folds.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["report_holdout_indices"] = [0, 1, 2]
        write_json(manifest_path, manifest)
        with self.assertRaises(CheckFailure):
            check_cv_protocol(self.root, self.origin, manifest_path)


class DepthSetGuardTest(unittest.TestCase):
    """序贯选深度的前置条件：必须从 V0 连续覆盖。"""

    def test_contiguous_set_starting_at_zero_is_accepted(self):
        validate_depth_set([0, 1, 2, 3])

    def test_missing_baseline_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "缺少 V0"):
            validate_depth_set([1, 2, 3])

    def test_interior_gap_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "断档"):
            validate_depth_set([0, 1, 3, 4])

    def test_empty_set_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_depth_set([])

    def test_find_depth_gaps_reports_missing_depths(self):
        self.assertEqual(find_depth_gaps([0, 1, 3, 5]), [2, 4])
        self.assertEqual(find_depth_gaps([0, 1, 2]), [])

    def test_select_depth_rejects_a_gapped_curve(self):
        curve = {
            0: DepthPoint(0, measure([scoring_row(0, [3] * 3, [6, 6, 6])]), [1]),
            2: DepthPoint(2, measure([scoring_row(1, [6] * 3, [6, 6, 6])]), [1]),
        }
        with self.assertRaisesRegex(ValueError, "断档"):
            select_depth(curve)


class StopGuardTest(unittest.TestCase):
    """停止条件落在边界或报告重复评估时必须显式失败，不能静默通过。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_report = cv_runner.REPORT_PATH
        cv_runner.REPORT_PATH = str(Path(self.temp.name) / "report.json")

    def tearDown(self):
        cv_runner.REPORT_PATH = self.original_report
        self.temp.cleanup()

    def test_report_refuses_to_overwrite_an_existing_evaluation(self):
        Path(cv_runner.REPORT_PATH).write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "只允许评估一次"):
            cv_runner.run_report_holdout(3, "origin_scoring_results.json")

    def test_forced_report_overwrite_is_allowed_to_reach_the_guard(self):
        # force=True 应当越过覆盖保护；此处不调用 API，只确认保护分支不触发。
        Path(cv_runner.REPORT_PATH).write_text("{}", encoding="utf-8")
        with self.assertRaises(SystemExit) as caught:
            cv_runner.run_report_holdout(3, "origin_scoring_results.json")
        self.assertIn("只允许评估一次", str(caught.exception))
        # 覆盖保护是唯一在 run_scoring 之前抛出的分支
        self.assertNotIn("AES_API_KEY", str(caught.exception))


class LegacyEntryGuardTest(unittest.TestCase):
    """CV 协议生效时，旧入口必须拒绝运行，否则会把报告留出当作选版集。"""

    def test_run_pipeline_refuses_when_cv_protocol_is_active(self):
        with mock.patch("pipeline_entry.os.path.exists", return_value=True):
            with self.assertRaisesRegex(SystemExit, "报告留出"):
                pipeline_entry.run_pipeline("origin_scoring_results.json")

    def test_guard_message_points_to_the_cv_runner(self):
        with mock.patch("pipeline_entry.os.path.exists", return_value=True):
            with self.assertRaises(SystemExit) as caught:
                pipeline_entry.run_pipeline("origin_scoring_results.json")
        self.assertIn("cv_runner.py", str(caught.exception))

    def test_run_pipeline_proceeds_when_no_cv_protocol(self):
        calls = []

        def fake_exists(path):
            return path != "cv_folds.json"

        with mock.patch("pipeline_entry.os.path.exists", side_effect=fake_exists):
            with mock.patch("pipeline_entry.ensure_origin_prompt", side_effect=lambda: calls.append(1)):
                with mock.patch("pipeline_entry.extract_samples", side_effect=lambda _: calls.append(1)):
                    with mock.patch("pipeline_entry.run_scoring", side_effect=lambda *a, **k: calls.append(1)):
                        with mock.patch(
                            "pipeline_entry.run_badcase_mining",
                            return_value={"statistics": {"B_bias": {"severe_count": 0, "soft_count": 0}},
                                          "B_bias": {"severe": [], "soft": []}},
                        ):
                            with mock.patch("pipeline_entry.run_prompt_optimization"):
                                with mock.patch(
                                    "pipeline_entry.decide_iteration",
                                    return_value=IterationDecision("compare", "severe_rebound", 0),
                                ):
                                    with mock.patch(
                                        "pipeline_entry.compare_candidates", return_value=0
                                    ):
                                        with mock.patch(
                                            "pipeline_entry.save_final_prompt"
                                        ):
                                            pipeline_entry.run_pipeline(
                                                "origin_scoring_results.json", resume_from=0
                                            )
        self.assertTrue(calls)


if __name__ == "__main__":
    unittest.main()
