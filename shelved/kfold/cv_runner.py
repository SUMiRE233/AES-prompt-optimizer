"""B 路线 CV 协议编排（设计稿 §14）。

协议
----
    48 篇
      ｜-- 报告留出 12 篇   只被使用一次：最终报告。不参与迭代/选深度/最终拟合
      ｜-- 工作池   36 篇
              ｜-- 4 折，每折 评估 9 / 训练 27
                      V0..VT 各跑一次 -> cv_runs/fold{j}/eval_scoring_results{t}.json
              汇总 t 上的 4 个折 = 36 篇 out-of-fold 预测 -> 选深度 t*
              用全部 36 篇重拟合到 t* -> 最终 prompt
              在 12 篇报告留出上评一次

与 legacy 的区别
----------------
- 不在折内做自适应停止；所有折跑到固定深度 T，使各折曲线可直接对比。
- 选深度依据 out-of-fold 曲线（b_metric 多分量规则），不是训练集 severe 计数。

用法
----
    python cv_runner.py                 # 计划预览，不调用 API、不写评分产物
    python cv_runner.py --execute       # 真正执行
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from b_cv_select import (
    CV_RUN_ROOT,
    available_depths,
    load_curve,
    load_folds,
    render_curve,
    select_depth,
)
from b_metric import measure
from pipeline_entry import (
    ORIGIN_PROMPT,
    RunPaths,
    ensure_origin_prompt,
    fold_essay_paths,
    preprocess_prompt,
    run_badcase_mining,
    run_prompt_optimization,
    run_scoring,
    run_test_iteration,
    run_train_iteration,
)


DEFAULT_MAX_VERSION = 6
FINAL_DIR = os.path.join(CV_RUN_ROOT, "final")
REPORT_PATH = os.path.join(CV_RUN_ROOT, "report_holdout_eval.json")


def final_paths() -> RunPaths:
    return RunPaths(FINAL_DIR)


def report_plan(max_version: int, folds) -> None:
    print("=" * 78)
    print("B 路线 CV 协议 —— 执行计划")
    print("=" * 78)
    print("  报告留出: report_holdout_essays.json（12 篇，全程只评一次）")
    print("  工作池  : work_pool_essays.json（36 篇）")
    print(f"  折      : {len(folds)} 折，每折 评估 9 / 训练 27")
    print(f"  深度    : V0..V{max_version}（折内不做自适应停止）")
    print()
    fold_calls = len(folds) * (2 + 2 * max_version)
    final_calls = max_version + 1
    print(f"  评分批次: 折内 {len(folds)} x (V0 训练 1 + V0 评估 1 + 每深度训练/评估各 1) "
          f"= {fold_calls}")
    print(f"            + 最终拟合 V0..V{max_version} 共 {final_calls} + 报告留出 1 "
          f"= 合计 {fold_calls + final_calls + 1} 批")
    print(f"  优化调用: {len(folds) * max_version}（折内） + {max_version}（最终拟合）"
          f" = {len(folds) * max_version + max_version} 次")
    print()
    print("  产物:")
    for fold in folds:
        print(f"    {os.path.join(CV_RUN_ROOT, f'fold{fold}')}/  "
              "optimized_prompt*_meta.md / train_scoring_results*.json / "
              "eval_scoring_results*.json / aes_badcases*.json")
    print(f"    {FINAL_DIR}/  最终拟合产物")
    print(f"    {REPORT_PATH}  报告留出评估（只此一次）")
    print()


def run_fold(fold: int, max_version: int, origin_file: str) -> None:
    train_essays, eval_essays = fold_essay_paths(fold)
    paths = fold_paths_for(fold)
    os.makedirs(paths.run_dir, exist_ok=True)

    print(f"\n--- fold{fold}: V0 基线（训练集评分 + 评估集评分）---")
    run_scoring(ORIGIN_PROMPT, train_essays, paths.train_scoring(0), origin_file)
    run_badcase_mining(paths.train_scoring(0), paths.badcase(0))
    run_scoring(ORIGIN_PROMPT, eval_essays, paths.eval_scoring(0), origin_file)

    for depth in range(1, max_version + 1):
        print(f"\n--- fold{fold}: V{depth - 1} -> V{depth} ---")
        run_prompt_optimization(
            paths.prompt_meta(depth - 1),
            paths.badcase(depth - 1),
            paths.prompt_meta(depth),
            iteration_log=paths.iteration_log(),
        )
        run_train_iteration(
            depth, origin_file, paths=paths, train_essays=train_essays
        )
        run_test_iteration(depth, origin_file, paths=paths, eval_essays=eval_essays)


def fold_paths_for(fold: int) -> RunPaths:
    return RunPaths(os.path.join(CV_RUN_ROOT, f"fold{fold}"))


def run_final_fit(depth: int, max_version: int, origin_file: str) -> None:
    """用全部工作池 36 篇重拟合到 depth。"""
    paths = final_paths()
    os.makedirs(paths.run_dir, exist_ok=True)
    work_pool = "work_pool_essays.json"

    print(f"\n--- final: V0 基线（工作池 36 篇）---")
    run_scoring(ORIGIN_PROMPT, work_pool, paths.train_scoring(0), origin_file)
    run_badcase_mining(paths.train_scoring(0), paths.badcase(0))

    for step in range(1, depth + 1):
        print(f"\n--- final: V{step - 1} -> V{step} ---")
        run_prompt_optimization(
            paths.prompt_meta(step - 1),
            paths.badcase(step - 1),
            paths.prompt_meta(step),
            iteration_log=paths.iteration_log(),
        )
        run_train_iteration(step, origin_file, paths=paths, train_essays=work_pool)


def run_report_holdout(depth: int, origin_file: str, force: bool = False) -> dict:
    """在报告留出上评一次，写出报告。

    若报告已存在则拒绝覆盖：看过一次结果之后再重评，无偏性立即失效。
    """
    if Path(REPORT_PATH).exists() and not force:
        raise SystemExit(
            f"{REPORT_PATH} 已存在。报告留出只允许评估一次；"
            "若确需重评（例如换了新的报告留出划分），显式加 --force-report。"
        )

    paths = final_paths()
    prompt_file = paths.prompt(depth)
    if not Path(prompt_file).exists():
        preprocess_prompt(paths.prompt_meta(depth), prompt_file)

    scoring_file = os.path.join(CV_RUN_ROOT, "report_holdout_scoring.json")
    print("\n--- report holdout: 12 篇，只评一次 ---")
    run_scoring(prompt_file, "report_holdout_essays.json", scoring_file, origin_file)

    rows = json.loads(Path(scoring_file).read_text(encoding="utf-8"))
    metrics = measure(rows)
    payload = {
        "protocol": "cv",
        "selected_depth": depth,
        "prompt": prompt_file,
        "report_holdout_essays": "report_holdout_essays.json",
        "report_holdout_scoring": scoring_file,
        "metrics": metrics.as_dict(),
        "note": (
            "报告留出在整条流程中只被使用一次；不参与迭代、不参与深度选择、"
            "不参与最终拟合。"
        ),
    }
    Path(REPORT_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(REPORT_PATH).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  报告已写出: {REPORT_PATH}")
    return payload


def warn_on_stop_boundary(depth: int, depths) -> None:
    """停止条件落在边界上时必须显式提示，不能静默返回。"""
    ordered = sorted(int(item) for item in depths)
    if depth == ordered[-1]:
        print()
        print(f"  [WARN] 选中深度 V{depth} 等于可达上限 V{ordered[-1]}：")
        print("         曲线在最后一版仍在改善，说明可能尚未收敛，")
        print("         或深度上限给得太低。当前结果应视为'未找到最优点'。")
    if depth == 0:
        print()
        print("  [WARN] 选中深度 V0：out-of-fold 曲线上没有任何迭代版本优于 origin，")
        print("         最终 prompt 将等同于 origin prompt，B 迭代未产生净收益。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="B 路线 CV 协议编排")
    parser.add_argument("--execute", action="store_true",
                        help="真正调用 API；不加时只打印计划")
    parser.add_argument("--max-version", type=int, default=DEFAULT_MAX_VERSION)
    parser.add_argument("--origin-file", default="origin_scoring_results.json")
    parser.add_argument("--manifest", default="cv_folds.json")
    parser.add_argument("--skip-folds", action="store_true")
    parser.add_argument("--skip-final-fit", action="store_true")
    parser.add_argument("--skip-report", action="store_true")
    parser.add_argument("--force-report", action="store_true",
                        help="允许覆盖已存在的报告留出评估（会破坏无偏性）")
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if not Path(args.manifest).exists():
        raise SystemExit(
            f"缺少 {args.manifest}；请先运行 python sample_extractor.py 生成划分。"
        )
    folds = load_folds(args.manifest)

    if not args.execute:
        report_plan(args.max_version, folds)
        depths = available_depths(folds)
        if depths:
            curve = load_curve(depths, folds)
            depth, accepted, rejected = select_depth(curve)
            render_curve(curve, selected=depth)
            print(f"  已可用深度: {depths}")
            print(f"  当前选中深度: V{depth}  接受链: {' -> '.join(accepted)}")
            for item in rejected:
                print(f"    拒绝 {item['version']}: {'; '.join(item['reasons'])}")
            warn_on_stop_boundary(depth, list(curve))
        else:
            print("  尚无折评估产物；执行 --execute 后此处会显示 out-of-fold 曲线。")
        print()
        print("  这是计划预览，未调用 API、未写任何评分产物。")
        return

    ensure_origin_prompt()

    if not args.skip_folds:
        for fold in folds:
            run_fold(fold, args.max_version, args.origin_file)

    depths = available_depths(folds)
    if not depths:
        raise SystemExit("没有可用的折评估产物，无法选择深度。")
    curve = load_curve(depths, folds)
    depth, accepted, rejected = select_depth(curve)
    render_curve(curve, selected=depth)
    print(f"  选中深度: V{depth}  接受链: {' -> '.join(accepted)}")
    for item in rejected:
        print(f"    拒绝 {item['version']}: {'; '.join(item['reasons'])}")
    warn_on_stop_boundary(depth, list(curve))

    if not args.skip_final_fit:
        run_final_fit(depth, args.max_version, args.origin_file)
    if not args.skip_report:
        run_report_holdout(depth, args.origin_file, force=args.force_report)


if __name__ == "__main__":
    main()
