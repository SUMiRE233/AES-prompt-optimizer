"""统一 Q 协议（Q = 2.5*mean(Severe) + mean(Soft)）的离线测试。

覆盖：均值聚合（含 rerun、不取 min）、Q 判据（含平台期）、权重交换率语义、
低精度预警 -> 补评复核 -> 高精度不可逆、V6 上限强制补评、协议状态持久化与
重放、validation 固定两次评测与排序、manifest 覆盖全部运行并声明新协议。

全部测试不发起任何 API 调用。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline_entry as pe


TEACHER = {"content": 6, "expression": 6, "structure": 6}
# 整数网格上精确命中三种分类（与旧标定口径一致）：
#   severe: 三维差 -3        -> |bias| = 3.000 > 1.5
#   soft:   三维差 -2,-1,-1  -> |bias| = 1.333 > 1.0 且 <= 1.5
#   clean:  三维差 -1,-1,-1  -> |bias| = 1.000 不大于 1.0
ROW_KINDS = {
    "severe": {"content": 3, "expression": 3, "structure": 3},
    "soft": {"content": 4, "expression": 5, "structure": 5},
    "clean": {"content": 5, "expression": 5, "structure": 5},
}


def write_scoring(path, severe=0, soft=0, clean=0):
    rows = []
    index = 0
    for kind, count in (("severe", severe), ("soft", soft), ("clean", clean)):
        for _ in range(count):
            rows.append(
                {"index": index, "AI": dict(ROW_KINDS[kind]), "teacher": dict(TEACHER)}
            )
            index += 1
    Path(path).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


def compliant_prompt():
    """满足输入契约的最小 prompt：1 个 `## 注意事项` + 三维各一个 `特殊情形`。"""
    return "\n".join(
        [
            "你是一位评分老师。",
            "",
            "## 注意事项",
            "",
            "**评分原则**：先划档再微调。",
            "",
            "**内容分特殊情形**：",
            "",
            "- 明显偏题的内容分不超过1分",
            "",
            "**表达分特殊情形**：",
            "",
            "- 语言分评价范围为句式变化、词汇选择",
            "",
            "**结构分特殊情形**：",
            "",
            "- 缺少开头段或结尾段，结构分不超过3分",
            "",
        ]
    )


class _TempCwd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def write_run(self, iteration, severe, soft, rerun=False):
        path = (
            pe.rerun_scoring_path(iteration)
            if rerun
            else pe.train_scoring_path(iteration)
        )
        write_scoring(path, severe=severe, soft=soft)


class HelperCalibrationTest(_TempCwd):
    def test_helper_produces_exact_counts(self):
        write_scoring("train_scoring_results0.json", severe=2, soft=3, clean=4)
        self.assertEqual(
            pe.b_bias_counts_from_scoring_results("train_scoring_results0.json"),
            {"severe": 2, "soft": 3, "total": 5},
        )


class MeanAggregationTest(_TempCwd):
    def test_single_run_mean_is_itself(self):
        self.write_run(2, severe=7, soft=5)
        mean = pe.version_mean_counts(2)
        self.assertEqual(mean["severe"], 7.0)
        self.assertEqual(mean["soft"], 5.0)
        self.assertEqual(mean["runs"], 1)
        self.assertAlmostEqual(mean["q"], pe.Q_SEVERE_WEIGHT * 7 + 5)

    def test_mean_includes_the_rerun_never_the_min(self):
        # 实测场景：V2 run1 7/5、rerun 9/6 -> 路线内均值 8.0/5.5（不是 min 7/5）
        self.write_run(2, severe=7, soft=5)
        self.write_run(2, severe=9, soft=6, rerun=True)
        state = pe.new_protocol_state()
        state["repeat_level"] = pe.REPEAT_LEVEL_HIGH
        state["upgraded_at"] = 2
        mean = pe.version_mean_counts(2, state=state)
        self.assertEqual(mean["runs"], 2)
        self.assertEqual(mean["available_runs"], 2)
        self.assertEqual(mean["excluded"], [])
        self.assertEqual(mean["severe"], 8.0)
        self.assertEqual(mean["soft"], 5.5)
        self.assertAlmostEqual(mean["q"], pe.Q_SEVERE_WEIGHT * 8.0 + 5.5)

    def test_reruns_outside_the_route_are_excluded(self):
        # 路线外（未升级）的 rerun 不得计入均值，转入 excluded。
        self.write_run(1, severe=13, soft=5)
        self.write_run(1, severe=9, soft=4, rerun=True)
        mean = pe.version_mean_counts(1, state=pe.new_protocol_state())
        self.assertEqual(mean["severe"], 13.0)
        self.assertEqual(mean["soft"], 5.0)
        self.assertEqual(mean["runs"], 1)
        self.assertEqual(mean["available_runs"], 2)
        self.assertEqual([run["severe"] for run in mean["excluded"]], [9])

    def test_boundary_pair_is_inside_the_route(self):
        state = pe.new_protocol_state()
        state["repeat_level"] = pe.REPEAT_LEVEL_HIGH
        state["upgraded_at"] = 3
        self.assertEqual(pe.allowed_run_count(0, state), 1)
        self.assertEqual(pe.allowed_run_count(1, state), 1)
        self.assertEqual(pe.allowed_run_count(2, state), 2)
        self.assertEqual(pe.allowed_run_count(3, state), 2)
        self.assertEqual(pe.allowed_run_count(6, state), 2)

    def test_gate_signal_requires_strict_decrease(self):
        previous = {"severe": 8.0, "soft": 5.5}  # Q = 25.50
        worse = {"severe": 8.5, "soft": 4.5}  # Q = 25.75 -> 触发
        better = {"severe": 6.5, "soft": 4.0}  # Q = 20.25 -> 不触发
        self.assertTrue(pe.gate_signal(previous, worse))
        self.assertFalse(pe.gate_signal(previous, better))

    def test_plateau_triggers(self):
        counts = {"severe": 4.0, "soft": 4.0}
        self.assertTrue(pe.gate_signal(counts, dict(counts)))


class WeightSemanticsTest(unittest.TestCase):
    """w=2.5 的交换率语义（用户决策）：+1S/-3F 允许，+1S/-2F 触发。"""

    def test_one_severe_may_be_offset_by_three_soft(self):
        previous = {"severe": 4.0, "soft": 6.0}
        current = {"severe": 5.0, "soft": 3.0}  # ΔQ = -0.5 -> 继续
        self.assertFalse(pe.gate_signal(previous, current))

    def test_one_severe_may_not_be_offset_by_two_soft(self):
        previous = {"severe": 4.0, "soft": 6.0}
        current = {"severe": 5.0, "soft": 4.0}  # ΔQ = +0.5 -> 触发
        self.assertTrue(pe.gate_signal(previous, current))


class DecideIterationTest(unittest.TestCase):
    def test_strict_decrease_continues(self):
        decision = pe.decide_iteration(
            {"severe": 6.5, "soft": 4.0}, {"severe": 4.0, "soft": 4.0}, 5
        )
        self.assertEqual(
            (decision.action, decision.reason), ("continue", "q_strictly_decreased")
        )

    def test_real_v2_to_v3_case_does_not_warn_at_weight_2_5(self):
        # 实测均值：V2 8.0/5.5 -> V3 8.5/4.0；ΔQ = -0.25（w=3 时恰好为 0）
        decision = pe.decide_iteration(
            {"severe": 8.0, "soft": 5.5}, {"severe": 8.5, "soft": 4.0}, 3
        )
        self.assertEqual(decision.action, "continue")

    def test_low_precision_warning_requests_upgrade(self):
        decision = pe.decide_iteration(
            {"severe": 8.0, "soft": 5.5}, {"severe": 8.5, "soft": 4.5}, 3
        )
        self.assertEqual(
            (decision.action, decision.reason), ("upgrade", "low_precision_warning")
        )
        self.assertEqual(decision.candidates, ())

    def test_high_precision_gate_stops_with_candidates(self):
        decision = pe.decide_iteration(
            {"severe": 8.0, "soft": 5.5},
            {"severe": 8.5, "soft": 4.5},
            3,
            repeat_level=pe.REPEAT_LEVEL_HIGH,
        )
        self.assertEqual(
            (decision.action, decision.reason), ("stop", "high_precision_gate")
        )
        self.assertEqual(decision.candidates, (2, 3))

    def test_high_precision_gate_at_cap_is_named(self):
        decision = pe.decide_iteration(
            {"severe": 3.0, "soft": 5.5},
            {"severe": 3.0, "soft": 6.5},
            6,
            repeat_level=pe.REPEAT_LEVEL_HIGH,
        )
        self.assertEqual(
            (decision.action, decision.reason),
            ("stop", "high_precision_gate_at_cap"),
        )
        self.assertEqual(decision.candidates, (5, 6))

    def test_cap_without_warning_stops_and_forces_top_up(self):
        decision = pe.decide_iteration(
            {"severe": 6.0, "soft": 4.0}, {"severe": 4.0, "soft": 4.0}, 6
        )
        self.assertEqual((decision.action, decision.reason), ("stop", "cap_reached"))
        self.assertEqual(decision.candidates, (5, 6))
        self.assertTrue(decision.top_up_first)

    def test_cap_in_high_precision_needs_no_top_up(self):
        decision = pe.decide_iteration(
            {"severe": 6.0, "soft": 4.0},
            {"severe": 4.0, "soft": 4.0},
            6,
            repeat_level=pe.REPEAT_LEVEL_HIGH,
        )
        self.assertEqual(decision.action, "stop")
        self.assertFalse(decision.top_up_first)

    def test_origin_always_continues(self):
        decision = pe.decide_iteration(None, None, 0)
        self.assertEqual((decision.action, decision.reason), ("continue", "origin"))


class TopUpTest(_TempCwd):
    def test_top_up_is_idempotent_and_bounded(self):
        self.write_run(3, severe=6, soft=5)

        def fake(iteration, origin_file, paths=pe.LEGACY_PATHS,
                 train_essays="train_essays.json", rerun=False):
            write_scoring(pe.rerun_scoring_path(iteration), severe=6, soft=5)

        with mock.patch.object(pe, "run_train_iteration", side_effect=fake) as runner:
            first = pe.top_up_version(3, "origin.json")
            second = pe.top_up_version(3, "origin.json")

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(len(pe.version_run_counts(3)), 2)

    def test_top_up_never_exceeds_max_runs_even_if_no_file_is_produced(self):
        self.write_run(3, severe=6, soft=5)
        with mock.patch.object(pe, "run_train_iteration") as runner:
            performed = pe.top_up_version(3, "origin.json")
        self.assertLessEqual(performed, pe.MAX_RUNS_PER_VERSION)


class ResolveDecisionTest(_TempCwd):
    def test_first_warning_confirmed_by_double_eval_stops(self):
        self.write_run(0, severe=4, soft=6)   # 单评 Q = 16.0
        self.write_run(1, severe=5, soft=4)   # 单评 Q = 16.5 -> 预警

        def fake(iteration, origin_file, paths=pe.LEGACY_PATHS,
                 train_essays="train_essays.json", rerun=False):
            if iteration == 0:
                write_scoring(pe.rerun_scoring_path(0), severe=5, soft=4)
            else:
                write_scoring(pe.rerun_scoring_path(1), severe=5, soft=4)

        decision = pe.IterationDecision("upgrade", "low_precision_warning")
        with mock.patch.object(pe, "run_train_iteration", side_effect=fake):
            final, state, touched = pe.resolve_decision(
                decision, 1, "origin.json", pe.new_protocol_state()
            )

        # 复核后：V0 均值 4.5/5.0、V1 均值 5.0/4.0 -> ΔQ = +0.25，确认
        self.assertEqual(
            (final.action, final.reason), ("stop", "first_warning_confirmed")
        )
        self.assertEqual(final.candidates, (0, 1))
        self.assertEqual(state["repeat_level"], pe.REPEAT_LEVEL_HIGH)
        self.assertEqual(sorted(touched), [0, 1])
        self.assertTrue(state["warning_events"][-1]["confirmed"])
        self.assertTrue(Path(pe.RERUN_LOG).is_file())

    def test_refuted_warning_continues_with_irreversible_upgrade(self):
        self.write_run(0, severe=4, soft=6)   # 单评 Q = 16.0
        self.write_run(1, severe=5, soft=4)   # 单评 Q = 16.5 -> 预警

        def fake(iteration, origin_file, paths=pe.LEGACY_PATHS,
                 train_essays="train_essays.json", rerun=False):
            if iteration == 0:
                write_scoring(pe.rerun_scoring_path(0), severe=4, soft=6)
            else:
                write_scoring(pe.rerun_scoring_path(1), severe=1, soft=0)

        decision = pe.IterationDecision("upgrade", "low_precision_warning")
        with mock.patch.object(pe, "run_train_iteration", side_effect=fake):
            final, state, touched = pe.resolve_decision(
                decision, 1, "origin.json", pe.new_protocol_state()
            )

        # 复核后：V0 均值 4.0/6.0、V1 均值 3.0/2.0 -> ΔQ = -6.5，判为噪声
        self.assertEqual(
            (final.action, final.reason), ("continue", "warning_refuted_by_precision")
        )
        self.assertEqual(state["repeat_level"], pe.REPEAT_LEVEL_HIGH)
        self.assertFalse(state["warning_events"][-1]["confirmed"])

    def test_cap_forced_top_up_only_touches_the_last_two(self):
        self.write_run(5, severe=4, soft=4)
        self.write_run(6, severe=3, soft=4)

        def fake(iteration, origin_file, paths=pe.LEGACY_PATHS,
                 train_essays="train_essays.json", rerun=False):
            write_scoring(pe.rerun_scoring_path(iteration), severe=3, soft=4)

        decision = pe.IterationDecision(
            "stop", "cap_reached", (5, 6), top_up_first=True
        )
        with mock.patch.object(pe, "run_train_iteration", side_effect=fake):
            final, state, touched = pe.resolve_decision(
                decision, 6, "origin.json", pe.new_protocol_state()
            )

        self.assertEqual(final.action, "stop")
        self.assertEqual(sorted(touched), [5, 6])
        self.assertEqual(state["repeat_level"], pe.REPEAT_LEVEL_HIGH)
        log = json.loads(Path(pe.RERUN_LOG).read_text(encoding="utf-8"))
        self.assertEqual(log[-1]["trigger"], "cap_forced")
        self.assertEqual(len(pe.version_run_counts(5)), 2)
        self.assertEqual(len(pe.version_run_counts(6)), 2)


class ProtocolStateTest(_TempCwd):
    def _write_history(self, rows):
        for iteration, runs in rows.items():
            for position, (severe, soft) in enumerate(runs):
                self.write_run(iteration, severe, soft, rerun=position > 0)

    def _write_real_trajectory(self):
        self.write_run(0, severe=16, soft=7)
        self.write_run(1, severe=13, soft=5)
        self.write_run(2, severe=7, soft=5)
        self.write_run(2, severe=9, soft=6, rerun=True)
        self.write_run(3, severe=11, soft=3)
        self.write_run(3, severe=6, soft=5, rerun=True)
        self.write_run(4, severe=7, soft=3)
        self.write_run(4, severe=6, soft=5, rerun=True)
        self.write_run(5, severe=4, soft=4)
        self.write_run(5, severe=2, soft=7, rerun=True)
        self.write_run(6, severe=3, soft=5)
        self.write_run(6, severe=3, soft=8, rerun=True)

    def test_replay_follows_the_true_timeline(self):
        self._write_real_trajectory()
        state, rows = pe.replay_protocol_route(3)
        self.assertEqual(state["upgraded_at"], 3)
        self.assertEqual(len(state["warning_events"]), 1)
        event = state["warning_events"][0]
        self.assertEqual(event["iteration"], 3)
        self.assertEqual(event["delta_q_before"], 8.0)
        self.assertEqual(event["delta_q_after"], -0.25)
        self.assertFalse(event["confirmed"])
        self.assertNotIn("stop", state)
        self.assertEqual(rows[3]["decision"], "low_precision_warning_refuted")

    def test_replay_reaches_high_precision_gate_at_cap(self):
        self._write_real_trajectory()
        state, rows = pe.replay_protocol_route(6)
        self.assertEqual(state["stop"]["reason"], "high_precision_gate_at_cap")
        self.assertEqual(state["stop"]["candidates"], [5, 6])
        self.assertEqual(rows[6]["delta_q"], 1.0)
        self.assertEqual(rows[6]["runs_used"], 2)
        self.assertEqual(rows[5]["decision"], "high_precision_pass")
        self.assertEqual(rows[1]["decision"], "low_precision_pass")
        # 低精度阶段只用 run1；边界与升级后用双评
        self.assertEqual(rows[2]["runs_used"], 1)
        self.assertEqual(rows[2]["q"], 22.5)
        self.assertEqual(rows[3]["runs_used"], 2)
        self.assertEqual(rows[3]["q"], 25.25)
        self.assertEqual(rows[3]["boundary_previous"]["q"], 25.5)

    def test_replay_without_warning_does_not_upgrade(self):
        self._write_history({0: [(16, 7)], 1: [(13, 5)]})
        state, rows = pe.replay_protocol_route(1)
        self.assertEqual(state["repeat_level"], pe.REPEAT_LEVEL_LOW)
        self.assertIsNone(state["upgraded_at"])
        self.assertEqual(state["warning_events"], [])

    def test_rebuild_writes_state_route_and_log(self):
        self._write_real_trajectory()
        state, rows, route_path = pe.rebuild_protocol_timeline()
        self.assertEqual(state["stop"]["reason"], "high_precision_gate_at_cap")
        saved = json.loads(Path(pe.PROTOCOL_STATE).read_text(encoding="utf-8"))
        self.assertEqual(saved["upgraded_at"], 3)
        route = json.loads(Path(route_path).read_text(encoding="utf-8"))
        self.assertEqual(route["stop"]["reason"], "high_precision_gate_at_cap")
        log = json.loads(Path(pe.RERUN_LOG).read_text(encoding="utf-8"))
        self.assertEqual(log[-1]["kind"], "timeline_rebuild")

    def test_load_protocol_state_seeds_from_replay_and_persists(self):
        self._write_history({0: [(16, 7)], 1: [(13, 5)]})
        state = pe.load_protocol_state(1)
        self.assertTrue(Path(pe.PROTOCOL_STATE).is_file())
        reloaded = pe.load_protocol_state(1)
        self.assertEqual(reloaded["repeat_level"], state["repeat_level"])
        self.assertEqual(reloaded["w"], pe.Q_SEVERE_WEIGHT)

    def test_state_file_with_a_foreign_weight_is_rejected(self):
        Path(pe.PROTOCOL_STATE).write_text(
            json.dumps({"protocol_version": pe.PROTOCOL_VERSION, "w": 1.5}),
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit):
            pe.load_protocol_state(1)


class ValidationSelectionTest(_TempCwd):
    def _write_eval(self, iteration, runs):
        for position, (severe, soft) in enumerate(runs):
            path = (
                pe.LEGACY_PATHS.rerun_eval_scoring(iteration)
                if position
                else pe.test_scoring_path(iteration)
            )
            write_scoring(path, severe=severe, soft=soft)

    def test_two_fixed_runs_per_candidate_and_selection_by_q(self):
        self._write_eval(5, [(4, 4), (4, 4)])  # Q = 14.0
        self._write_eval(6, [(3, 4), (4, 4)])  # 均值 3.5/4.0 -> Q = 12.75
        selected = pe.run_validation_and_select((5, 6), "origin.json", "cap_reached")
        self.assertEqual(selected, 6)
        report = json.loads(
            Path(pe.VALIDATION_REPORT).read_text(encoding="utf-8")
        )
        self.assertEqual(report["selected_iteration"], 6)
        self.assertEqual(report["w"], pe.Q_SEVERE_WEIGHT)
        self.assertEqual(len(report["candidates"][1]["runs"]), 2)

    def test_second_eval_always_runs_when_only_run1_exists(self):
        self._write_eval(5, [(9, 9)])
        calls = []

        def fake(iteration, origin_file, paths=pe.LEGACY_PATHS,
                 eval_essays="test_essays.json", rerun=False):
            calls.append((iteration, rerun))
            write_scoring(
                pe.LEGACY_PATHS.rerun_eval_scoring(iteration), severe=3, soft=4
            )
            return {"severe": 3, "soft": 4, "total": 7}

        with mock.patch.object(pe, "run_test_iteration", side_effect=fake):
            performed = pe.ensure_eval_runs(5, "origin.json")

        self.assertEqual(performed, 1)
        self.assertEqual(calls, [(5, True)])
        self.assertEqual(len(pe.version_eval_scoring_paths(5)), 2)

    def test_tie_on_q_prefers_fewer_severe(self):
        self._write_eval(4, [(2, 0), (2, 0)])  # Q = 5.0，severe=2
        self._write_eval(6, [(0, 5), (0, 5)])  # Q = 5.0，severe=0
        self.assertEqual(
            pe.run_validation_and_select((4, 6), "origin.json", "cap_reached"), 6
        )

    def test_full_tie_prefers_the_earlier_version(self):
        for iteration in (5, 6):
            self._write_eval(iteration, [(0, 5), (0, 5)])
        self.assertEqual(
            pe.run_validation_and_select((5, 6), "origin.json", "cap_reached"), 5
        )


class ManifestCoverageTest(_TempCwd):
    """旧 test_b_rerun_neutral 的 manifest 覆盖测试在 Q 协议下的替代版本。"""

    def _prepare_prompt_files(self, iteration=3):
        Path(f"optimized_prompt{iteration}_meta.md").write_text(
            compliant_prompt(), encoding="utf-8"
        )
        Path(f"optimized_prompt{iteration}.md").write_text(
            compliant_prompt(), encoding="utf-8"
        )

    def test_all_runs_and_means_are_recorded(self):
        state = pe.new_protocol_state()
        state["repeat_level"] = pe.REPEAT_LEVEL_HIGH
        state["upgraded_at"] = 2
        pe.save_protocol_state(state)
        self.write_run(3, severe=11, soft=3)
        self.write_run(3, severe=6, soft=5, rerun=True)
        Path(pe.badcase_path(3)).write_text("{}", encoding="utf-8")
        Path(pe.rerun_badcase_path(3)).write_text("{}", encoding="utf-8")
        self._prepare_prompt_files(3)
        Path("train_essays.json").write_text("[]", encoding="utf-8")
        Path("origin_scoring_results.json").write_text("[]", encoding="utf-8")
        manifest_path = Path("run_manifest_run_z.json")
        manifest_path.write_text(json.dumps({"run_id": "run_z"}), encoding="utf-8")

        record = pe.record_version_in_manifest(3, manifest_path)

        names = [item["name"] for item in record["outputs"]]
        for expected in (
            "train_scoring_results3.json",
            "train_scoring_results3_rerun.json",
            "aes_badcases3.json",
            "aes_badcases3_rerun.json",
        ):
            self.assertIn(expected, names)
        self.assertEqual(record["means"]["severe"], 8.5)
        self.assertEqual(record["means"]["soft"], 4.0)
        self.assertIn("mean", record["aggregation"])

    def test_manifest_declares_the_unified_q_protocol(self):
        Path("origin.json").write_text("[]", encoding="utf-8")
        Path("origin_prompt_meta.md").write_text(
            compliant_prompt(), encoding="utf-8"
        )
        Path("origin_prompt.md").write_text(compliant_prompt(), encoding="utf-8")
        Path("train_essays.json").write_text('[{"index": 0}]', encoding="utf-8")
        Path("test_essays.json").write_text('[{"index": 1}]', encoding="utf-8")

        manifest, _ = pe.build_run_manifest("origin.json", pe.MAX_AUTO_VERSION)

        protocol = manifest["protocol"]
        self.assertEqual(protocol["run_class"], pe.RUN_CLASS)
        self.assertIn("mean", protocol["aggregation"])
        self.assertIn("2.5", protocol["objective"])
        self.assertEqual(len(protocol["amendments"]), 4)
        self.assertIn("irreversible", protocol["precision"])
        self.assertEqual(
            manifest["stop_policy"]["objective"]["w"], pe.Q_SEVERE_WEIGHT
        )


class MeanBaselineTest(_TempCwd):
    def test_mean_baseline_averages_per_essay_per_dimension(self):
        rows_a = [
            {"index": 0, "AI": {"content": 6, "expression": 5, "structure": 6},
             "teacher": dict(TEACHER)},
            {"index": 1, "AI": {"content": 7, "expression": 6, "structure": 6},
             "teacher": dict(TEACHER)},
        ]
        rows_b = [
            {"index": 0, "AI": {"content": 8, "expression": 7, "structure": 4},
             "teacher": dict(TEACHER)},
            {"index": 1, "AI": {"content": 9, "expression": 6, "structure": 8},
             "teacher": dict(TEACHER)},
        ]
        Path(pe.train_scoring_path(6)).write_text(
            json.dumps(rows_a), encoding="utf-8"
        )
        Path(pe.rerun_scoring_path(6)).write_text(
            json.dumps(rows_b), encoding="utf-8"
        )

        output = pe.write_mean_baseline(6)

        merged = json.loads(Path(output).read_text(encoding="utf-8"))
        self.assertEqual(
            merged[0]["AI"], {"content": 7.0, "expression": 6.0, "structure": 5.0}
        )
        self.assertEqual(
            merged[1]["AI"], {"content": 8.0, "expression": 6.0, "structure": 7.0}
        )
        self.assertEqual(merged[0]["teacher"], dict(TEACHER))
        provenance = json.loads(
            Path("final_train_scoring_results_mean.provenance.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(len(provenance["derived_from"]), 2)


class FinalEvidenceTest(_TempCwd):
    def test_final_evidence_lists_runs_means_and_validation(self):
        self.write_run(5, severe=4, soft=4)
        self.write_run(5, severe=2, soft=7, rerun=True)
        self.write_run(6, severe=3, soft=5)
        self.write_run(6, severe=3, soft=8, rerun=True)
        state = pe.new_protocol_state()
        state["repeat_level"] = pe.REPEAT_LEVEL_HIGH
        state["upgraded_at"] = 3
        pe.save_protocol_state(state)
        Path(pe.VALIDATION_REPORT).write_text(
            json.dumps(
                {
                    "candidates": [{"iteration": 5}, {"iteration": 6}],
                    "selected_iteration": 6,
                }
            ),
            encoding="utf-8",
        )

        evidence = pe.write_final_evidence(6)

        self.assertEqual(evidence["final_iteration"], 6)
        self.assertEqual(evidence["train"]["V5"]["q"], 13.0)
        self.assertEqual(evidence["train"]["V6"]["q"], 14.0)
        self.assertEqual(evidence["validation"]["selected_iteration"], 6)


if __name__ == "__main__":
    unittest.main()
