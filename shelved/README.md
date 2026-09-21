# 已搁置的工作

本目录保存在“B 类迭代改造与重跑”阶段被**暂时放弃但不删除**的实现，以便将来需要时恢复。

---

## kfold/ —— k 折交叉验证改造（2026-09-20 搁置）

### 内容

| 文件 | 作用 |
|---|---|
| `cv_runner.py` | 4 折编排：每折跑 V0..VT，汇总 out-of-fold 曲线选深度，工作池重拟合，报告留出评一次 |
| `b_cv_select.py` | out-of-fold 曲线加载、断档校验、深度选择 |
| `b_metric.py` | 多分量 B 指标（bias_score / aligned / exact / MAE-above-floor）与 `legacy_rank` 对照实现 |
| `b_metric_replay.py` | 用 V0–V6 归档产物重放，对照现规则与新规则的选版结果 |
| `b_eval_power.py` | 训练暴露偏置、bootstrap 稳定性、k 折对照、留出集敏感性分析 |
| `test_b_metric.py` | `b_metric` 的单元测试 |
| `test_kfold_protocol.py` | 划分、折完整性、`RunPaths`、`project_checks.check_cv_protocol` 的测试 |

### 搁置原因

指导大纲 §3.1 明确要求本轮**保留既有简单留出协议**：

> 继续使用既有简单留出流程：36 篇训练集 / 12 篇留出集；不引入 CV；不重新随机划分；
> 不使用 `cv_folds.json`、`cv_runner.py` 或 `cv_runs` 作为本轮执行入口。

因此 `sample_extractor.py` 已恢复为 seed-42 随机 36/12 划分，
`train_essays.json` / `test_essays.json` 已重新生成并**与归档评分产物的 index 集合逐一对上**。

### 搁置时仍然保留在主干里的东西

以下改动**没有回退**，因为它们是行为等价的改造接缝，且已通过回归测试：

- `pipeline_entry.RunPaths` / `LEGACY_PATHS`：`run_train_iteration` / `run_test_iteration`
  的路径解析参数。LEGACY 模式与原有模块级路径函数逐字一致，
  由 `test_pipeline_paths.RunPathsRegressionTest` 守护。
- `pipeline_entry.run_pipeline` 的 CV 标记守卫：当 `cv_folds.json` 存在时拒绝执行旧入口，
  防止把留出集用作选版依据。当前 `cv_folds.json` 不存在，守卫不触发。
- `project_checks.check_cv_protocol`：仅在 `cv_folds.json` 与工作池文件存在时生效，当前为空操作。
- `.gitignore` 中 CV 产物的忽略条目：保留为防御性配置。

### 未完成的已知缺口（搁置时已识别）

| 编号 | 问题 | 状态 |
|---|---|---|
| F-7 | 停止阈值缺少最小效应量（out-of-fold 108 格中 2 格即可判定"严格更优"；`bias_score` 为整数，1 篇 soft 即可翻转） | 未修 |
| F-9 | `batch_scoring` 的调试产物是固定路径，多次调用互相覆盖，审计链断裂 | 未修 |
| F-10 | `pipeline_entry.candidate_rank` 与 `b_metric.legacy_rank` 是实现同一规则的两份代码 | 未修 |

### 恢复方式

1. 把本目录下的 `.py` 移回仓库根目录；
2. 运行 `python sample_extractor.py` 前需要恢复 CV 版划分逻辑
   （见 `archive/v1_seed42_20260920/ARCHIVE_MANIFEST.json` 之后的 git 历史或本文件的历史版本）；
3. `python cv_runner.py` 查看计划，`--execute` 才会真正调用 API。

> 注意：`archive/v1_seed42_20260920/` 保存的是**v1 简单留出协议**下的全部 B 实验产物，
> 与 k 折改造无关，不要混用。
