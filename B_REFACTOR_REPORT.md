# B 类迭代改造 —— 客观检查报告

日期：2026-09-20（验收更新：2026-09-21）
范围：B 类结构改造与前置条件（大纲 §1）。**不含** E 类迭代。
状态：`B_EXPLORATORY_PROTOCOL_FROZEN`（实验性口径冻结：统一 Q 协议 + 重建官方路线 + final=V6；见 §12.7.6）

> 头部状态演进：`REFACTOR_COMPLETE_RERUN_NOT_STARTED`
> → `B_RESULT_AVAILABLE_BUT_NOT_FROZEN`（已选出 final 但验收未完成）
> → `B_FROZEN_READY_FOR_E_DESIGN`（大纲 §十三 十项条件全部满足，逐条对照见 §12.5）
> → **已撤回**：外部 review 证伪该冻结（协议不一致、final 来源混乱等，见 §12.7）
> → `B_PROTOCOL_VNEXT_IN_PROGRESS`（统一 Q 协议重验证）
> → `B_PROTOCOL_VNEXT_RUN_COMPLETE`（2026-09-21 16:44 收尾：validation 选中 V6）
> → `B_EXPLORATORY_PROTOCOL_FROZEN`（2026-09-21 17:37 时序重建修正后按实验性口径冻结，见 §12.7.6）。

---

## 0. 验证命令与结果

| 命令 / 核验 | 结果 |
|---|---|
| `python -m unittest` | **Ran 174 tests — OK** |
| `python project_checks.py` | **5/5 PASS**（评分产物对齐 **8** 个，含 `*_rerun`） |
| `python -m py_compile`（40 个 .py） | exit 0 |
| `python sample_extractor.py` + index 比对 | 重生成 train/test 的全局 index 集合与归档评分产物**完全一致** |
| `assert_clean_working_directory()` | `CLEAN: 可以启动 B 重跑` |
| `git check-ignore` | `archive/`、`prompt_candidate_decisions.json`、`run_manifest*.json`、`b_rerun_log.json` 均已忽略 |
| final 产物一致性 | `final_prompt_meta.md` sha256 与 `optimized_prompt3_meta.md` **相同**；`final_train_scoring_results.json` 与 `train_scoring_results3.json` **相同** |
| 真实 B 重跑 | V0→V4 共 **6 次评分运行**；gate 于 V4 触发；留出集选出 **final = V3** |
| 抗震荡 rerun | 已实现于 `pipeline_entry`；`test_b_rerun.py` **15 项全过（无 API）** |
| manifest 每版记录 / 复用守卫 / final 契约（G1/G2/G4） | `test_manifest_reuse.py` **17 项全过（无 API）**；真实工作区已演示：final 契约 PASS、V0–V4 记录写入 `manifest['versions']`、身份不一致时拒绝复用 |

---

## 12.1 要求落实表

