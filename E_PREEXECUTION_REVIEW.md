# E 类预执行报告（第一阶段）

- 日期：2026-09-21
- 停止状态：`E_PREEXECUTION_AWAITING_SAMPLE_APPROVAL`
- 性质：仓库内报告（未来可提交的最小证据之一）；本轮**未提交**任何变更

> **历史快照（已失效）**：本文档描述 2026-09-21 预执行阶段的当时状态；
> “停止于样本审批”等结论已被同日后续执行事实取代。终局见
> `e_protocol_manifest.json → route_conclusion` 与 `E_ROUTE_CLOSING_REPORT_DRAFT.md`。
> 保留仅作阶段留档，不构成当前协议状态。

## 1. Git 起始状态与本轮变更

- 起始 HEAD：`1457fc8`（B 冻结 `2f06521` + CI 修复 `1457fc8`，均已推送）
- 起始工作区：仅 `.gitignore` 一处未提交改动（上一轮「指导大纲不入库」规则）
- 本轮完成后（**全部未提交**）：

```text
修改：.gitignore（新增 E 草稿/本地证据忽略规则）
      micro_scoring_gate.py / etype_iteration_runner.py
      E_VALIDATION_PLAN.md / STRUCTURE_E_VALIDATION_REPORT.md / CONTENT_E_VALIDATION_REPORT.md
新增（仓库根、未提交）：
      decision_thresholds.py / e_consensus_miner.py / e_phase1_prepare.py / test_e_phase1.py
      e_protocol_manifest.json / e_baseline_manifest.json / E_PREEXECUTION_REVIEW.md
新增（被 .gitignore 覆盖、从不入库）：
      e_candidate_manifest.draft.json / e_candidate_evidence.local.json
      final_validation_scoring_results_mean.json (+.provenance.json)
      etype_analysis/e_consensus_v6.json
```

- 提交策略：按 §10，待负责人审批后再决定最小证据清单的入库范围。

## 2. 重挖公式与实现位置

E-consensus-robust-z-v1（`e_consensus_miner.py`，纯离线）：

```text
delta(i,d,r)    = AI(i,d,r) - Teacher(i,d)
residual(i,d,r) = delta(i,d,r) - median_i delta(i,d,r)
z(i,d,r)        = residual(i,d,r) / (1.4826 * MAD(d,r) + epsilon)
E 样本: z(i,d,1)*z(i,d,2) > 0 且 |mean(delta1, delta2)| >= 1.0
严重度: |mean(z1, z2)| > 2.5 -> Severe；1.5 < |mean(z1,z2)| <= 2.5 -> Soft
```

B consensus（b_consensus_samples）采用可复现口径（见 §3 说明）：

```text
members  = 两跑均值文件（final_train_scoring_results_mean.json）上被冻结 B 规则标记的样本
severity = 均值与「重跑」双重确认 Severe 才记 Severe，否则 Soft
冻结 B 规则: severe: |B|>1.5 且一致性>=3；soft: |B|>1.0 且一致性>=2
```

实现位置：`e_consensus_miner.py`（统计/共识/CLI）、`e_phase1_prepare.py`（编排与产物）、
`decision_thresholds.py`（共享判定）。旧单跑 miner（`aes_badcase_miner.py`）未修改。

## 3. E / B consensus 离线复算结果

| 项 | 结果 | 与指导书 |
|---|---|---|
| E-content | 1 Severe / 7 Soft | ✅ 一致 |
| E-expression | 0 / 0 | ✅ 一致 |
| E-structure | 2 Severe / 2 Soft | ✅ 一致 |
| B 每次运行 | run1 = 3/5，run2 = 3/8 | ✅ |
| b_route_metric | mean Severe 3.0 / mean Soft 6.5 / Q 14.0 | ✅ |
| B consensus | **2 Severe / 6 Soft（总数 8）** | ✅ 一致 |
| 均值文件上的 B 计数 | 4/4 | 记录用 |

