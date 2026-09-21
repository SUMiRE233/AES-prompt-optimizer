# E 路线结题报告（v0.3，定稿）

- 日期：2026-09-21
- 状态：**定稿 —— 复核已通过（冻结意见见 §0）；中文翻译已执行；随本轮提交推送**
- 数据来源：仓库内归档文件（JSON 为准；本报告与任一 JSON 冲突时，以 JSON 为准）
- 执行主体：GitHub Copilot（按项目负责人在会话中逐条裁定执行；本报告为汇总，不含新事实）
- 修订记录：
  - v0.2 按 2026-09-21 首次评审修订——① 冻结名单运行时哈希校验；② 阶段复用身份 sidecar 校验；③ 表述收紧；④ 模型记录拆分；⑤ stop_status 更名；⑥ 产物策略
  - v0.3 按复核意见修订——⑦ `route_conclusion.noise_reference` 口径统一；⑧ `status` 增加 `manifest_integrity: valid|invalid` 输出（“每次入口”表述相应改为“依赖名单的执行入口”）；⑨ 新增推送前中文翻译清单
  - 定稿（推送版）——⑩ 说明性文本中文化执行完毕；文件更名为 `E_ROUTE_CLOSING_REPORT.md`

---

## 0. 一页摘要

**路线目标**：在 B 冻结版（V6, Q-mean-2.5-v1）基础上，为"结构分"维度寻找一条可注入 `**结构分特殊情形**` 的最小补全规则（E 类惰性补全），并经受冻结的多级门禁检验。

**结果**：在冻结协议与逐次预算裁定下，**未产出可通过门禁的候选规则**。三条候选处置完毕：

| 响应序 | 机制 | 结局 | 消耗（评分调用） |
|---|---|---|---|
| 0 | 回环框架（隐性首尾呼应识别） | 首评门拒绝（idx20 对照 structure Δ+2.0） | 17 |
| 1 | 断尾处理（总括末段后中断不等于结尾缺失） | 首评门拒绝（idx19 跨样本 expression Δ+2.0） | 17 |
| 2 | 错别字解耦（语言缺陷不压低结构判定） | Gate 0 拒绝（禁用词“一律”无语境命中；冻结机械规则下的有效拒绝，属规则的已知语义窄化） | 0 |

**最终消耗**：评分成功 **34/113**、分析成功 **1/2**、请求尝试 **49/220**；传输失败重试 14 次（全部自愈，无丢样本）。

**关键判据（经验观察，非裁定）**：基线自身 rerun 噪声上限 **+1.0**（17 样本×3 维）；两次首评命中均为 **+2.0**，超出该经验噪声参考，按冻结门被判为灾难性回归。单次基线重跑不能统计上排除评分随机性，故不作更强断言。

**红线**：`final_prompt.md` / `final_prompt_meta.md` 全程未被修改；无任何自动晋升；全部裁定（含操作性裁定）有记录可溯源。

**结论（评审建议措辞）**：在本次冻结样本、门禁与逐次批准预算下，三条模型生成候选均未进入完整 E 链路：两条因首评灾难门拒绝，一条因 Gate 0 关键词约束拒绝。因此本轮未获得可晋升的结构维 E 类补全规则；稳定版 V6 保持不变。

**终局**：项目负责人裁定“接受收束”（2026-09-21），已写入 `e_protocol_manifest.json → route_conclusion`（`final_route_status = E_NO_VALID_CANDIDATE`；`phase1_stop_status` 保留）。

**复核冻结意见（2026-09-21）**：完成 v0.3 两项修正后，可冻结为“E 类实验路线已收束，三条候选均未通过，无规则晋升，V6 保持不变”；无需继续产生 API 请求。

---

## 1. 范围与冻结基线

