import unittest

from etype_preference_analyzer import ContrastiveETypeAnalyzer


class ETypePromptConstraintTests(unittest.TestCase):
    def test_rule_ban_precedes_general_constraints(self):
        analyzer = ContrastiveETypeAnalyzer()
        request = analyzer.build_contrastive_analysis_request(
            "## 注意事项\n**结构分特殊情形**：示例",
            "structure",
            [],
            [],
        )
        system = request["system"]
        self.assertEqual(request["max_tokens"], 8192)
        self.assertLess(
            system.index("NON-NEGOTIABLE RULE-WRITING BAN"),
            system.index("CRITICAL CONSTRAINTS"),
        )
        self.assertIn("silently preflight every localized rule", system)
        self.assertIn("do_not_inject", system)

    def test_numeric_score_anchors_are_rejected_deterministically(self):
        analyzer = ContrastiveETypeAnalyzer()
        self.assertTrue(
            analyzer.is_hard_threshold_rule(
                {"scoring_adjustment": "将结构分锚定在70-85区间"}
            )
        )
        self.assertTrue(
            analyzer.is_hard_threshold_rule(
                {"scoring_adjustment": "至少提高1分"}
            )
        )
        self.assertFalse(
            analyzer.is_hard_threshold_rule(
                {"scoring_adjustment": "结合段落衔接与首尾照应谨慎调整"}
            )
        )


if __name__ == "__main__":
    unittest.main()
