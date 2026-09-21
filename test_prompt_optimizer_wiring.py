"""`PromptOptimizer.run()` 真实调用链的结构契约接线测试（大纲 §6/§7/§11）。

这些测试不直接调用 `validate_optimizer_edit`，而是驱动完整的 `run()`：
读当前 prompt → 校验输入契约 → 调 API（被 mock）→ 提取候选 →
校验修改权限与预算 → 合格才原子写入 / 不合格保留原因且不覆盖。
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import prompt_optimizer
from prompt_optimizer import (
    CandidateRejectedError,
    NoOptimizationTargetError,
    PromptOptimizer,
)


BASE_PROMPT = """你是一位熟悉马来西亚UEC华文作文批改的老师，
请对以下作文进行评分。

## 注意事项

**评分原则**：采取先给作文分维度划档，再在档内以增量加分的形式批改。

**内容分特殊情形**：

- 若作文明显偏题或抄写无关文章，内容分不超过1分

**表达分特殊情形**：

- 语言分评价范围为句式变化、词汇选择、修辞运用、语言流畅度

**结构分特殊情形**：

- 缺少开头段、结尾段、无段落划分或段落混乱，结构分不超过3分
"""


def api_response(prompt_block, reasons="test", impact="test"):
    text = (
        "===MODIFIED_PROMPT_START===\n"
        f"{prompt_block}\n"
        "===MODIFIED_PROMPT_END===\n\n"
        "===METADATA_START===\n"
        + json.dumps(
            {
                "changed_sections": [],
                "added_sections": [],
                "modification_reasons": reasons,
                "expected_impact": impact,
                "confidence_score": 0.5,
            }
        )
        + "\n===METADATA_END==="
    )
    return {"content": [{"type": "text", "text": text}]}


def badcases(severe=1, soft=0):
    case = {
        "name": "x",
        "direction": "strict",
        "bias_score": -2.0,
        "teacher": {"content": 7, "expression": 7, "structure": 7},
        "AI": {"content": 5, "expression": 5, "structure": 5},
        "diffs": {"content": -2, "expression": -2, "structure": -2},
    }
    return {
        "statistics": {"B_bias": {"severe_count": severe, "soft_count": soft}},
        "B_bias": {"severe": [case] * severe, "soft": [case] * soft},
    }


class OptimizerWiringTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original_cwd = os.getcwd()
        self.addCleanup(os.chdir, self.original_cwd)
        os.chdir(self.temp.name)

        with open("current_meta.md", "w", encoding="utf-8") as handle:
            handle.write(BASE_PROMPT)
        with open("badcases.json", "w", encoding="utf-8") as handle:
            json.dump(badcases(), handle, ensure_ascii=False)

        self.optimizer = PromptOptimizer()
        self.optimizer.CURRENT_PROMPT = "current_meta.md"
        self.optimizer.TARGET_PROMPT = "target_meta.md"
        self.optimizer.BADCASE_FILE = "badcases.json"
        self.optimizer.api_key = "test-key"

    def target_exists(self):
        return os.path.exists(self.optimizer.TARGET_PROMPT)

    def read_target(self):
        with open(self.optimizer.TARGET_PROMPT, encoding="utf-8") as handle:
            return handle.read()

    def run_with(self, responses):
        """用给定的响应序列驱动真实 run()；返回（是否抛错, 错误）。"""
        iterator = iter(responses)

        def fake_send(_request, *args, **kwargs):
            return next(iterator)

        error = None
        with mock.patch.object(self.optimizer, "send_api_request", side_effect=fake_send):
            try:
                self.optimizer.run()
            except BaseException as caught:  # noqa: BLE001 - 测试需要观察任何中止
                error = caught
        return error

    # ---------------- 接受路径 ----------------

    def test_compliant_candidate_is_written(self):
        candidate = BASE_PROMPT.replace(
            "- 若作文明显偏题或抄写无关文章，内容分不超过1分",
            "- 若作文明显偏题或抄写无关文章，内容分不超过1分\n- 内容空洞但未偏题的作文应相应下调",
        )
        error = self.run_with([api_response(candidate)])
        self.assertIsNone(error)
        self.assertTrue(self.target_exists())
        self.assertIn("内容空洞但未偏题", self.read_target())

    def test_accepted_candidate_records_structure_delta(self):
        candidate = BASE_PROMPT + "\n**输出前自查**：\n\n- 输出前核对是否已引用至少两处原文证据\n"
        error = self.run_with([api_response(candidate)])
        self.assertIsNone(error)
        with open(prompt_optimizer.DECISION_LOG, encoding="utf-8") as handle:
            decisions = json.load(handle)
        self.assertEqual(decisions[-1]["status"], "accepted")
        self.assertEqual(decisions[-1]["added_sections"], ["输出前自查"])
        # 基线 4 个分点（评分原则 + 三个特殊情形）+ 1 个新增
        self.assertEqual(len(decisions[-1]["candidate_stats"]["bold_sections"]), 5)

    def test_accepted_candidate_populates_iteration_history_sections(self):
        candidate = BASE_PROMPT + "\n**输出前自查**：\n\n- 输出前核对是否引用原文证据\n"
        self.run_with([api_response(candidate)])
        with open(self.optimizer.ITERATION_LOG, encoding="utf-8") as handle:
            history = json.load(handle)
        self.assertEqual(history[-1]["added_sections"], ["输出前自查"])

    # ---------------- 拒绝路径 ----------------

    def rejected_variants(self):
        renamed = BASE_PROMPT.replace("**评分原则**", "**评分总原则**")
        deleted = BASE_PROMPT.replace(
            "**表达分特殊情形**：\n\n- 语言分评价范围为句式变化、词汇选择、修辞运用、语言流畅度\n\n",
            "",
        )
        reordered = BASE_PROMPT.replace(
            "**内容分特殊情形**：\n\n- 若作文明显偏题或抄写无关文章，内容分不超过1分\n\n"
            "**表达分特殊情形**：\n\n- 语言分评价范围为句式变化、词汇选择、修辞运用、语言流畅度",
            "**表达分特殊情形**：\n\n- 语言分评价范围为句式变化、词汇选择、修辞运用、语言流畅度\n\n"
            "**内容分特殊情形**：\n\n- 若作文明显偏题或抄写无关文章，内容分不超过1分",
        )
        duplicated = BASE_PROMPT + "\n**评分原则**：\n\n- 重复的分点\n"
        too_many_new = BASE_PROMPT + (
            "\n**操作分点甲**：\n\n- a\n\n**操作分点乙**：\n\n- b\n\n**操作分点丙**：\n\n- c\n"
        )
        dimension_named = BASE_PROMPT + "\n**结构维度专项强化规则**：\n\n- c\n"
        anchor_named = BASE_PROMPT + "\n**语言分强制锚点**：\n\n- c\n"
        third_level = BASE_PROMPT.replace("**评分原则**：", "### 评分原则\n\n")
        return {
            "renamed": renamed,
            "deleted": deleted,
            "reordered": reordered,
            "duplicated": duplicated,
            "too_many_new": too_many_new,
            "dimension_named": dimension_named,
            "anchor_named": anchor_named,
            "third_level": third_level,
        }

    def test_every_illegal_variant_is_rejected_and_never_written(self):
        for name, variant in self.rejected_variants().items():
            with self.subTest(variant=name):
                os.path.exists(self.optimizer.TARGET_PROMPT) and os.unlink(
                    self.optimizer.TARGET_PROMPT
                )
                error = self.run_with([api_response(variant)] * 3)
                self.assertIsInstance(error, CandidateRejectedError)
                self.assertFalse(
                    self.target_exists(),
                    f"{name} 的非法候选不应被写入目标文件",
                )

    def test_rejection_reasons_are_recorded(self):
        variant = self.rejected_variants()["renamed"]
        error = self.run_with([api_response(variant)] * 3)
        self.assertIsInstance(error, CandidateRejectedError)
        with open(prompt_optimizer.DECISION_LOG, encoding="utf-8") as handle:
            decisions = json.load(handle)
        record = decisions[-1]
        self.assertEqual(record["status"], "rejected")
        self.assertEqual(len(record["attempts"]), 3)
        self.assertTrue(
            any("R1" in reason for reason in record["attempts"][0]["reasons"])
        )

    def test_retry_succeeds_when_a_later_attempt_is_compliant(self):
        illegal = self.rejected_variants()["renamed"]
        legal = BASE_PROMPT.replace("先给作文分维度划档", "先划档、再在档内微调")
        error = self.run_with([api_response(illegal), api_response(legal)])
        self.assertIsNone(error)
        self.assertTrue(self.target_exists())
        self.assertIn("先划档、再在档内微调", self.read_target())

    def test_stable_prompt_is_untouched_after_rejection(self):
        before = open(self.optimizer.CURRENT_PROMPT, encoding="utf-8").read()
        self.run_with([api_response(self.rejected_variants()["deleted"])] * 3)
        after = open(self.optimizer.CURRENT_PROMPT, encoding="utf-8").read()
        self.assertEqual(before, after)

    def test_missing_prompt_block_is_rejected(self):
        error = self.run_with([{"content": [{"type": "text", "text": "no block here"}]}] * 3)
        self.assertIsInstance(error, CandidateRejectedError)
        self.assertFalse(self.target_exists())

    # ---------------- 前置条件 ----------------

    def test_current_prompt_must_satisfy_the_input_contract(self):
        with open(self.optimizer.CURRENT_PROMPT, "w", encoding="utf-8") as handle:
            handle.write("## 注意事项\n\n**评分原则**：没有任何特殊情形分点。\n")
        error = self.run_with([api_response(BASE_PROMPT)])
        self.assertIsNotNone(error)
        self.assertEqual(type(error).__name__, "PromptContractError")
        self.assertFalse(self.target_exists())

    def test_empty_badcases_do_not_divide_by_zero(self):
        with open(self.optimizer.BADCASE_FILE, "w", encoding="utf-8") as handle:
            json.dump(badcases(severe=0, soft=0), handle, ensure_ascii=False)
        error = self.run_with([api_response(BASE_PROMPT)])
        self.assertIsInstance(error, NoOptimizationTargetError)
        self.assertFalse(self.target_exists())
        with open(prompt_optimizer.DECISION_LOG, encoding="utf-8") as handle:
            decisions = json.load(handle)
        self.assertEqual(decisions[-1]["status"], "no_target")

    def test_analyze_empty_badcases_returns_zeroed_payload(self):
        payload = self.optimizer.analyze_B_bias_distribution(badcases(0, 0))
        self.assertFalse(payload["has_targets"])
        self.assertEqual(payload["total_badcases"], 0)
        self.assertIsNone(payload["priority_dimension"])
        self.assertEqual(payload["avg_bias_score"], 0.0)


class PipelineBlockedTest(unittest.TestCase):
    """优化器阻塞时 pipeline 必须停止且不改动稳定版本（大纲 §7.4）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original_cwd = os.getcwd()
        self.addCleanup(os.chdir, self.original_cwd)
        os.chdir(self.temp.name)

    def test_optimize_or_stop_stops_the_run(self):
        import pipeline_entry

        with mock.patch(
            "pipeline_entry.run_prompt_optimization",
            side_effect=CandidateRejectedError([{"attempt": 1, "reasons": ["R1"]}]),
        ):
            with self.assertRaisesRegex(SystemExit, "blocked at V3"):
                pipeline_entry.optimize_or_stop(
                    "current.md", "badcases.json", "target.md", 3
                )

    def test_optimize_or_stop_stops_on_missing_targets(self):
        import pipeline_entry

        with mock.patch(
            "pipeline_entry.run_prompt_optimization",
            side_effect=NoOptimizationTargetError("no B badcases"),
        ):
            with self.assertRaisesRegex(SystemExit, "NoOptimizationTargetError"):
                pipeline_entry.optimize_or_stop(
                    "current.md", "badcases.json", "target.md", 2
                )


if __name__ == "__main__":
    unittest.main()