| 项目 | 值 |
|---|---|
| B 冻结版本 | V6；commit `2f06521`（CI 修复 `1457fc8`，均已推送） |
| 评分协议 | Q-mean-2.5-v1（Q = 2.5·Severe + Soft） |
| E 共识协议 | `E-consensus-robust-z-v1`（逐轮 median/MAD 稳健 z；两轮同向且 |Δ均值|≥1.0 才入选；|均值 z|>2.5 Severe、1.5<\|均值 z\|≤2.5 Soft） |
| B 共识协议 | `mean_and_rerun_conservative`（**操作性裁定**，非自然对称共识；Severe={46,47}、Soft={13,15,16,18,22,28}） |
| 基线锚点 | 15 项 SHA-256（见 `e_baseline_manifest.json`；执行前由 `verify_baseline_manifest` 校验，全程通过） |
| 冻结候选名单 | `e_candidate_manifest.json`；批准人"项目负责人"@2026-09-21T21:07:39；`frozen_sha256`=2c80f90b…（规范体哈希，`approval.frozen_sha256`） |

**冻结名单角色结构（17 样本）**

- 结构目标 `structure_e_target`：{13, 18, 22, 47}（Severe×2、Soft×2；方向：13/18/47 strict，22 lenient）
- 正常对照 `normal_control`：{2, 8, 17, 20, 26, 36}
- 跨维观察（内容）：{22, 37}；（表达）：{19, 25}
- B 共识：Soft {13,15,16,18,22,28}、Severe {46,47}（共 8 样本，含跨维重叠）

> 备注 1：`e_baseline_manifest.json` 自身 `status` 字段为 `draft_pending_human_approval`（Phase 1 创建时状态）；其锚点哈希在每次执行启动前均校验通过，未发生漂移。
>
> 备注 2（2026-09-21 评审后补强；复核后修订）：`require_frozen_manifest` 现于**依赖名单的执行入口**（`generate-rule` / `select-rule` / `execute`）重算规范体 SHA-256 并与 `approval.frozen_sha256` 比对，失配即阻断（含篡改测试）；`status` 为只读状态下**同时输出** `manifest_integrity: valid|invalid`（复核后新增），此前仅校验 `approval.status`。
>
> 备注 3（标签澄清）：`e_candidate_manifest.json` 为不改动的冻结对象，其 `file_status` 文本为冻结前所写（保留原样）；“score-free”的准确含义为“无原始作文与逐维原始分数”——选择理由文本中仍含派生 z/Δ 用于审计。候选提示词与 `e_candidate_rule.json` 原文已转入本地忽略。

---

## 2. 门禁与权重体系（全部冻结）

| 层级 | 规则摘要 |
|---|---|
| Gate 0（选取） | 锚点=`**结构分特殊情形**`、scope⊆structure、无硬阈值词、证据池完备（outlier⊆目标池、normal⊆对照池、evidence_count 一致）、非重复、契约/预算 dry-run 通过 |
| 首评门 | 单跑对单跑（候选 run1 vs V6 run1）；**任一（样本,维）Δ≥+2.0 即拒**（单次保守门） |
| micro | U = mean(U_target)+mean(U_other)+mean(U_B) > 0.01；≥1 个目标明确改善；四类零容忍（目标维/对照维/跨维/B）；U_target 权重 1.0、U_other 权重 1/3（样本级）、U_B 惩罚权重 Severe 2/3、Soft 1/3（惩罚只减不增） |
| regular | 默认行为：≥ceil(0.5×4)=2 个目标改善；明确恶化预算：目标 0 / 对照 ≤1 / 跨维 ≤1；B Δ≥1.0 一票拒 |
| 全量（36 篇×2） | 目标结构 Severe 下降（或持平且 Soft 下降）；B mean Severe ≤3.0 且 Q ≤14.0；非目标维无新增共识成员、无 Soft→Severe 升级；跨维明确恶化 =0；MAE 仅记录 |
| 验证（12 篇×2，双评均值） | B mean Severe ≤1.5、Q ≤4.75；**全维**明确恶化 =0 |
| 终局 | 全部通过 → `E_CANDIDATE_PASSED_AWAITING_HUMAN`；**永不自动晋升** |

