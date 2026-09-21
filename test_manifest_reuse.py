"""G1–G4 验收补口（大纲 §9.2 / §十 第 9 步 / §11）。

- G1：manifest 记录**每版输入/输出文件 hash** 与**每版校验结果**。
- G2：旧 run manifest 与当前身份不一致时**拒绝复用**旧产物。
- G3/G4：`save_final_prompt` 在晋升 final 前**再执行一次完整结构契约检查**。

全部测试不发起任何 API 调用。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline_entry as pe
import run_manifest as rm


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


def prompt_missing_expression_special_case():
    return compliant_prompt().replace("**表达分特殊情形**", "**语言分评价范围**")


class VersionRecordTest(unittest.TestCase):
    """G1：每版输入/输出 hash 与校验结果。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_record_carries_input_and_output_hashes_plus_contract_result(self):
        Path("meta.md").write_text("meta", encoding="utf-8")
        Path("runtime.md").write_text("runtime", encoding="utf-8")
        Path("essays.json").write_text("[]", encoding="utf-8")
        Path("scoring.json").write_text("[]", encoding="utf-8")

        record = rm.build_version_record(
            iteration=2,
            prompt_meta=Path("meta.md"),
            runtime_prompt=Path("runtime.md"),
            contract_violations=[],
            inputs=[Path("essays.json")],
            outputs=[Path("scoring.json")],
        )

        self.assertEqual(record["iteration"], 2)
        self.assertTrue(record["contract_ok"])
        self.assertEqual(record["contract_violations"], [])
        self.assertEqual(
            [item["name"] for item in record["inputs"]], ["meta.md", "essays.json"]
        )
        self.assertEqual(
            [item["name"] for item in record["outputs"]], ["runtime.md", "scoring.json"]
        )
        for item in record["inputs"] + record["outputs"]:
            self.assertEqual(len(item["sha256"]), 64)
            self.assertGreater(item["bytes"], 0)

    def test_missing_file_is_recorded_not_silently_dropped(self):
        Path("meta.md").write_text("meta", encoding="utf-8")
        Path("runtime.md").write_text("runtime", encoding="utf-8")
        record = rm.build_version_record(
            iteration=0,
            prompt_meta=Path("meta.md"),
            runtime_prompt=Path("runtime.md"),
            contract_violations=[],
            outputs=[Path("train_scoring_results0.json")],
        )
        missing = [item for item in record["outputs"] if item.get("missing")]
        self.assertEqual([item["name"] for item in missing], ["train_scoring_results0.json"])
        self.assertIsNone(missing[0]["sha256"])

    def test_violations_are_recorded_and_flip_contract_ok(self):
        Path("meta.md").write_text("meta", encoding="utf-8")
        Path("runtime.md").write_text("runtime", encoding="utf-8")
        record = rm.build_version_record(
            iteration=1,
            prompt_meta=Path("meta.md"),
            runtime_prompt=Path("runtime.md"),
            contract_violations=["IN-2: 缺少 expression 维的“特殊情形”分点"],
        )
        self.assertFalse(record["contract_ok"])
        self.assertEqual(len(record["contract_violations"]), 1)

    def test_attach_upserts_by_iteration_and_keeps_order(self):
        manifest_path = Path("run_manifest_run_x.json")
        manifest_path.write_text(json.dumps({"run_id": "run_x"}), encoding="utf-8")
        for iteration in (2, 0, 1, 2):
            rm.attach_version_record(
                manifest_path, {"iteration": iteration, "contract_ok": True}
            )
        stored = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual([item["iteration"] for item in stored["versions"]], [0, 1, 2])
        self.assertEqual(stored["run_id"], "run_x", "不得丢掉 manifest 的其余字段")

    def test_pipeline_records_every_scored_version(self):
        Path("optimized_prompt2_meta.md").write_text(compliant_prompt(), encoding="utf-8")
        Path("optimized_prompt2.md").write_text(compliant_prompt(), encoding="utf-8")
        Path("train_essays.json").write_text("[]", encoding="utf-8")
        Path("origin_scoring_results.json").write_text("[]", encoding="utf-8")
        Path("train_scoring_results2.json").write_text("[]", encoding="utf-8")
        Path("aes_badcases2.json").write_text("{}", encoding="utf-8")
        manifest_path = Path("run_manifest_run_y.json")
        manifest_path.write_text(json.dumps({"run_id": "run_y", "versions": []}), encoding="utf-8")

        record = pe.record_version_in_manifest(2, manifest_path)

        stored = json.loads(manifest_path.read_text(encoding="utf-8"))["versions"]
        self.assertEqual([item["iteration"] for item in stored], [2])
        self.assertTrue(record["contract_ok"])
        names = [item["name"] for item in stored[0]["inputs"]] + [
            item["name"] for item in stored[0]["outputs"]
        ]
        for expected in (
            "optimized_prompt2_meta.md",
            "optimized_prompt2.md",
            "train_scoring_results2.json",
            "aes_badcases2.json",
        ):
            self.assertIn(expected, names)


