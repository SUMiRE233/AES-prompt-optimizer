import json
import tempfile
import unittest
from pathlib import Path

from rule_integration_engine import RuleIntegrationEngine


class RuleIntegrationTransactionTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.source = root / "consolidated.json"
        self.prompt = root / "prompt_meta.md"
        self.output_prompt = root / "candidate_meta.md"
        self.injected = root / "injected.json"

        self.payload = {
            "iteration": 3,
            "stats_summary": {
                "content": {
                    "severe_count": 1,
                    "soft_count": 0,
                    "total_outliers": 1,
                }
            },
            "compiled_rules": {
                "content": {
                    "feasible_rules": [
                        {
                            "trigger_condition": "specific content condition",
                            "scoring_adjustment": "adjust content score locally",
                            "counter_examples": "not applicable otherwise",
                            "confidence": 0.9,
                            "confidence_level": "trusted",
                            "evidence_index_type": "global_index",
                            "evidence_count": 2,
                            "safe_for_global_bias": True,
                            "evidence": {
                                "outlier_indices": [0],
                                "normal_indices": [1],
                            },
                            "should_be_injected_at": "**内容分特殊情形**",
                            "expected_scope": ["content"],
                        }
                    ]
                },
                "expression": {"feasible_rules": []},
                "structure": {"feasible_rules": []},
            },
        }
        self.write_json(self.source, self.payload)
        self.prompt.write_text(
            "## 注意事项\n\n**内容分特殊情形**\n\n原有规则\n\n"
            "**表达分特殊情形**\n\n原有规则\n\n"
            "**结构分特殊情形**\n\n原有规则\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def write_json(path, payload):
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def read_json(path):
        return json.loads(path.read_text(encoding="utf-8"))

    def build_engine(self):
        return RuleIntegrationEngine(
            source_file=str(self.source),
            prompt_file=str(self.prompt),
            output_prompt_file=str(self.output_prompt),
            injected_file=str(self.injected),
            target_dimension="content",
            defer_commit=True,
        )

    def test_deferred_run_does_not_consume_rule_until_commit(self):
        engine = self.build_engine()

        report = engine.run()

        self.assertEqual(report["commit_status"], "pending")
        self.assertTrue(self.output_prompt.exists())
        self.assertEqual(
            len(self.read_json(self.source)["compiled_rules"]["content"]["feasible_rules"]),
            1,
        )
        self.assertFalse(self.injected.exists())
        self.assertEqual(
            report["inserted_rules"][0]["evidence_index_type"],
            "global_index",
        )

        engine.commit()

        self.assertEqual(
            len(self.read_json(self.source)["compiled_rules"]["content"]["feasible_rules"]),
            0,
        )
        self.assertEqual(len(self.read_json(self.injected)["injected_rules"]), 1)

    def test_rollback_preserves_rule_pool_and_injected_log(self):
        engine = self.build_engine()
        engine.run()

        engine.rollback()

        self.assertEqual(
            len(self.read_json(self.source)["compiled_rules"]["content"]["feasible_rules"]),
            1,
        )
        self.assertFalse(self.injected.exists())


if __name__ == "__main__":
    unittest.main()
