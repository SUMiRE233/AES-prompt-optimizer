import argparse
import glob
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aes_badcase_miner import AESBadcaseMiner
from batch_scoring import BatchEssayScorer
from preprocess_prompt import PromptPreprocessor
from prompt_optimizer import (
    CandidateRejectedError,
    NoOptimizationTargetError,
    PromptOptimizer,
)
from run_manifest import (
    POLICY_FIELDS,
    apply_policy_migration,
    assert_manifest_compatible,
    attach_version_record,
    build_experiment_manifest,
    build_version_record,
    manifest_mismatches,
    sha256,
    write_experiment_manifest,
)
from sample_extractor import EssayExtractor


ORIGIN_META = "origin_prompt_meta.md"
ORIGIN_PROMPT = "origin_prompt.md"

FINAL_META = "final_prompt_meta.md"
FINAL_PROMPT = "final_prompt.md"
FINAL_BADCASE = "final_aes_badcases.json"
FINAL_TRAIN_SCORING = "final_train_scoring_results.json"
GATE_REPORT = "b_gate_report.json"
MAX_AUTO_VERSION = 6

# B 类 severe/soft 判据（大纲 §3.3：本轮不得修改）。集中在此，供 manifest 记录。
B_SEVERE_ABS_BIAS = 1.5
B_SOFT_ABS_BIAS = 1.0
B_SEVERE_MIN_DIRECTION = 3
B_SOFT_MIN_DIRECTION = 2

# ============================================================
# 统一目标函数协议（Q 口径；2026-09-21 用户冻结，完全取代旧 A1/A2/A3 机制）
# ============================================================
# Q = Q_SEVERE_WEIGHT * mean(Severe) + mean(Soft)：均值取该版本在当前精度等级
# 下的**全部**固定次数评测（不取最优值、不四舍五入；展示保留 2 位小数）。
# gate 判据：Q_n 未严格下降（含平台期）即触发。
# 权重依据是与结果无关的交换率：1 个 Severe 允许由 3 个 Soft 抵消
# （+1S/-3F 记为改善），但不允许由 2 个 Soft 抵消（+1S/-2F 触发）；
# 故 w 落在 (2, 3) 内并取偏大值 2.5。
Q_SEVERE_WEIGHT = 2.5

# 精度等级（B1：repeat_level 持久化于 b_protocol_state.json，跨 resume 生效、
# 高精度不可逆）。低精度 = 每版单评；首次预警后永久升级为双评。
# 首次预警补评 x-1 与 x；到达 V6 上限且从未预警时强制补评 V5、V6；
# 不向前回补更早版本（低精度搜索误差由设计接受）。
REPEAT_LEVEL_LOW = 1
REPEAT_LEVEL_HIGH = 2
PROTOCOL_STATE = "b_protocol_state.json"
PROTOCOL_VERSION = "Q-mean-2.5-v1"

# validation：候选各自固定两次独立评测（不依第一次结果决定第二次），
# 取 Severe/Soft 算术平均后按 Q 排序（Q -> 更少 Severe -> 更早版本）。
VALIDATION_RUNS_PER_CANDIDATE = 2
VALIDATION_REPORT = "b_validation_report.json"

MAX_RUNS_PER_VERSION = 2
RUN_CLASS = "exploratory"
RERUN_SUFFIX = "_rerun"
RERUN_LOG = "b_rerun_log.json"

# 易被误复用的中间产物：启动前必须为空，否则说明存有上一次 run 的残留。
STALE_ARTIFACT_PATTERNS = (
    "train_scoring_results*.json",
    "test_scoring_results*.json",
    "aes_badcases*.json",
    "optimized_prompt*_meta.md",
    "optimized_prompt*.md",
    "iteration_history.json",
    RERUN_LOG,
    PROTOCOL_STATE,
    VALIDATION_REPORT,
    "b_rebuilt_route.json",
    "final_evidence.json",
    "final_train_scoring_results_mean*.json",
)


@dataclass(frozen=True)
class IterationDecision:
    """统一 Q 协议的决策结果（纯判据；补评/复核由 `resolve_decision` 执行）。

    action:
      - "continue": 继续生成下一版；
      - "upgrade" : 低精度预警，需补评 (x-1, x) 后以双评均值复核；
      - "stop"    : 停止迭代，`candidates` 进入 validation。
    top_up_first: stop 且从未预警到达上限时，先强制补评候选（最后两版）再进 validation。
    """

    action: str
    reason: str
    candidates: tuple = ()
    top_up_first: bool = False


def prompt_meta_path(iteration):
    if iteration == 0:
        return ORIGIN_META
    return f"optimized_prompt{iteration}_meta.md"


def prompt_path(iteration):
    if iteration == 0:
        return ORIGIN_PROMPT
    return f"optimized_prompt{iteration}.md"


def train_scoring_path(iteration):
    return f"train_scoring_results{iteration}.json"


def test_scoring_path(iteration):
    return f"test_scoring_results{iteration}.json"


def badcase_path(iteration):
    return f"aes_badcases{iteration}.json"


def rerun_scoring_path(iteration):
    return LEGACY_PATHS.rerun_scoring(iteration)


def rerun_badcase_path(iteration):
    return LEGACY_PATHS.rerun_badcase(iteration)


# ============================================================
# 折感知路径（CV 协议，ETYPE_ITERATION_V2_DESIGN.md §14）
# ============================================================
CV_RUN_ROOT = "cv_runs"


@dataclass(frozen=True)
class RunPaths:
    """一次运行（legacy 单次 / 某个 CV 折）的产物路径解析。

    CV 协议下每个折有自己的目录，避免不同折互相覆盖 prompt 与评分产物；
    `LEGACY_PATHS` 复现改动前的行为，使既有 V0–V6 产物仍可解释。
    """

    run_dir: str = "."

    def _join(self, name: str) -> str:
        if self.run_dir in (".", ""):
            return name
        return os.path.join(self.run_dir, name)

    def prompt_meta(self, iteration: int) -> str:
        return ORIGIN_META if iteration == 0 else self._join(
            f"optimized_prompt{iteration}_meta.md"
        )

    def prompt(self, iteration: int) -> str:
        return ORIGIN_PROMPT if iteration == 0 else self._join(
            f"optimized_prompt{iteration}.md"
        )

    def train_scoring(self, iteration: int) -> str:
        return self._join(f"train_scoring_results{iteration}.json")

    def eval_scoring(self, iteration: int) -> str:
        # legacy 单次运行沿用既有的 test_scoring_results*.json 命名，
        # 折目录使用 eval_scoring_results*.json，两者不互相覆盖。
        prefix = "test" if self.run_dir in (".", "") else "eval"
        return self._join(f"{prefix}_scoring_results{iteration}.json")

    def rerun_eval_scoring(self, iteration: int) -> str:
        """validation 的第二次评测产物（与第一次分存，不互相覆盖）。"""
        prefix = "test" if self.run_dir in (".", "") else "eval"
        return self._join(f"{prefix}_scoring_results{iteration}{RERUN_SUFFIX}.json")

    def badcase(self, iteration: int) -> str:
        return self._join(f"aes_badcases{iteration}.json")

    def rerun_scoring(self, iteration: int) -> str:
        """抗震荡 rerun 的评分产物路径（与 run1 分存，不互相覆盖）。"""
        return self._join(f"train_scoring_results{iteration}{RERUN_SUFFIX}.json")

    def rerun_badcase(self, iteration: int) -> str:
        return self._join(f"aes_badcases{iteration}{RERUN_SUFFIX}.json")

    def gate_report(self) -> str:
        return self._join(GATE_REPORT)

    def iteration_log(self) -> str:
        return self._join("iteration_history.json")


LEGACY_PATHS = RunPaths(".")


def fold_paths(fold: int) -> RunPaths:
    return RunPaths(os.path.join(CV_RUN_ROOT, f"fold{fold}"))


def fold_essay_paths(fold: int) -> tuple:
    """返回 (训练集作文文件, 评估集作文文件)。"""
    base = os.path.join("fold_essays", f"fold{fold}")
    return f"{base}_train_essays.json", f"{base}_eval_essays.json"


def preprocess_prompt(input_path, output_path):
    preprocessor = PromptPreprocessor()
    preprocessor.process(input_path=input_path, output_path=output_path)


def run_scoring(prompt_file, essays_file, output_file, origin_file):
    scorer = BatchEssayScorer()
    scorer.prompt_path = prompt_file
    scorer.essays_path = essays_file
    scorer.output_path = output_file
    scorer.origin_data_path = origin_file
    scorer.run()