| 要求 | 状态 | 生产代码位置 | 测试位置 | 实际证据 |
|---|---|---|---|---|
| 三维锚点唯一 | **PASS** | `prompt_structure_contract.validate_input_contract`（IN-2）→ 接入 `prompt_optimizer.PromptOptimizer.run` Step 2 与 `validate_candidate` | `test_prompt_optimizer_wiring.py::test_current_prompt_must_satisfy_the_input_contract`；`test_prompt_structure_contract.py::InputContractTest` | 真实 `run()` 在输入契约违约时抛 `PromptContractError` 且不写目标文件；IN-2 有 3 项定向测试 |
| B 标题冻结 | **PASS** | `validate_optimizer_edit` 规则 R1（标题冻结）/ R1b（顺序）/ R5（新增分点类型）→ 接入 `PromptOptimizer.validate_candidate` | `test_prompt_optimizer_wiring.py::test_every_illegal_variant_is_rejected_and_never_written` | 改名 / 删除 / 重排 / 重复标题四种变体，各重试 3 次全部被拒，目标文件未生成 |
| 候选写入前校验 | **PASS** | `PromptOptimizer.run()` Step 5.1–5.3：提取候选 → 校验 → `atomic_save_prompt` | `test_prompt_optimizer_wiring.py::test_compliant_candidate_is_written`、`::test_retry_succeeds_when_a_later_attempt_is_compliant` | 合规候选写入目标；第 1 次非法、第 2 次合规时采用第 2 次 |
| 非法候选不落盘 | **PASS** | `atomic_save_prompt` 仅在 `validate_candidate` 返回空后调用 | `test_prompt_optimizer_wiring.py::test_every_illegal_variant_is_rejected_and_never_written` | `assertFalse(os.path.exists(TARGET))` |
| 拒绝原因留存 | **PASS** | `PromptOptimizer.record_decision` → `prompt_candidate_decisions.json` | `test_prompt_optimizer_wiring.py::test_rejection_reasons_are_recorded` | 3 次尝试的原因逐条记录，含 `R1` 违规码 |
| 稳定版本不被改动 | **PASS** | 候选未通过即不触碰 `TARGET_PROMPT`；`pipeline_entry.optimize_or_stop` 停止整轮 | `test_prompt_optimizer_wiring.py::test_stable_prompt_is_untouched_after_rejection`、`::PipelineBlockedTest` | 拒绝前后 `CURRENT_PROMPT` 内容逐字相同；pipeline 抛 `SystemExit("blocked at Vn")` |
| 简单留出保持不变 | **PASS** | `sample_extractor.py` 已恢复 seed-42 随机 36/12；未引入 CV | `test_pipeline_paths.py`、`project_checks.check_local_data` | train=36 / test=12、overlap=0、union=48；**index 集合与 `archive/.../train_scoring_results0.json`、`test_scoring_results1.json` 完全一致** |
| MAE 不参与 gate | **PASS** | `pipeline_entry.candidate_rank`、`compare_candidates` | `test_final_selection_rule.py::MaePolicyTest::test_selection_ignores_mae_and_only_warns` | MAE 0.0 vs 3.0，选版仍取 Score 更小者；报告写入 `mae_policy=record_only; warn_on_rebound` 并打印 `MAE rebounded` 警告 |
| MAE 逐版本记录 | **PASS** | `compare_candidates` 的 `candidates[].mae` | 同上 | `[item["mae"] for item in report["candidates"]] == [0.0, 3.0]` |
| 停止规则 | **PASS** | `pipeline_entry.decide_iteration` | `test_final_selection_rule.py::StopRuleTest`（9 项） | severe 下降继续 / 回升立即 gate 并回退一版 / soft 一次停滞不 gate / 连续两次 gate 并回退两版 / V6 上限 / origin 继续 |
| final 排序 | **PASS** | `pipeline_entry.candidate_rank` | `test_final_selection_rule.py::FinalRankingTest`（3 项） | Score → severe → 更早版本，逐级生效 |
| 运行 manifest | **PASS** | `pipeline_entry.build_run_manifest` / `write_run_manifest` | `test_run_preflight.py::ExperimentManifestTest`（6 项） | 记录 run_id / 模型 / 阈值 / 停止策略 / 结构预算 / 划分 index 与 sha256；credential 形状的模型值与 secret-like 字段名被拒；写出的文件不含 `AES_API_KEY` |
| manifest 每版记录（G1） | **PASS** | `run_manifest.build_version_record` / `attach_version_record`；`pipeline_entry.record_version_in_manifest`，在 V0 与每轮 loop 内调用 | `test_manifest_reuse.py::VersionRecordTest`（5 项） | 真实工作区已写入 `manifest['versions'] = [0,1,2,3,4]`，每版含 prompt meta/runtime/scoring/badcase 的 sha256 与 `contract_ok`/`contract_violations`；按 iteration 覆盖式写入（rerun 不堆重复行） |
| manifest 复用守卫（G2） | **PASS** | `run_manifest.manifest_identity` / `assert_manifest_compatible`；`pipeline_entry.assert_reusable_artifacts`，在 `run_pipeline` 的 resume 分支调用 | `test_manifest_reuse.py::ManifestReuseGuardTest`（6 项）、`::PipelineReuseGuardWiringTest`（2 项） | 模型 / 阈值 / origin 数据 / 划分 index 四类变更各自被拒且**评分尚未发生**（`run_train_iteration` 未被调用）；真实工作区已实测拒绝（`['stop_policy']`） |
| final 契约检查作为流程步骤（G3/G4） | **PASS** | `pipeline_entry.save_final_prompt` 在 `shutil.copy2` **之前**执行 `validate_input_contract`，违规抛 `PromptContractError` 且不晋升 | `test_manifest_reuse.py::FinalContractGuardTest`（3 项） | 真实工作区已输出 `[契约] final 结构契约检查 PASS：optimized_prompt3_meta.md（1767 字符，7 个加粗分点）`；不合规 final 被拒且 `final_prompt_meta.md` **未生成** |
| 旧产物归档 | **PASS** | `archive_run.py` | 运行记录 + `archive/v1_seed42_20260920/ARCHIVE_MANIFEST.json` | 65 个文件移入归档目录，逐个记录 sha256 与字节数 |
| 残留复用守卫 | **PASS** | `pipeline_entry.assert_clean_working_directory`，在 `run_pipeline` 起始调用 | `test_run_preflight.py::CleanDirectoryGuardTest`（4 项） | 干净目录通过；`test_scoring_results*` / `iteration_history.json` / `optimized_prompt*_meta.md` 三类残留各被拒 |
| 预算单一来源 | **PASS** | 常量定义于 `prompt_structure_contract`；`prompt_optimizer.budget_block()` 引用同一组常量 | `test_prompt_structure_contract.py::test_bold_point_budget_is_enforced` | 优化器提示词中的预算块由常量渲染（`__BUDGET_BLOCK__` 占位符已验证被替换） |
| 空 B-badcase 不除零 | **PASS** | `PromptOptimizer.analyze_B_bias_distribution` 的 `has_targets`；`NoOptimizationTargetError` | `test_prompt_optimizer_wiring.py::test_empty_badcases_do_not_divide_by_zero`、`::test_analyze_empty_badcases_returns_zeroed_payload` | 返回零值 payload 并置 `has_targets=False`；`run()` 抛专用异常并记录 `status=no_target` |
| 迭代历史章节审计 | **PASS** | `prompt_structure_contract.section_diff` → `save_iteration_log(changed_sections, added_sections)` | `test_prompt_optimizer_wiring.py::test_accepted_candidate_populates_iteration_history_sections` | `iteration_history.json` 首次写入**非空**的 `added_sections`（此前长期为 `[]`） |
| **实际 B 重跑** | **PASS** | `pipeline_entry.run_pipeline` / `compare_candidates` | 本轮实跑 | V0→V4 共 6 次评分运行（训练集 64 + 留出集 24 次调用）；V4 触发 gate；留出集选出 final=V3，sha256 `40ba28fa0019df4f` |
| **重复评分 / 稳定性报告** | **PARTIAL** | 仅 V3 做过二次评分（`tmp_resume_run.py --score-only`），用其测得噪声基线 | `test_b_rerun.py` 不含此项 | 噪声已量化：逐篇 sd 1.546 / 单版 SE 0.258 / MAE 差 0.194。但**未对每个版本做重复评分**，故逐版增量除 V0→V1 外无法与噪声区分（见 §12.2 与 §12.4 风险 2） |
| **Git 提交历史** | **FAIL** | — | — | 未提交，不满足完成标准 §2.3 / §2.4 |
| 抗震荡 rerun | **PASS** | `RERUN_BUDGET` / `version_reference_counts` / `run_train_iteration(rerun=True)` / `record_rerun` | `test_b_rerun.py`（15 项，无 API） | gate 参照取该版所有运行的最小值；一次流程最多一次；`safety_cap` 不触发；被屏蔽的 run1 写入 `b_rerun_log.json`；**`decide_iteration` 判据未改** |
| R5 词表分层（歧义词 + 操作标记） | **PASS** | `UNAMBIGUOUS_RESTRICTION_KEYWORDS` / `AMBIGUOUS_RESTRICTION_KEYWORDS` / `OPERATIONAL_NAME_MARKERS` / `restriction_keyword_hits` | `test_prompt_structure_contract.py`（4 项） | `评分前锚定校准`（含两个歧义词）放行；`评分锚点校准` 仍拒；V1 首次尝试即通过（旧词表 3 次全灭） |
| R5 正文紧邻匹配 | **PASS** | `_DIMENSION_HARD_LIMIT_RE` / `body_dimension_restriction_hits` | `test_prompt_structure_contract.py`（2 项） | `内容分不得低于 6` 拒；`至少两处证据（…病句内容…）` 放行（修正 V1→V2 的同列共现误杀） |
| 契约词表单一来源 | **PASS** | `prompt_optimizer.contract_block()` 从校验器常量渲染（`__CONTRACT_BLOCK__`） | `test_prompt_structure_contract.py::OptimizerPromptContractTest`（4 项） | 提示词中渲染的就是实际匹配词；无未替换占位符；拒绝原因回喂下一次尝试 |
| gate soft 窗口不跨 severe 下降段 | **PASS** | `decide_iteration` 的 `severe_flat_twice` | `test_final_selection_rule.py`（3 项） | severe 下降时不启用 soft 控制；仅连续两段持平才启用 |

---

## 12.2 逐版本结果

**已执行 V0 → V4，共 6 次真实评分运行**（训练集 6 次 × 36 篇 = 216 篇次，
实际 API 调用略少，因断点续跑复用了已完成的篇目；另留出集 24 篇次）。
表中取值口径为 **gate 参照 = 该版本所有运行的最小值**（抗震荡 rerun，见 §12.3）。

| 版本 | Severe | Soft | Score | MAE | MAE 回升 | 字符数 | 加粗分点 | 结构契约 |
|---|---|---|---|---|---|---|---|---|
| V0 | 16 | 7 | 87 | 1.5880 | — | 504 | 5 | PASS |
| V1 | 13 | 5 | 70 | 1.1528 | 否 | 911 | 6 | PASS |
| V2 | 7 | 5 | 40 | 1.0139 | 否 | 1489 | 7 | PASS |
| V3 | **6** | 5 | **35** | **0.9120** | 否 | 1767 | 7 | PASS |
| V4 | 7 | 3 | 38 | 0.9398 | **是** | 2103 | 7 | PASS |
| V5 | — 未执行（V4 触发 gate） | — | — | — | — | — | — | — |
| V6 | — 未执行（V4 触发 gate） | — | — | — | — | — | — | — |

**完整运行序列**（含被屏蔽的 run1，数据源 `version_run_counts`）：

| 运行 | Severe | Soft | Score | MAE | 说明 |
|---|---|---|---|---|---|
| V0 run1 | 16 | 7 | 87 | 1.5880 | |
| V1 run1 | 13 | 5 | 70 | 1.1528 | |
| V2 run1 | 7 | 5 | 40 | 1.0139 | |
| V3 run1 | 11 | 3 | 58 | 1.1065 | **被屏蔽**（噪声）| 
| V3 rerun | 6 | 5 | 35 | 0.9120 | gate 参照 |
| V4 run1 | 7 | 3 | 38 | 0.9398 | |

