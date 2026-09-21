"""B 指标重放（离线，无 API）。

用已归档的 V0–V6 产物验证"补分辨率分量"的可行性：
1. 现规则（``Score -> severe -> 更早版本``）选出的版本；
2. 新规则（偏置不劣化 + 分辨率不退化 + 至少一项严格改善）选出的版本；
3. 两者分歧点及其原因。

不读写任何线上状态，只读 ``train_scoring_results*.json`` /
``test_scoring_results*.json``。用法：

    python b_metric_replay.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

from b_metric import (
    BiasMetrics,
    measure,
    legacy_rank,
    pareto_frontier,
    select_sequential,
)


SPLITS = (
    ("TRAIN", "train_scoring_results{i}.json", range(0, 7)),
    ("VALIDATION", "test_scoring_results{i}.json", range(1, 7)),
)


def load_metrics(pattern: str, indices) -> Dict[str, BiasMetrics]:
    metrics: Dict[str, BiasMetrics] = {}
    for index in indices:
        path = pattern.format(i=index)
        if not Path(path).exists():
            continue
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
        metrics[f"V{index}"] = measure(rows)
    return metrics


def legacy_select(metrics: Dict[str, BiasMetrics]) -> str:
    return min(
        metrics,
        key=lambda version: legacy_rank(
            metrics[version], int(version.lstrip("V"))
        ),
    )


def render_split(name: str, metrics: Dict[str, BiasMetrics]) -> tuple:
    print("=" * 108)
    print(f"### {name}   (n_versions={len(metrics)})")
    print("=" * 108)
    for version, item in metrics.items():
        print(f"  {version:<4s} {item.format_row()}")
    print()

    legacy = legacy_select(metrics)
    sequential = select_sequential(list(metrics.keys()), metrics)
    frontier = pareto_frontier(metrics)

    print(f"  现规则  Score -> severe -> 更早版本  ->  {legacy}")
    print(f"  新规则  多分量序贯接受                ->  {sequential.selected}")
    print(f"     接受的版本链: {' -> '.join(sequential.accepted_versions)}")
    for item in sequential.rejected:
        print(f"     拒绝 {item['version']}: {'; '.join(item['reasons'])}")
    print(f"  Pareto 前沿 (bias_score / aligned / MAE-floor 三者不可同时更优): {frontier}")

    mixed = sorted({legacy, sequential.selected} | set(frontier))
    if len(mixed) > 1:
        print()
        print("  [取舍集] 以下版本互不支配，选谁取决于分量权重：")
        for version in mixed:
            print(f"     {version}: {metrics[version].format_row()}")
    print()
    return sequential.selected, frontier


def main() -> None:
    print()
    print("B 指标重放 —— 只读取已归档的 train/validation 评分产物")
    print()
    selections = {}
    frontiers = {}
    for name, pattern, indices in SPLITS:
        metrics = load_metrics(pattern, indices)
        if not metrics:
            print(f"[SKIP] {name}: 没有可用产物（pattern={pattern}）")
            continue
        selections[name], frontiers[name] = render_split(name, metrics)

    print("=" * 108)
    print("结论要点")
    print("=" * 108)
    for name in selections:
        print(
            f"  {name:<11s} 现规则/新规则选中 = {selections[name]}"
            f"   Pareto 前沿 = {frontiers[name]}"
        )
    if len(frontiers) == 2:
        overlap = sorted(set(frontiers["TRAIN"]) & set(frontiers["VALIDATION"]))
        print()
        print(f"  两个 split 的前沿交集: {overlap}")
        print("  -> 交集之外的版本没有任何 split 能同时支持，说明最终选择是")
        print("     分量权重问题，而不是单一计数能裁决的问题。")
    print()
    print("  附注:")
    print("  1. aligned / exact 计数型分量在两个 split 上随版本单调改善，可直接使用。")
    print("  2. pstdev(AI - teacher) 被离群样本主导（n=36 时 SD 标准误约 12%），")
    print("     已在 b_metric 中弃用。")
    print("  3. 现规则的偏置来自 severe 计数单独拥有否决权；新规则仍需显式决定")
    print("     bias_score 与 aligned 的优先级，重放只能暴露取舍、不能替你做取舍。")


if __name__ == "__main__":
    main()