**B consensus 裁定（2026-09-21，已批准）**：采用 `mean_and_rerun_conservative`，定位为
「**均值成员 + rerun 复核的操作性裁定**」——它不是交换对称的「两跑交集」，也不得包装成
自然统计结论；理由是 run2 在本项目时序中承担复核运行角色，与现有精度升级思想一致。
最终 Severe = {46, 47}、Soft = {13, 15, 16, 18, 22, 28}，即 **2/6**。其余五种变体仅留档
审计（`e_protocol_manifest.json`、`etype_analysis/e_consensus_v6.json`），不参与样本角色
或 gate：

```text
mean_and_rerun_conservative   2/6/8   <- 采用（Severe = {46, 47}）
mean_and_run1_conservative    2/6/8   备选（Severe = {18, 22}；仅留档）
mean_membership_only          4/4/8
both_runs_mean_rule           2/4/6
both_runs_conservative        0/6/6
both_runs_either_severe       5/1/6
```

## 4. validation 均值证据及 SHA-256

| 文件 | SHA-256 |
|---|---|
| test_scoring_results6.json（源） | `4cd9278ebd4a7c26…` |
| test_scoring_results6_rerun.json（源） | `ae818ce7a3a51c66…` |
| final_validation_scoring_results_mean.json（生成） | `802c77fc26f99a5a…` |
| final_validation_scoring_results_mean.provenance.json | `401d2e38f00aecde…` |

复算校验：每跑 B 计数 = 2/0 与 1/2 → mean Severe = 1.5、mean Soft = 1.0、Q = 4.75，
与 `b_validation_report.json`（iteration 6）完全一致；12 个 index 与预期集合一致；
教师分保持不变。

## 5. baseline 锚点校验（Gate 0 输入）

`e_baseline_manifest.json`：15 个锚点全部记录 SHA-256，`verify_baseline_manifest()`
复检无漂移。关键锚点：

```text
final_prompt.md            72ca44765e86c075…
b_protocol_state.json      7d428f24758a4d5c…   (停止原因权威来源)
b_validation_report.json   5e007f7ef0c5a66e…   (cap_reached 仅作 legacy 标签保留)
final_evidence.json        48dd15d5da8dbff3…
```

## 6. D10 样本表（草稿，待审批）

经负责人裁定（2026-09-21），样本清单拆分为两份：`e_candidate_manifest.draft.json`
（**无分数**：index / evidence roles / 选择理由 / consensus 规则与版本 / 聚合计数 /
基线文件哈希 / 审批占位；已 gitignore）与 `e_candidate_evidence.local.json`（逐篇教师分、
两次模型分、diffs、z-score 等详细证据；本地专用，从不入库）。审批后将草稿重命名为
`e_candidate_manifest.json`、填写审批元数据与冻结哈希；正式入口只接受该冻结文件。
下表依据来自本地证据文件：

| index | 角色 | 选择依据（摘要） |
|---:|---|---|
| 2 | normal_control | 补足至 6 的匹配对照 |
| 8 | normal_control | 距 structure 目标 13 最近（Δstructure=0.0, Δlen=123） |
| 13 | structure_e_target, b_soft | structure E soft（mean_z=-1.518）；B soft |
| 15 | b_soft | B soft（1.167 / None-soft） |
| 16 | b_soft | B soft（1.167 / None-severe） |
| 17 | normal_control | 距目标 47 最近（Δstructure=0.0, Δlen=103） |
| 18 | structure_e_target, b_soft | structure E **severe**（-2.698）；B soft |
| 19 | cross_dim_expression | |mean Δexpr| 最大之一（-1.50） |
| 20 | normal_control | 距目标 22 最近（Δstructure=1.0, Δlen=151） |
| 22 | structure_e_target, b_soft, cross_content | structure E **severe**（+2.698）；B soft；content 边界（|z|=3.035） |
| 25 | cross_dim_expression | |mean Δexpr| 最大之一（-1.50） |
| 26 | normal_control | 距目标 18 最近（Δstructure=0.0, Δlen=0） |
| 28 | b_soft | B soft（-1.500 / severe-soft） |
| 36 | normal_control | 补足至 6 的匹配对照 |
| 37 | cross_dim_content | content 边界（|z|=2.361） |
| 46 | b_**severe** | B severe（-1.667 / soft-severe） |
| 47 | structure_e_target, b_**severe** | structure E soft（-1.855）；B severe |