> 数据源：`prompt_candidate_decisions.json` 的 `candidate_stats`、`b_version_review.json`、`b_gate_report.json`、`b_rerun_log.json`。
> **MAE 仅记录，全程未参与 gate 与排序**；V4 在训练集参照 MAE 与留出集 MAE 上均回升，已按
> `mae_policy=record_only; warn_on_rebound` 告警但不影响选版。

### 逐版本增量的可检出性（人工观测，**不介入 gate**）

同一 prompt（V3）在同一 36 篇训练集上独立评两次所得噪声基线：
逐篇三维总分差 **sd 1.546 / SE 0.258**，MAE 差 **0.194**（95% CI `[-0.309, -0.080]`）。

区间取**改善量**（正数 = 变好）：`改善量 = 前版|偏差| − 后版|偏差|`。

| 转变 | Δ合计偏差（改善量） | ΔMAE（改善量） | 判定 |
|---|---|---|---|
| V0 → V1 | **+1.306** | **+0.4352** | **超出噪声，真实改善** |
| V1 → V2 | +1.028 | +0.1389 | 带内 |
| V2 → V3（对 run1） | −0.223（劣化） | −0.0926 | 带内 |
| V2 → V3（对 rerun） | +0.083 | +0.1019 | 带内（两次运行分列 V2 两侧） |
| V3（rerun）→ V4 | +0.417 | −0.0278 | 带内 |
| **V0 → V4（整体）** | **+2.834** | **+0.648** | **远超噪声，整体有效** |

> 结论：B 路线的**整体改善确凿**，但**除 V0→V1 外，逐版增量均无法与噪声区分**。
> 增益高度前置（V1 之后趋于平缓），与“早期优化强、末期平缓”的预期一致。

---

## 12.3 Gate 与 final

**已执行**。

> **已被取代（2026-09-21 review 后重跑）**：本节记录的是旧协议的收尾（final=V3）。
> 统一 Q 协议下重跑后 final 改为 **V6**，结果见 §12.7.5；本节仅作历史记录。

| 项 | 值 |
|---|---|
| gate 触发版本 | **V4** |
| gate 原因 | `severe_rebound` |
| 触发依据 | 参照 severe：V4=7 > V3=**6**（V3 参照取两次运行的最小值） |
| 抗震荡 rerun | V3 一次（**人工执行**，见 `b_rerun_log.json`）；run1 `11/3` 被屏蔽，参照取 rerun `6/5` |
| gate 候选集合 | `comparison_iterations(decision, 4)` = **(3, 4)** |
| 留出集比较 | V3：severe=1 / soft=2 / **Score=7** / MAE=0.9167；V4：severe=2 / soft=1 / Score=11 / MAE=0.9444 |
| **selected_iteration** | **V3** |
| final prompt | `final_prompt_meta.md` = `optimized_prompt3_meta.md`，1767 字符 |
| final prompt sha256（前 16）| **`40ba28fa0019df4f`** |
| MAE 告警 | `validation MAE rebounded from V3=0.917 to V4=0.944`（已记录，未参与排序） |
| final 附带产物 | `final_prompt.md`、`final_aes_badcases.json`、`final_train_scoring_results.json`、`b_gate_report.json` |

> 留出集（V3 胜）与训练集参照（V3 Score 35 vs V4 38）**独立地指向同一结果**。
> 但留出集只有 12 篇且每版只评一次，其单独功效不足（见 §12.4 风险 7）；
> 两者一致提高了结论的可信度，而非各自构或证明。

### 已确认的执行前参数（大纲 §3.3 要求在执行前明确）

| 项 | 值 | 来源 |
|---|---|---|
| gate 触发版本 | 由 `decide_iteration` 决定 | `pipeline_entry.py` |
| gate 候选集合（severe 回升 / soft 停滞） | `(stable_iteration, last_iteration)` | `comparison_iterations` |
| gate 候选集合（V6 安全上限） | 最后三版 `(V4, V5, V6)` | `comparison_iterations` |
| 排序规则 | `Score = 5×Severe + Soft` → severe → 更早版本 | `candidate_rank` |
| MAE 政策 | 只记录 + 回升告警，不参与排序 | `compare_candidates` |
| 抗震荡 rerun | 一次流程最多一次；gate 参照取该版所有运行的最小值 | `RERUN_BUDGET` / `version_reference_counts` |
| 留出集 | `test_essays.json`，12 篇，index `[3,4,9,10,12,21,23,24,29,30,35,39]` | 实测 |
| 训练集 | `train_essays.json`，36 篇 | 实测 |
| 结构预算 | 分点 ≤ 12、字符 ≤ 4200、单轮 +800、单轮新增分点 ≤ 2 | `prompt_structure_contract` |

---

## 12.4 未完成与风险

### 未接入 / 未完成的模块

| 项 | 状态 | 说明 |
|---|---|---|
| 重复评分 / 稳定性报告 | **未实施** | 完成标准 §3.2.3 P0 #5 要求；需先确定模型是否接受 `temperature` |
| Git 提交历史 | **未实施** | 完成标准 §2.3 / §2.4；当前仅 1 个 commit + 大量未提交改动 |
| 凭据轮换确认 | **待人工** | 自动化只能扫描源码，不能验证轮换 |
| `etype_iteration_runner` / `micro_scoring_gate` 的既有缺陷 | **未修** | 属 E 路线，本阶段不实施；已在 `ETYPE_ITERATION_V2_DESIGN.md` §5.2 记录（micro 的 `MAX_SINGLE_DIM_REGRESSION = 1.0` 与 regular 的 `0.5` 不一致） |
| k 折改造 | **已搁置** | 见 `shelved/README.md`；`pipeline_entry` 中保留了 `RunPaths` 接缝与协议混用守卫 |

### 只完成设计、未完成实现的内容

- `ETYPE_ITERATION_V2_DESIGN.md` §14 描述的是 CV 协议，**当前已搁置**。
  该文档是设计稿，不构成任何实现声明。

### 未覆盖的测试分支

- `PromptOptimizer.send_api_request` 的真实重试/超时路径未测（会被网络 mock 绕过）。
- `main()` CLI 入口未做端到端测试。
- `archive_run.py` 未加单元测试（其为一次性运维脚本，靠运行记录与 manifest 佐证）。
- `write_run_manifest` 未覆盖"split 文件缺失"分支。

### 已知风险

1. **12 篇留出的代表性限制**：该集合在本协议下**仍然参与版本选择**
   （`compare_candidates` 用它选 final），因此不是独立测试集。
   完成标准与 README 中的相关披露**必须保留**，不得因改造而删除。
2. **模型非确定性（已量化）**：评分请求中不含 `temperature` 参数。
   以同一 prompt（V3）在同一 36 篇训练集上**独立评两次**测得：
   - 逐篇三维总分差：均值 `+0.306`、标准差 `1.546`、**标准误 `0.258`**；
     95% CI `[-0.199, +0.811]` 含 0，即**无系统性漂移，纯方差**。
   - 逐篇 AI 三维完全相同仅 **13/36（36%）**，平均单维绝对差 **0.398 分**。
   - **两版比较的可检出阈值 ≈ 0.729**（三维合计均值差）。
   - 同一 prompt 的 `severe` 两次运行分别为 **11 与 6（相差 5）**。
   据此复检逐版增量：V0→V1 `+1.306`、V1→V2 `+1.028` **均超过阈值**（真实改善）；
   V2→V3 两次运行分列 V2 两侧（`−0.222` / `+0.306`）**均不可检出**。
   **推论**：V3 触发的 `severe_rebound` gate 是**噪声误报** —— 同一 prompt 的
   `severe` 自身波动即达 5，而规则用严格不等式比较相邻版本。这印证了此前的
   F-7 缺口（停止阈值缺少最小效应量）。本阶段仍未对每个版本做重复评分，
   故单版本结论的置信度低于表中所示。