def run_badcase_mining(scoring_file, output_file):
    miner = AESBadcaseMiner(scoring_file)
    return miner.run(output_file)


def run_prompt_optimization(current_meta, badcases_file, target_meta, iteration_log=None):
    optimizer = PromptOptimizer()
    optimizer.CURRENT_PROMPT = current_meta
    optimizer.BADCASE_FILE = badcases_file
    optimizer.TARGET_PROMPT = target_meta
    if iteration_log is not None:
        optimizer.ITERATION_LOG = iteration_log
    optimizer.run()


def assert_clean_working_directory():
    """启动前确认没有上一次 run 的残留产物（大纲 §9.1/§9.2）。

    旧 `test_scoring_results*.json` 会被 `run_test_iteration` 静默复用，
    旧 `iteration_history.json` 会让版本号从上次的位置继续，二者都会污染本轮结论。
    """
    stale = []
    for pattern in STALE_ARTIFACT_PATTERNS:
        stale.extend(glob.glob(pattern))
    stale = sorted(set(stale))
    if stale:
        listing = "\n".join(f"  - {name}" for name in stale)
        raise SystemExit(
            "检测到上一次运行残留的产物，拒绝启动以免静默复用：\n"
            f"{listing}\n"
            "请先运行 python archive_run.py --execute 归档，或手工移走这些文件。"
        )


def build_run_manifest(origin_file, max_version):
    """构造本轮 run manifest（大纲 §9.2），返回 `(manifest, 目标路径)`，不落盘。"""
    from aes_badcase_miner import Config as BadcaseConfig
    from batch_scoring import BatchEssayScorer
    from prompt_structure_contract import (
        MAX_ADDED_CHARS_PER_ITERATION,
        MAX_BOLD_SECTIONS,
        MAX_NEW_BOLD_SECTIONS_PER_ROUND,
        MAX_PROMPT_CHARS,
    )

    run_id = datetime.now().strftime("run_%Y%m%dT%H%M%S")
    inputs = [
        Path(origin_file),
        Path(ORIGIN_META),
        Path(ORIGIN_PROMPT),
    ]
    split_files = {
        "train": Path("train_essays.json"),
        "validation": Path("test_essays.json"),
    }
    models = {
        "scoring": BatchEssayScorer().model,
        "optimizer": PromptOptimizer().model,
    }
    thresholds = {
        "b_severe_abs_bias": B_SEVERE_ABS_BIAS,
        "b_soft_abs_bias": B_SOFT_ABS_BIAS,
        "b_severe_min_direction": B_SEVERE_MIN_DIRECTION,
        "b_soft_min_direction": B_SOFT_MIN_DIRECTION,
        "e_business_min_diff": BadcaseConfig.MIN_DIFF,
    }
    stop_policy = {
        "rule": (
            "unified_Q_gate: warning when Q_n >= Q_{n-1} (strict decrease required; "
            "a plateau also triggers); a low-precision first warning permanently "
            "upgrades to double evaluation and is re-checked with means; a "
            "high-precision gate stops; cap=V6 forces a precision upgrade of the "
            "last two versions only"
        ),
        "objective": {
            "formula": f"Q = {Q_SEVERE_WEIGHT}*mean(severe) + mean(soft)",
            "w": Q_SEVERE_WEIGHT,
            "w_rationale": (
                "one severe may be offset by three soft but not two; w within (2, 3), "
                "skewed high (user decision 2026-09-21)"
            ),
        },
        "max_version": max_version,
        "final_ranking": (
            "lower Q -> fewer mean severe -> earlier version; two fixed independent "
            "validation runs per candidate"
        ),
        "mae_policy": "record_only; warn_on_rebound",
    }
    structure_budget = {
        "max_bold_sections": MAX_BOLD_SECTIONS,
        "max_prompt_chars": MAX_PROMPT_CHARS,
        "max_added_chars_per_iteration": MAX_ADDED_CHARS_PER_ITERATION,
        "max_new_bold_sections_per_round": MAX_NEW_BOLD_SECTIONS_PER_ROUND,
    }
    manifest = build_experiment_manifest(
        run_id=run_id,
        inputs=inputs,
        split_files=split_files,
        models=models,
        thresholds=thresholds,
        stop_policy=stop_policy,
        structure_budget=structure_budget,
    )
    manifest["api_url"] = os.getenv("AES_API_URL", "https://api.pateway.ai/v1/messages")
    manifest["versions"] = []
    # 实际生效的协议（review #1）：manifest 必须声明代码真正执行的规则，
    # 否则“同协议可复用”无法判定。修订均已登记于报告 §12.6。
    manifest["protocol"] = {
        "run_class": RUN_CLASS,
        "objective": (
            f"Q = {Q_SEVERE_WEIGHT}*mean(severe) + mean(soft); arithmetic mean over "
            "all runs of a version at the current precision level; no min, no rounding"
        ),
        "aggregation": "arithmetic mean over all runs of a version",
        "precision": (
            "low precision = single run per version; the first warning permanently "
            "upgrades to two runs for x-1 and x (no top-up of x-2, earlier decisions "
            "are not re-opened); high precision is irreversible; the V6 cap forces a "
            "top-up of the last two versions only"
        ),
        "validation": (
            "two candidates; two independent runs per candidate on test_essays.json; "
            "the second run is always executed regardless of the first; means only"
        ),
        "final_ranking": "Q -> fewer mean severe -> earlier version (MAE recorded only)",
        "amendments": [
            "replaces the prior A1/A2/A3 mechanisms (severe/soft gate, symmetric min rerun, human override)",
            "w=2.5: +1S/-3F counts as improvement, +1S/-2F triggers (user decision 2026-09-21)",
            "no minimum effect size theta by design (user decision 2026-09-21)",
            "validation uncertainty must be disclosed in the limitations section",
        ],
    }
    output = Path(f"run_manifest_{run_id}.json")
    return manifest, output


def write_run_manifest(origin_file, max_version):
    """写出本轮 run manifest（大纲 §9.2），返回路径。"""
    manifest, output = build_run_manifest(origin_file, max_version)
    write_experiment_manifest(manifest, output)
    return output


def latest_run_manifest():
    """最近一次写出的 run manifest（无则 `None`）。"""
    candidates = sorted(glob.glob("run_manifest_*.json"))
    return Path(candidates[-1]) if candidates else None


def assert_reusable_artifacts(origin_file, max_version):
    """复用旧产物前核对 manifest 身份（大纲 §9.2 末句 / §11）。

    受控迁移（用户授权全面采用统一 Q 协议）：若差异**仅**落在策略声明字段
    （`stop_policy` / `protocol`），则把存储的声明更新为新协议并登记迁移记录；
    数据身份（模型、阈值、文件、划分）任一不一致仍拒绝复用。
    """
    manifest_path = latest_run_manifest()
    if manifest_path is None:
        print("[manifest] 未发现既有 run manifest，跳过复用身份核对")
        return None
    current, _ = build_run_manifest(origin_file, max_version)
    mismatched = manifest_mismatches(manifest_path, current)
    if mismatched and set(mismatched) <= set(POLICY_FIELDS):
        apply_policy_migration(
            manifest_path,
            current,
            note="统一 Q 协议替换 severe/soft gate 与对称 min 机制（用户授权 2026-09-21）",
        )
        print(
            f"[manifest] 策略字段按新协议受控迁移：{sorted(mismatched)}"
            "（数据身份不变；迁移记录已写入 manifest）"
        )
    assert_manifest_compatible(manifest_path, current)
    print(f"[manifest] 复用身份核对通过：{manifest_path.name}")
    return manifest_path


def record_version_in_manifest(iteration, manifest_path):
    """把一个版本的输入/输出 hash、**全部运行**与契约校验结果写入 manifest（§9.2）。"""
    from prompt_structure_contract import validate_input_contract

    meta_file = Path(prompt_meta_path(iteration))
    runtime_file = Path(prompt_path(iteration))
    text = meta_file.read_text(encoding="utf-8") if meta_file.is_file() else ""
    violations = validate_input_contract(text) if text else []
    outputs = [Path(train_scoring_path(iteration)), Path(badcase_path(iteration))]
    # 决定 gate / validation 路径的补评产物也必须被 hash 覆盖。
    outputs += [Path(item) for item in version_scoring_paths(iteration)[1:]]
    outputs += [Path(item) for item in version_badcase_paths(iteration)[1:]]
    outputs += [Path(item) for item in version_eval_scoring_paths(iteration)]
    record = build_version_record(
        iteration=iteration,
        prompt_meta=meta_file,
        runtime_prompt=runtime_file,
        contract_violations=violations,
        inputs=[Path("train_essays.json"), Path("origin_scoring_results.json")],
        outputs=outputs,
    )
    runs = version_run_counts(iteration)
    record["runs"] = runs
    record["aggregation"] = (
        "arithmetic mean over all runs of a version "
        f"(Q = {Q_SEVERE_WEIGHT}*mean_severe + mean_soft)"
    )
    if runs:
        record["means"] = version_mean_counts(iteration)
    attach_version_record(manifest_path, record)
    return record


