import tempfile
import unittest
from pathlib import Path

from project_checks import CheckFailure, check_source_hygiene, index_rows, validate_scoring_artifact, validate_subset


class ProjectChecksTest(unittest.TestCase):
    def setUp(self):
        self.origin_rows = [
            {"index": 10, "essay": "alpha", "teacher": {"content": 5}},
            {"index": 20, "essay": "beta", "teacher": {"content": 6}},
        ]
        self.origin = index_rows(self.origin_rows, "origin")

    def test_duplicate_global_index_is_rejected(self):
        with self.assertRaises(CheckFailure):
            index_rows([self.origin_rows[0], self.origin_rows[0]], "duplicate")

    def test_subset_uses_global_index_and_text_integrity(self):
        self.assertEqual(
            validate_subset([{"index": 20, "essay": "beta"}], self.origin, "subset"),
            {20},
        )
        with self.assertRaises(CheckFailure):
            validate_subset([{"index": 20, "essay": "wrong"}], self.origin, "subset")

    def test_scoring_teacher_pairing_is_checked(self):
        row = {
            "index": 10,
            "essay": "alpha",
            "teacher": {"content": 999},
            "AI": {"content": 5, "expression": 5, "structure": 5},
        }
        with self.assertRaises(CheckFailure):
            validate_scoring_artifact([row], self.origin, "scores")

    def test_credential_shape_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.py"
            credential = "sk" + "-exampleCredential123"
            path.write_text(f'key = "{credential}"', encoding="utf-8")
            with self.assertRaises(CheckFailure):
                check_source_hygiene(Path(temp_dir))


if __name__ == "__main__":
    unittest.main()