Δe 定义：`Δe = |AI_候选 − 师评| − |AI_基线 − 师评|`；明确改善 ≤−1.0；明确恶化 ≥+1.0。

---

## 3. 时间线

| 时间 | 事件 | 结果/记录 |
|---|---|---|
| 21:07:39 | 候选名单冻结（批准） | `e_candidate_manifest.json`；draft 删除 |
| ~21:12 | `generate-rule`（1 次分析调用） | 模型返回 **3 条**规则；按当时"恰好 1 条"冻结门禁 → `E_NO_VALID_CANDIDATE`（**此接口问题经裁定修复，见 §6.3**） |
| ~21:45 | 裁定：惰性串行选取（按序取第一条，硬拒后顺延，禁预筛选） | 写入协议清单 `rule_selection_policy` |
| 21:47:20 | `select-rule` → 响应序 0 入选 | Gate 0 八项全过 |
| ~21:47 | 裁定：硬审查通过即准入执行（无人工通过点；人工否决权至执行前） | 状态改为 `approved_pending_execution` |
| 21:47:32 → 21:55:22 | `execute` 候选 0 | 首评门拒绝 → `E_EVALUATED_REJECTED` |
| ~22:00 | 裁定：预算修正①（评分上限 96→113、总 98→115、attempts 110→220） | 写入 `budget_amendments` |
| 22:05:43 | `select-rule --start-index 1` → 响应序 1 入选 | 记录候选 0 结局入 `history` |
| 22:05:55 → 22:14:43 | `execute` 候选 1 | 首评门拒绝 → `E_EVALUATED_REJECTED` |
| ~22:20 | 裁定：授权候选 2（响应序 2）仅首评快筛 | 新增停机状态 `E_FIRST_PASS_PASSED_PENDING_BUDGET`；写入 `process_amendments` |
| ~22:25 | `select-rule --start-index 2` → 响应序 2 **Gate 0 拒绝** | 三条规则全部处置完毕 → `E_NO_VALID_CANDIDATE` |
| ~22:35 | 终局裁定：**接受收束** | 写入 `route_conclusion` |
| 评审修订 | 项目负责人 + 审阅模型评审；两项机制修复与记录统一 | v0.2；250 tests OK |
| 复核修订 | `noise_reference` 口径统一；`status` 输出 `manifest_integrity` | v0.3；251 tests OK |
| 终局校验 | 251 tests OK；`project_checks` 5/5 PASS | 见 §7 |

---

## 4. 候选详录

### 4.1 候选 0（响应序 0，"回环框架"）

- **规则摘要**：记叙文以现实场景起笔、中段入回忆、结尾回到该场景/重现开头意象 → 视为首尾呼应，结构分宜中等偏上；时间切换缺过渡词、段落不均属轻微瑕疵（完整文本见 `e_candidate_rule.json`）
- **证据**：outlier {13,47}；normal {17,26,20}；含 3 条支持 span 与 2 条反例 span（反例恰含 idx20）
- **Gate 0**：8/8 通过（注入 +261 字符，2956 总量，契约校验通过）
- **首评违规**：idx20（对照）structure Δ+2.0（师评 5，基线 5，候选 **7**）
- **结构分复盘（17 样本）**：13 −1.0、18 −1.0、22 −1.0、37 −1.0（改善）；20 **+2.0（拒）**、36 +1.0、15 +1.0、46 +1.0（恶化）；其余 0
- **结论**：目标侧有效（3/4 目标改善、目标池零恶化），但对照被抬高 2 分——首评门一票否决

### 4.2 候选 1（响应序 1，"断尾处理"）