class ManifestReuseGuardTest(unittest.TestCase):
    """G2：旧 manifest 身份与当前不一致时拒绝复用。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)
        Path("origin.json").write_text("[]", encoding="utf-8")
        Path("origin_prompt_meta.md").write_text(compliant_prompt(), encoding="utf-8")
        Path("origin_prompt.md").write_text(compliant_prompt(), encoding="utf-8")
        Path("train_essays.json").write_text('[{"index": 0}]', encoding="utf-8")
        Path("test_essays.json").write_text('[{"index": 1}]', encoding="utf-8")

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _current(self):
        return pe.build_run_manifest("origin.json", pe.MAX_AUTO_VERSION)[0]

    def test_identity_excludes_run_id_and_version_history(self):
        manifest = self._current()
        manifest["run_id"] = "run_a"
        manifest["versions"] = [{"iteration": 0}]
        identity = rm.manifest_identity(manifest)
        self.assertNotIn("run_id", identity)
        self.assertNotIn("versions", identity)
        self.assertIn("models", identity)
        self.assertIn("split", identity)

    def test_compatible_manifest_passes(self):
        current = self._current()
        Path("m.json").write_text(json.dumps(current), encoding="utf-8")
        rm.assert_manifest_compatible(Path("m.json"), current)

    def test_model_change_is_refused(self):
        current = self._current()
        stale = dict(current)
        stale["models"] = {"scoring": "some-other-model", "optimizer": "x"}
        Path("m.json").write_text(json.dumps(stale), encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            rm.assert_manifest_compatible(Path("m.json"), current)
        self.assertIn("models", str(ctx.exception))

    def test_threshold_change_is_refused(self):
        current = self._current()
        stale = json.loads(json.dumps(current))
        stale["thresholds"]["b_severe_abs_bias"] = 99
        Path("m.json").write_text(json.dumps(stale), encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            rm.assert_manifest_compatible(Path("m.json"), current)
        self.assertIn("thresholds", str(ctx.exception))

    def test_origin_data_change_is_refused(self):
        current = self._current()
        Path("m.json").write_text(json.dumps(current), encoding="utf-8")
        Path("origin.json").write_text("[1]", encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            rm.assert_manifest_compatible(Path("m.json"), self._current())
        self.assertIn("files", str(ctx.exception))

    def test_split_index_change_is_refused(self):
        current = self._current()
        stale = json.loads(json.dumps(current))
        stale["split"]["train"]["indices"] = [999]
        Path("m.json").write_text(json.dumps(stale), encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            rm.assert_manifest_compatible(Path("m.json"), current)
        self.assertIn("split", str(ctx.exception))

    def test_latest_run_manifest_picks_the_last(self):
        self.assertIsNone(pe.latest_run_manifest())
        for name in ("run_manifest_run_20200101T000000.json", "run_manifest_run_20200102T000000.json"):
            Path(name).write_text("{}", encoding="utf-8")
        self.assertEqual(
            pe.latest_run_manifest().name, "run_manifest_run_20200102T000000.json"
        )


class PipelineReuseGuardWiringTest(unittest.TestCase):
    """G2 的真实接线：`run_pipeline` 的 resume 分支必须执行身份核对。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)
        Path("origin.json").write_text("[]", encoding="utf-8")
        Path("origin_prompt_meta.md").write_text(compliant_prompt(), encoding="utf-8")
        Path("origin_prompt.md").write_text(compliant_prompt(), encoding="utf-8")
        Path("train_essays.json").write_text('[{"index": 0}]', encoding="utf-8")
        Path("test_essays.json").write_text('[{"index": 1}]', encoding="utf-8")

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_resume_refuses_before_any_scoring_when_identity_differs(self):
        stale = pe.build_run_manifest("origin.json", pe.MAX_AUTO_VERSION)[0]
        stale["models"] = {"scoring": "a-different-model", "optimizer": "x"}
        Path("run_manifest_run_20200101T000000.json").write_text(
            json.dumps(stale), encoding="utf-8"
        )

        with mock.patch.object(pe, "run_train_iteration") as scoring, \
             mock.patch.object(pe, "load_protocol_state") as state_loader:
            with self.assertRaises(RuntimeError) as ctx:
                pe.run_pipeline("origin.json", resume_from=1, max_version=6)

        self.assertIn("models", str(ctx.exception))
        scoring.assert_not_called()
        state_loader.assert_not_called()

    def _current_manifest(self):
        return pe.build_run_manifest("origin.json", pe.MAX_AUTO_VERSION)[0]

    def _store(self, manifest, name="run_manifest_run_20200101T000000.json"):
        Path(name).write_text(json.dumps(manifest), encoding="utf-8")

    def _resume_to_final(self):
        """带全套替身把 resume 流程跑到 final（不触碰网络）。"""
        state = pe.new_protocol_state()
        with mock.patch.object(
            pe, "load_protocol_state", return_value=state
        ), mock.patch.object(
            pe, "version_review",
            return_value={"iteration": 1, "contract_ok": True,
                          "contract_violations": []},
        ), mock.patch.object(pe, "print_version_review"), \
             mock.patch.object(
                 pe, "version_mean_counts",
                 return_value={"severe": 5.0, "soft": 5.0, "runs": 1, "q": 17.5},
             ), mock.patch.object(
                 pe, "decide_iteration",
                 return_value=pe.IterationDecision("stop", "cap_reached", (1, 2)),
             ), mock.patch.object(
                 pe, "resolve_decision",
                 side_effect=lambda decision, iteration, origin_file, state: (
                     decision, state, []),
             ), mock.patch.object(
                 pe, "run_validation_and_select", return_value=1
             ), mock.patch.object(pe, "record_version_in_manifest"), \
             mock.patch.object(pe, "save_final_prompt"):
            return pe.run_pipeline("origin.json", resume_from=1, max_version=6)

    def test_resume_proceeds_when_identity_matches(self):
        self._store(self._current_manifest())
        self.assertEqual(self._resume_to_final(), pe.prompt_meta_path(1))

    def test_resume_migrates_policy_only_changes_and_records_them(self):
        stale = self._current_manifest()
        stale["protocol"] = {"legacy": True}
        stale["stop_policy"] = {"rule": "legacy"}
        self._store(stale)

        result = self._resume_to_final()

        self.assertEqual(result, pe.prompt_meta_path(1))
        stored = json.loads(
            Path("run_manifest_run_20200101T000000.json").read_text(encoding="utf-8")
        )
        current = self._current_manifest()
        self.assertEqual(stored["protocol"], current["protocol"])
        migrations = stored.get("policy_migrations", [])
        self.assertEqual(len(migrations), 1)
        self.assertEqual(
            sorted(migrations[0]["fields"]), ["protocol", "stop_policy"]
        )

    def test_resume_at_the_cap_is_allowed_for_finalisation(self):
        """上限处 resume：边界决策直接停止，进入 validation/final（不再生成新版本）。"""
        self._store(self._current_manifest())
        with mock.patch.object(
            pe, "load_protocol_state", return_value=pe.new_protocol_state()
        ), mock.patch.object(
            pe, "version_review",
            return_value={"iteration": 6, "contract_ok": True,
                          "contract_violations": []},
        ), mock.patch.object(pe, "print_version_review"), \
             mock.patch.object(
                 pe, "version_mean_counts",
                 return_value={"severe": 4.0, "soft": 4.0, "runs": 1, "q": 14.0},
             ), mock.patch.object(
                 pe, "decide_iteration",
                 return_value=pe.IterationDecision("stop", "cap_reached", (5, 6)),
             ), mock.patch.object(
                 pe, "resolve_decision",
                 side_effect=lambda decision, iteration, origin_file, state: (
                     decision, state, []),
             ), mock.patch.object(
                 pe, "run_validation_and_select", return_value=6
             ), mock.patch.object(pe, "record_version_in_manifest"), \
             mock.patch.object(pe, "save_final_prompt"):
            result = pe.run_pipeline("origin.json", resume_from=6, max_version=6)

        self.assertEqual(result, pe.prompt_meta_path(6))


