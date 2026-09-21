"""B 终局评估的统计功效分析（离线，无 API）。

回答一个问题：把 48 篇的简单留出换成 k 折交叉验证，能否解决
"n=12 时 2 篇作文就能翻转选版"的问题？

关键约束：V1–V6 的 36 篇训练集**是 B 迭代的优化目标**，不是留出数据。
只有 12 篇验证集从未参与迭代。因此本脚本先量化"训练暴露"造成的偏置，
再分别测 bootstrap 与 k 折在**未污染数据**与**池化 48 篇**上的表现。

用法：python b_eval_power.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

from b_metric import BiasMetrics, measure, legacy_rank, select_sequential


VERSIONS = [f"V{i}" for i in range(1, 7)]
TRAIN = "train_scoring_results{i}.json"
VALIDATION = "test_scoring_results{i}.json"

BOOTSTRAP_ROUNDS = 2000
BOOTSTRAP_SEED = 20260920


def load_rows(pattern: str, version: str) -> List[dict]:
    index = int(version.lstrip("V"))
    path = Path(pattern.format(i=index))
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def rows_by_version(pattern: str, versions: Sequence[str]) -> Dict[str, List[dict]]:
    return {
        version: rows
        for version in versions
        if (rows := load_rows(pattern, version))
    }


def legacy_choice(version_rows: Mapping[str, Sequence[dict]]) -> str:
    table = {version: measure(rows) for version, rows in version_rows.items()}
    return min(
        table,
        key=lambda version: legacy_rank(table[version], int(version.lstrip("V"))),
    )


def sequential_choice(version_rows: Mapping[str, Sequence[dict]]) -> str:
    table = {version: measure(rows) for version, rows in version_rows.items()}
    return select_sequential(list(table.keys()), table).selected


def subset_rows(rows: Sequence[dict], indices: Sequence[int]) -> List[dict]:
    wanted = set(indices)
    return [row for row in rows if int(row["index"]) in wanted]


def render_gap_table() -> None:
    print("=" * 100)
    print("A. 训练暴露造成的偏置：同一版本在 train(36) 与 validation(12) 上的指标差")
    print("=" * 100)
    print(
        "%-5s %10s %10s %8s | %10s %10s %8s | %10s %10s %8s"
        % ("ver", "tr|bias|", "va|bias|", "gap", "tr aligned", "va aligned", "gap",
           "tr MAE-fl", "va MAE-fl", "gap")
    )
    train_rows = rows_by_version(TRAIN, [f"V{i}" for i in range(7)])
    val_rows = rows_by_version(VALIDATION, VERSIONS)
    for version in [f"V{i}" for i in range(7)]:
        if version not in train_rows:
            continue
        tr = measure(train_rows[version])
        if version not in val_rows:
            print(f"{version:<5s} {tr.mean_abs_cross_dim_bias:10.3f} {'—':>10s} {'—':>8s} |"
                  f" {100 * tr.aligned_rate:9.1f}% {'—':>10s} {'—':>8s} |"
                  f" {tr.mae_above_floor:10.3f} {'—':>10s} {'—':>8s}")
            continue
        va = measure(val_rows[version])
        print(
            f"{version:<5s} {tr.mean_abs_cross_dim_bias:10.3f} {va.mean_abs_cross_dim_bias:10.3f}"
            f" {va.mean_abs_cross_dim_bias - tr.mean_abs_cross_dim_bias:+8.3f} |"
            f" {100 * tr.aligned_rate:9.1f}% {100 * va.aligned_rate:9.1f}%"
            f" {100 * (va.aligned_rate - tr.aligned_rate):+7.1f}% |"
            f" {tr.mae_above_floor:10.3f} {va.mae_above_floor:10.3f}"
            f" {va.mae_above_floor - tr.mae_above_floor:+8.3f}"
        )
    print()
    print("  解读：gap 的符号在 V4 处**翻转**——V1-V3 是 train 更好（-11.1% ~ -2.8% 对齐率），")
    print("        V4-V6 变成 validation 更好（+6.5% ~ +11.1%）。")
    print("        单纯的泛化误差不会翻转符号。翻转出现在 V4，正是选版发生的版本，")
    print("        说明 12 篇验证集已经变成选择信号，不再是留出数据。")
    print("        因此两个 split 都不干净：train 是迭代目标，validation 是选择集。")
    print()


def bootstrap_flip_rate(version_rows: Mapping[str, Sequence[dict]], size: int, rounds: int) -> Dict[str, float]:
    rng = random.Random(BOOTSTRAP_SEED)
    counts: Dict[str, int] = {}
    versions = list(version_rows.keys())
    pool_size = len(next(iter(version_rows.values())))
    for _ in range(rounds):
        picks = [rng.randrange(pool_size) for _ in range(size)]
        sampled = {
            version: [rows[position] for position in picks]
            for version, rows in version_rows.items()
        }
        choice = legacy_choice(sampled)
        counts[choice] = counts.get(choice, 0) + 1
    return {version: counts.get(version, 0) / rounds for version in versions}


def render_bootstrap() -> None:
    print("=" * 100)
    print(f"B. Bootstrap 稳定性：从 12 篇验证集有放回抽样，看现规则选版的翻转率")
    print("=" * 100)
    val_rows = rows_by_version(VALIDATION, VERSIONS)
    for size in (12, 24, 48):
        rates = bootstrap_flip_rate(val_rows, size, BOOTSTRAP_ROUNDS)
        winner = max(rates, key=rates.get)
        ordered = sorted(rates.items(), key=lambda item: -item[1])
        print(f"  子样本 n={size:<3d}  " + "  ".join(f"{v}={100 * r:.1f}%" for v, r in ordered if r > 0))
        print(f"      -> 最常见选择 {winner}（{100 * rates[winner]:.1f}%）")
    print()
    print("  解读：n=24/48 两行是把同 12 篇重复放大，只反映统计量自身的抽样分布，")
    print("        不代表泛化。真正有信息的是第一行：**给定这 12 篇**，现规则 87.6% 稳定。")
    print("        所以风险不是「抽样抖动」，而是「这 12 篇是否代表那 48 篇」。")
    print()


def kfold_choice(version_rows: Mapping[str, Sequence[dict]], k: int) -> Dict[str, object]:
    versions = list(version_rows.keys())
    indices = [int(row["index"]) for row in next(iter(version_rows.values()))]
    rng = random.Random(BOOTSTRAP_SEED)
    shuffled = indices[:]
    rng.shuffle(shuffled)
    folds = [shuffled[i::k] for i in range(k)]

    per_fold = []
    for number, fold in enumerate(folds, 1):
        fold_rows = {
            version: subset_rows(rows, fold) for version, rows in version_rows.items()
        }
        fold_rows = {version: rows for version, rows in fold_rows.items() if rows}
        per_fold.append(
            {
                "fold": number,
                "size": len(fold),
                "legacy": legacy_choice(fold_rows),
                "sequential": sequential_choice(fold_rows),
            }
        )

    legacy_votes: Dict[str, int] = {}
    sequential_votes: Dict[str, int] = {}
    for item in per_fold:
        legacy_votes[item["legacy"]] = legacy_votes.get(item["legacy"], 0) + 1
        sequential_votes[item["sequential"]] = sequential_votes.get(item["sequential"], 0) + 1
    return {
        "k": k,
        "per_fold": per_fold,
        "legacy_votes": legacy_votes,
        "sequential_votes": sequential_votes,
    }


def render_kfold() -> None:
    print("=" * 100)
    print("C. k 折交叉验证：分别作用在「12 篇留出」与「池化 48 篇」上")
    print("=" * 100)
    for label, pattern, versions in (
        ("validation only (12 篇留出)", VALIDATION, VERSIONS),
        ("pooled (48 篇)", None, VERSIONS),
    ):
        if pattern is None:
            train_rows = rows_by_version(TRAIN, versions)
            val_rows = rows_by_version(VALIDATION, versions)
            merged: Dict[str, List[dict]] = {}
            for version in versions:
                if version in train_rows and version in val_rows:
                    merged[version] = train_rows[version] + val_rows[version]
            version_rows = merged
        else:
            version_rows = rows_by_version(pattern, versions)

        pool = len(next(iter(version_rows.values())))
        print(f"  -- {label}   (pool={pool})")
        for k in (4, 6):
            result = kfold_choice(version_rows, k)
            print(f"     k={k}  每折 {result['per_fold'][0]['size']} 篇")
            for item in result["per_fold"]:
                print(
                    f"        折{item['fold']}: legacy={item['legacy']}  "
                    f"sequential={item['sequential']}"
                )
            print(f"        现规则票数: {result['legacy_votes']}")
            print(f"        新规则票数: {result['sequential_votes']}")
        print()
    print("  解读：池化 48 篇的 k=4 结果里 **V4 一票未得**（V5/V6 各 2 票），")
    print("        而只切 12 篇留出时 V4 是多数。同一个 k 折流程，仅换评估集合，")
    print("        结论完全反转 —— 说明 k 折本身不提供新信息，只是换一种切法。")
    print("        池化之所以偏向 V5/V6，是因为 3/4 的折由 36 篇训练数据组成。")
    print()


def render_protocol() -> None:
    print("=" * 100)
    print("E. 可行方案：把 k 折用在**流程**上，而不是**评估**上")
    print("=" * 100)
    print("  目标：得到一个 48 篇规模、且未被污染的选择依据。")
    print()
    print("  协议（在最终阶段一次性执行，不进入每轮迭代）：")
    print("    1. 把 48 篇切成 k 折（k=4 → 每折 12 篇留出）")
    print("    2. 第 j 折：用其余 36 篇跑完整 B 迭代链 V0..VT，得到该折的 prompt 序列")
    print("    3. 对每个深度 t，用第 j 折的 12 篇留出评估 → metric_j(t)")
    print("    4. 汇总 k 条曲线：每个深度 t 得到 48 篇规模的 **out-of-fold** 估计")
    print("    5. 用该曲线选**迭代深度** t*（不是选某一条折的 prompt）")
    print("    6. 用全部 48 篇重新拟合到深度 t*，作为最终产物")
    print()
    print("  为什么这是唯一无偏的：每篇作文恰好被留出一次，48 篇贡献 48 个")
    print("  out-of-fold 预测，规模是当前 12 篇的 4 倍且零污染。")
    print()
    print("  为什么选深度而不是选 prompt：k 条折会产出 k 个不同的 prompt，")
    print("  从中挑一个会立刻重新引入选择偏差。标准做法是用 CV 定超参（= 深度），")
    print("  再在全量数据上重新拟合。")
    print()
    print("  代价：k × (T 轮 × 36 篇评分 + T 次优化调用)。k=4、T=6 时约为当前")
    print("        一整轮 B 迭代的 4 倍。这是唯一能换取无偏性的成本。")
    print()


def merged_rows(versions: Sequence[str]) -> Dict[str, List[dict]]:
    train_rows = rows_by_version(TRAIN, versions)
    val_rows = rows_by_version(VALIDATION, versions)
    merged: Dict[str, List[dict]] = {}
    for version in versions:
        if version in train_rows and version in val_rows:
            merged[version] = train_rows[version] + val_rows[version]
    return merged


def render_holdout_sensitivity() -> None:
    """若留出集是另外 12 篇，现规则会得出什么结论？"""
    print("=" * 100)
    print("F. 留出集敏感性：从 48 篇中无放回抽 12 篇作为「另一种留出集」")
    print("=" * 100)
    version_rows = merged_rows(VERSIONS)
    pool = len(next(iter(version_rows.values())))
    rng = random.Random(BOOTSTRAP_SEED)
    counts: Dict[str, int] = {}
    for _ in range(BOOTSTRAP_ROUNDS):
        picks = rng.sample(range(pool), 12)
        sampled = {
            version: [rows[position] for position in picks]
            for version, rows in version_rows.items()
        }
        choice = legacy_choice(sampled)
        counts[choice] = counts.get(choice, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: -item[1])
    print("  " + "   ".join(f"{v}={100 * c / BOOTSTRAP_ROUNDS:.1f}%" for v, c in ordered))
    winner = ordered[0][0]
    print(f"  -> 最常见的结论是 {winner}（{100 * ordered[0][1] / BOOTSTRAP_ROUNDS:.1f}%）")
    actual = "V4"
    actual_share = 100 * counts.get(actual, 0) / BOOTSTRAP_ROUNDS
    print(f"  -> 实际采用的 {actual} 只在 {actual_share:.1f}% 的 12 篇子集上被选中")
    print()
    print("  解读：这直接量化了「换一批 12 篇会得出不同结论」的风险。")
    print("        实际采用中的 V4 之所以胜出，很大程度上是那 12 篇的构成使然——")
    print("        而那 12 篇同时又是选择集，所以这个数字无法自我印证。")
    print()


def main() -> None:
    print()
    print("B 终局评估的统计功效分析 —— 只读取已归档产物，无 API 调用")
    print()
    render_gap_table()
    render_bootstrap()
    render_holdout_sensitivity()
    render_kfold()
    render_protocol()
    print("=" * 100)
    print("D. 结论")
    print("=" * 100)
    print("  1. k 折用在**评估侧**不可行：同一批 48 篇换一种切法，结论就从 V4 变成 V5/V6。")
    print("     它不产生新信息，只是重新分配已有信息。")
    print("  2. 两个 split 都不干净：train 是迭代目标，validation 是选择集。")
    print("     train/val gap 在 V4 处符号翻转就是这一点的直接证据。")
    print("  3. 现规则在那 12 篇上是稳定的（bootstrap 87.6%），所以问题不是抽样抖动，")
    print("     而是这 12 篇是否代表那 48 篇——见 F 的换集测试。")
    print("  4. 唯一无偏的用法是把 k 折用在**流程**上（见 E），但其口径取决于")
    print("     是否还要保留一个可报告的留出数字。")


if __name__ == "__main__":
    main()