3. **人工操作步骤**：
   - 归档需人工执行 `python archive_run.py --execute`（本次已执行）；
   - 重跑需人工设置 `AES_API_KEY` / `AES_SCORING_MODEL` / `AES_OPTIMIZER_MODEL`；
   - final 晋升需人工执行 `--promote-final`。
4. **结构改造的边界**：`validate_optimizer_edit` 只能拒绝**可机器判定**的违规
   （标题集合/顺序、新增分点类型与数量、`###`、预算）。
   新增分点是否属操作型由两层词表判定：`UNAMBIGUOUS_RESTRICTION_KEYWORDS`
   必拒；`AMBIGUOUS_RESTRICTION_KEYWORDS`（锚点/校准）在标题含
   `OPERATIONAL_NAME_MARKERS` 时放行。正文另有**紧邻**匹配
   `(维度词)分?\s*(硬比较标记)`。**两个方向都已有实测偏差**：
   - **过宽（已修）**：`校准` 曾误杀 `**评分前锚定校准**`，导致 V1 三次尝试全废
     （`CandidateRejectedError`）；正文"同行共现"版曾误杀
     `**打分前逐维度扣分依据自查**`，并迫使模型删掉"至少两处具体证据"，
     造成自查精度退化（V1→V2 实测）。
   - **漏网（未修）**：新造一个不含任何关键词、但语义上是维度限制的标题仍可通过；
     且维度限制写成 `特殊情形` 分点下的**条目**是契约**明确允许**的
     （R5 的提示语本身就在引导模型这么做），该位置无任何数值合理性校验。
   正向白名单只写在优化器提示词里，未做机器强制。

5. **百分比锚点 × 整数评分的未定义行为（越界 1：仅记录，不控制）**：
   V2 给内容 / 表达 / 结构三维度各加了 `满分的 70% / 60% / 40%` 锚点。
   但评分网格是 **0–9 整数**（教师端另有 0.5 尾），满分 = 9，于是
   `70% = 6.3`、`60% = 5.4`、`40% = 3.6`，而 prompt 中**没有任何取整约定** ——
   模型须自行决定向上或向下取整，可能造成系统性 ±1 偏移。
   **风险随百分比继续精细化而上升**（系数越细，取整歧义越大）。
   按设计原则，观测者**不为此加控制**：不改契约、不加取整规则、不加数值校验。
   本项仅作为**未定义行为**的记录，供后续判读逐版本数据时参考。

   **已实际发生（V2→V3）**：V3 把三个维度的下限由 `满分的 70%` 统一上调到
   `满分的 75%`。但在 0–9 整数网格上 `70% = 6.3`、`75% = 6.75`，
   两者取整后的最低合格分**同为 7** —— 即这次“精细化”没有改变任何有效下限，
   属**语义空转**；它却占用了指令预算，并引入了一个整数网格无法满足的
   要求（6.75）。同期 V3 的 severe 由 7 回升到 11，三维度平均偏差全部变负
   （合计 −2.208 → −2.431）。**此为相关而非因果**：模型非确定性尚未排除
   （见风险 2），需重复评分才能分离。

6. **模型自负风险行为（观测者不控制）**：
   - **维度锚点叠加**：三维度同时获得同向百分比下限，叠加"两档之间取高"，
     可能整体上推分数分布；B 指标只看 badcase 计数，**不观测分布中心位移**。
   - **防松条款移除**：`不可因文章有情节而维持高分` 于 V1 被删除且未恢复。
     就 V0–V2 数据而言无宽松漂移证据（三版方向 **100% 为 `strict`**），仅记录在案。
   这两项与越界 1 同属"模型对条款内容自行判断"的范畴，按设计原则不做观测者控制。
7. **§3.4 候选比较的统计功效不足（未实施，仅估算）**：`compare_candidates` 在
   12 篇留出集上、每版各评一次。以本次实测噪声（逐篇 sd 1.546）推算，
   12 篇上的标准误 ≈ 0.446，两版差 ≈ 0.631，可检出阈值 ≈ **1.26**；
   而 V2 与 V3 的实测差约 **0.2**，远低于阈值。即按现行 §3.4 执行，
   **比较结果不可靠**，且功效**低于**已用过的 36 篇训练集。

8. **配对 t 检验在本数据集上不可靠（方法学）**：同一 prompt（V3）两次运行
   在逐篇 MAE 上就给出配对 `t = 3.3`、95% CI `[-0.309, -0.080]`（按标准判据“显著”），
   而真实差异**必然为零**。原因：36 篇共享同一个运行级偏移，逐篇差异**不独立**，
   **单版本单次运行的有效样本量是 1，不是 36**。因此本项目此前所有跨版本
   配对 t 检验（含此前“V0→V4 t = −4.75~−8.62 全部显著”）都需附此限制阅读。

9. **“打补丁”在 V2 处饱和**：V0→V1（+1.306）与 V1→V2（+1.028）均超过噪声阈值，
   是真实改善，且两者用的是同一类手段（收紧 `特殊情形` 触发条件 + 操作型自查 + 数值锚点）。
   V3、V4 继续同向加码后增益不可检出。叠加风险 5（`70%`→`75%` 在整数网格上取整后
   同为 7，属语义空转），可知该类手段的边际收益已耗尽。

10. **抗震荡 rerun 规则的采纳时点晚于其判定的事件**：V3 的第二次评分先于
    机制实现（`b_rerun_log.json` 记为 `trigger: "manual"`），而最终选版
   依赖于该 rerun 得到的参照值。即本次“min 规则”属于**事后采纳**，
    不在执行前确认的参数内。缓解：留出集比较（V3 Score 7 vs V4 11）
    与训练集参照（35 vs 38）**独立地指向同一结果** —— 两者均指向 V3。

---

## 12.5 验收决议（2026-09-21）

### 验收范围

B 类结构改造的**全部机制** + **一次完整真实迭代（V0→V4）+ final 选出**。
不含 E 类迭代。

### 逐项验收结果

| 类别 | 项 | 结果 |
|---|---|---|
| 结构契约 | R1/R1b（标题冻结与顺序）、R4b（每轮新增 ≤ 2）、R6（顶层 `##`）、R7（`###`）、预算 | **PASS** |
| 结构契约 | R5 两层词表（歧义词 + 操作标记）、正文紧邻匹配 | **PASS** |
| 候选事务化 | 校验→重试→原子写；非法候选不落盘；拒绝原因留存并回喂 | **PASS** |
| 稳定版本保护 | 拒绝后 `CURRENT_PROMPT` 逐字不变；`optimize_or_stop` 阻塞即停 | **PASS** |
| 划分协议 | seed-42 36/12；index 集合可复现且与归档一致 | **PASS** |
| 指标政策 | MAE 只记录 + 回升告警，不参与 gate 与排序 | **PASS** |
| 停止规则 | severe 降/升/持平 + soft 窗口不跨 severe 下降段 + V6 上限 | **PASS** |
| final 排序 | Score → severe → 更早版本 | **PASS** |
| 可复现性 | 运行 manifest、残留守卫、产物归档（含 sha256） | **PASS** |
| 抗震荡 rerun | min 参照、一次/流程、`b_rerun_log.json` 记录、`decide_iteration` 未改 | **PASS** |
| 真实重跑 | V0→V4 共 6 次评分运行；V4 触发 gate；final=V3 | **PASS** |
| 单一来源 | 契约词表由校验器常量渲染进优化器提示词 | **PASS** |
| 稳定性 | 重复评分 / 噪声基线 | **PARTIAL**：仅 V3 有噪声基线，未逐版重复 |
| 工程 | Git 提交历史（完成标准 §2.3/§2.4） | **FAIL**：未建立 |

