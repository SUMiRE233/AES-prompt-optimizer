"""运行前置检查与 run manifest 的测试（大纲 §9.1 / §9.2 / §11）。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline_entry
import run_manifest


def write_essays(path, indices):
    path.write_text(
        json.dumps([{"index": i, "essay": f"e{i}"} for i in indices]),
        encoding="utf-8",
    )


# 构造 credential-形状的字符串用于负向测试；拼装以避免在源码里留下字面量。
CREDENTIAL_SHAPED = "sk" + "-" + "a" * 14


class CleanDirectoryGuardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original = os.getcwd()
        self.addCleanup(os.chdir, self.original)
        os.chdir(self.temp.name)

    def test_clean_directory_passes(self):
        pipeline_entry.assert_clean_working_directory()

    def test_stale_scoring_artifact_blocks_startup(self):
        Path("test_scoring_results1.json").write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "残留"):
            pipeline_entry.assert_clean_working_directory()

    def test_stale_iteration_history_blocks_startup(self):
        Path("iteration_history.json").write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "archive_run.py"):
            pipeline_entry.assert_clean_working_directory()

    def test_stale_prompt_variant_blocks_startup(self):
        Path("optimized_prompt2_meta.md").write_text("x", encoding="utf-8")
        with self.assertRaises(SystemExit):
            pipeline_entry.assert_clean_working_directory()


class ExperimentManifestTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original = os.getcwd()
        self.addCleanup(os.chdir, self.original)
        os.chdir(self.temp.name)

    def build(self):
        Path("origin.json").write_text("[]", encoding="utf-8")
        Path("meta.md").write_text("m", encoding="utf-8")
        Path("runtime.md").write_text("r", encoding="utf-8")
        write_essays(Path("train_essays.json"), [1, 2, 3])
        write_essays(Path("test_essays.json"), [4, 5])
        return run_manifest.build_experiment_manifest(
            run_id="run_test",
            inputs=[Path("origin.json"), Path("meta.md"), Path("runtime.md")],
            split_files={
                "train": Path("train_essays.json"),
                "validation": Path("test_essays.json"),
            },
            models={"scoring": "m-a", "optimizer": "m-b"},
            thresholds={"b_severe_abs_bias": 1.5},
            stop_policy={"max_version": 6},
            structure_budget={"max_bold_sections": 12},
        )

    def test_records_split_indices_and_hashes(self):
        manifest = self.build()
        self.assertEqual(manifest["run_id"], "run_test")
        self.assertEqual(manifest["split"]["train"]["indices"], [1, 2, 3])
        self.assertEqual(manifest["split"]["train"]["count"], 3)
        self.assertEqual(len(manifest["split"]["train"]["sha256"]), 64)
        self.assertEqual(manifest["split"]["validation"]["indices"], [4, 5])

    def test_records_models_thresholds_and_policy(self):
        manifest = self.build()
        self.assertEqual(manifest["models"], {"optimizer": "m-b", "scoring": "m-a"})
        self.assertEqual(manifest["thresholds"]["b_severe_abs_bias"], 1.5)
        self.assertEqual(manifest["stop_policy"]["max_version"], 6)
        self.assertEqual(manifest["structure_budget"]["max_bold_sections"], 12)

    def test_secret_like_model_value_is_rejected(self):
        self.build()
        with self.assertRaisesRegex(ValueError, "credential"):
            run_manifest.build_experiment_manifest(
                run_id="r",
                inputs=[Path("origin.json")],
                split_files={"train": Path("train_essays.json")},
                models={"scoring": CREDENTIAL_SHAPED},
                thresholds={},
                stop_policy={},
                structure_budget={},
            )

    def test_secret_like_model_role_is_rejected(self):
        self.build()
        with self.assertRaisesRegex(ValueError, "forbidden"):
            run_manifest.build_experiment_manifest(
                run_id="r",
                inputs=[Path("origin.json")],
                split_files={"train": Path("train_essays.json")},
                models={"api_key": "abc"},
                thresholds={},
                stop_policy={},
                structure_budget={},
            )

    def test_written_manifest_contains_no_api_key_field(self):
        Path("origin_scoring_results.json").write_text("[]", encoding="utf-8")
        Path("origin_prompt_meta.md").write_text("m", encoding="utf-8")
        Path("origin_prompt.md").write_text("r", encoding="utf-8")
        write_essays(Path("train_essays.json"), [1, 2])
        write_essays(Path("test_essays.json"), [3])

        with mock.patch.dict(os.environ, {"AES_API_KEY": CREDENTIAL_SHAPED}):
            path = pipeline_entry.write_run_manifest("origin_scoring_results.json", 6)

        text = Path(path).read_text(encoding="utf-8")
        self.assertNotIn(CREDENTIAL_SHAPED, text)
        self.assertNotIn("AES_API_KEY", text)
        payload = json.loads(text)
        self.assertIn("split", payload)
        self.assertEqual(payload["split"]["train"]["indices"], [1, 2])

    def test_write_experiment_manifest_roundtrip(self):
        manifest = self.build()
        output = run_manifest.write_experiment_manifest(manifest, Path("out.json"))
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8"))["run_id"], "run_test"
        )


if __name__ == "__main__":
    unittest.main()
