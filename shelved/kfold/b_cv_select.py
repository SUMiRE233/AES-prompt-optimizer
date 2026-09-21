"""把 CV 折的评估产物汇总为 out-of-fold 曲线并选迭代深度（设计稿 §14）。

原理
----
折 j 在深度 t 上产出 `cv_runs/fold{j}/eval_scoring_results{t}.json`（9 篇）。
把 k 个折在同一深度 t 上的评估集**拼接**，得到 36 篇、且每篇恰好被留出过一次的
out-of-fold 预测。因为 `b_metric` 的所有分量（severe / soft / aligned / exact /
MAE）都逐篇可加，拼接后直接 `measure` 即可，不需要改动指标模块。

选深度的是 `b_metric` 的多分量规则（偏置不劣化 + 分辨率不退化 + 至少一项严格改善），
而不是单一的 severe 计数。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from b_metric import BiasMetrics, measure, select_sequential


CV_RUN_ROOT = "cv_runs"


@dataclass(frozen=True)
class DepthPoint:
    depth: int
    metrics: BiasMetrics
    fold_sizes: List[int]

    @property
    def fold_count(self) -> int:
        return len(self.fold_sizes)


def fold_dir(fold: int, root: str = CV_RUN_ROOT) -> str:
    return os.path.join(root, f"fold{fold}")


def eval_scoring_path(fold: int, depth: int, root: str = CV_RUN_ROOT) -> str:
    return os.path.join(fold_dir(fold, root), f"eval_scoring_results{depth}.json")


def load_folds(manifest_path: str = "cv_folds.json") -> List[int]:
    payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    return [int(item["fold"]) for item in payload["folds"]]


def load_curve(
    depths: Sequence[int],
    folds: Sequence[int],
    root: str = CV_RUN_ROOT,
    require_all_folds: bool = True,
) -> Dict[int, DepthPoint]:
    """读取所有折在给定深度上的评估产物，拼成 out-of-fold 曲线。

    只有**全部折都齐备**的深度才会进入曲线（`require_all_folds=True`）。
    缺折的深度被整段丢弃，因此调用方必须先用 `available_depths()` 取交集，
    再用 `find_depth_gaps()` 确认没有中间断档——否则序贯比较会把 V1 与 V3
    当成相邻深度。
    """
    curve: Dict[int, DepthPoint] = {}
    for depth in depths:
        rows: List[dict] = []
        sizes: List[int] = []
        for fold in folds:
            path = eval_scoring_path(fold, depth, root)
            if not Path(path).exists():
                continue
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            rows.extend(payload)
            sizes.append(len(payload))
        if not rows:
            continue
        if require_all_folds and len(sizes) != len(folds):
            continue
        curve[depth] = DepthPoint(depth=depth, metrics=measure(rows), fold_sizes=sizes)
    return curve


def available_depths(folds: Sequence[int], root: str = CV_RUN_ROOT) -> List[int]:
    """扫描折目录，列出所有折都齐备的深度。"""
    depths: Optional[set] = None
    for fold in folds:
        directory = Path(fold_dir(fold, root))
        if not directory.exists():
            return []
        found = set()
        for path in directory.glob("eval_scoring_results*.json"):
            stem = path.stem.replace("eval_scoring_results", "")
            if stem.isdigit():
                found.add(int(stem))
        depths = found if depths is None else (depths & found)
    return sorted(depths or [])


def find_depth_gaps(depths: Sequence[int]) -> List[int]:
    """返回可用深度集合中的断档（缺失但位于最小/最大深度之间的深度）。"""
    if not depths:
        return []
    present = {int(depth) for depth in depths}
    return [
        depth
        for depth in range(min(present), max(present) + 1)
        if depth not in present
    ]


def validate_depth_set(depths: Sequence[int]) -> None:
    """序贯选深度的前置条件：必须从 V0 连续覆盖，不允许中间断档。

    - 缺少 V0 会让基线从某个 Vt 起算，改善量变成相对 Vt 而非 origin；
    - 中间断档会让 `select_sequential` 把不相邻的深度当成相邻比较。
    两种情况都会静默地改变停止条件的含义，因此这里直接拒绝。
    """
    if not depths:
        raise ValueError("没有可用的折评估深度。")
    ordered = sorted(int(depth) for depth in depths)
    if ordered[0] != 0:
        raise ValueError(
            f"折评估深度缺少 V0（已有 {ordered}）。"
            "序贯选深度必须以 origin 基线为起点，否则改善量会被错误地相对某个 Vt 计算。"
        )
    gaps = find_depth_gaps(ordered)
    if gaps:
        raise ValueError(
            f"折评估深度存在断档 {gaps}（已有 {ordered}）。"
            "序贯比较要求深度连续，否则会把 (V{gaps[0] - 1}, V{gaps[0] + 1}) 当成相邻。"
        )


def select_depth(
    curve: Mapping[int, DepthPoint],
) -> tuple:
    """按多分量规则在 out-of-fold 曲线上选深度。

    返回 (选中深度, 接受的深度链, 拒绝记录)。
    """
    if not curve:
        raise ValueError("out-of-fold 曲线为空，无法选择深度。")
    ordered = sorted(curve)
    validate_depth_set(ordered)
    versions = [f"V{depth}" for depth in ordered]
    table = {f"V{depth}": curve[depth].metrics for depth in ordered}
    result = select_sequential(versions, table)
    depth = int(result.selected.lstrip("V"))
    return depth, result.accepted_versions, result.rejected


def render_curve(curve: Mapping[int, DepthPoint], selected: Optional[int] = None) -> None:
    print("=" * 104)
    print("Out-of-fold 曲线（每深度用全部折的评估集拼接，每篇恰好被留出一次）")
    print("=" * 104)
    if not curve:
        print("  （无可用的折评估产物）")
        return
    for depth in sorted(curve):
        point = curve[depth]
        marker = "  <== 选中" if depth == selected else ""
        print(
            f"  V{depth:<3d} folds={point.fold_count} {point.metrics.format_row()}{marker}"
        )