> 关于 §十三 条件 10（“报告无未披露的 `PARTIAL` 或 `FAIL`”）：
> 上述两项**已如实披露**。其中“重复评分”不在 §十三 的冻结清单内，
> 其影响已在 §12.2 与 §12.4 风险 2 量化（逐版增量除 V0→V1 外不可检出）；
> “Git 提交历史”出自另一份文档（PROJECT_COMPLETION_STANDARD §2.3/§2.4），
> **不在 §十三 清单内**。因此条件 10 成立。

### 冻结前提（大纲 §十三，逐条对照）

| # | 条件 | 状态 |
|---|---|---|
| 1 | 结构约束已接入生产路径 | ✅ |
| 2 | origin、所有正式候选和 final 均通过契约 | ✅ origin/V0–V4/final 均 PASS；final 检查已是**流程步骤**（G4） |
| 3 | 旧产物已归档，新旧 run 不混用 | ✅ `archive/` 四处 + `discarded/v2_superseded_*` |
| 4 | 固定 36/12 划分得到确认 | ✅ index 与归档一致，overlap=0，union=48 |
| 5 | 离线测试全部通过 | ✅ **174 tests OK**；`project_checks` 5/5 |
| 6 | 实际 B 重跑完整结束 | ✅ V0→V4，final 已选出 |
| 7 | gate 和 final 选择符合预定规则 | ✅ **按 §12.6 登记的修订版规则**（3 处修订均经人工授权并登记） |
| 8 | MAE 只记录和告警 | ✅ 训练集与留出集回升均已告警，未参与排序 |
| 9 | final 结构稳定，可供后续 E 使用 | ✅ 1767 字符、7 分点、契约 PASS、sha256 固定 |
| 10 | 客观检查报告无未披露的 `PARTIAL` 或 `FAIL` | ✅ 见 §12.1；残项仅 Git（**非 §十三 条件**，见下） |

> **更正**：本节早期版本把"Git 提交历史"列为冻结阻断项。**该条出自另一份文档
> （PROJECT_COMPLETION_STANDARD §2.3 / §2.4），不在大纲 §十三 的冻结清单内。**
> Git 未提交仍作为 §12.1 的一个 FAIL 条目保留，但**不阻断**
> `B_FROZEN_READY_FOR_E_DESIGN`。

### 决议建议：**可冻结**

§十三 的 10 项条件已全部满足。建议：

1. 将报告头部状态由 `B_RESULT_AVAILABLE_BUT_NOT_FROZEN` 改为
   `B_FROZEN_READY_FOR_E_DESIGN`；
2. 冻结同时移交下方"需随 final 一并移交的已知限制"全部 9 条；
3. 仍建议尽快建立 Git 提交（虽非 §十三 条件），使该基线事后可复核。

### 需随 final 一并移交的已知限制

1. **无 `temperature` 控制**：同一 prompt 的逐篇三维总分差 sd 1.546、单版 SE 0.258；
   `severe` 自身波动可达 5。
2. **逐版增量除 V0→V1 外均不可检出**：V0→V4 整体改善（MAE 0.648）确凿，
   但 V2/V3/V4 三版之间无统计上可区分的优劣。
3. **配对 t 检验在本数据集上不成立**（风险 8），任何引用需附限制。
4. **§3.4 功效不足**（风险 7）：留出集 12 篇、每版一次，可检出阈值 ≈ 1.26。
   本次留出集结果与训练集一致，但若两者不一致，留出集不足以单独裁决。
5. **min 规则事后采纳**（风险 10）。
6. **越界 1 未受控**（风险 5）：百分比锚点在 0–9 整数网格上的取整歧义，
   已实测发生语义空转；按设计原则**不加以控制**。
7. **契约不约束分点间的语义矛盾（review 修正）**：跨维度耦合条款自 **V3** 起即已可见
   （`评分前锚点校准自查` 要求比对表达/结构分与内容分），V4 进一步加码
   （`不得单独收紧表达分`、`低于内容分超过满分 10% 应上调`）；它与顶层
   `**独立赋分**`（“三项独立评分，互不影响”）存在表述张力（顶层“但以下特殊
   情形除外”为其限定）。R1–R7 均无法拦截（约束缺口见风险 4），
   按“模型自负风险”原则仅记录、不控制。
8. **E 侧接口未接线**：`build_anchor_registry` / `write_anchor_registry` 已实现，
   但属 E 类范围，本阶段未接入。
9. **条目级 diff 未实现（经人工裁定接受，不计为 `PARTIAL`）**：`section_diff`
   只到**分点**粒度，大纲 §八“被删除或改写的项目符号”未做到条目级。
   人工已逐轮审阅每版的 `changed_sections` / `added_sections` 与逐项改动对照，
   视为**经人工干预且结果可信**，因此不列入未完成项。

---

## 12.6 协议修订登记（大纲 §3.2 / §3.3 偏离记录）

本轮在执行过程中对大纲 §3.2 / §3.3 做了 **3 处修订**。三处**均为人工（用户）
明确指示**，非模型自行改动；此处逐条登记，以满足 §十三 条件 7
（“gate 和 final 选择符合预定规则”）的可核查性。

| # | 部位 | 原规则（大纲） | 修订后规则 | 生产代码 | 测试 | 影响 |
|---|---|---|---|---|---|---|
| A1 | §3.2 soft 停滞 | `severe_n == severe_{n-1}` 时，soft 连续两个版本不下降即 gate | 额外要求**两段 severe 均持平**（`severe_{n-2} == severe_{n-1} == severe_n`）才启用 soft 控制 | `decide_iteration` 的 `severe_flat_twice` | `test_final_selection_rule.py`（3 项） | 防止把“severe 降级为 soft”的噪声误判为放宽漂移（V0→V2 实测：V2 的 8 条 soft 中 5 条来自 V0 severe 降级，三版方向 100% `strict`） |
| A2 | §3.3 版本代表值 | 未定义“一个版本多次评分时取哪次” | 新增**抗震荡 rerun**：一次流程最多一次；gate 参照取该版所有运行的**最小值** | `RERUN_BUDGET` / `version_reference_counts` / `run_train_iteration(rerun=True)` / `record_rerun` | `test_b_rerun.py`（15 项） | V3 参照由 run1 的 `11/3` 改为 rerun 的 `6/5`；使 V4（`7/3`）触发 `severe_rebound` gate |
| A3 | §3.2 severe 回升 | `severe_n > severe_{n-1}`→立即 gate | V3 处经人工临时覆盖为 continue（`--force-continue`），以便用 V4 检验 V3-run1 是否为噪声 | 原在 `tmp_resume_run.py --force-continue`；**已按验收要求从工作区移除**，移至 `discarded/tmp_tools_20260921123129/` 备查 | 无（临时件不加测试） | 若 V3 不覆盖 gate，流水线会继续到 V5；`force_continue` **不影响 `safety_cap`** |

### 修订的审计后果

A1 修改了 `stop_policy.rule` 的文字标识，因此**在本章生效后构建的运行 manifest
与修订前的历史 manifest 身份不一致**；G2 复用守卫会据此拒绝复用旧产物。
首次实测触发了该拒绝：

```
旧 run manifest 与当前身份不一致，拒绝复用旧产物（大纲 §9.2）：['stop_policy']
  manifest: run_manifest_run_20260920T180152.json
  run_id  : run_20260920T180152
```

这是**正确行为**：该 manifest 写于 A1 之前，其产物确实不属于当前协议。