class FinalContractGuardTest(unittest.TestCase):
    """G3/G4：final 晋升前必须再执行一次完整结构契约检查。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_final_meta_satisfies_the_three_special_case_contract(self):
        from prompt_structure_contract import validate_input_contract

        Path("optimized_prompt3_meta.md").write_text(compliant_prompt(), encoding="utf-8")
        Path("optimized_prompt3.md").write_text(compliant_prompt(), encoding="utf-8")

        pe.save_final_prompt("optimized_prompt3_meta.md", 3)

        self.assertTrue(Path(pe.FINAL_META).is_file())
        self.assertTrue(Path(pe.FINAL_PROMPT).is_file())
        self.assertEqual(validate_input_contract(compliant_prompt()), [])

    def test_non_compliant_final_is_refused_and_not_promoted(self):
        from prompt_structure_contract import PromptContractError

        Path("optimized_prompt3_meta.md").write_text(
            prompt_missing_expression_special_case(), encoding="utf-8"
        )
        Path("optimized_prompt3.md").write_text(
            prompt_missing_expression_special_case(), encoding="utf-8"
        )

        with self.assertRaises(PromptContractError) as ctx:
            pe.save_final_prompt("optimized_prompt3_meta.md", 3)

        self.assertIn("IN-2", str(ctx.exception))
        self.assertFalse(Path(pe.FINAL_META).exists(), "不合规的 final 不得被晋升")
        self.assertFalse(Path(pe.FINAL_PROMPT).exists())

    def test_missing_target_is_a_no_op(self):
        pe.save_final_prompt("does_not_exist_meta.md", 9)
        self.assertFalse(Path(pe.FINAL_META).exists())


if __name__ == "__main__":
    unittest.main()