计数：structure 目标 4 / 正常对照 6 / content 跨维 2 / expression 跨维 2 /
B consensus 8（含 Severe 2）/ 唯一 17。评分身份去重：每个唯一 global index 只产生
一次候选评分，多角色共享（写入草稿的 `scoring_dedup_note`）。

## 7. 共享阈值实现位置

- `decision_thresholds.py`：`clear_improvement: delta <= -1.0`、`clear_regression: delta >= +1.0`；
  Q 权重镜像常量 `Q_SEVERE_WEIGHT = 2.5`（有与 `pipeline_entry` 的一致性测试）；
  分层预算 micro 0/0/0、regular 0/1/1。
- 接线：`micro_scoring_gate.py`（单维守卫 / 角色守卫 / B 守卫 / `hard_guards` 载荷）；
  `etype_iteration_runner.py`（gate 计数、validation 恶化计数、两处全局 B 回归
  从 5S+F 统一为 **Q=2.5S+F**、criteria 载荷）。
- 四个旧阈值参数（deprecated）保留解析兼容，判定不再使用。

## 8. 测试与验证

```text
python -m unittest -v      -> Ran 194 tests, OK   (原 177 + 新增 17)
python project_checks.py   -> 5/5 PASS
网络调用次数                -> 0（哨兵 dry-run 测试 + 源码扫描双重保证）
```

测试数量最终值：**194**（README 已同步更新）。

新增 `test_e_phase1.py` 覆盖指导书 §5 的 12 项要求（两跑对齐拒绝、方向/下限过滤、
严重度边界、真实数据复算、哈希漂移、±1.0 边界、去重、.draft 守卫、离线入口）。

## 9. 尚未执行事项（按 §3 的禁止项）

未调用任何外部模型/API；未生成候选规则；未修改 `final_prompt.md` / `final_prompt_meta.md`；
未执行 micro/regular/full-train/validation 候选评分；未调用 `--promote-final`；
未进入第二阶段。

## 10. 停止状态

```text
E_PREEXECUTION_AWAITING_SAMPLE_APPROVAL
```

> 已完成 E 类离线预执行准备。尚未调用模型 API，尚未生成候选规则，尚未修改 final prompt。
> 请项目负责人先 review 并审批 `e_candidate_manifest.draft.json`；未收到新的明确继续指令前，停止工作。

## 附：裁定记录（2026-09-21，已处理）

1. **B consensus**：采纳 `mean_and_rerun_conservative`（操作性裁定措辞，见 §3）；
   五种其余变体仅留档，不参与样本角色或 gate。
2. **manifest 拆分**：已实施——无分数草稿 + 本地证据文件；`.gitignore` 新增
   `e_candidate_manifest.draft.json`、`e_candidate_evidence.local.json`；正式入口
   收紧为仅接受冻结后的 `e_candidate_manifest.json`。
3. **README**：测试数已更新为 **194**（本地实测 `Ran 194 tests … OK`）。
4. **CLI 文档-行为不一致**：已修复——`e_consensus_miner.py` 移除不存在的
   `--b-severity-variants` 用法说明；变体表始终随 JSON 摘要输出，函数级
   `b_severity_variants()` 保留。
5. 遗留备注：《B 类迭代改造与重跑指导大纲.md》的历史入库处置仍待定（不阻塞）。
