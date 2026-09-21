import unittest

from batch_scoring import BatchEssayScorer
from prompt_structure_contract import (
    AMBIGUOUS_RESTRICTION_KEYWORDS,
    MAX_BOLD_SECTIONS,
    OPERATIONAL_NAME_MARKERS,
    UNAMBIGUOUS_RESTRICTION_KEYWORDS,
    build_anchor_registry,
    dimension_anchor_map,
    parse_bold_sections,
    validate_input_contract,
    validate_optimizer_edit,
)


def make_prompt(expression_heading="**表达分特殊情形**", extra_sections=()):
    lines = [
        "你是一位熟悉马来西亚UEC华文作文批改的老师，",
        "请对以下作文进行评分。",
        "",
        "## 注意事项",
        "",
        "**评分原则**：先划档再微调。",
        "",
        "**内容分特殊情形**：",
        "",
        "- 明显偏题的内容分不超过1分",
        "",
        expression_heading + "：",
        "",
        "- 语言分评价范围为句式变化、词汇选择",
        "",
        "**结构分特殊情形**：",
        "",
        "- 缺少开头段或结尾段，结构分不超过3分",
    ]
    for section in extra_sections:
        lines.extend(["", section, "", "- 示例条目"])
    return "\n".join(lines) + "\n"


def codes(violations):
    return [item.code for item in violations]


class InputContractTest(unittest.TestCase):
    def test_origin_prompt_satisfies_contract(self):
        with open("origin_prompt_meta.md", encoding="utf-8") as handle:
            text = handle.read()
        self.assertEqual(validate_input_contract(text), [])

    def test_deployed_origin_prompt_matches_meta_after_cleanup(self):
        with open("origin_prompt_meta.md", encoding="utf-8") as handle:
            meta = handle.read()
        with open("origin_prompt.md", encoding="utf-8") as handle:
            deployed = handle.read()
        # The meta no longer carries sections that preprocessing strips, so the
        # optimizer sees exactly what the scorer receives (design principle P8).
        self.assertEqual(meta.strip(), deployed.strip())

    def test_missing_note_section_reports_in_1(self):
        self.assertEqual(codes(validate_input_contract("## 其它\n\n内容")), ["IN-1"])

    def test_missing_expression_special_case_reports_in_2(self):
        text = make_prompt(expression_heading="**语言分评价范围**")
        violations = validate_input_contract(text)
        self.assertIn("IN-2", codes(violations))
        self.assertIn("expression", str(violations[0]))

    def test_duplicate_special_case_for_one_dimension_reports_in_2(self):
        text = make_prompt(extra_sections=["**表达分补充特殊情形**"])
        violations = validate_input_contract(text)
        self.assertTrue(any("2 个" in item.detail for item in violations))

    def test_duplicate_bold_point_name_reports_in_3(self):
        text = make_prompt(extra_sections=["**评分原则**"])
        self.assertIn("IN-3", codes(validate_input_contract(text)))

    def test_dimension_anchor_map_covers_three_dimensions(self):
        anchors = dimension_anchor_map(make_prompt())
        self.assertEqual(
            anchors,
            {
                "content": "**内容分特殊情形**",
                "expression": "**表达分特殊情形**",
                "structure": "**结构分特殊情形**",
            },
        )

    def test_anchor_registry_requires_contract(self):
        with self.assertRaises(ValueError):
            build_anchor_registry(make_prompt(expression_heading="**语言分评价范围**"))

    def test_anchor_registry_hashes_the_prompt(self):
        registry = build_anchor_registry(make_prompt(), prompt_path="p.md")
        self.assertEqual(len(registry["prompt_sha256"]), 64)
        self.assertEqual(len(registry["dimension_anchors"]), 3)


