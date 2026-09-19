"""
E 类残差偏好自迭代流水线入口。

常用方式：
1. 仅查看计划，不调用 API、不写入候选结果：
   python etype_iteration_runner.py --skip-analysis --consolidated etype_analysis/contrastive_consolidated_1.json --score gate

2. 使用已有 E 类分析结果，执行 1 条规则最小化注入，并用 gate 小样本试批改：
   python etype_iteration_runner.py --execute --skip-analysis --consolidated etype_analysis/contrastive_consolidated_1.json --score gate

3. 从 badcase 到 E 类分析、规则注入、gate 试批改完整执行：
   python etype_iteration_runner.py --execute --refresh-badcases --score gate

默认约束：
- 基底 prompt 使用 final_prompt_meta.md。
- 每轮只注入 1 条 feasible rule，降低局部规则污染风险。
- 注入后先生成 injected_prompt_next_meta.md / injected_prompt_next.md，不自动覆盖 final。
- gate 门控失败时，丢弃本轮候选 prompt、gate essays、gate scoring，仅保留 gate_eval 报告。
- gate 门控通过也只进入人工审核；人工确认后再显式使用 --promote-final 覆盖 final。

关键参数说明：
- --execute：真正执行流水线；不加时只做 dry-run 计划预览。
- --skip-analysis：跳过 E 类对比分析，改用已有 consolidated 文件。
- --consolidated：指定已有 E 类分析结果，例如 etype_analysis/contrastive_consolidated_1.json。
- --refresh-badcases：重新从 --scoring-file 挖掘 badcase，覆盖/生成 --badcase-file。
- --prompt-meta：本轮注入的基底 meta prompt，默认 final_prompt_meta.md。
- --scoring-file：训练集评分结果，也是 badcase 挖掘和 gate baseline 的来源。
- --badcase-file：badcase 挖掘结果，默认 aes_badcases3.json。
- --output-meta：规则注入后的候选 meta prompt，默认 injected_prompt_next_meta.md。
- --output-prompt：候选 meta prompt 经 preprocess_prompt 后的实际评分 prompt。
- --injected-log：记录本轮注入了哪条 E 类规则，gate sampler 会用它识别改进维度。
- --dimension：只分析/门控指定维度，可选 content、expression、structure；不填则自动选择。
- --max-rules-per-sub-iteration：每轮最多注入几条规则，当前默认 1。
- --score：试批改范围；none 不评分，gate 跑小样本门控，train 跑训练集，test 跑测试集，both 跑 train+test，gate-and-train 先 gate 再 train。
- --gate-badcase-count：gate 中抽取的 E 类 badcase 数，默认 6。
- --gate-normal-count：gate 中抽取的 normal 样本数，默认 6。
- --gate-min-badcase-improve-rate：badcase 至少改善比例，默认 0.5。
- --gate-min-badcase-improvement：目标维度绝对误差至少下降多少才算改善，默认 0.5 分。
- --gate-max-badcase-worsening：badcase 目标维度允许恶化阈值，默认 0.0 分，即不允许恶化。
- --gate-max-normal-regression：normal 目标维度允许恶化阈值，默认 0.5 分。
- --gate-max-cross-dim-regression：非目标维度允许恶化阈值，默认 0.5 分。
- --gate-eval：gate 门控报告输出路径，默认 etype_analysis/gate_eval_etype_next.json。
- --promote-final：人工审核通过后才使用；将候选 prompt 覆盖为 final_prompt_meta.md / final_prompt.md。
"""

import argparse
import json
import math
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from aes_badcase_miner import AESBadcaseMiner
from batch_scoring import BatchEssayScorer
from etype_preference_analyzer import ContrastiveETypeAnalyzer
from gate_test_sampler import GateTestSampler
from micro_scoring_gate import MicroScoringGate
from preprocess_prompt import PromptPreprocessor
from rule_integration_engine import RuleIntegrationEngine
from pipeline_entry import (
    b_bias_counts_from_badcases,
    b_bias_counts_from_scoring_results,
    scoring_mae,
)