- **规则摘要**：主体分层清楚且已有总括末段，但末句仓促中断 → 按"首尾齐全"评价，断尾视为轻微瑕疵；不得套用"完全无分段/首尾缺失"极端条款压分
- **证据**：outlier {18,47}；normal {2,8,36}
- **Gate 0**：通过（注入 +265 字符）
- **首评违规**：idx19（跨维观察，**表达维**）Δ+2.0（师评 7，基线 6，候选 **4**）
- **结构分复盘（17 样本）**：13 −1.0、18 **−2.0**、22 −1.0、28 −1.0、37 −1.0（改善）；**47（目标）+1.0**、20 +1.0、15 +1.0、25 +1.0、46 +1.0（恶化）；其余 0
- **结论**：即便越过首评门，micro 门（目标池零恶化）也会因 47 号被拒；且表达维溢出表明规则影响面超出结构维

### 4.3 候选 2（响应序 2，"错别字解耦"）

- **规则摘要**：错别字/语病密集但分段清晰、首尾可辨 → 结构判定先与语言缺陷脱钩
- **证据**：outlier {18,13}；normal {20,36}
- **Gate 0 拒绝项**：`G0-hard-threshold` —— `forbidden_generalization` 含禁用词 **“一律”**（出现于否定式护栏“不得推广为语言差也**一律**给高结构分”）。冻结检查器为无语境子串扫描，生成提示亦明确禁止所有字段出现该词——**这是冻结机械规则下的有效拒绝**，属该规则的已知语义窄化，而非执行错误
- **消耗**：0（选择阶段纯离线）
- **处置**：按惰性串行规则，扫描已至末条 → `E_NO_VALID_CANDIDATE`

---

## 5. 判别力分析（首评门）

1. **噪声标定（经验观察，非裁定）**：基线 run2 vs run1 在全部 17×3 上最大正向 Δ=**+1.0**；两次命中为 **+2.0**，超出该经验参考——据此按冻结门判为灾难性回归。该观察不能统计上排除单次跑随机性，故不作“真实溢出”的更强断言。
2. **溢出模式**：两条被评估候选的致命命中均不在"自证有利"的样本上（一处对照、一处跨维），且伴随 ≥+1.0 级的次级恶化 4–5 处——单条软规则在 17 样本池上的外溢是本次的主要拒绝原因。
3. **单跑门说明**：首评门为**单次运行保守门**（冻结设计）。评审建议的更经济改进：首跑命中 +2.0 后追加一次“确认跑”再决定是否硬拒（而非全候选双评）；列为未来重开时的候选改进（需新裁定）。
4. **Gate 0 无语境命中（已知语义窄化）**：`一律/固定` 等词即使出现在否定式护栏中也会命中。本轮维持原判、不追溯放行；未来若重开可考虑“仅扫描实际注入字段”或增加否定语境识别，但必须作为新协议版本（需新裁定）。

---

## 6. 协议观察（供审阅模型逐条核对）

### 6.1 操作裁定 vs 自然统计（表述纪律）

- `mean_and_rerun_conservative`（B 共识）、惰性串行选取、自动准入、预算修正、收束 —— 均为**操作性裁定**，报告与清单中不得包装为自然统计结论。请审阅模型逐条核对本报告表述。

### 6.2 预算与复用（评审后修订）

- 预算按“一条候选=完整链路 96 次评分”尺寸设计；多候选场景需逐次裁定（本次实际执行：修正①为候选 2 开额度；候选 3 仅授权 17 次快筛）。
- 快筛：`--first-pass-only` 模式已实现并测试，本次未实际消耗（候选 2 未过 Gate 0）。
- 复用（2026-09-21 评审后补强）：阶段结果复用现要求身份 sidecar 匹配——`prompt_sha256 / model / global_indices / repeat_id / scoring_protocol`（`etype_analysis/*.meta.json`）；sidecar 缺失或任一字段失配即阻断，不再静默复用（含四类测试）。此前版本仅按文件存在性复用，声明中的 reuse-key 校验不完整。