def optimize_or_stop(current_meta, badcases_file, target_meta, iteration):
    """调用优化器；无法产出合规候选时停止 B 迭代并报告阻塞原因（大纲 §7.4）。

    `target_meta` 只在候选通过结构契约、修改权限与预算校验后才被写入，
    所以这里不需要额外回滚：当前稳定版本从未被触碰。
    """
    try:
        run_prompt_optimization(current_meta, badcases_file, target_meta)
    except (CandidateRejectedError, NoOptimizationTargetError) as error:
        kind = type(error).__name__
        print("\n" + "!" * 62)
        print(f"[BLOCKED] 迭代 {iteration} 无法产出合规候选：{kind}")
        print(f"  {error}")
        print(f"  当前稳定版本 {current_meta} 未被修改。")
        print(f"  非法候选未写入 {target_meta}，因此不会进入预处理、评分或版本历史。")
        print("!" * 62)
        raise SystemExit(f"B iteration blocked at V{iteration}: {kind}")
    return True


def b_bias_counts_from_badcases(badcase_data):
    stats = badcase_data["statistics"]["B_bias"]
    severe = int(stats["severe_count"])
    soft = int(stats["soft_count"])
    return {
        "severe": severe,
        "soft": soft,
        "total": severe + soft,
    }


def b_bias_counts_from_scoring_results(scoring_file):
    with open(scoring_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    severe = 0
    soft = 0

    for item in data:
        content_diff = item["AI"]["content"] - item["teacher"]["content"]
        expression_diff = item["AI"]["expression"] - item["teacher"]["expression"]
        structure_diff = item["AI"]["structure"] - item["teacher"]["structure"]

        bias_score = (content_diff + expression_diff + structure_diff) / 3
        sign = 1 if bias_score > 0 else -1 if bias_score < 0 else 0

        direction_consistency = 0
        if sign != 0:
            if content_diff * sign > 0:
                direction_consistency += 1
            if expression_diff * sign > 0:
                direction_consistency += 1
            if structure_diff * sign > 0:
                direction_consistency += 1

        if (
            abs(bias_score) > B_SEVERE_ABS_BIAS
            and direction_consistency >= B_SEVERE_MIN_DIRECTION
        ):
            severe += 1
        elif (
            abs(bias_score) > B_SOFT_ABS_BIAS
            and direction_consistency >= B_SOFT_MIN_DIRECTION
        ):
            soft += 1

    return {
        "severe": severe,
        "soft": soft,
        "total": severe + soft,
    }


def q_value(counts):
    """统一目标 Q = w*severe + soft（均值口径；见文件头协议说明）。"""
    return Q_SEVERE_WEIGHT * counts["severe"] + counts["soft"]


def gate_signal(previous, current):
    """Q 未严格下降（含平台期）即预警。previous/current 为该版本的**均值**。"""
    return q_value(current) >= q_value(previous)


def decide_iteration(previous, current, iteration, max_version=MAX_AUTO_VERSION,
                     repeat_level=REPEAT_LEVEL_LOW):
    """统一 Q 协议的停止判据（vNext；见 manifest 的 protocol 块）。

    - Q 严格下降：continue（到达上限则 stop/cap_reached）；
    - 低精度阶段首次 `Q_n >= Q_{n-1}`：upgrade（补评 x-1、x 后复核，精度永久升级）；
    - 高精度阶段 `Q_n >= Q_{n-1}`：stop（有效 gate，候选 n-1、n）；
    - 从未预警且到达上限：stop/cap_reached，先强制补评最后两版（top_up_first）。
    """
    if iteration == 0:
        return IterationDecision("continue", "origin")

    if not gate_signal(previous, current):
        if iteration >= max_version:
            return IterationDecision(
                "stop",
                "cap_reached",
                (iteration - 1, iteration),
                top_up_first=repeat_level == REPEAT_LEVEL_LOW,
            )
        return IterationDecision("continue", "q_strictly_decreased")

    if repeat_level == REPEAT_LEVEL_LOW:
        # 低精度预警：不立即停止；补评复核，且精度**永久**升级。
        return IterationDecision("upgrade", "low_precision_warning")
    return IterationDecision(
        "stop",
        (
            "high_precision_gate_at_cap"
            if iteration >= max_version
            else "high_precision_gate"
        ),
        (iteration - 1, iteration),
    )


def scoring_mae(scoring_file):
    with open(scoring_file, "r", encoding="utf-8") as f:
        rows = json.load(f)
    errors = [
        abs(item["AI"][dimension] - item["teacher"][dimension])
        for item in rows
        for dimension in ("content", "expression", "structure")
    ]
    return sum(errors) / len(errors)


def candidate_rank(iteration, counts):
    """final/排序键：Q（越低越好）-> 均值 severe 越少 -> 版本越早。"""
    return (q_value(counts), counts["severe"], iteration)


def version_eval_scoring_paths(iteration, paths=LEGACY_PATHS):
    """该版本已存在的 validation 评分产物（第一次在前，第二次在后）。"""
    return [
        candidate
        for candidate in (paths.eval_scoring(iteration), paths.rerun_eval_scoring(iteration))
        if os.path.exists(candidate)
    ]


def ensure_eval_runs(iteration, origin_file, paths=LEGACY_PATHS,
                     eval_essays="test_essays.json",
                     target=VALIDATION_RUNS_PER_CANDIDATE):
    """validation 的固定评测次数（默认 2 次/候选）。

    **不根据第一次结果决定是否运行第二次**：两次都执行；产物已存在则幂等复用。
    """
    performed = 0
    for _ in range(VALIDATION_RUNS_PER_CANDIDATE):
        done = len(version_eval_scoring_paths(iteration, paths))
        if done >= target:
            break
        print(f"[validation] V{iteration} 第 {done + 1} 次评测")
        run_test_iteration(
            iteration,
            origin_file,
            paths=paths,
            eval_essays=eval_essays,
            rerun=done >= 1,
        )
        performed += 1
    return performed


def run_validation_and_select(candidates, origin_file, reason, paths=LEGACY_PATHS,
                              eval_essays="test_essays.json"):
    """validation 与 final 选择（统一 Q 协议）。

    - 每候选固定两次独立评测（先补足评测数，再取 Severe/Soft 的算术平均）；
    - 排序：Q 更低 -> 均值 Severe 更少 -> 更早版本；
    - MAE 只记录；回升时告警，不参与排序。
    """
    rows = []
    for iteration in dict.fromkeys(candidates):
        ensure_eval_runs(iteration, origin_file, paths=paths, eval_essays=eval_essays)
        scoring_files = version_eval_scoring_paths(iteration, paths)
        runs = [b_bias_counts_from_scoring_results(path) for path in scoring_files]
        count = len(runs)
        severe = sum(run["severe"] for run in runs) / count
        soft = sum(run["soft"] for run in runs) / count
        maes = [scoring_mae(path) for path in scoring_files]
        rows.append(
            {
                "iteration": iteration,
                "runs": runs,
                "mean_severe": round(severe, 4),
                "mean_soft": round(soft, 4),
                "q": round(Q_SEVERE_WEIGHT * severe + soft, 4),
                "mae_runs": [round(mae, 4) for mae in maes],
                "mae_mean": round(sum(maes) / count, 4),
            }
        )

    selected = min(
        rows,
        key=lambda item: candidate_rank(
            item["iteration"], {"severe": item["mean_severe"], "soft": item["mean_soft"]}
        ),
    )
    for previous, current in zip(rows, rows[1:]):
        if current["mae_mean"] > previous["mae_mean"]:
            print(
                "WARNING: validation MAE rebounded "
                f"from V{previous['iteration']}={previous['mae_mean']:.3f} "
                f"to V{current['iteration']}={current['mae_mean']:.3f}; "
                "MAE is recorded only and did not affect final selection."
            )

    report = {
        "reason": reason,
        "protocol_version": PROTOCOL_VERSION,
        "w": Q_SEVERE_WEIGHT,
        "aggregation": "arithmetic mean over fixed validation runs per candidate",
        "ranking": (
            f"Q = {Q_SEVERE_WEIGHT}*mean_severe + mean_soft -> fewer mean severe -> "
            "earlier version"
        ),
        "mae_policy": "record_only; warn_on_rebound",
        "candidates": rows,
        "selected_iteration": selected["iteration"],
    }
    Path(VALIDATION_REPORT).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[保存] {VALIDATION_REPORT}")
    print(
        f"[validation] 选中 V{selected['iteration']}："
        f"Q={selected['q']:.2f}（severe={selected['mean_severe']:.2f}，"
        f"soft={selected['mean_soft']:.2f}）"
    )
    return selected["iteration"]


def ensure_origin_prompt():
    if os.path.exists(ORIGIN_PROMPT):
        return
    preprocess_prompt(ORIGIN_META, ORIGIN_PROMPT)


def parse_bold_sections_for_report(text):
    """只为 final 契约检查的输出信息提供加粗分点计数（避免顶部额外导入）。"""
    from prompt_structure_contract import parse_bold_sections

    return parse_bold_sections(text)


def save_final_prompt(meta_path, iteration):
    """保存 final，**并在保存前对 final 再执行一次完整结构契约检查**（大纲 §十 第 9 步）。

    检查失败即报错中止，不得将不合规的版本晋升为 final。
    """
    from prompt_structure_contract import PromptContractError, validate_input_contract

    if os.path.exists(meta_path):
        text = Path(meta_path).read_text(encoding="utf-8")
        violations = validate_input_contract(text)
        if violations:
            raise PromptContractError(violations)
        print(
            f"[契约] final 结构契约检查 PASS：{meta_path}"
            f"（{len(text)} 字符，{len(parse_bold_sections_for_report(text))} 个加粗分点）"
        )
        shutil.copy2(meta_path, FINAL_META)
        print(f"[保存] {FINAL_META}")

    runtime_file = meta_path.replace("_meta.md", ".md")
    if os.path.exists(runtime_file):
        shutil.copy2(runtime_file, FINAL_PROMPT)
        print(f"[保存] {FINAL_PROMPT}")

    # final 的评分与 badcase 必须来自**与 gate 参照值同一次运行**（review #2）：
    # 原先固定复制 run1，而参照值可能来自 rerun，导致 final bundle 内部不一致。
    pair = final_artifact_pair(iteration)
    if pair is None:
        print(
            f"[final][WARN] V{iteration} 无评分产物，跳过 final 评分/badcase 晋升"
        )
        return
    scoring_file, badcases_file = pair
    if not os.path.exists(badcases_file):
        # 该次运行是 rerun 且当时未挖 badcase（`--score-only` 路径）；本地补挖，不耗 API。
        print(f"[final] {badcases_file} 缺失，按 {scoring_file} 本地补挖 badcase")
        run_badcase_mining(scoring_file, badcases_file)
    shutil.copy2(badcases_file, FINAL_BADCASE)
    print(f"[保存] {FINAL_BADCASE}  <- {badcases_file}")
    shutil.copy2(scoring_file, FINAL_TRAIN_SCORING)
    print(f"[保存] {FINAL_TRAIN_SCORING}  <- {scoring_file}")


def extract_samples(origin_file):
    extractor = EssayExtractor()
    extractor.input_path = origin_file
    extractor.run(split=True)


def run_train_iteration(iteration, origin_file, paths=LEGACY_PATHS,
                        train_essays="train_essays.json", rerun=False):
    """评分 → 挖掘 badcase。

    `rerun=True` 时写入 `*_rerun` 系列产物（与 run1 分存，不互相覆盖），
    且不重新预处理 prompt —— run1 已写过同一份，重写只会引入无意义的写入点。
    """
    prompt_file = paths.prompt(iteration)
    scoring_file = (
        paths.rerun_scoring(iteration) if rerun else paths.train_scoring(iteration)
    )
    badcases_file = (
        paths.rerun_badcase(iteration) if rerun else paths.badcase(iteration)
    )

    if not rerun:
        preprocess_prompt(paths.prompt_meta(iteration), prompt_file)
    run_scoring(prompt_file, train_essays, scoring_file, origin_file)
    return run_badcase_mining(scoring_file, badcases_file)


def run_test_iteration(iteration, origin_file, paths=LEGACY_PATHS,
                       eval_essays="test_essays.json", rerun=False):
    """validation 的单次评测；`rerun=True` 写入第二次评测的独立产物。"""
    prompt_file = paths.prompt(iteration)
    scoring_file = (
        paths.rerun_eval_scoring(iteration) if rerun else paths.eval_scoring(iteration)
    )

    if not os.path.exists(prompt_file):
        preprocess_prompt(paths.prompt_meta(iteration), prompt_file)

    run_scoring(prompt_file, eval_essays, scoring_file, origin_file)
    return b_bias_counts_from_scoring_results(scoring_file)


def version_scoring_paths(iteration, paths=LEGACY_PATHS):
    """该版本所有已存在的评分产物路径（run1 在前，rerun 在后）。"""
    return [
        candidate
        for candidate in (paths.train_scoring(iteration), paths.rerun_scoring(iteration))
        if os.path.exists(candidate)
    ]


def version_badcase_paths(iteration, paths=LEGACY_PATHS):
    return [
        candidate
        for candidate in (paths.badcase(iteration), paths.rerun_badcase(iteration))
        if os.path.exists(candidate)
    ]


def version_run_counts(iteration, paths=LEGACY_PATHS):
    """该版本所有已知运行的 B-severe/soft 计数（run1 在前，rerun 在后）。"""
    return [
        b_bias_counts_from_scoring_results(path)
        for path in version_scoring_paths(iteration, paths)
    ]


def allowed_run_count(iteration, state):
    """该版本在协议路线中**允许计入**的运行次数（1 或 2）。

    - 从未升级（upgraded_at 为空）：只用 run1；
    - 升级点及其后（iteration >= upgraded_at - 1，含边界对 x-1、x）：双评。
    """
    upgraded_at = state.get("upgraded_at")
    if upgraded_at is None:
        return 1
    if iteration >= upgraded_at - 1:
        return 2
    return 1


def mean_counts_from(runs, limit):
    """对前 `limit` 次运行取 (severe, soft) 算术平均；其余运行转入 `excluded`。"""
    used = runs[:limit]
    count = len(used)
    severe = sum(run["severe"] for run in used) / count
    soft = sum(run["soft"] for run in used) / count
    return {
        "severe": severe,
        "soft": soft,
        "runs": count,
        "q": Q_SEVERE_WEIGHT * severe + soft,
        "available_runs": len(runs),
        "excluded": runs[limit:],
    }


def protocol_state_or_default(path=PROTOCOL_STATE):
    """离线/展示场景的状态读取；缺失或口径不符时返回**保守默认**（只用 run1）。"""
    file_path = Path(path)
    if file_path.is_file():
        try:
            state = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}
        if (
            state.get("protocol_version") == PROTOCOL_VERSION
            and state.get("w") == Q_SEVERE_WEIGHT
        ):
            return state
    return new_protocol_state()


