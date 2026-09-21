"""B 路线候选指标（ETYPE_ITERATION_V2_DESIGN.md §14）。

问题背景
--------
现指标 ``Score = 5 * severe + soft`` 中的 ``severe``/``soft`` 只判断每篇的
**跨维均值** ``|bias_score|`` 是否跨过 1.5 / 1.0 阈值。它对逐篇分辨率完全不可见，
于是 V0→V6 的重放显示选版实际由 ``severe`` 这一个计数单独决定：

    validation: V4 = (Score 3, severe 0)  被选中
                V5 = (Score 11, severe 2) 被淘汰
    但 V5 在其余四项上全部优于 V4（mean|bias| / MAE / aligned / exact-hit）。

本模块补齐分辨率分量。分量的选择是**由重放实测决定的**：

- 采用**计数型** ``aligned_rate`` / ``exact_hit_rate``：抗离群，双集单调改善
  （train aligned 13.9%→50.9%，validation 16.7%→58.3%）。
- 明确**不采用** ``pstdev(AI - teacher)``：该量被离群样本主导，n=36 时
  SD 的标准误约为 ``sd / sqrt(2n) ≈ 12%``，无法分辨 4% 量级的差异，
  会把 V3/V4 误判为"分辨率退化"（实测 V0→V4 的 1.083→1.101 在噪声内）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


DIMENSIONS = ("content", "expression", "structure")

SEVERE_ABS_BIAS = 1.5
SOFT_ABS_BIAS = 1.0
SEVERE_MIN_DIRECTION = 3
SOFT_MIN_DIRECTION = 2

BIAS_WEIGHT_SEVERE = 5
BIAS_WEIGHT_SOFT = 1

# 相对 incumbent 的容忍度。分辨率分量是计数型的，容忍度以"评分格"为单位，
# 而不是以比率为单位——n=108 时 1 格 = 0.93%，0 容忍会把单格抖动判成退化。
DEFAULT_ALIGNED_TOLERANCE_CELLS = 1
DEFAULT_MAE_TOLERANCE = 0.0


def label_floor(teacher_score: float) -> float:
    """教师半整数标签的误差下限为 0.5，整数标签为 0（见设计稿 §12）。"""
    return 0.0 if abs(teacher_score - round(teacher_score)) < 1e-9 else 0.5


@dataclass(frozen=True)
class BiasMetrics:
    """单个候选版本在一个 split 上的指标全集。"""

    sample_count: int
    severe: int
    soft: int
    bias_score: int
    mean_abs_cross_dim_bias: float
    aligned_count: int
    exact_count: int
    score_count: int
    aligned_rate: float
    exact_hit_rate: float
    mae: float
    mae_above_floor: float

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def format_row(self) -> str:
        return (
            f"n={self.sample_count:<3d} sev={self.severe:<3d} soft={self.soft:<3d} "
            f"Score={self.bias_score:<4d} |bias|={self.mean_abs_cross_dim_bias:6.3f} "
            f"aligned={100 * self.aligned_rate:5.1f}% exact={100 * self.exact_hit_rate:5.1f}% "
            f"MAE={self.mae:6.3f} MAE-floor={self.mae_above_floor:6.3f}"
        )


def measure(rows: Sequence[Mapping[str, Any]]) -> BiasMetrics:
    """从一个评分结果文件的行集合计算全部指标分量。"""
    if not rows:
        raise ValueError("Cannot measure bias metrics from an empty scoring result.")

    severe = soft = 0
    abs_biases: List[float] = []
    errors: List[float] = []
    excess: List[float] = []
    aligned = exact = 0
    score_count = 0

    for row in rows:
        diffs = [float(row["AI"][dim]) - float(row["teacher"][dim]) for dim in DIMENSIONS]
        bias = sum(diffs) / len(diffs)
        abs_biases.append(abs(bias))

        sign = 1 if bias > 0 else -1 if bias < 0 else 0
        direction = sum(1 for value in diffs if sign != 0 and value * sign > 0)
        if abs(bias) > SEVERE_ABS_BIAS and direction >= SEVERE_MIN_DIRECTION:
            severe += 1
        elif abs(bias) > SOFT_ABS_BIAS and direction >= SOFT_MIN_DIRECTION:
            soft += 1

        for dimension in DIMENSIONS:
            teacher = float(row["teacher"][dimension])
            error = abs(float(row["AI"][dimension]) - teacher)
            floor = label_floor(teacher)
            errors.append(error)
            excess.append(error - floor)
            score_count += 1
            if error <= floor + 1e-9:
                aligned += 1
            if error <= 1e-9:
                exact += 1

    return BiasMetrics(
        sample_count=len(rows),
        severe=severe,
        soft=soft,
        bias_score=BIAS_WEIGHT_SEVERE * severe + BIAS_WEIGHT_SOFT * soft,
        mean_abs_cross_dim_bias=sum(abs_biases) / len(abs_biases),
        aligned_count=aligned,
        exact_count=exact,
        score_count=score_count,
        aligned_rate=aligned / score_count,
        exact_hit_rate=exact / score_count,
        mae=sum(errors) / len(errors),
        mae_above_floor=sum(excess) / len(excess),
    )


def measure_file(scoring_path: str) -> BiasMetrics:
    rows = json.loads(Path(scoring_path).read_text(encoding="utf-8"))
    return measure(rows)


def legacy_rank(metrics: BiasMetrics, iteration: int):
    """现规则：Score -> severe -> 更早版本。保留用于对照。"""
    return (metrics.bias_score, metrics.severe, iteration)


@dataclass(frozen=True)
class SelectionDecision:
    accepted: bool
    reasons: List[str]


def compare(
    candidate: BiasMetrics,
    incumbent: BiasMetrics,
    aligned_tolerance_cells: float = DEFAULT_ALIGNED_TOLERANCE_CELLS,
    mae_tolerance: float = DEFAULT_MAE_TOLERANCE,
) -> SelectionDecision:
    """多分量接受规则：偏置不劣化、分辨率不退化，且至少一项严格改善。

    单一 ``severe`` 计数不再拥有否决权——它只是偏置分量的一部分。
    """
    reasons: List[str] = []
    strictly_better = False
    not_worse = True

    if candidate.bias_score < incumbent.bias_score:
        strictly_better = True
    elif candidate.bias_score > incumbent.bias_score:
        not_worse = False
        reasons.append(
            f"bias_score 劣化 {incumbent.bias_score} -> {candidate.bias_score}"
        )

    aligned_delta = candidate.aligned_count - incumbent.aligned_count
    if aligned_delta > aligned_tolerance_cells:
        strictly_better = True
    elif aligned_delta < -aligned_tolerance_cells:
        not_worse = False
        reasons.append(
            f"aligned 退化 {incumbent.aligned_count}/{incumbent.score_count}"
            f" -> {candidate.aligned_count}/{candidate.score_count} ({aligned_delta:+.0f} 格)"
        )

    limit = incumbent.mae_above_floor * (1.0 + mae_tolerance)
    if candidate.mae_above_floor <= limit:
        if candidate.mae_above_floor < incumbent.mae_above_floor:
            strictly_better = True
    else:
        not_worse = False
        reasons.append(
            "mae_above_floor 退化 "
            f"{incumbent.mae_above_floor:.3f} -> {candidate.mae_above_floor:.3f}"
        )

    accepted = not_worse and strictly_better
    if not accepted and not reasons:
        reasons.append("无任何分量严格改善")
    return SelectionDecision(accepted=accepted, reasons=reasons)


def dominates(left: BiasMetrics, right: BiasMetrics) -> bool:
    """left 在 {bias_score, aligned_count, mae_above_floor} 上不劣且至少一项更优。"""
    not_worse = (
        left.bias_score <= right.bias_score
        and left.aligned_count >= right.aligned_count
        and left.mae_above_floor <= right.mae_above_floor
    )
    strictly_better = (
        left.bias_score < right.bias_score
        or left.aligned_count > right.aligned_count
        or left.mae_above_floor < right.mae_above_floor
    )
    return not_worse and strictly_better


def pareto_frontier(metrics: Mapping[str, BiasMetrics]) -> List[str]:
    """不被任何其他版本支配的版本集合——即真正的取舍集合。"""
    return [
        version
        for version in metrics
        if not any(
            other != version and dominates(metrics[other], metrics[version])
            for other in metrics
        )
    ]


@dataclass(frozen=True)
class SequentialSelection:
    selected: str
    accepted_versions: List[str]
    rejected: List[Dict[str, Any]]


def select_sequential(
    versions: Sequence[str],
    metrics: Mapping[str, BiasMetrics],
    aligned_tolerance_cells: float = DEFAULT_ALIGNED_TOLERANCE_CELLS,
    mae_tolerance: float = DEFAULT_MAE_TOLERANCE,
) -> SequentialSelection:
    """按版本顺序模拟逐轮接受/拒绝，返回最终留存的版本。"""
    if not versions:
        raise ValueError("No versions supplied for sequential selection.")

    incumbent = versions[0]
    accepted = [incumbent]
    rejected: List[Dict[str, Any]] = []

    for version in versions[1:]:
        decision = compare(
            metrics[version],
            metrics[incumbent],
            aligned_tolerance_cells=aligned_tolerance_cells,
            mae_tolerance=mae_tolerance,
        )
        if decision.accepted:
            incumbent = version
            accepted.append(version)
        else:
            rejected.append({"version": version, "reasons": decision.reasons})

    return SequentialSelection(
        selected=incumbent, accepted_versions=accepted, rejected=rejected
    )
