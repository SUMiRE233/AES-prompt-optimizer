import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aes_badcase_miner import AESBadcaseMiner
from batch_scoring import BatchEssayScorer


class BatchScoringContractTest(unittest.TestCase):
    def test_saved_ai_scores_do_not_copy_unpredicted_teacher_dimensions(self):
        origin = [
            {
                "index": 7,
                "name": "synthetic",
                "page": "p1",
                "essay": "synthetic essay",
                "teacher": {
                    "content": 6,
                    "expression": 6,
                    "structure": 6,
                    "technique": 2,
                    "length": 3,
                },
            }
        ]
        essays = [{"index": 7, "essay": "synthetic essay"}]
        results = [{"content": 5, "expression": 6, "structure": 7, "comment": "ok"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "scores.json"
            scorer = BatchEssayScorer()
            scorer.output_path = str(output)
            scorer.save_results(results, essays, origin)
            saved = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(
            saved[0]["AI"],
            {"content": 5, "expression": 6, "structure": 7},
        )
        self.assertEqual(saved[0]["teacher"]["technique"], 2)

    def test_remote_call_requires_environment_credential(self):
        scorer = BatchEssayScorer()
        scorer.api_key = None
        with self.assertRaisesRegex(RuntimeError, "AES_API_KEY"):
            scorer.send_api_request({})

    def test_scoring_model_comes_from_environment(self):
        with patch.dict(os.environ, {"AES_SCORING_MODEL": "new-scoring-model"}):
            scorer = BatchEssayScorer()
            with patch.object(scorer, "_save_debug_prompt"):
                request = scorer.build_batch_request(
                    [{"index": 1, "essay": "synthetic"}],
                    "## 注意事项",
                )
        self.assertEqual(request["model"], "new-scoring-model")
        self.assertNotIn("temperature", request)

    def test_scoring_response_skips_thinking_block(self):
        scorer = BatchEssayScorer()
        response = {
            "content": [
                {"type": "thinking", "thinking": "private reasoning"},
                {"type": "text", "text": "visible scoring result"},
            ]
        }
        self.assertEqual(
            scorer.extract_response_text(response),
            "visible scoring result",
        )

    def test_badcase_output_excludes_upstream_deterministic_dimensions(self):
        rows = [
            {
                "index": 7,
                "name": "synthetic",
                "page": "p1",
                "essay": "synthetic essay",
                "teacher": {
                    "content": 6,
                    "expression": 6,
                    "structure": 6,
                    "technique": 2,
                    "length": 3,
                },
                "AI": {"content": 5, "expression": 6, "structure": 7},
            }
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "scores.json"
            output_path = Path(temp_dir) / "badcases.json"
            input_path.write_text(json.dumps(rows), encoding="utf-8")
            result = AESBadcaseMiner(str(input_path)).run(str(output_path))

        self.assertEqual(set(result), {"statistics", "B_bias", "E_residual"})
        self.assertNotIn("C_technique", result["statistics"])
        self.assertNotIn("D_length", result["statistics"])


if __name__ == "__main__":
    unittest.main()