class OptimizerPermissionTest(unittest.TestCase):
    def setUp(self):
        self.before = make_prompt()

    def test_editing_existing_bullet_items_is_allowed(self):
        after = self.before.replace(
            "- 明显偏题的内容分不超过1分",
            "- 明显偏题的内容分不超过1分\n- 内容空洞但未偏题的作文应相应下调",
        )
        self.assertEqual(validate_optimizer_edit(self.before, after), [])

    def test_rewriting_prose_under_a_bold_point_is_allowed(self):
        after = self.before.replace(
            "**评分原则**：先划档再微调。",
            "**评分原则**：先划档、再在档内微调，避免从最低分逐项扣分。",
        )
        self.assertEqual(validate_optimizer_edit(self.before, after), [])

    def test_renaming_a_bold_point_is_rejected(self):
        after = self.before.replace("**评分原则**", "**评分总原则**")
        self.assertIn("R1", codes(validate_optimizer_edit(self.before, after)))

    def test_deleting_a_bold_point_is_rejected(self):
        after = make_prompt(expression_heading="**表达分特殊情形**").replace(
            "**表达分特殊情形**：\n\n- 语言分评价范围为句式变化、词汇选择\n\n", ""
        )
        self.assertIn("R1", codes(validate_optimizer_edit(self.before, after)))

    def test_adding_an_operation_bold_point_is_allowed(self):
        after = make_prompt(extra_sections=["**输出前自查**"])
        self.assertEqual(validate_optimizer_edit(self.before, after), [])

    def test_adding_an_anchor_bold_point_is_rejected(self):
        for heading in ("**语言分强制锚点**", "**结构分强制锚点**", "**评分锚点校准**"):
            with self.subTest(heading=heading):
                after = make_prompt(extra_sections=[heading])
                self.assertIn("R5", codes(validate_optimizer_edit(self.before, after)))

    def test_adding_a_dimension_scoped_bold_point_is_rejected(self):
        after = make_prompt(extra_sections=["**表达维度专项强化规则**"])
        self.assertIn("R5", codes(validate_optimizer_edit(self.before, after)))

    def test_operation_marker_neutralises_an_ambiguous_restriction_word(self):
        # V1 实测回归：`**评分前锚定校准**` / `**打分前档位校准**` 曾被 R5 误杀，
        # 但它们是“评分前操作步骤”，属于 §5.2 允许的操作分点。
        for heading in (
            "**评分前锚定校准**",
            "**打分前档位校准**",
            "**结束前自查校准**",
        ):
            with self.subTest(heading=heading):
                after = make_prompt(extra_sections=[heading])
                self.assertEqual(validate_optimizer_edit(self.before, after), [])

    def test_unambiguous_restriction_word_is_rejected_even_with_operation_marker(self):
        for heading in ("**评分前下限确认**", "**输出前上限复核**", "**评分步骤门槛**"):
            with self.subTest(heading=heading):
                after = make_prompt(extra_sections=[heading])
                self.assertIn("R5", codes(validate_optimizer_edit(self.before, after)))

    def test_dimension_restriction_hidden_in_an_operation_point_body_is_rejected(self):
        # 标题含操作标记，但正文把维度硬限制写成了条目 -> 仍须拒绝。
        after = self.before + "\n" + "\n".join(
            ["**评分前锚定校准**", "", "- 内容分不得低于 6 分", ""]
        )
        violations = validate_optimizer_edit(self.before, after)
        self.assertIn("R5", codes(violations))
        self.assertTrue(any("正文" in item.detail for item in violations))

    def test_operation_point_without_any_restriction_is_allowed(self):
        after = self.before + "\n" + "\n".join(
            [
                "**评分前锚定校准**",
                "",
                "- 先通读全文再落笔赋分",
                "- 确认各维度独立判断后再汇总",
                "",
            ]
        )
        self.assertEqual(validate_optimizer_edit(self.before, after), [])

    def test_self_check_that_merely_mentions_dimensions_is_allowed(self):
        # V1->V2 实测回归：这条正文是“自查要落实到具体证据”，属操作要求；
        # 同行共现规则因 `病句内容`/`结构缺陷描述` + `至少` 把它误杀，
        # 浪费 1 次调用，并迫使模型删掉“至少两处”（自查精度反而下降）。
        after = self.before + "\n" + "\n".join(
            [
                "**打分前逐维度扣分依据自查**",
                "",
                "- 在给出内容、表达、结构任一维度的扣分之前，先定位至少两处具体证据"
                "（如错别字位置、病句内容、结构缺陷描述等），"
                "并核对程度是否达到“严重”",
                "",
            ]
        )
        self.assertEqual(validate_optimizer_edit(self.before, after), [])

    def test_adjacent_dimension_hard_limit_in_body_is_rejected(self):
        for bullet in ("- 内容分不得低于 6 分", "- 语言分不低于 5 分", "- 结构分至少 4 分"):
            with self.subTest(bullet=bullet):
                after = self.before + "\n" + "\n".join(
                    ["**打分前自查**", "", bullet, ""]
                )
                self.assertIn("R5", codes(validate_optimizer_edit(self.before, after)))

    def test_changing_top_level_headings_is_rejected(self):
        after = self.before.replace("## 注意事项", "## 注意事项\n\n## 新增章节")
        self.assertIn("R6", codes(validate_optimizer_edit(self.before, after)))

    def test_third_level_header_is_rejected(self):
        after = self.before.replace("**评分原则**：", "### 评分原则\n\n")
        violations = validate_optimizer_edit(self.before, after)
        self.assertIn("R7", codes(violations))

    def test_bold_point_budget_is_enforced(self):
        extra = [f"**操作分点{i}**" for i in range(MAX_BOLD_SECTIONS)]
        after = make_prompt(extra_sections=extra)
        self.assertIn("BUDGET", codes(validate_optimizer_edit(self.before, after)))

    def test_single_iteration_character_budget_is_enforced(self):
        after = self.before + "\n" + ("补充说明" * 400)
        self.assertIn("BUDGET", codes(validate_optimizer_edit(self.before, after)))

    def test_parse_bold_sections_ignores_points_outside_the_note_section(self):
        text = self.before + "\n## 其它\n\n**外部加粗**：不应计入\n"
        names = [section.name for section in parse_bold_sections(text)]
        self.assertNotIn("外部加粗", names)