def version_mean_counts(iteration, paths=LEGACY_PATHS, state=None):
    """该版本**协议路线允许范围内**运行的 (severe, soft) 算术平均。

    - 允许次数由 `allowed_run_count` 决定（读取协议状态，或由调用方显式传入）；
    - 路线之外的 rerun（探索性测量）不计入均值，转移到 `excluded` 只作留档，
      不参与 gate、validation 候选或 final 排序；
    - `state=None` 时读取 `b_protocol_state.json`；无文件或口径不符时按
      保守默认（未升级、只用 run1）处理，避免历史/额外 rerun 被静默混入。
    """
    if state is None:
        state = protocol_state_or_default()
    runs = version_run_counts(iteration, paths)
    if not runs:
        raise FileNotFoundError(f"Cannot resolve mean counts for V{iteration}")
    return mean_counts_from(runs, allowed_run_count(iteration, state))


def final_artifact_pair(iteration, paths=LEGACY_PATHS):
    """final 应晋升的 `(评分文件, badcase 文件)`。

    统一 Q 协议下 gate 与 validation 全部使用**均值**，不存在“代表参照值的
    那一次运行”。为保证 final bundle 内部同源（prompt / 评分 / badcase 来自
    同一版本、同一次运行），固定晋升 **run1** 组合；run1 缺失时退回 rerun 组合。
    均值与选择依据记录在 `b_validation_report.json`，全部运行由 manifest 覆盖。
    """
    for scoring, badcase in (
        (paths.train_scoring(iteration), paths.badcase(iteration)),
        (paths.rerun_scoring(iteration), paths.rerun_badcase(iteration)),
    ):
        if os.path.exists(scoring):
            return scoring, badcase
    return None