### 6.3 生成端/选取端接口问题（已在过程中裁定修复）

- 原始冻结门禁要求"模型恰好返回 1 条规则"，而生成端提示词本身按**列表**设计（无数量约束）——首轮因此结构性停于 `E_NO_VALID_CANDIDATE`。
- 修复（经项目负责人裁定）：改"入选恰好 1 条"（惰性串行、首过即止、硬拒顺延、全量留痕），生成端未动。

### 6.4 模型记录（评审后已修订）

- 实际执行：分析回合模型为 **claude-opus-5**（`etype_analysis/structure_contrastive_request.json → model` 实证）；评分回合为 **claude-sonnet-5**（终端回显）。
- `e_protocol_manifest.json → budgets` 现拆分保留双记录：`configured_models_at_protocol_freeze`（原配置，不改历史）与 `observed_execution_models`（实测 + 来源注记）。

---

## 7. 预算与可靠性

| 项 | 值 |
|---|---|
| 评分成功 | 34（上限 113；候选 0/1 各 17） |
| 分析成功 | 1（上限 2） |
| 请求尝试 | 49（上限 220） |
| 未成功尝试 | 14 次（49 − 34 − 1 可复算；候选 0：6；候选 1：8），全部经重试自愈，无丢样本 |
| 失败模式 | 终端观测为 `SSLEOFError(UNEXPECTED_EOF)`（api.pateway.ai）；计数可复算，错误类型未独立留档 |
| 终局校验 | `unittest discover` 251 项 OK（评审+复核修复后）；`project_checks.py` 5/5 PASS |

---

## 8. 产物与入库策略（评审后定案 @2026-09-21）

**工作区状态总览**：本路线（含前序 E 阶段）全部变更**均未提交**；`git status` 实测见下。

**计划提交（评审口径：代码/测试/阈值/协议清单/结题报告/无私人文本的聚合证据）**：

- 代码与测试：`e_gate_engine.py`、`e_phase2_run.py`、`e_consensus_miner.py`、`e_phase1_prepare.py`、`decision_thresholds.py`、`test_e_phase1.py`、`test_e_phase2.py`
- 协议与清单：`e_baseline_manifest.json`、`e_protocol_manifest.json`、`e_candidate_manifest.json`（冻结对象，不得改动；标签澄清见 §1 备注 3）
- 脱敏证据：`e_candidate_rule.public.json`（已生成；引文 span 已删除，`span_leaks=0`，6 处 `removed_for_privacy` 标记）；`e_evaluation_report.json`（仅候选序号/门禁/维度/Δ，无作文文本）
- 报告：本文件；`E_PREEXECUTION_REVIEW.md`（已加“历史快照（已失效）”横幅，保留作阶段留档）

**本地专有（已 gitignore，不进仓库）**：

- `e_candidate_rule.json`（原文，含作文引文 span）、`e_candidate_prompt.md`、`e_candidate_prompt_meta.md`
- `e_candidate_manifest.draft.json`、`e_candidate_evidence.local.json`
- `etype_analysis/` 整目录（现为目录级忽略；含归档报告、评分档、请求/响应、预算状态、sidecar、cand 副本）

**推送前清单（已执行 @2026-09-21）**：说明性文本已完成中文化——`e_protocol_manifest.json` 的 `route_conclusion.noise_reference`、`candidate_outcomes`（响应序 2 的结局短语）、`budgets.derivation`、`observed_execution_models.sources`、`budget_amendments.derivation`，以及 `e_candidate_rule.public.json` 的 `note`。README 为既有英文文档，本轮仅更新数值（251），无新增说明句，未作翻译。

**已清理**：`etype_analysis/_*.py` 临时调查脚本已删除（脱敏生成器 `export-public-rule` 已成为 `e_phase2_run.py` 的正式子命令）。

---

## 9. 评审意见落实对照（v0.2）