> **2026-09-21 补注**：本节 A1–A3 三处修订已被 §12.7 的统一 Q 协议**完全取代**，
> 在流程中视为失效（并入历史记录），仅作执行史保留。

---

## 12.7 协议修订：统一 Q 目标（vNext，2026-09-21，review 后）

> 背景：外部 review 指出旧 A1/A2/A3 机制存在“执行后修改规则”“asymmetric min 聚合”
> “final bundle 与参照运行不一致”等问题。经用户逐项核定（含“全面采用新协议”授权），
> 本节的新协议**完全取代** A1/A2/A3；§12.6 的三处旧修订在流程中失效。

### 12.7.1 冻结的规则

| 部位 | 规则 |
|---|---|
| 目标函数 | `Q = 2.5 * mean(Severe) + mean(Soft)`；均值为该版本在当前精度等级下**全部**固定次数评测的算术平均（不取 min、不四舍五入；展示 2 位小数） |
| gate 判据 | `Q_n >= Q_{n-1}` 即触发（**平台期也触发**；只有严格下降才算通过） |
| 低精度探索 | 从 V0 起每版单评；严格下降则继续；首次预警**不立即停止** |
| 精度升级（首次预警） | **永久**设置 `repeat_level=2`；补评 x-1 与 x 各至两次；不补评 x-2、不重开更早比较；复核：双评均值仍 `Q̄x >= Q̄(x-1)` 则停止（候选 x-1、x），否则判为低精度噪声、接受 x 继续（此后固定双评、不可回退） |
| 高精度阶段 | 每版固定双评；`Qn >= Q(n-1)` 即停止（有效 gate，候选 n-1、n；无需等待“第二次 gate”） |
| V6 上限 | 已在高精度：送 V5、V6 进 validation；从未预警：在 V6 强制提升精度，**只**补评 V5、V6；不向前回补 |
| validation | 两候选使用同一 validation 样本；**各自固定两次独立评测**（不依第一次结果决定第二次）；取 Severe/Soft 算术平均 |
| final 排序 | 期望 Q 更低 -> 期望 Severe 更少 -> 更早版本；MAE 仍只记录 + 回升告警 |
| 状态持久化 | `b_protocol_state.json`（`repeat_level` / `upgraded_at` / `warning_events`）；跨 resume 生效、不可回退 |

### 12.7.2 权重 w=2.5 的正式解释（业务风险权重，非拟合兑换率）

`Q = 2.5*mean(Severe) + mean(Soft)`，交换判定 `ΔQ = +w - k`（+1S/-kF）：

| 交换 | 判定 | 语义 |
|---|---|---|
| +1S / -3F | ΔQ = **-0.5** -> 通过 | “3 个 Soft 可以抵消 1 个 Severe” |
| +1S / -2F | ΔQ = **+0.5** -> 触发 | 拒绝“消 1 个 Soft、另 1 个 Soft 升级为 Severe”的净变化 |
| 平台 ΔQ = 0 | 触发 | 平台期不容忍（A4） |

w=2.5 是位于 **(2, 3)** 内的**业务风险权重**，不是对实验结果拟合出的“精确兑换率”：

1. **防早停**：轨迹在 V3 之后仍有大额改善（V4 −5.00、V5 −7.25）。
   重建时序下 V3 正是首次低精度预警点；若 w=3，边界复核 ΔQ 恰为 0（平台）
   -> 预警被确认 -> 在 V3 提前停止，后续改善全部丢失。w=2.5 时边界 ΔQ=-0.25
   -> 判为噪声 -> 继续（见 §12.7.3/§12.7.5）。
2. **防套利**：w<2 会放行 `+1S/-2F` 的净变化；2.5 恰好拒绝。

### 12.7.3 历史实验中 gate 触发的主流倾向（要求项；2026-09-21 时序重建后修订）

对已发生的 V0–V6 真实数据，分别按“旧规则实况 / **新协议真实时序重建** / 单次观测口径”核对：

1. **旧规则实况**：全部两次真实 gate 事件都源自**单次观测**，且都被第二次评测推翻——
   - V3：run1 = severe 11 / soft 3 触发 `severe_rebound`；补评得 6/5（下降）
     -> 按“severe 下降”继续（`b_rerun_log.json` 第 1 条，人工触发）；
   - V4：`7/3` 相对 V3 参照 `6/5` 触发 `severe_rebound`；对称化补评后 V4 = `6/5`，
     rebound 消失（第 2、3 条）——后经 review 认定为**不对称聚合的伪影**。
2. **新协议真实时序重建**（`replay_protocol_route`，低精度只用 run1；路线表见 §12.7.5）：
   首次且唯一一次预警出现在 **V3**（单评 Q 30.50 >= V2 的 22.50，Δ=+8.00）；
   边界双评复核（V3 均值 25.25 < V2 均值 25.50，Δ=-0.25）**推翻**该预警，
   接受 V3 并永久进入双评；V4（Δ=-5.00）、V5（Δ=-7.25）高精度通过；
   **V6（Δ=+1.00）在双评均值上触发高精度 gate**（`high_precision_gate_at_cap`）。
   > 早期版本的旧重放把 V2–V4 的历史 rerun 提前并入低精度阶段，得到“全程零预警”的
   > 错误结论；现按真实时序重建后修正（旧重放已删除，route-aware 聚合见 §12.7.4）。
3. **权重敏感性（重建时序）**：边界复核的翻转点为 **w*=3**——
   w=3 时 ΔQ=0（平台）-> 预警被确认 -> 在 V3 提前停止；w=2.5 时 ΔQ=-0.25 -> 判为噪声继续。
   这正是 w 取 2.5 的“防早停”依据（§12.7.2）。

**主流倾向归纳**：历史 gate 压力**集中出现在第一个接近持平的转变（V2->V3）**；
旧规则下的两次真实触发与重建时序下的首次预警**都落在同一位置**，且来源都是单次观测波动
（V3 run1 异常；V4 的 rebound 来自不对称聚合）。V3 的预警在双评复核下被推翻，
说明“低精度预警 -> 精度升级复核”机制按设计生效；而 V6 的高精度 gate 是本轮
**唯一未被推翻的停止信号**（恰好与安全上限重合）。

### 12.7.4 实现与受控迁移

- 代码：`pipeline_entry.py`（`Q_SEVERE_WEIGHT`、`decide_iteration`、`resolve_decision`、
  `top_up_version`、`run_validation_and_select`、`replay_protocol_state` 等）；
  `run_manifest.py`（`manifest_mismatches` / `apply_policy_migration`）。
- 状态文件：`b_protocol_state.json`；缺失时按**真实时序重放**重建并落盘
  （2026-09-21 17:37 重建结果：`repeat_level=2`、`upgraded_at=3`、V3 预警经复核被推翻、
  `stop=high_precision_gate_at_cap`；见 §12.7.5 路线表与 `b_rebuilt_route.json`）。
- manifest 受控迁移：仅允许 `stop_policy` / `protocol` 两个**策略声明**字段按新协议更新，
  并写入 `policy_migrations` 记录（时间、字段、前后值、说明）；数据身份（模型/阈值/文件/划分）
  任一不一致仍拒绝复用。本轮已将 `run_manifest_run_20260920T180152.json` 迁移至新协议声明
  （旧声明留存于迁移记录）。
- 旧机制清除：`ensure_symmetric_runs` / `version_reference_counts`（min）/ `log_symmetric_rerun` /
  `compare_candidates` / `comparison_iterations` / `load_count_history` 已删除或替换；
  被取代的 3 个旧测试文件归档至 `discarded/test_superseded_20260921/`，
  替代测试为 `test_q_protocol.py` 与重写的 `test_final_selection_rule.py`。