def top_up_version(iteration, origin_file, target=REPEAT_LEVEL_HIGH, paths=LEGACY_PATHS,
                   train_essays="train_essays.json"):
    """把单个版本补评到 `target` 次，返回本次实际执行的补评次数。

    幂等：补评写入 `_rerun` 槽位，文件已存在即计入，跨 resume 不重复消耗。
    **有界**循环：以“剩余可补评次数”为界，而不是以“文件是否出现”为界——
    若 `run_train_iteration` 未产出文件，以文件为条件会死循环。
    """
    performed = 0
    for _ in range(MAX_RUNS_PER_VERSION):
        if len(version_run_counts(iteration, paths)) >= target:
            break
        print(f"[precision] V{iteration} 补评一次（目标 {target} 次评测）")
        run_train_iteration(
            iteration, origin_file, paths=paths, train_essays=train_essays, rerun=True
        )
        performed += 1
    return performed


def log_precision_event(kind, trigger, versions, extra=None):
    """精度升级 / 补评的审计记录（追加到 `b_rerun_log.json`，与旧机制条目共存）。"""
    payload = {
        "kind": kind,
        "trigger": trigger,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "versions": list(versions),
        "runs_after": {f"V{version}": version_run_counts(version) for version in versions},
        "w": Q_SEVERE_WEIGHT,
        "protocol_version": PROTOCOL_VERSION,
    }
    if extra:
        payload.update(extra)
    return record_rerun(payload)


def record_rerun(payload, path=RERUN_LOG):
    """抗震荡 rerun 的审计记录（即使 run1 被屏蔽也必须留下）。"""
    history = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                history = json.load(handle)
        except json.JSONDecodeError:
            history = []
    history.append(payload)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(history, handle, ensure_ascii=False, indent=2)
    return path


def new_protocol_state():
    return {
        "protocol_version": PROTOCOL_VERSION,
        "w": Q_SEVERE_WEIGHT,
        "repeat_level": REPEAT_LEVEL_LOW,
        "upgraded_at": None,
        "warning_events": [],
    }


def save_protocol_state(state, path=PROTOCOL_STATE):
    """持久化协议状态（`repeat_level` 的跨 resume 锚点，B1）。"""
    payload = {**state, "updated_at": datetime.now().isoformat(timespec="seconds")}
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def _route_row(iteration, stage, mean, delta, decision, boundary_previous=None):
    row = {
        "iteration": iteration,
        "stage": stage,
        "runs_used": mean["runs"],
        "available_runs": mean["available_runs"],
        "excluded_runs": mean["excluded"],
        "severe": round(mean["severe"], 4),
        "soft": round(mean["soft"], 4),
        "q": round(mean["q"], 4),
        "delta_q": None if delta is None else round(delta, 4),
        "decision": decision,
    }
    if boundary_previous is not None:
        row["boundary_previous"] = {
            "severe": round(boundary_previous["severe"], 4),
            "soft": round(boundary_previous["soft"], 4),
            "q": round(boundary_previous["q"], 4),
        }
    return row


def replay_protocol_route(up_to_version, paths=LEGACY_PATHS,
                          max_version=MAX_AUTO_VERSION):
    """按新协议**真实时序**重放 V0..V(up_to)，返回 `(state, rows)`。

    与“对全部已有 rerun 直接取均值”的旧重放不同：低精度阶段只用 run1；
    首次 `Q_i >= Q_{i-1}` 为低精度预警，随后把 (i-1, i) 提升为双评并复核
    （确认 -> 停止；推翻 -> 接受 i 并按双评继续，升级永久）；升级之后的所有
    版本一律双评；上限处的高精度 gate 记为 `high_precision_gate_at_cap`。
    """
    state = new_protocol_state()
    state["source"] = "timeline_rebuild"
    rows = []
    if up_to_version < 1:
        return state, rows

    def block(iteration, limit):
        runs = version_run_counts(iteration, paths)
        if not runs:
            raise FileNotFoundError(
                f"Cannot rebuild route: missing scoring artifacts for V{iteration}"
            )
        return mean_counts_from(runs, limit)

    previous = block(0, 1)
    rows.append(_route_row(0, "run1", previous, None, "origin"))
    level = REPEAT_LEVEL_LOW
    iteration = 1
    while iteration <= up_to_version:
        limit = 1 if level == REPEAT_LEVEL_LOW else 2
        current = block(iteration, limit)
        delta = q_value(current) - q_value(previous)
        if not gate_signal(previous, current):
            rows.append(
                _route_row(
                    iteration,
                    "high_precision" if level == REPEAT_LEVEL_HIGH else "low_precision",
                    current,
                    delta,
                    (
                        "high_precision_pass"
                        if level == REPEAT_LEVEL_HIGH
                        else "low_precision_pass"
                    ),
                )
            )
            previous = current
            iteration += 1
            continue

        if level == REPEAT_LEVEL_LOW:
            state["warning_events"].append(
                {
                    "iteration": iteration,
                    "kind": "low_precision_warning",
                    "delta_q_before": round(delta, 4),
                }
            )
            level = REPEAT_LEVEL_HIGH
            state["repeat_level"] = REPEAT_LEVEL_HIGH
            state["upgraded_at"] = state.get("upgraded_at") or iteration
            boundary_previous = block(iteration - 1, 2)
            current2 = block(iteration, 2)
            delta2 = q_value(current2) - q_value(boundary_previous)
            confirmed = gate_signal(boundary_previous, current2)
            state["warning_events"][-1].update(
                {"delta_q_after": round(delta2, 4), "confirmed": confirmed}
            )
            rows.append(
                _route_row(
                    iteration,
                    "boundary",
                    current2,
                    delta2,
                    (
                        "first_warning_confirmed"
                        if confirmed
                        else "low_precision_warning_refuted"
                    ),
                    boundary_previous=boundary_previous,
                )
            )
            previous = current2
            if confirmed:
                state["stop"] = {
                    "iteration": iteration,
                    "reason": "first_warning_confirmed",
                    "candidates": [iteration - 1, iteration],
                }
                break
            iteration += 1
            continue

        reason = (
            "high_precision_gate_at_cap"
            if iteration >= max_version
            else "high_precision_gate"
        )
        rows.append(_route_row(iteration, "high_precision", current, delta, reason))
        state["stop"] = {
            "iteration": iteration,
            "reason": reason,
            "candidates": [iteration - 1, iteration],
        }
        break

    if "stop" not in state and up_to_version >= max_version:
        state["stop"] = {
            "iteration": up_to_version,
            "reason": "cap_reached",
            "candidates": [up_to_version - 1, up_to_version],
        }
    return state, rows


def replay_protocol_state(up_to_version, paths=LEGACY_PATHS,
                          max_version=MAX_AUTO_VERSION):
    """`replay_protocol_route` 的兼容包装（只返回 state）。"""
    state, _ = replay_protocol_route(
        up_to_version, paths=paths, max_version=max_version
    )
    return state


def load_protocol_state(up_to_version, paths=LEGACY_PATHS):
    """读取协议状态；文件缺失时按重放重建并落盘（首次接管既有轨迹）。"""
    path = Path(PROTOCOL_STATE)
    if path.is_file():
        state = json.loads(path.read_text(encoding="utf-8"))
        if (
            state.get("protocol_version") != PROTOCOL_VERSION
            or state.get("w") != Q_SEVERE_WEIGHT
        ):
            raise SystemExit(
                f"{PROTOCOL_STATE} 与当前协议不一致，拒绝续跑："
                f"file={state.get('protocol_version')}/{state.get('w')}，"
                f"code={PROTOCOL_VERSION}/{Q_SEVERE_WEIGHT}"
            )
        return state
    state = replay_protocol_state(up_to_version, paths)
    save_protocol_state(state)
    print(
        "[protocol] 未发现状态文件，已按现有运行重放重建："
        f"repeat_level={state['repeat_level']}，"
        f"warning_events={len(state['warning_events'])} 条"
    )
    return state