| 评审意见 | 落实 |
|---|---|
| 机制①：冻结名单只有哈希、执行未验证 | 已修复：`require_frozen_manifest` 运行时重算规范体 SHA-256 并比对 `approval.frozen_sha256`，失配即阻断；新增篡改测试 |
| 机制②：复用未做 reuse-key 身份校验 | 已修复：身份 sidecar（prompt_sha256/model/global_indices/repeat_id/scoring_protocol）；sidecar 缺失或失配即阻断；四类测试 |
| 措辞①：+2.0“真实溢出”表述过强 | 已收紧为“超出本次经验噪声参考、按冻结门判为灾难性回归”（§0/§5.1） |
| 措辞②：“误报”定性 | 已改为“冻结机械规则下的有效拒绝、规则的已知语义窄化”（§0/§4.3/§5.4） |
| 措辞③：SSL 类型未独立留档 | 已注明“终端观测、计数可复算（49−34−1=14）”（§7） |
| 措辞④：stop_status 语义冲突 | 已更名 `phase1_stop_status`，新增 `final_route_status`；协议清单已更新 |
| 措辞⑤：README 计数 | 已更新为 250 |
| 审批①：模型记录拆分 | 已实施（配置/实测两栏 + 来源注记），见 §6.4 |
| 审批②：“一律”本轮维持、未来可议新版本 | 已按此记录（§5.4） |
| 审批③：双跑首评→“确认跑”方案 | 已列为未来候选改进（§5.3），不改本轮结论 |
| 审批④：入库范围 | 已按建议执行（§8），含 `e_candidate_rule.public.json` 与 `etype_analysis/` 目录级忽略 |
| 审批⑤：操作裁定标注 | §6.1 已列全（B 共识/惰性顺延/自动准入/预算修正/收束）；噪声标定已降格为经验观察 |
| 审批⑥：结论措辞 | 已采纳评审建议版本（§0） |
| 复核①：`noise_reference` 与 v0.2 口径矛盾 | 已修订口径，并于推送前完成中文化（见 §8） |
| 复核②：“每次入口”表述过宽 | `status` 现输出 `manifest_integrity: valid|invalid`；报告表述改为“依赖名单的执行入口”（§1 备注 2、§10.2） |

---

## 10. 附录

### 10.1 关键哈希（完整值见 `e_baseline_manifest.json` / `e_candidate_manifest.json`）

| 对象 | SHA-256 |
|---|---|
| `final_prompt_meta.md` / `final_prompt.md` | 72ca44765e86c075cf06865c7c7fcf1d6a6d74339c5131a10add6ee809830a49 |
| `train_scoring_results6.json` (V6 run1) | 2d2c1af7a4176d9ee88f6e49a15072fece013a0d714886e8b468e1506bcec629 |
| `train_scoring_results6_rerun.json` (V6 run2) | d43c5496e5c8bf7c0906e581a9745de114d9dbdd69a6f7113159ec254bd8e0f4 |
| `test_scoring_results6.json` (验证 run1) | 4cd9278ebd4a7c26fb5e24848c0e15b70d1680b940dce8af0cd609783694783b |
| 冻结名单（规范体） | 2c80f90b426fd38db94ed7889acd8d9db098be6c1cba804000e199e87fbd0817 |

### 10.2 复核方式（只读）

```
python -m unittest discover -s . -p "test_*.py"      # 251 项
python project_checks.py                              # 5/5 PASS
python e_phase2_run.py status                         # 终态三件套（含 manifest_integrity: valid|invalid）
```

逐条事实核对：`e_protocol_manifest.json`（终局/裁定/评审修订）、`e_candidate_rule.public.json`（脱敏规则与 Gate 0）、`etype_analysis/e_evaluation_report_candidate1.json` 与 `e_evaluation_report.json`（两次首评）、`etype_analysis/e_budget_state.json`（预算）。

---

（报告完。）