DIMENSIONS = ("content", "expression", "structure")
DEFAULT_ANALYSIS_DIR = "etype_analysis"
DEFAULT_ORIGIN_FILE = "origin_scoring_results.json"
DEFAULT_PROMPT_META = "final_prompt_meta.md"
DEFAULT_SCORING_FILE = "final_train_scoring_results.json"
DEFAULT_BADCASE_FILE = "final_aes_badcases.json"
DEFAULT_OUTPUT_META = "injected_prompt_next_meta.md"
DEFAULT_OUTPUT_PROMPT = "injected_prompt_next.md"
DEFAULT_INJECTED_LOG = "etype_analysis/injected_rules_next.json"
DEFAULT_RUN_REPORT = "etype_analysis/etype_iteration_run_report.json"
DEFAULT_GATE_ESSAYS = "gate_test_essays_etype_next.json"
DEFAULT_GATE_MANIFEST = "gate_test_manifest_etype_next.json"
DEFAULT_GATE_OUTPUT = "gate_scoring_results_etype_next.json"
DEFAULT_GATE_EVAL = "etype_analysis/gate_eval_etype_next.json"
DEFAULT_MICRO_ESSAYS = "micro_scoring_essays_etype_next.json"
DEFAULT_MICRO_MANIFEST = "etype_analysis/micro_scoring_manifest_etype_next.json"
DEFAULT_MICRO_OUTPUT = "micro_scoring_results_etype_next.json"
DEFAULT_MICRO_EVAL = "etype_analysis/micro_scoring_eval_etype_next.json"
DEFAULT_TRAIN_BADCASE = "etype_analysis/train_badcases_etype_next.json"
DEFAULT_TRAIN_EVAL = "etype_analysis/full_train_eval_etype_next.json"
DEFAULT_VALIDATION_BASELINE = "test_scoring_results4.json"
DEFAULT_VALIDATION_EVAL = "etype_analysis/validation_eval_etype_next.json"
REJECTED_CANDIDATE_FILES = (
    "output_meta",
    "output_prompt",
    "micro_essays",
    "micro_manifest",
    "micro_output",
    "gate_essays",
    "gate_manifest",
    "gate_output",
    "train_output",
    "test_output",
    "train_badcase",
)


def required_improvement_count(sample_count: int, minimum_rate: float) -> int:
    if sample_count <= 0:
        return 0
    return min(sample_count, max(1, math.ceil(sample_count * minimum_rate)))