- route-aware 聚合（冻结前补丁）：`allowed_run_count` / `mean_counts_from` / `excluded`；
  旧重放删除，`replay_protocol_route` 按真实时序重建（`rebuild_protocol_timeline`）。
- 离线验证：`Ran 177 tests - OK`；`project_checks` 5/5 PASS。

### 12.7.5 收尾结果（2026-09-21，V6）

**执行路径（2026-09-21 17:37 按真实时序重建，见 `b_rebuilt_route.json`）**：
低精度阶段（只用 run1）-> **V3 首次预警** -> 边界双评复核（V3 均值 25.25 < V2 均值 25.50）
**推翻预警**、接受 V3 并永久升级 -> V4/V5 高精度通过 -> **V6 高精度 gate**
（ΔQ=+1.00，`high_precision_gate_at_cap`）-> validation 候选 (V5, V6)。

| 节点 | 使用 | S/F | Q | ΔQ | 判定 |
|---|---|---|---|---|---|
| V0 | run1 | 16/7 | 47.00 | — | 起点 |
| V1 | run1 | 13/5 | 37.50 | −9.50 | 低精度通过 |
| V2 | run1 | 7/5 | 22.50 | −15.00 | 低精度通过 |
| V3 | run1 | 11/3 | 30.50 | **+8.00** | **首次低精度预警** |
| V2（边界） | 双评均值 | 8/5.5 | 25.50 | — | 升级边界 |
| V3（边界） | 双评均值 | 8.5/4 | 25.25 | **−0.25** | **预警被推翻**，接受 V3；永久双评 |
| V4 | 双评均值 | 6.5/4 | 20.25 | −5.00 | 高精度通过 |
| V5 | 双评均值 | 3/5.5 | 13.00 | −7.25 | 高精度通过 |
| V6 | 双评均值 | 3/6.5 | **14.00** | **+1.00** | **`high_precision_gate_at_cap`** |

> 记录：`upgraded_at=3`；V3=`low_precision_warning_refuted`；V4/V5=`high_precision_pass`；
> V6=`high_precision_gate_at_cap`。训练集均值与 validation 排序不一致（train 指向 V5
> 13.00 < V6 14.00；validation 指向 V6），差值在噪声量级（见下方限制）。
> **可追溯真相**：live 会话（16:44）在精度等级判断前以 `cap_forced` 执行了同一组
> (V5, V6) 补评；其测量位置与路线要求的双评位置一致，测量全部采纳，官方停止原因
> 按重建时序命名；`cap_forced` 记录保留于 `b_rerun_log.json` 第 4 条。

**validation（留出 12 篇，每候选固定两次独立评测，取均值）**：

| 候选 | 全部运行 (S/F) | 均值 S/F | Q | MAE（两次 -> 均值）|
|---|---|---|---|---|
| V5 | 1/2；2/1 | 1.50 / 1.50 | 5.25 | 0.8333 / 0.8056 -> 0.8194 |
| **V6** | 2/0；1/2 | **1.50 / 1.00** | **4.75** | 0.6944 / 0.7500 -> 0.7222 |

**final = V6**（validation Q 更低；两候选 severe 均值相同（1.50），差异全部来自 soft 的 1-2 条）。
final bundle 按**同源 run1** 晋升并全部通过校验：

| 产物 | 来源 | 说明 |
|---|---|---|
| `final_prompt_meta.md` / `final_prompt.md` | `optimized_prompt6_meta.md`（2695 字符、8 分点、契约 PASS）| sha256 前 16：`72ca44765e86c075` |
| `final_train_scoring_results.json` | `train_scoring_results6.json` | 与来源逐字节一致（同一次运行）|
| `final_aes_badcases.json` | `aes_badcases6.json` | 与来源逐字节一致（同一次运行）|

**协议状态与审计**（重建后）：`b_protocol_state.json` -> `repeat_level=2`、`upgraded_at=3`、
`warning_events=[V3 预警，经双评复核被推翻]`、`stop=high_precision_gate_at_cap (5,6)`；
`b_rerun_log.json` 第 4 条保留 `cap_forced` 历史真相，第 5 条为 `timeline_rebuild`（重建记录）；
manifest 覆盖 V0–V6 全部运行与 validation 产物（`policy_migrations=1`）。

**证据文件**：`b_rebuilt_route.json`（逐节点路线）、`final_evidence.json`
（训练双评 + validation 双评 + 均值/Q + 选择理由）、
`final_train_scoring_results_mean.json` + `.provenance.json`（E 类均值基准，逐篇逐维平均）。

**执行说明**：本轮收尾分两次会话完成（中断点为 V6 强制补评前，已定位并幂等恢复，无重复评测）；
续跑期间修正了 `--resume-from` 在上限处的入参守卫；冻结前按审查结论完成离线**时序重建**
（不消耗 API）与 **route-aware 聚合**（`allowed_run_count`：路线外 rerun 只记 `excluded`，
不参与 gate/validation/final）。

**限制（D3，需随 final 一并披露）**：
1. validation 每候选仅 **2 次**评测、留出仅 **12 篇**，可检出阈值有限；
2. **train 与 validation 排序不一致**（train：V5 更优；validation：V6 更优），
   差值（ΔQ=0.5–1.0、Δsoft=0.5）在噪声量级——V5 与 V6 在本轮样本量下**难以可靠区分**；
3. 更早版本（V5 及之前）可能因**单评/双评的有限样本**而实际更优：低精度搜索策略的既有代价；
4. 所谓“停止信号”只在 V6 一次通过复核（`high_precision_gate_at_cap`），
   且恰与安全上限重合——本轮**不能证明协议在非上限位置也能正常停止**；
5. `test_essays.json` 参与了 V5/V6 选择：本项目**没有独立 test**（INDEPENDENT_TEST=NONE）。

---

## 12.7.6 冻结记录（2026-09-21，实验性口径）

```
PROJECT_CLASS = EXPERIMENTAL
B_PROTOCOL = FROZEN
B_RESULT = EXPERIMENTAL_RESULT_ACCEPTED
FINAL_B_VERSION = V6
E_STATUS = DEFERRED
INDEPENDENT_TEST = NONE
```

**审查结论逐项落实（1–10）**：

| # | 决定 | 落实 |
|---|---|---|
| 1 | 权重冻结 2.5 | §12.7.2 改写为“业务风险权重 (2,3) 偏大”；不再表述为“Severe=3 Soft”精确等价 |
| 2 | 允许复用历史 rerun，离线纠正时序 | `replay_protocol_route` 按真实时序重建（V3 预警 -> 复核推翻 -> 永久双评）；state 修正为 `upgraded_at=3`；`b_rebuilt_route.json` 落盘 |
| 3 | 定性为实验性项目 | 头部状态与本节状态块；不声称前瞻性完整实验/生产标准 |
| 4 | 接受 V6 | final=V6；报告不称“V6 显著优于 V5”（限制 2） |
| 5 | validation 定义 | INDEPENDENT_TEST=NONE（限制 5） |
| 6 | E 类用平均值基准 | `final_train_scoring_results_mean.json` + `.provenance.json`（逐篇逐维均值；教师分不变）；E 候选单评/双评留待 E 设计 |
| 7 | rerun 归属 | `allowed_run_count` + `excluded`：路线内计数参与 gate/validation/final；路线外仅留档 |
| 8 | 复用哈希强化 | **暂不考虑**（维持现状；记为已知缺口） |
| 9 | 报告陈旧值 | 循环内补评 x-1 后刷新 `previous_review`（本轨迹走 pre-loop 路径未受影响） |
| 10 | 停止原因重命名 | V6 官方原因 = `high_precision_gate_at_cap`；`cap_forced` 保留于日志以追溯真相 |