def resolve_decision(decision, iteration, origin_file, state, paths=LEGACY_PATHS,
                     train_essays="train_essays.json"):
    """执行决策的全部副作用（补评、复核、协议状态更新）。

    返回 `(decision, state, touched)`；最终 decision 只有 "continue" / "stop"，
    `touched` 为被补评的版本列表。
    """
    if decision.action == "continue":
        return decision, state, []

    if decision.action == "upgrade":
        before_prev = version_mean_counts(iteration - 1, paths, state=state)
        before_cur = version_mean_counts(iteration, paths, state=state)
        delta_before = q_value(before_cur) - q_value(before_prev)
        print(
            f"[gate] V{iteration} 低精度预警：ΔQ={delta_before:+.2f}"
            f"（严格为负才算改善）；补评 V{iteration - 1}、V{iteration} 后以双评均值复核"
        )
        touched = []
        if top_up_version(iteration - 1, origin_file, paths=paths,
                          train_essays=train_essays):
            touched.append(iteration - 1)
        if top_up_version(iteration, origin_file, paths=paths,
                          train_essays=train_essays):
            touched.append(iteration)
        # 边界双评：复核必须使用**升级后状态**下的均值（允许 x-1/x 各计两次运行）。
        upgraded_state = {
            **state,
            "repeat_level": REPEAT_LEVEL_HIGH,
            "upgraded_at": state.get("upgraded_at") or iteration,
        }
        after_prev = version_mean_counts(iteration - 1, paths, state=upgraded_state)
        after_cur = version_mean_counts(iteration, paths, state=upgraded_state)
        delta_after = q_value(after_cur) - q_value(after_prev)
        confirmed = gate_signal(after_prev, after_cur)
        events = list(state.get("warning_events", []))
        events.append(
            {
                "iteration": iteration,
                "kind": "low_precision_warning",
                "delta_q_before": round(delta_before, 4),
                "delta_q_after": round(delta_after, 4),
                "confirmed": confirmed,
            }
        )
        state = {**upgraded_state, "warning_events": events}
        log_precision_event(
            "precision_upgrade",
            decision.reason,
            (iteration - 1, iteration),
            extra={
                "iteration": iteration,
                "delta_q_before": round(delta_before, 4),
                "delta_q_after": round(delta_after, 4),
                "confirmed": confirmed,
            },
        )
        save_protocol_state(state)
        if confirmed:
            print(
                f"[gate] 双评复核确认：ΔQ={delta_after:+.2f}；停止迭代，"
                f"候选 V{iteration - 1}、V{iteration}"
            )
            return (
                IterationDecision(
                    "stop", "first_warning_confirmed", (iteration - 1, iteration)
                ),
                state,
                touched,
            )
        if iteration >= MAX_AUTO_VERSION:
            print("[gate] 双评复核未确认，但已在上限处；按上限收尾进入 validation")
            return (
                IterationDecision("stop", "cap_reached", (iteration - 1, iteration)),
                state,
                touched,
            )
        print(
            f"[gate] 双评复核未确认：ΔQ={delta_after:+.2f}，判为低精度噪声；"
            "接受当前版本并继续（此后固定双评，不可回退）"
        )
        return (
            IterationDecision("continue", "warning_refuted_by_precision"),
            state,
            touched,
        )

    touched = []
    if decision.top_up_first:
        print(
            f"[precision] 到达上限且从未预警：强制提升精度，"
            f"只补评 V{iteration - 1}、V{iteration}（不向前回补）"
        )
        if top_up_version(iteration - 1, origin_file, paths=paths,
                          train_essays=train_essays):
            touched.append(iteration - 1)
        if top_up_version(iteration, origin_file, paths=paths,
                          train_essays=train_essays):
            touched.append(iteration)
        state = {
            **state,
            "repeat_level": REPEAT_LEVEL_HIGH,
            "upgraded_at": state.get("upgraded_at") or iteration,
        }
        log_precision_event(
            "precision_upgrade",
            "cap_forced",
            (iteration - 1, iteration),
            extra={"iteration": iteration},
        )
        save_protocol_state(state)
    return decision, state, touched