class SafeParseIntTest(unittest.TestCase):
    def setUp(self):
        self.scorer = BatchEssayScorer()

    def test_clean_integer_is_accepted(self):
        self.assertEqual(self.scorer._safe_parse_int("6"), 6)
        self.assertEqual(self.scorer._safe_parse_int(" 7 "), 7)

    def test_half_score_is_rejected_instead_of_becoming_zero(self):
        # score_min is 0, so a silent fallback to 0 would pass _validate_score
        # and record a fabricated zero score.
        self.assertIsNone(self.scorer._safe_parse_int("6.5"))
        self.assertFalse(self.scorer._validate_score(self.scorer._safe_parse_int("6.5")))

    def test_malformed_score_is_rejected(self):
        for value in ("6分", "--", "", None, "六"):
            with self.subTest(value=value):
                self.assertIsNone(self.scorer._safe_parse_int(value))

    def test_missing_score_field_is_not_silently_zero(self):
        block = (
            "===ESSAY_START===\n"
            "ID:1\n"
            "EXPRESSION:6\n"
            "STRUCTURE:5\n"
            "COMMENT_START\n评语\nCOMMENT_END\n"
        )
        parsed = self.scorer._parse_single_essay(block)
        self.assertIsNone(parsed["content"])
        self.assertFalse(self.scorer._validate_score(parsed["content"]))

    def test_zero_remains_a_legitimate_score(self):
        self.assertEqual(self.scorer._safe_parse_int("0"), 0)
        self.assertTrue(self.scorer._validate_score(0))


class OptimizerPromptContractTest(unittest.TestCase):
    """optimizer 提示词中的词表必须与校验器同源（§13.3 单一来源）。"""

    @staticmethod
    def _analysis():
        return {
            "total_badcases": 23,
            "severe_count": 19,
            "soft_count": 4,
            "bias_distribution": {"strict": 23, "lenient": 0},
            "avg_bias_score": -2.06,
            "priority_dimension": "expression",
            "dimension_analysis": {},
        }

    def test_contract_block_renders_every_matcher_word(self):
        from prompt_optimizer import PromptOptimizer

        block = PromptOptimizer().contract_block()
        for word in (
            UNAMBIGUOUS_RESTRICTION_KEYWORDS
            + AMBIGUOUS_RESTRICTION_KEYWORDS
            + OPERATIONAL_NAME_MARKERS
        ):
            self.assertIn(word, block)

    def test_system_prompt_has_no_unrendered_placeholder(self):
        from prompt_optimizer import PromptOptimizer

        request = PromptOptimizer().build_api_request(
            "## 注意事项\n\n**评分原则**：先划档。\n",
            self._analysis(),
            [],
        )
        self.assertNotIn("__CONTRACT_BLOCK__", request["system"])
        self.assertNotIn("__BUDGET_BLOCK__", request["system"])
        self.assertIn("Machine-checked word lists", request["system"])

    def test_previous_rejection_reasons_are_fed_back_on_retry(self):
        from prompt_optimizer import PromptOptimizer

        reason = "R5: 新增分点 **评分前锚定校准** 属维度评分限制类"
        request = PromptOptimizer().build_api_request(
            "## 注意事项\n\n**评分原则**：先划档。\n",
            self._analysis(),
            [],
            previous_reasons=[reason],
        )
        content = request["messages"][0]["content"]
        self.assertIn("Your Previous Attempt Was Rejected", content)
        self.assertIn(reason, content)

    def test_first_attempt_has_no_rejection_feedback(self):
        from prompt_optimizer import PromptOptimizer

        request = PromptOptimizer().build_api_request(
            "## 注意事项\n\n**评分原则**：先划档。\n",
            self._analysis(),
            [],
        )
        self.assertNotIn(
            "Your Previous Attempt Was Rejected", request["messages"][0]["content"]
        )

    def test_describe_candidate_records_why_a_reject_is_auditable(self):
        from prompt_optimizer import PromptOptimizer

        before = "## 注意事项\n\n**评分原则**：先划档。\n"
        after = before + "\n**评分前锚定校准**\n\n- 先通读全文再落笔赋分\n"
        described = PromptOptimizer.describe_candidate(before, after)
        self.assertEqual(described["added_sections"], ["评分前锚定校准"])
        self.assertIn("先通读全文", described["added_section_bodies"]["评分前锚定校准"])


if __name__ == "__main__":
    unittest.main()