**冻结范围**：代码机制（Q=2.5 + 精度分级 + route-aware 聚合 + validation/final 规则）、
候选 final=V6、上述证据文件。**不含**：E 迭代、独立 test、Git 正式提交。

**移交限制**：§12.7.5 限制 1–5、§12.4 风险 2/5/6/8（噪声、越界、配对检验限制）。

---

## 附：本轮改动文件清单

| 文件 | 性质 |
|---|---|
| `prompt_structure_contract.py` | 新增 R1b（顺序）、R4b（每轮新增 ≤ 2）、`bold_section_bodies`、`section_diff` |
| `prompt_optimizer.py` | 候选事务化：`validate_candidate` / `atomic_save_prompt` / `extract_candidate_from_response` / `record_decision` / `structure_stats`；`run()` 改为"校验→重试→原子写"；空 badcase 除零修复；`iteration_history` 章节审计补齐 |
| `pipeline_entry.py` | `optimize_or_stop`（阻塞即停）、`assert_clean_working_directory`、`write_run_manifest`、severe/soft 阈值常量化 |
| `run_manifest.py` | `build_experiment_manifest` / `write_experiment_manifest` |
| `archive_run.py` | 新增：归档旧产物并写 sha256 manifest |
| `sample_extractor.py` | 恢复 seed-42 36/12（回退 CV 划分） |
| `test_prompt_optimizer_wiring.py` | 新增：13 项真实调用链测试 |
| `test_run_preflight.py` | 新增：10 项前置检查与 manifest 测试 |
| `test_final_selection_rule.py` | 新增：14 项停止规则、排序、MAE 政策测试 |
| `test_pipeline_paths.py` | 新增：6 项路径与协议混用守卫测试 |
| `.gitignore` | 新增 `archive/`、`prompt_candidate_decisions.json` |
| `shelved/` | 新增：搁置的 k 折模块 + 说明 |

### B 全链路验收阶段的追加改动（2026-09-20 / 09-21）

| 文件 | 性质 |
|---|---|
| `prompt_structure_contract.py` | R5 词表分层（`UNAMBIGUOUS_*` / `AMBIGUOUS_*` / `OPERATIONAL_NAME_MARKERS`）、`restriction_keyword_hits`、正文紧邻匹配 `_DIMENSION_HARD_LIMIT_RE` |
| `prompt_optimizer.py` | `contract_block()`（从校验器渲染实际匹配词，`__CONTRACT_BLOCK__`）、拒绝原因回喂 `previous_reasons`、`describe_candidate()`（拒绝可审计）、响应解析异常不再中断整轮 |
| `pipeline_entry.py` | 抗震荡 rerun：`RERUN_BUDGET` / `RERUN_SUFFIX` / `RERUN_LOG`、`RunPaths.rerun_scoring()` / `rerun_badcase()`、`version_run_counts` / `version_reference_counts` / `record_rerun`、`run_train_iteration(rerun=True)`、主循环 rerun 步骤、`version_review` 暴露 `runs` |
| `pipeline_entry.py` | `decide_iteration` 的 soft 控制加 `severe_flat_twice`（不跨 severe 下降段） |
| `archive_run.py` / `.gitignore` | 纳入 `b_rerun_log.json` |
| `test_prompt_structure_contract.py` | 追加 9 项（R5 分层 / 正文紧邻 / 契约单一来源 / 回喂） |
| `test_final_selection_rule.py` | 追加 3 项（soft 窗口不跨 severe 下降段） |
| `test_b_rerun.py` | 新增：15 项抗震荡 rerun 无 API 测试 |
| `run_manifest.py` | 新增 `_atomic_write_json` / `file_hashes` / `build_version_record` / `attach_version_record`（manifest 每版记录）、`manifest_identity` / `assert_manifest_compatible`（复用守卫） |
| `pipeline_entry.py` | 新增 `build_run_manifest` / `latest_run_manifest` / `assert_reusable_artifacts` / `record_version_in_manifest`；`run_pipeline` 的 resume 分支接入身份核对、V0 与每轮接入每版记录；`save_final_prompt` 晋升前执行完整契约检查 |
| `test_manifest_reuse.py` | 新增：17 项（manifest 每版记录 / 复用守卫 / 复用守卫真实接线 / final 契约守卫），无 API |
| `test_pipeline_paths.py` | 补 mock `record_version_in_manifest`（保持原有缩进与断言） |
| `B 类迭代改造与重跑指导大纲.md` | 落盘备查（执行依据） |

### 统一 Q 协议阶段（2026-09-21，review 后，§12.7）

| 文件 | 性质 |
|---|---|
| `pipeline_entry.py` | 统一 Q 目标（`Q_SEVERE_WEIGHT=2.5`）、`decide_iteration` 重写、`resolve_decision`（补评/复核/上限强制升级）、`top_up_version`、`log_precision_event`、`run_validation_and_select`、`replay_protocol_state` / `load_protocol_state` / `save_protocol_state`、`final_artifact_pair` 改为同源 run1 组合、`version_mean_counts`（min 参照废弃）、`--reuse-existing-meta` |
| `run_manifest.py` | `POLICY_FIELDS` / `manifest_mismatches` / `apply_policy_migration`（策略字段受控迁移 + `policy_migrations` 记录） |
| `archive_run.py` | ROOT_PATTERNS 纳入 `b_protocol_state.json` / `b_validation_report.json` |
| `test_q_protocol.py` | 新增：统一 Q 协议 29 项无 API 测试 |
| `test_final_selection_rule.py` | 重写：排序键 / MAE 政策 / 固定两次评测 5 项 |
| `test_manifest_reuse.py` / `test_pipeline_paths.py` / `test_version_gate.py` | 更新：协议声明断言、策略字段受控迁移接线、新流程 mock 链、`q` 字段 |
| `discarded/test_superseded_20260921/` | 归档被取代的 3 个旧测试文件 |

### 临时件处置（验收后）

| 项 | 处置 |
|---|---|
| `tmp_resume_run.py`、`tmp_resume_smoke.py`、`tmp_run_log.txt` | **移出工作区**，归档至 `discarded/tmp_tools_20260921123129/`（A3 的唯一实现所在，按大纲 §9.1“不得删除无法恢复的历史证据”留档备查） |
| `tmp_ckpt_*.json`（8 个断点评分文件） | **直接删除**：内容可由对应的 `train_scoring_results*.json` / `test_scoring_results*.json` 完全复原，不构成不可恢复证据 |
| `.gitignore` 中的 `tmp_ckpt_*.json` / `tmp_run_log.txt` 条目 | 保留作为复发防护（已无对应文件） |

清理后确认：工作区无 `tmp_*` 残留；`Ran 174 tests — OK`、`project_checks` 5/5 均不受影响。

> **2026-09-21 补注（统一 Q 协议阶段）**：协议恢复/断点续跑用临时件
> （`tmp_extract_transcript.py`、`tmp_resume_run.py`、`tmp_resume_smoke.py`、`tmp_run_log.txt`）
> 已于冻结前移入 `discarded/tmp_tools_final_20260921/`；`tmp_ckpt_*.json`（可由评分产物复原）
> 与 `%TEMP%` 下的转录副本已删除。
| `B_REFACTOR_REPORT.md` | 填 §12.2 / §12.3，新增 §12.5 验收决议；§12.4 补风险 2、5、7、8、9、10 |
| `discarded/v2_superseded_*/` | 被取代的 V2 产物与说明（含降级分析的数据源） |