class ETypeIterationRunner:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.analysis_dir = Path(args.analysis_dir)
        self.actions: List[Dict[str, Any]] = []
        self.rule_engine: Optional[RuleIntegrationEngine] = None
        self.rule_report: Optional[Dict[str, Any]] = None

    def log_action(self, step: str, status: str, **details: Any) -> None:
        record = {
            "step": step,
            "status": status,
            "details": details,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
        self.actions.append(record)
        detail_text = f" {details}" if details else ""
        print(f"[{status.upper()}] {step}{detail_text}")

    def require_file(self, path: str, purpose: str) -> None:
        if not Path(path).exists():
            raise FileNotFoundError(f"Missing {purpose}: {path}")

    def latest_consolidated_file(self) -> Optional[Path]:
        if not self.analysis_dir.exists():
            return None
        candidates = list(self.analysis_dir.glob("contrastive_consolidated_*.json"))
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def next_analysis_iteration(self) -> int:
        latest = self.latest_consolidated_file()
        if latest is None:
            return 1
        stem = latest.stem.rsplit("_", 1)[-1]
        try:
            return int(stem) + 1
        except ValueError:
            return 1

    def planned_consolidated_file(self) -> Path:
        return self.analysis_dir / f"contrastive_consolidated_{self.next_analysis_iteration()}.json"

    def resolve_consolidated_source(self) -> Path:
        if self.args.consolidated:
            return Path(self.args.consolidated)
        latest = self.latest_consolidated_file()
        if latest is None:
            raise FileNotFoundError(
                "No consolidated E-type analysis file found. Run with --execute "
                "without --skip-analysis, or pass --consolidated <path>."
            )
        return latest

    def run_badcase_mining(self) -> None:
        if not self.args.refresh_badcases and Path(self.args.badcase_file).exists():
            self.log_action(
                "badcase-mining",
                "skipped",
                reason="badcase file already exists",
                file=self.args.badcase_file,
            )
            return

        if not self.args.execute:
            self.log_action(
                "badcase-mining",
                "planned",
                input=self.args.scoring_file,
                output=self.args.badcase_file,
            )
            return

        self.require_file(self.args.scoring_file, "scoring results for badcase mining")
        miner = AESBadcaseMiner(self.args.scoring_file)
        miner.run(self.args.badcase_file)
        self.log_action(
            "badcase-mining",
            "done",
            input=self.args.scoring_file,
            output=self.args.badcase_file,
        )

    def run_contrastive_analysis(self) -> Optional[Path]:
        if self.args.skip_analysis:
            source = self.resolve_consolidated_source()
            self.log_action("contrastive-analysis", "skipped", source=str(source))
            return source

        planned_output = self.planned_consolidated_file()

        if not self.args.execute:
            self.log_action(
                "contrastive-analysis",
                "planned",
                badcase=self.args.badcase_file,
                all_data=self.args.scoring_file,
                prompt=self.args.prompt_meta,
                dimension=self.args.dimension or "all active",
                expected_output=str(planned_output),
            )
            return planned_output

        self.require_file(self.args.badcase_file, "E-type badcase file")
        self.require_file(self.args.scoring_file, "all scoring data file")
        self.require_file(self.args.prompt_meta, "prompt meta file")
        analyzer = ContrastiveETypeAnalyzer()
        analyzer.BADCASE_FILE = self.args.badcase_file
        analyzer.ALL_DATA_FILE = self.args.scoring_file
        analyzer.SCORING_PROMPT_PATH = self.args.prompt_meta
        analyzer.OUTPUT_DIR = self.args.analysis_dir
        analyzer.ITERATION_LOG = self.args.iteration_log
        analyzer.CONTRASTIVE_LOG = self.args.contrastive_log
        analyzer.MAX_OUTLIER_SAMPLES = self.args.max_outlier_samples
        analyzer.MAX_NORMAL_SAMPLES = self.args.max_normal_samples
        analyzer.NORMAL_ZSCORE_THRESHOLD = self.args.normal_zscore_threshold
        analyzer.NORMAL_DIFF_THRESHOLD = self.args.normal_diff_threshold
        analyzer.run(dimension=self.args.dimension)

        output = self.latest_consolidated_file()
        self.log_action(
            "contrastive-analysis",
            "done",
            output=str(output) if output else None,
        )
        return output

    def run_rule_injection(self, source: Path) -> None:
        if not self.args.execute:
            self.log_action(
                "rule-injection",
                "planned",
                source=str(source),
                prompt=self.args.prompt_meta,
                output_prompt_meta=self.args.output_meta,
                injected_log=self.args.injected_log,
                max_rules=self.args.max_rules_per_sub_iteration,
            )
            return

        self.require_file(str(source), "consolidated E-type analysis")
        self.require_file(self.args.prompt_meta, "prompt meta file")
        engine = RuleIntegrationEngine(
            source_file=str(source),
            prompt_file=self.args.prompt_meta,
            output_prompt_file=self.args.output_meta,
            source_output_file=self.args.consolidated_output,
            injected_file=self.args.injected_log,
            major_iteration=self.args.major_iteration,
            sub_iteration=self.args.sub_iteration,
            max_rules_per_sub_iteration=self.args.max_rules_per_sub_iteration,
            target_dimension=self.args.dimension,
            defer_commit=True,
        )
        report = engine.run()
        self.rule_engine = engine
        self.rule_report = report
        self.log_action("rule-injection", "done", report=report)

    def commit_rule_injection(self) -> None:
        if not self.args.execute:
            self.log_action("rule-injection-commit", "planned")
            return
        if self.rule_engine is None:
            raise RuntimeError("Rule injection was not prepared.")
        report = self.rule_engine.commit()
        self.log_action("rule-injection-commit", "done", report=report)

    def rollback_rule_injection(self) -> None:
        if self.rule_engine is not None:
            self.rule_engine.rollback()
        self.log_action("rule-injection-rollback", "done")

    def preprocess_output_prompt(self) -> None:
        if self.args.skip_preprocess:
            self.log_action("prompt-preprocess", "skipped")
            return

        if not self.args.execute:
            self.log_action(
                "prompt-preprocess",
                "planned",
                input=self.args.output_meta,
                output=self.args.output_prompt,
            )
            return

        self.require_file(self.args.output_meta, "injected prompt meta file")
        PromptPreprocessor().process(
            input_path=self.args.output_meta,
            output_path=self.args.output_prompt,
        )
        self.log_action(
            "prompt-preprocess",
            "done",
            input=self.args.output_meta,
            output=self.args.output_prompt,
        )

    def score_candidate(self, label: str, essays_file: str, output_file: str) -> None:
        if not self.args.execute:
            self.log_action(
                "scoring",
                "planned",
                split=label,
                prompt=self.args.output_prompt,
                essays=essays_file,
                output=output_file,
            )
            return

        self.require_file(self.args.output_prompt, "processed scoring prompt")
        self.require_file(essays_file, f"{label} essays file")
        self.require_file(self.args.origin_file, "origin scoring file")
        candidate_output = Path(output_file)
        if candidate_output.exists():
            candidate_output.unlink()
        scorer = BatchEssayScorer()
        scorer.prompt_path = self.args.output_prompt
        scorer.essays_path = essays_file
        scorer.output_path = output_file
        scorer.origin_data_path = self.args.origin_file
        scorer.run()
        self.require_file(output_file, f"fresh {label} scoring output")
        self.log_action("scoring", "done", split=label, output=output_file)

    @staticmethod
    def e_counts(badcases: Dict[str, Any], dimension: str) -> Dict[str, int]:
        payload = badcases["statistics"]["E_residual"][dimension]
        severe = int(payload["severe_count"])
        soft = int(payload["soft_count"])
        return {"severe": severe, "soft": soft, "score": 5 * severe + soft}

    def evaluate_full_train(self) -> bool:
        if not self.args.execute:
            self.log_action("full-train-eval", "planned", output=self.args.train_eval)
            return True
        self.require_file(self.args.train_output, "candidate full-train scoring")
        self.require_file(self.args.badcase_file, "baseline badcases")
        baseline = self.load_json(self.args.badcase_file)
        candidate = AESBadcaseMiner(self.args.train_output).run(self.args.train_badcase)
        dimension = self.rule_report.get("dimension") if self.rule_report else self.args.dimension
        baseline_target = self.e_counts(baseline, dimension)
        candidate_target = self.e_counts(candidate, dimension)
        baseline_b = b_bias_counts_from_badcases(baseline)
        candidate_b = b_bias_counts_from_badcases(candidate)
        new_dimensions = []
        for other in DIMENSIONS:
            if other == dimension:
                continue
            if self.e_counts(baseline, other)["score"] == 0 and self.e_counts(candidate, other)["score"] > 0:
                new_dimensions.append(other)
        baseline_mae = scoring_mae(self.args.scoring_file)
        candidate_mae = scoring_mae(self.args.train_output)
        passed = (
            candidate_target["severe"] <= baseline_target["severe"]
            and candidate_target["score"] < baseline_target["score"]
            and 5 * candidate_b["severe"] + candidate_b["soft"]
            <= 5 * baseline_b["severe"] + baseline_b["soft"]
            and not new_dimensions
        )
        payload = {
            "gate_passed": passed,
            "dimension": dimension,
            "target_baseline": baseline_target,
            "target_candidate": candidate_target,
            "b_baseline": baseline_b,
            "b_candidate": candidate_b,
            "new_e_dimensions": new_dimensions,
            "mae_baseline": baseline_mae,
            "mae_candidate": candidate_mae,
            "mae_warning": candidate_mae > baseline_mae,
        }
        Path(self.args.train_eval).parent.mkdir(parents=True, exist_ok=True)
        with open(self.args.train_eval, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        if not passed:
            self.discard_rejected_candidate()
        self.log_action("full-train-eval", "done", output=self.args.train_eval, **payload)
        return passed

    def evaluate_validation(self) -> bool:
        if not self.args.execute:
            self.log_action("validation-eval", "planned", output=self.args.validation_eval)
            return True
        self.require_file(self.args.validation_baseline, "B-final validation baseline")
        self.require_file(self.args.test_output, "candidate validation scoring")
        dimension = self.rule_report.get("dimension") if self.rule_report else self.args.dimension
        baseline_rows = self.score_lookup(self.load_json(self.args.validation_baseline))
        candidate_rows = self.score_lookup(self.load_json(self.args.test_output))
        baseline_b = b_bias_counts_from_scoring_results(self.args.validation_baseline)
        candidate_b = b_bias_counts_from_scoring_results(self.args.test_output)
        target_deltas = []
        for index, baseline in baseline_rows.items():
            candidate = candidate_rows[index]
            teacher = float(baseline["teacher"][dimension])
            base_error = abs(float(baseline["AI"][dimension]) - teacher)
            candidate_error = abs(float(candidate["AI"][dimension]) - teacher)
            target_deltas.append(candidate_error - base_error)
        baseline_target_mae = sum(
            abs(float(row["AI"][dimension]) - float(row["teacher"][dimension]))
            for row in baseline_rows.values()
        ) / len(baseline_rows)
        candidate_target_mae = sum(
            abs(float(row["AI"][dimension]) - float(row["teacher"][dimension]))
            for row in candidate_rows.values()
        ) / len(candidate_rows)
        worsened = sum(delta > 0.5 for delta in target_deltas)
        passed = (
            candidate_b["severe"] <= baseline_b["severe"]
            and 5 * candidate_b["severe"] + candidate_b["soft"]
            <= 5 * baseline_b["severe"] + baseline_b["soft"]
            and candidate_target_mae <= baseline_target_mae
            and worsened <= 1
        )
        payload = {
            "gate_passed": passed,
            "dimension": dimension,
            "b_baseline": baseline_b,
            "b_candidate": candidate_b,
            "target_mae_baseline": baseline_target_mae,
            "target_mae_candidate": candidate_target_mae,
            "target_samples_worsened_gt_0_5": worsened,
            "overall_mae_baseline": scoring_mae(self.args.validation_baseline),
            "overall_mae_candidate": scoring_mae(self.args.test_output),
        }
        Path(self.args.validation_eval).parent.mkdir(parents=True, exist_ok=True)
        with open(self.args.validation_eval, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        if not passed:
            self.discard_rejected_candidate()
        self.log_action("validation-eval", "done", output=self.args.validation_eval, **payload)
        return passed

    def run_scoring(self) -> bool:
        if self.args.score == "none":
            self.log_action("scoring", "skipped")
            return True

        if self.args.score in ("gate", "gate-and-train"):
            gate_essays = self.run_gate_sampling()
            self.score_candidate("gate", gate_essays, self.args.gate_output)
            if not self.evaluate_gate():
                return False
            if self.args.score == "gate":
                return True
            self.score_candidate("train", self.args.train_essays, self.args.train_output)
            if not self.evaluate_full_train():
                return False
            self.score_candidate("validation", self.args.test_essays, self.args.test_output)
            return self.evaluate_validation()

        if self.args.score in ("train", "both"):
            self.score_candidate("train", self.args.train_essays, self.args.train_output)
        if self.args.score in ("test", "both"):
            self.score_candidate("validation", self.args.test_essays, self.args.test_output)
        return True

    def run_micro_scoring(self) -> bool:
        if self.args.skip_micro_scoring:
            self.log_action("micro-scoring", "skipped")
            return True

        if not self.args.execute:
            self.log_action(
                "micro-scoring",
                "planned",
                baseline=self.args.scoring_file,
                badcases=self.args.badcase_file,
                injected_rule=self.args.injected_log,
                prompt=self.args.output_prompt,
                essays=self.args.micro_essays,
                candidate=self.args.micro_output,
                evaluation=self.args.micro_eval,
            )
            return True

        self.require_file(self.args.scoring_file, "baseline scoring results for micro scoring")
        self.require_file(self.args.badcase_file, "badcases for micro scoring")
        if self.rule_report is None:
            self.require_file(self.args.injected_log, "injected rule log for micro scoring")
        self.require_file(self.args.output_prompt, "processed candidate prompt for micro scoring")
        self.require_file(self.args.origin_file, "origin scoring data for micro scoring")

        gate = MicroScoringGate(
            baseline_path=self.args.scoring_file,
            badcase_path=self.args.badcase_file,
            injected_rule_path=self.args.injected_log,
            essays_output_path=self.args.micro_essays,
            manifest_output_path=self.args.micro_manifest,
            candidate_path=self.args.micro_output,
            eval_output_path=self.args.micro_eval,
            injected_rules=(
                self.rule_report.get("inserted_rules", [])
                if self.rule_report
                else None
            ),
        )
        manifest = gate.build_manifest()
        self.log_action(
            "micro-sampling",
            "done",
            essays=self.args.micro_essays,
            manifest=self.args.micro_manifest,
            counts=manifest["counts"],
            dimension=manifest["dimension"],
        )

        candidate_output = Path(self.args.micro_output)
        if candidate_output.exists():
            candidate_output.unlink()

        scorer = BatchEssayScorer()
        scorer.prompt_path = self.args.output_prompt
        scorer.essays_path = self.args.micro_essays
        scorer.output_path = self.args.micro_output
        scorer.origin_data_path = self.args.origin_file
        scorer.run()
        self.require_file(self.args.micro_output, "candidate micro scoring results")

        evaluation = gate.evaluate()
        discarded_files = []
        if not evaluation["gate_passed"]:
            discarded_files = self.discard_rejected_candidate()
        self.log_action(
            "micro-scoring",
            "done",
            output=self.args.micro_output,
            evaluation=self.args.micro_eval,
            gate_passed=evaluation["gate_passed"],
            decision=evaluation["decision"],
            weighted_average=evaluation["score"]["weighted_average"],
            violations=len(evaluation["violations"]),
            discarded_files=discarded_files,
        )
        return bool(evaluation["gate_passed"])

    def run_gate_sampling(self) -> str:
        if not self.args.execute:
            self.log_action(
                "gate-sampling",
                "planned",
                train=self.args.scoring_file,
                badcase=self.args.badcase_file,
                injected_log=self.args.injected_log,
                output_essays=self.args.gate_essays,
                manifest=self.args.gate_manifest,
                badcase_count=self.args.gate_badcase_count,
                normal_count=self.args.gate_normal_count,
            )
            return self.args.gate_essays

        self.require_file(self.args.scoring_file, "train scoring results for gate sampling")
        self.require_file(self.args.badcase_file, "badcase file for gate sampling")
        improved_dimension = (
            self.rule_report.get("dimension")
            if self.rule_report
            else self.args.dimension
        )
        if improved_dimension is None:
            self.require_file(self.args.injected_log, "injected rule log for gate sampling")

        sampler = GateTestSampler(
            train_path=self.args.scoring_file,
            badcase_path=self.args.badcase_file,
            injected_rule_path=self.args.injected_log,
            output_essays_path=self.args.gate_essays,
            output_manifest_path=self.args.gate_manifest,
            improved_dimension=improved_dimension,
            badcase_count=self.args.gate_badcase_count,
            normal_count=self.args.gate_normal_count,
        )
        manifest = sampler.run()
        sampler.print_summary(manifest)
        self.log_action(
            "gate-sampling",
            "done",
            output_essays=self.args.gate_essays,
            manifest=self.args.gate_manifest,
            improved_dimension=manifest["improved_dimension"],
            total_count=manifest["total_count"],
        )
        return self.args.gate_essays

    def load_json(self, path: str) -> Any:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def score_lookup(self, rows: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
        return {int(item["index"]): item for item in rows}

    def evaluate_gate(self) -> bool:
        if not self.args.execute:
            self.log_action(
                "gate-eval",
                "planned",
                manifest=self.args.gate_manifest,
                baseline=self.args.scoring_file,
                candidate=self.args.gate_output,
                output=self.args.gate_eval,
            )
            return True

        self.require_file(self.args.gate_manifest, "gate manifest")
        self.require_file(self.args.scoring_file, "baseline scoring results")
        self.require_file(self.args.gate_output, "candidate gate scoring results")

        manifest = self.load_json(self.args.gate_manifest)
        baseline = self.score_lookup(self.load_json(self.args.scoring_file))
        candidate = self.score_lookup(self.load_json(self.args.gate_output))
        dimension = manifest["improved_dimension"]

        rows = []
        badcase_improved = 0
        badcase_worsened = 0
        normal_worsened = 0
        severe_cross_dim_regressions = 0

        for sample in manifest["samples"]:
            index = int(sample["index"])
            base_item = baseline[index]
            cand_item = candidate[index]
            sample_type = sample["sample_type"]

            base_target_abs = abs(base_item["AI"][dimension] - base_item["teacher"][dimension])
            cand_target_abs = abs(cand_item["AI"][dimension] - cand_item["teacher"][dimension])
            target_delta = cand_target_abs - base_target_abs

            dim_details = {}
            for dim in DIMENSIONS:
                base_abs = abs(base_item["AI"][dim] - base_item["teacher"][dim])
                cand_abs = abs(cand_item["AI"][dim] - cand_item["teacher"][dim])
                dim_details[dim] = {
                    "baseline_abs_error": base_abs,
                    "candidate_abs_error": cand_abs,
                    "delta_abs_error": cand_abs - base_abs,
                }
                if dim != dimension and cand_abs - base_abs > self.args.gate_max_cross_dim_regression:
                    severe_cross_dim_regressions += 1

            if sample_type == "badcase":
                if target_delta <= -self.args.gate_min_badcase_improvement:
                    badcase_improved += 1
                if target_delta > self.args.gate_max_badcase_worsening:
                    badcase_worsened += 1
            elif sample_type == "normal":
                if target_delta > self.args.gate_max_normal_regression:
                    normal_worsened += 1

            rows.append(
                {
                    "index": index,
                    "sample_type": sample_type,
                    "dimension": dimension,
                    "target_delta_abs_error": target_delta,
                    "details": dim_details,
                }
            )

        badcase_count = int(manifest["badcase_count"])
        required_badcase_improved = required_improvement_count(
            badcase_count,
            self.args.gate_min_badcase_improve_rate,
        )

        passed = (
            badcase_improved >= required_badcase_improved
            and badcase_worsened <= self.args.gate_max_badcase_worsened_count
            and normal_worsened <= self.args.gate_max_normal_worsened_count
            and severe_cross_dim_regressions <= self.args.gate_max_cross_dim_regression_count
        )
        discarded_files = [] if passed else self.discard_rejected_candidate()

        payload = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "gate_passed": passed,
            "decision": "needs_human_review" if passed else "reject_candidate",
            "discarded_files": discarded_files,
            "criteria": {
                "min_badcase_improve_rate": self.args.gate_min_badcase_improve_rate,
                "min_badcase_improvement": self.args.gate_min_badcase_improvement,
                "max_badcase_worsening": self.args.gate_max_badcase_worsening,
                "max_normal_regression": self.args.gate_max_normal_regression,
                "max_cross_dim_regression": self.args.gate_max_cross_dim_regression,
            },
            "summary": {
                "dimension": dimension,
                "badcase_count": badcase_count,
                "badcase_improved": badcase_improved,
                "required_badcase_improved": required_badcase_improved,
                "badcase_worsened": badcase_worsened,
                "normal_worsened": normal_worsened,
                "severe_cross_dim_regressions": severe_cross_dim_regressions,
            },
            "rows": rows,
        }

        Path(self.args.gate_eval).parent.mkdir(parents=True, exist_ok=True)
        with open(self.args.gate_eval, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        self.log_action(
            "gate-eval",
            "done",
            output=self.args.gate_eval,
            gate_passed=passed,
            decision=payload["decision"],
            discarded_files=discarded_files,
            summary=payload["summary"],
        )
        return passed

    def discard_rejected_candidate(self) -> List[str]:
        discarded = []
        for arg_name in REJECTED_CANDIDATE_FILES:
            path = Path(getattr(self.args, arg_name))
            if not path.exists() or path.is_dir():
                continue
            path.unlink()
            discarded.append(str(path))
        self.log_action(
            "discard-rejected-candidate",
            "done",
            files=discarded,
        )
        return discarded

    def save_run_report(self) -> None:
        report_path = Path(self.args.run_report)
        if not self.args.execute:
            self.log_action("run-report", "planned", output=str(report_path))
            return

        report_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "execute": self.args.execute,
            "arguments": vars(self.args),
            "actions": self.actions,
        }
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        self.log_action("run-report", "done", output=str(report_path))

    def promote_to_final(self) -> None:
        if not self.args.promote_final:
            self.log_action("promote-final", "skipped")
            return

        if not self.args.execute:
            self.log_action(
                "promote-final",
                "planned",
                meta_source=self.args.output_meta,
                prompt_source=self.args.output_prompt,
                final_meta="final_prompt_meta.md",
                final_prompt="final_prompt.md",
            )
            return

        self.require_file(self.args.output_meta, "prompt meta to promote")
        shutil.copy2(self.args.output_meta, "final_prompt_meta.md")
        if Path(self.args.output_prompt).exists():
            shutil.copy2(self.args.output_prompt, "final_prompt.md")
        self.log_action("promote-final", "done")

    def run(self) -> None:
        self.analysis_dir.mkdir(parents=True, exist_ok=True)
        self.log_action("start", "done", mode="execute" if self.args.execute else "dry-run")

        self.run_badcase_mining()
        source = self.run_contrastive_analysis()
        if source is None:
            source = self.resolve_consolidated_source()
        try:
            self.run_rule_injection(source)
        except ValueError as exc:
            if "No feasible_rules for target dimension" not in str(exc):
                raise
            self.log_action(
                "pipeline",
                "stopped",
                reason="no feasible E rule",
                detail=str(exc),
            )
            self.save_run_report()
            return
        self.preprocess_output_prompt()
        if not self.run_micro_scoring():
            self.rollback_rule_injection()
            self.log_action(
                "pipeline",
                "stopped",
                reason="micro scoring gate rejected candidate",
            )
            self.save_run_report()
            return
        if not self.run_scoring():
            self.rollback_rule_injection()
            self.log_action(
                "pipeline",
                "stopped",
                reason="regular gate rejected candidate",
            )
            self.save_run_report()
            return
        self.commit_rule_injection()
        self.promote_to_final()
        self.save_run_report()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Orchestrate one E-type residual preference iteration."
    )
    parser.add_argument("--execute", action="store_true", help="Actually run API-affecting steps.")
    parser.add_argument("--skip-analysis", action="store_true", help="Use an existing consolidated file.")
    parser.add_argument("--skip-preprocess", action="store_true")
    parser.add_argument("--refresh-badcases", action="store_true")
    parser.add_argument("--promote-final", action="store_true")
    parser.add_argument("--skip-micro-scoring", action="store_true")

    parser.add_argument("--analysis-dir", default=DEFAULT_ANALYSIS_DIR)
    parser.add_argument("--origin-file", default=DEFAULT_ORIGIN_FILE)
    parser.add_argument("--prompt-meta", default=DEFAULT_PROMPT_META)
    parser.add_argument("--scoring-file", default=DEFAULT_SCORING_FILE)
    parser.add_argument("--badcase-file", default=DEFAULT_BADCASE_FILE)
    parser.add_argument("--consolidated", default=None)
    parser.add_argument("--consolidated-output", default=None)
    parser.add_argument("--output-meta", default=DEFAULT_OUTPUT_META)
    parser.add_argument("--output-prompt", default=DEFAULT_OUTPUT_PROMPT)
    parser.add_argument("--injected-log", default=DEFAULT_INJECTED_LOG)
    parser.add_argument("--run-report", default=DEFAULT_RUN_REPORT)
    parser.add_argument("--iteration-log", default="etype_iteration_history.json")
    parser.add_argument("--contrastive-log", default="etype_contrastive_log.json")
    parser.add_argument("--micro-essays", default=DEFAULT_MICRO_ESSAYS)
    parser.add_argument("--micro-manifest", default=DEFAULT_MICRO_MANIFEST)
    parser.add_argument("--micro-output", default=DEFAULT_MICRO_OUTPUT)
    parser.add_argument("--micro-eval", default=DEFAULT_MICRO_EVAL)
    parser.add_argument("--train-badcase", default=DEFAULT_TRAIN_BADCASE)
    parser.add_argument("--train-eval", default=DEFAULT_TRAIN_EVAL)
    parser.add_argument("--validation-baseline", default=DEFAULT_VALIDATION_BASELINE)
    parser.add_argument("--validation-eval", default=DEFAULT_VALIDATION_EVAL)

    parser.add_argument("--dimension", choices=DIMENSIONS, default=None)
    parser.add_argument("--major-iteration", type=int, default=None)
    parser.add_argument("--sub-iteration", type=int, default=None)
    parser.add_argument("--max-rules-per-sub-iteration", type=int, default=1)
    parser.add_argument("--max-outlier-samples", type=int, default=5)
    parser.add_argument("--max-normal-samples", type=int, default=3)
    parser.add_argument("--normal-zscore-threshold", type=float, default=0.8)
    parser.add_argument("--normal-diff-threshold", type=float, default=1.0)

    parser.add_argument("--score", choices=("none", "gate", "train", "test", "both", "gate-and-train"), default="none")
    parser.add_argument("--train-essays", default="train_essays.json")
    parser.add_argument("--test-essays", default="test_essays.json")
    parser.add_argument("--train-output", default="train_scoring_results_etype_next.json")
    parser.add_argument("--test-output", default="test_scoring_results_etype_next.json")
    parser.add_argument("--gate-essays", default=DEFAULT_GATE_ESSAYS)
    parser.add_argument("--gate-manifest", default=DEFAULT_GATE_MANIFEST)
    parser.add_argument("--gate-output", default=DEFAULT_GATE_OUTPUT)
    parser.add_argument("--gate-eval", default=DEFAULT_GATE_EVAL)
    parser.add_argument("--gate-badcase-count", type=int, default=6)
    parser.add_argument("--gate-normal-count", type=int, default=6)
    parser.add_argument("--gate-min-badcase-improve-rate", type=float, default=0.5)
    parser.add_argument("--gate-min-badcase-improvement", type=float, default=0.5)
    parser.add_argument("--gate-max-badcase-worsening", type=float, default=0.0)
    parser.add_argument("--gate-max-normal-regression", type=float, default=0.5)
    parser.add_argument("--gate-max-cross-dim-regression", type=float, default=0.5)
    parser.add_argument("--gate-max-badcase-worsened-count", type=int, default=0)
    parser.add_argument("--gate-max-normal-worsened-count", type=int, default=1)
    parser.add_argument("--gate-max-cross-dim-regression-count", type=int, default=1)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    runner = ETypeIterationRunner(args)
    runner.run()


if __name__ == "__main__":
    main()