def rebuild_protocol_timeline(up_to_version=MAX_AUTO_VERSION,
                              max_version=MAX_AUTO_VERSION,
                              state_path=PROTOCOL_STATE,
                              route_path="b_rebuilt_route.json"):
    """离线重建官方路线并落盘（不发起任何 API 调用）。

    - 按新协议真实时序重放（低精度只用 run1；首次预警后 x-1/x 双评复核；
      升级永久；上限处的高精度 gate 记为 `high_precision_gate_at_cap`）；
    - 用重建结果**覆盖** `b_protocol_state.json`；
    - 写出 `b_rebuilt_route.json`（逐版本行 + 说明）；
    - 追加 `b_rerun_log.json` 的 `timeline_rebuild` 条目（保留 live 会话真相）。
    """
    state, rows = replay_protocol_route(up_to_version, max_version=max_version)
    state["rebuilt_at"] = datetime.now().isoformat(timespec="seconds")
    state["note"] = (
        "按新协议真实时序离线重建（不消耗 API）。live 会话曾在精度等级判断前以 "
        "cap_forced 执行 (V5,V6) 补评；其测量位置与路线要求的双评位置一致，"
        "测量全部采纳；官方停止原因按重建时序记为 high_precision_gate_at_cap，"
        "cap_forced 记录保留于 b_rerun_log.json 以保留可追溯真相。"
    )
    save_protocol_state(state, path=state_path)
    route = {
        "protocol_version": PROTOCOL_VERSION,
        "w": Q_SEVERE_WEIGHT,
        "upgraded_at": state.get("upgraded_at"),
        "repeat_level": state.get("repeat_level"),
        "warning_events": state.get("warning_events", []),
        "stop": state.get("stop"),
        "rows": rows,
        "note": state["note"],
    }
    Path(route_path).write_text(
        json.dumps(route, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    record_rerun(
        {
            "kind": "timeline_rebuild",
            "trigger": "freeze_correction",
            "recorded_at": state["rebuilt_at"],
            "w": Q_SEVERE_WEIGHT,
            "protocol_version": PROTOCOL_VERSION,
            "upgraded_at": state.get("upgraded_at"),
            "stop": state.get("stop"),
            "note": state["note"],
        }
    )
    print(
        f"[rebuild] 官方路线重建完成：upgraded_at={state.get('upgraded_at')}，"
        f"stop={state.get('stop')}"
    )
    print(f"[rebuild] 已写出 {route_path} 与修正后的 {state_path}")
    return state, rows, route_path


def write_final_evidence(iteration=MAX_AUTO_VERSION, paths=LEGACY_PATHS,
                         output="final_evidence.json"):
    """冻结用 final 证据文件：列出训练双评、validation 双评、均值、Q 与选择理由。"""
    state = protocol_state_or_default()

    def train_record(version):
        mean = version_mean_counts(version, paths, state=state)
        return {
            "runs": version_run_counts(version, paths),
            "mean_severe": round(mean["severe"], 4),
            "mean_soft": round(mean["soft"], 4),
            "q": round(mean["q"], 4),
            "runs_used": mean["runs"],
            "excluded_runs": mean["excluded"],
        }

    validation = None
    validation_path = Path(VALIDATION_REPORT)
    if validation_path.is_file():
        payload = json.loads(validation_path.read_text(encoding="utf-8"))
        validation = {
            "report": str(validation_path),
            "candidates": payload.get("candidates"),
            "selected_iteration": payload.get("selected_iteration"),
        }
    evidence = {
        "protocol_version": PROTOCOL_VERSION,
        "w": Q_SEVERE_WEIGHT,
        "final_iteration": iteration,
        "selection_rule": (
            "validation: Q -> fewer mean severe -> earlier version; MAE recorded only"
        ),
        "train": {
            f"V{iteration - 1}": train_record(iteration - 1),
            f"V{iteration}": train_record(iteration),
        },
        "validation": validation,
        "notes": [
            "validation（test_essays.json，12 篇）参与了 V5/V6 选择，不是独立 test；本项目独立测试为空。",
            "final_train_scoring_results.json / final_aes_badcases.json 为选中版本 run1 的同源副本；"
            "双评均值基准见 final_train_scoring_results_mean.json。",
            "V5 与 V6 的 validation 差异（ΔQ=0.5，全部来自 soft 的少量条目）处于噪声量级；"
            "选中 V6 是遵守预定排序规则，不构成 V6 显著优于 V5 的证据。",
        ],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    Path(output).write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[final-evidence] 已写出 {output}")
    return evidence


def write_mean_baseline(iteration=MAX_AUTO_VERSION, paths=LEGACY_PATHS,
                        output="final_train_scoring_results_mean.json",
                        provenance="final_train_scoring_results_mean.provenance.json"):
    """E 类基准：对选中版本两次训练的**逐篇逐维 AI 分算术平均**，教师分不变。

    保留原始两次运行；本文件是派生基准（按全局 index 对齐），供 E 类直接消费。
    """
    files = version_scoring_paths(iteration, paths)
    if not files:
        raise FileNotFoundError(
            f"Cannot build mean baseline: no scoring artifacts for V{iteration}"
        )
    datasets = [json.loads(Path(name).read_text(encoding="utf-8")) for name in files]
    lookups = [{int(row["index"]): row for row in dataset} for dataset in datasets]
    merged = []
    for row in datasets[0]:
        index = int(row["index"])
        merged_row = dict(row)
        merged_row["AI"] = {
            dimension: sum(lookup[index]["AI"][dimension] for lookup in lookups)
            / len(lookups)
            for dimension in ("content", "expression", "structure")
        }
        merged.append(merged_row)
    Path(output).write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    provenance_payload = {
        "derived_from": [
            {"file": name, "sha256": sha256(Path(name))} for name in files
        ],
        "method": (
            "per-essay per-dimension arithmetic mean of AI scores across the two "
            "training runs; teacher labels unchanged; aligned by global index"
        ),
        "iteration": iteration,
        "w": Q_SEVERE_WEIGHT,
        "protocol_version": PROTOCOL_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    Path(provenance).write_text(
        json.dumps(provenance_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[mean-baseline] 已写出 {output} 与 {provenance}")
    return output


def version_review(iteration, state=None):
    """汇总一个版本的结构与评分状态（均值口径；聚合遵循协议路线）。"""
    from prompt_structure_contract import parse_bold_sections, validate_input_contract

    review = {"iteration": iteration}
    meta_file = prompt_meta_path(iteration)
    review["prompt_meta"] = meta_file

    if Path(meta_file).is_file():
        text = Path(meta_file).read_text(encoding="utf-8")
        violations = validate_input_contract(text)
        review.update(
            {
                "chars": len(text),
                "bold_sections": len(parse_bold_sections(text)),
                "top_headings": len(
                    [line for line in text.splitlines() if line.startswith("## ")]
                ),
                "contract_ok": not violations,
                "contract_violations": [str(item) for item in violations],
            }
        )

    runs = version_run_counts(iteration)
    if runs:
        mean = version_mean_counts(iteration, state=state)
        review.update(
            {
                "severe": round(mean["severe"], 2),
                "soft": round(mean["soft"], 2),
                "q": round(mean["q"], 2),
                "runs": runs,
                "runs_used": mean["runs"],
                "excluded_runs": mean["excluded"],
            }
        )
        maes = [scoring_mae(path) for path in version_scoring_paths(iteration)]
        review["mae"] = round(sum(maes) / len(maes), 4)
    return review


def print_version_review(review):
    def fmt(value):
        return f"{value:.2f}" if isinstance(value, (int, float)) else "—"

    print("\n" + "=" * 72)
    print(f"版本审查 —— V{review['iteration']}")
    print("=" * 72)
    print(f"  prompt meta : {review.get('prompt_meta')}")
    print(
        f"  结构        : 字符 {review.get('chars', '—')} / 加粗分点 "
        f"{review.get('bold_sections', '—')} / 顶层章节 {review.get('top_headings', '—')}"
    )
    if review.get("contract_ok"):
        print("  结构契约    : PASS")
    else:
        print("  结构契约    : FAIL")
        for item in review.get("contract_violations", []):
            print(f"      - {item}")
    print(
        f"  B bias 均值 : severe={fmt(review.get('severe'))} "
        f"soft={fmt(review.get('soft'))} Q={fmt(review.get('q'))} "
        f"(w={Q_SEVERE_WEIGHT})"
    )
    print(f"  训练集 MAE  : {review.get('mae', '—')}（仅记录）")
    runs = review.get("runs")
    if runs:
        used = review.get("runs_used", len(runs))
        print(f"  运行明细    : {runs}（路线内计入 {used} 次）")
    if review.get("excluded_runs"):
        print(f"  探索性(未计入): {review['excluded_runs']}")
    print("=" * 72)


def append_version_review(review, path="b_version_review.json"):
    history = []
    if os.path.exists(path):
        try:
            history = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            history = []
    history.append(
        {**review, "recorded_at": datetime.now().isoformat(timespec="seconds")}
    )
    Path(path).write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


# 迭代前后对比的字段：键、中文标签、是否越大越好
DELTA_FIELDS = (
    ("chars", "字符数", None),
    ("top_headings", "顶层章节", None),
    ("bold_sections", "加粗分点", None),
    ("severe", "B-severe(均值)", False),
    ("soft", "B-soft(均值)", False),
    ("q", "Q=2.5S+F", False),
    ("mae", "训练集 MAE(均值)", False),
)


def _render_value(value):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def version_delta(previous, current):
    """V(n-1) 与 V(n) 的逐项变化（大纲 §12.2 逐版本表的数据源）。"""
    rows = []
    for field, label, lower_is_better in DELTA_FIELDS:
        before = previous.get(field) if previous else None
        after = current.get(field)
        change = None
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            change = round(after - before, 4)
        rows.append(
            {
                "field": field,
                "label": label,
                "before": before,
                "after": after,
                "change": change,
                "lower_is_better": lower_is_better,
            }
        )
    return {
        "from_iteration": previous.get("iteration") if previous else None,
        "to_iteration": current.get("iteration"),
        "rows": rows,
        "contract_ok": current.get("contract_ok"),
        "contract_from_ok": previous.get("contract_ok") if previous else None,
    }


def print_version_delta(delta):
    previous = delta["from_iteration"]
    current = delta["to_iteration"]
    title = f"V{previous} -> V{current}" if previous is not None else f"origin -> V{current}"
    print("\n" + "-" * 72)
    print(f"迭代前后对比：{title}")
    print("-" * 72)
    print(f"  {'指标':<16}{'before':>14}{'after':>14}{'变化':>14}")
    for row in delta["rows"]:
        change = row["change"]
        marker = ""
        if change is not None and change != 0 and row["lower_is_better"] is not None:
            improved = (change < 0) if row["lower_is_better"] is False else (change > 0)
            marker = "  改进" if improved else "  恶化"
        change_text = f"{change:+.4f}" if isinstance(change, float) else (
            f"{change:+d}" if isinstance(change, int) else "—"
        )
        print(
            f"  {row['label']:<16}{_render_value(row['before']):>14}"
            f"{_render_value(row['after']):>14}{change_text:>14}{marker}"
        )
    contract_text = "PASS" if delta["contract_ok"] else "FAIL"
    before_text = (
        "—"
        if delta["contract_from_ok"] is None
        else ("PASS" if delta["contract_from_ok"] else "FAIL")
    )
    print(f"  {'结构契约':<16}{before_text:>14}{contract_text:>14}")
    print("-" * 72)


def confirm_next_iteration(iteration, auto_continue=False):
    """人工确认闸门：未确认则停在当前版本（大纲 §10 第 4 步）。"""
    if auto_continue:
        print("\n  [--auto-continue] 跳过人工确认，继续下一次迭代。")
        return True
    question = f"\n确认进入下一次迭代（V{iteration} -> V{iteration + 1}）？[y/N]: "
    try:
        answer = input(question).strip().lower()
    except EOFError:
        print("\n[STOP] 无交互式输入，默认不继续。")
        return False
    return answer in ("y", "yes", "是", "继续")


def hold_message(iteration):
    return (
        f"\n[HOLD] 已在 V{iteration} 停止，等待人工审查后再放行下一轮（大纲 §10 第 4 步）。\n"
        f"  当前 prompt meta : {prompt_meta_path(iteration)}\n"
        f"  训练集评分       : {train_scoring_path(iteration)}\n"
        f"  版本审查记录     : b_version_review.json\n"
        f"  继续方式         : python pipeline_entry.py --resume-from {iteration}"
    )


def cv_protocol_marker_present():
    """CV 协议标记是否存在（集中在此，便于测试替身与将来恢复 k 折）。"""
    return os.path.exists("cv_folds.json")


def run_pipeline(origin_file, resume_from=0, max_version=MAX_AUTO_VERSION,
                 stop_after=None, auto_continue=False, reuse_existing_meta=False):
    if cv_protocol_marker_present():
        raise SystemExit(
            "检测到 CV 划分协议（cv_folds.json 存在），拒绝执行旧入口 run_pipeline。\n"
            "原因：run_pipeline 用 test_essays.json 作为版本比较集，而在 CV 协议下\n"
            "      test_essays.json 是报告留出的别名。继续执行会把报告留出用作\n"
            "      选版依据，永久破坏它的无偏性。\n"
            "请改用：python cv_runner.py [--execute]\n"
            "若确实要回到旧协议，需先移除 cv_folds.json 并恢复对应的 train/test 划分。"
        )

    print("=" * 60)
    print("AES prompt self-iteration pipeline")
    print("=" * 60)

    if not 0 <= resume_from <= max_version:
        raise ValueError("resume_from must be between V0 and the cap")

    if resume_from == 0:
        assert_clean_working_directory()
        print("\n[0] Extract train/test essays")
        extract_samples(origin_file)
        print("\n[0] Build origin prompt and score training set")
        ensure_origin_prompt()
        manifest_path = write_run_manifest(origin_file, max_version)
        print(f"[manifest] {manifest_path}")
        run_scoring(ORIGIN_PROMPT, "train_essays.json", train_scoring_path(0), origin_file)
        run_badcase_mining(train_scoring_path(0), badcase_path(0))
        state = new_protocol_state()
        save_protocol_state(state)
        review = version_review(0)
        print_version_review(review)
        print_version_delta(version_delta(None, review))
        append_version_review(review)
        record_version_in_manifest(0, manifest_path)
        previous_review = review
    else:
        print(f"\n[resume] Reusing completed artifacts through V{resume_from}")
        # 复用旧产物前先核对 manifest 身份（大纲 §9.2 末句）：
        # 模型 / 数据 / prompt / 阈值 / 划分任一不一致即拒绝；仅策略声明
        # （stop_policy / protocol）允许受控迁移为新协议并登记。
        manifest_path = assert_reusable_artifacts(origin_file, max_version)
        if manifest_path is None:
            print("[manifest][WARN] 无既有 run manifest，本轮版本记录将不会被写入 manifest")
        state = load_protocol_state(resume_from)
        previous_review = version_review(resume_from, state=state)
        print_version_review(previous_review)
        # 高精度阶段：到达即双评（resume 处补齐，防止中断点留下单评缺口）。
        if state["repeat_level"] == REPEAT_LEVEL_HIGH:
            performed = top_up_version(resume_from, origin_file)
            if performed:
                log_precision_event(
                    "precision_topup", "high_precision_arrival", (resume_from,)
                )
        # resume 边界先做一次决策：可能正落在 gate 落点或 V6 上限。
        if resume_from >= 1:
            decision = decide_iteration(
                version_mean_counts(resume_from - 1, state=state),
                version_mean_counts(resume_from, state=state),
                resume_from,
                max_version,
                state["repeat_level"],
            )
            decision, state, touched = resolve_decision(
                decision, resume_from, origin_file, state
            )
            for version in touched:
                if manifest_path is not None:
                    record_version_in_manifest(version, manifest_path)
            if decision.action == "stop":
                print(
                    f"\n[{resume_from}] validation 触发：{decision.reason}"
                    f"；候选 {decision.candidates}"
                )
                final_iteration = run_validation_and_select(
                    decision.candidates, origin_file, decision.reason
                )
                if manifest_path is not None:
                    for version in decision.candidates:
                        record_version_in_manifest(version, manifest_path)
                final_meta = prompt_meta_path(final_iteration)
                save_final_prompt(final_meta, final_iteration)
                print(f"FINAL_PROMPT_META={final_meta}")
                return final_meta

    if resume_from >= max_version:
        # 理论上不可达：上限处的边界决策必然在上面停止并完成 validation/final。
        # 若走到这里，说明状态或产物异常，拒绝越过上限继续优化。
        raise SystemExit(
            f"resume_from={resume_from} 已在上限且边界决策未触发停止；"
            "拒绝越过上限继续，请检查 b_protocol_state.json 与评分产物。"
        )

    print(f"\n[{resume_from} -> {resume_from + 1}] Optimize prompt meta")
    target_meta = prompt_meta_path(resume_from + 1)
    if reuse_existing_meta and os.path.exists(target_meta):
        print(f"[reuse] 复用既有 {target_meta}，跳过优化器调用")
    else:
        optimize_or_stop(
            prompt_meta_path(resume_from),
            badcase_path(resume_from),
            target_meta,
            resume_from + 1,
        )

    iteration = resume_from + 1
    while True:
        print(f"\n[{iteration}] Score training set and mine badcases")
        run_train_iteration(iteration, origin_file)
        # 高精度阶段：每个新版本固定双评（到达即补齐第二次）。
        if state["repeat_level"] == REPEAT_LEVEL_HIGH:
            performed = top_up_version(iteration, origin_file)
            if performed:
                log_precision_event(
                    "precision_topup", "high_precision_arrival", (iteration,)
                )

        current_mean = version_mean_counts(iteration, state=state)
        print(f"[{iteration}] Q(均值, w={Q_SEVERE_WEIGHT})={current_mean['q']:.2f}")
        decision = decide_iteration(
            version_mean_counts(iteration - 1, state=state),
            current_mean,
            iteration,
            max_version,
            state["repeat_level"],
        )
        decision, state, touched = resolve_decision(
            decision, iteration, origin_file, state
        )
        print(f"[{iteration}] 决策：{decision.action}（{decision.reason}）")

        review = version_review(iteration, state=state)
        if iteration - 1 in touched:
            # review 项 9：x-1 被边界补评后，差值表必须使用补评后的均值。
            previous_review = version_review(iteration - 1, state=state)
        print_version_review(review)
        delta = version_delta(previous_review, review)
        print_version_delta(delta)
        append_version_review({**review, "delta": delta})
        if manifest_path is not None:
            record_version_in_manifest(iteration, manifest_path)
            for version in touched:
                if version != iteration:
                    record_version_in_manifest(version, manifest_path)
        previous_review = review

        if stop_after is not None and iteration >= stop_after:
            print(hold_message(iteration))
            return prompt_meta_path(iteration)

        if decision.action == "stop":
            print(
                f"\n[{iteration}] validation 触发：{decision.reason}"
                f"；候选 {decision.candidates}"
            )
            final_iteration = run_validation_and_select(
                decision.candidates, origin_file, decision.reason
            )
            if manifest_path is not None:
                for version in decision.candidates:
                    record_version_in_manifest(version, manifest_path)
            final_meta = prompt_meta_path(final_iteration)
            save_final_prompt(final_meta, final_iteration)
            print(f"\nFINAL_PROMPT_META={final_meta}")
            return final_meta

        if not confirm_next_iteration(iteration, auto_continue):
            print(hold_message(iteration))
            return prompt_meta_path(iteration)

        print(f"\n[{iteration} -> {iteration + 1}] Optimize prompt meta")
        target_meta = prompt_meta_path(iteration + 1)
        if reuse_existing_meta and os.path.exists(target_meta):
            print(f"[reuse] 复用既有 {target_meta}，跳过优化器调用")
        else:
            optimize_or_stop(
                prompt_meta_path(iteration),
                badcase_path(iteration),
                target_meta,
                iteration + 1,
            )

        iteration += 1


def main():
    parser = argparse.ArgumentParser(
        description="Run the full AES prompt self-iteration pipeline."
    )
    parser.add_argument(
        "--origin-file",
        default="origin_scoring_results.json",
        help="Input scoring results file used to extract train/test essays.",
    )
    parser.add_argument(
        "--resume-from",
        type=int,
        default=0,
        help="Reuse complete artifacts through this version and continue from there.",
    )
    parser.add_argument(
        "--max-version",
        type=int,
        default=MAX_AUTO_VERSION,
        help="Last automatically generated version (default: V6).",
    )
    parser.add_argument(
        "--stop-after",
        type=int,
        default=None,
        help=(
            "Score up to this version, then stop for human review without "
            "optimizing further (大纲 §10 step 4)."
        ),
    )
    parser.add_argument(
        "--auto-continue",
        action="store_true",
        help=(
            "Skip the per-version human confirmation gate. Default is to stop "
            "after every scored version and wait for confirmation."
        ),
    )
    parser.add_argument(
        "--reuse-existing-meta",
        action="store_true",
        help=(
            "If the next version's prompt meta already exists, reuse it instead "
            "of calling the optimizer again (used when resuming at a breakpoint)."
        ),
    )
    args = parser.parse_args()
    run_pipeline(
        args.origin_file,
        args.resume_from,
        args.max_version,
        args.stop_after,
        args.auto_continue,
        args.reuse_existing_meta,
    )


if __name__ == "__main__":
    main()
