# AES Prompt Optimization / 教师反馈驱动的作文评分 Prompt 迭代原型

> 审计日期：2026-09-14  
> 事实优先级：当前代码与可复算产物 > 当前测试结果 > 本地 Codex 开发会话 > 文件时间戳与旧总结。  
> 归因边界：当前目录不是 Git repository，没有 `.git`、README、依赖清单、issue/PR 元数据。因此无法用 commit author 或 diff 证明代码作者；本文只把历史对话中由用户明确提出、质疑或决定的内容归为“我的贡献”，其余标记为 Codex 辅助或无法确认。

> 2026-09-19 范围决定：`technique` 与 `length` 从立项时即由上游确定性脚本评测，与评分 prompt 无关。本仓库只主张和优化 `content` / `expression` / `structure` 三个主观维度；本文中 C/D 相关内容仅作历史实现审计，不再构成当前项目主张、完成缺口或简历依据。

## 1. Project Summary

这是一个面向中学作文自动评分（AES）的实验型 Prompt Optimization 项目。它不是 OCR、作文录入平台或完整评分 SaaS，而是一个在已有作文文本和教师评分基础上，反复调用远程 LLM 评分、挖掘评分偏差、修改评分 prompt、再用回放结果决定保留或拒绝候选 prompt 的本地 Python 原型。

### 输入

- `origin_scoring_results.json`：48 篇已抽取的作文，包含全局 `index`、学生/页码、作文正文、教师评分和一份历史 AI 评分。
- `*_prompt_meta.md`：可被 optimizer 或 rule integrator 修改的评分 prompt 模板。
- `train_essays.json` / `test_essays.json`：由固定随机种子 42 划分出的 36/12 作文子集。
- 后期 E 路线还读取 `aes_badcases*.json` 和 `etype_analysis/contrastive_consolidated_*.json`。

### 输出

- 每轮 LLM 评分结果：`train_scoring_results*.json`、`test_scoring_results*.json`。
- B/C/D/E badcase 与统计：`aes_badcases*.json`。
- 候选/稳定 prompt：`optimized_prompt*_meta.md`、`final_prompt_meta.md` 及预处理后的 `.md`。
- E 类对比分析、规则池和注入日志：`etype_analysis/*.json`。
- 代码层支持的 micro gate、regular gate 和 run report；当前目录没有真实 gate eval/run report 产物。

### 用户、问题与核心流程

直接用户是需要让 LLM 作文评分更接近教师尺度的研发/实验人员。系统处理两类问题：模型在内容、表达、结构三维上整体偏严/偏宽；少数作文相对总体趋势仍有局部异常。直接反复手改 prompt 容易发生回归且难追踪，因此流程是：

```text
教师标注作文
  -> 固定 train/holdout 划分
  -> LLM 批量评分
  -> AI - teacher 误差分析
  -> B 路线：聚合跨维同向偏差并重写评分锚点
  -> E 路线：outlier + normal 对比生成局部规则
  -> 候选 prompt 回放与门控
  -> 拒绝、待人工审核或显式晋升 final
```

### 完成度

- B 路线有四轮可复算实验和一次明确回退，属于“实际跑过的研究原型”。
- E analyzer 与规则注入有真实产物；micro/regular gate、事务 commit/rollback 和人工 promote 有代码与 9 个通过的单元测试，但没有真实 API gate 报告，不能称完整闭环已验证。
- 没有后端服务、前端、数据库、部署、CI 或生产监控。

## 2. Problem & Motivation

原始评分 prompt 在 36 篇训练样本上表现为明显偏严：三维 signed mean diff 为 -2.199，MAE 为 2.199。项目不把所有错误交给一次自由重写，而是尝试区分：

- 跨维度同向偏差：一篇作文的内容、表达、结构同时偏高或偏低，后续汇总为评分尺度问题。
- 去除各维总体中位偏移后仍异常的 residual：更可能对应局部规则缺失或模型对某类文本模式的错误理解。
- prompt 修改副作用：修复目标样本可能伤害正常样本、其他维度或整体 bias，因此候选修改不应自动成为稳定版本。

需要主动说明的边界：

- 当前 B detector 检测的是“单样本三维同向”，并非严格的数据集级 systematic bias 统计检验；“全局”来自对多个 B badcase 的聚合。
- 教师 content/expression/structure 含 49 个半分值，而模型协议只允许 0-9 整数，因此部分 0.5 误差是量化下限。
- 所谓 `test` 集参与版本选择，应称 holdout/validation，不是最终独立 test。

## 3. Architecture

项目规模是平铺式 Python 脚本，不应包装成复杂分布式架构。

```text
CLI scripts
  -> JSON/Markdown file pipeline
  -> remote LLM API
  -> deterministic parsing/statistics/gates
  -> versioned local artifacts
```

| 层 | 作用 | 关键文件与函数 | 上游 -> 下游 | 核心逻辑 |
|---|---|---|---|---|
| 入口 | 串联 B 或 E 迭代 | `pipeline_entry.py::run_pipeline`; `etype_iteration_runner.py::run` | 文件配置 -> 各阶段脚本 | 是，编排层 |
| 数据处理 | 提取作文并固定 36/12 划分 | `sample_extractor.py::EssayExtractor.run` | 原始 48 条 -> train/holdout | 辅助但关键 |
| 评分执行 | 构造评分请求、解析 delimiter、按 `index` 补齐 teacher/meta | `batch_scoring.py::BatchEssayScorer` | prompt + essays -> scoring JSON | 核心执行 |
| Badcase | 计算 diff、median/MAD、B/C/D/E 分类 | `aes_badcase_miner.py::AESBadcaseMiner` | scoring -> badcases | 核心算法 |
| B 优化 | 聚合 B 分布、选 3 个极端样本、让 LLM 重写 meta prompt | `prompt_optimizer.py::PromptOptimizer` | B badcases + prompt -> 下一版 prompt | 核心但较粗糙 |
| E 分析 | 选择 outlier/matched normal、让 LLM 抽取局部规则并过滤 | `etype_preference_analyzer.py::ContrastiveETypeAnalyzer` | E badcases + baseline -> feasible rules | 核心实验逻辑 |
| 规则整合 | 选择单维 1-2 条规则、注入目标小节、事务提交或回滚 | `rule_integration_engine.py::RuleIntegrationEngine` | feasible rules + final meta -> candidate | 核心控制逻辑 |
| Micro gate | 回放 rule evidence 和 B badcase，计算加权收益和硬回归 | `micro_scoring_gate.py::MicroScoringGate` | baseline/candidate -> pass/reject | 核心控制逻辑 |
| Regular gate | 0-6 badcase + 6 normal 抽样与阈值判断 | `gate_test_sampler.py`; `etype_iteration_runner.py::evaluate_gate` | badcases + train -> gate decision | 核心控制逻辑 |
| Prompt 预处理 | 从 meta prompt 删除模板区块，生成 runtime prompt | `preprocess_prompt.py::PromptPreprocessor` | `_meta.md` -> `.md` | 辅助 |
| 存储 | JSON/Markdown 文件；无数据库和 registry | 根目录与 `etype_analysis/` | 所有阶段共享 | 实验存档 |
| 后端/前端 | 不存在 Web/API 服务和 UI | 无 | 无 | 否 |

`final_prompt_meta.md` 与 `optimized_prompt3_meta.md` SHA-256 完全一致；两个 runtime `.md` 仅有空行差异，语义上也是第 3 轮版本。

## 4. Core Workflow

### 4.1 已跑通的 B 路线

1. `sample_extractor.py` 用 seed=42 将 48 篇随机划分为 train=36、holdout=12；当前索引无重叠、无重复，并集等于全量 48。
2. `batch_scoring.py` 逐篇调用 `claude-sonnet-4-6`，要求 content/expression/structure 为 0-9 整数。
3. `AESBadcaseMiner` 计算 `AI - teacher`，生成 B/E 等 badcase。
4. `PromptOptimizer` 汇总 B badcase 的严/宽方向、各维平均差，选绝对 B 最大的 3 条，只提供分数而不提供正文，让 LLM 重写完整 meta prompt。
5. `pipeline_entry.py` 当 train B severe 上升或 B 总数不再下降时触发 holdout gate；只比较候选与前一版的 holdout B-severe 数。
6. 第 4 轮发生回退，最终保留第 3 轮。

### 4.2 后期 E 路线

1. 每维先估计 median diff 与 MAD，再用 residual robust z-score 选择 E outlier。
2. 从同一训练集挑选 raw diff/z 较小，且题型、教师分、长度更接近的 normal。
3. LLM 对比 outlier 与 normal，输出 trigger、adjustment、evidence、counter evidence、作用维度和 anti-overfit boundary。
4. 编译器只保留 safe、confidence >= 0.6、evidence_count >= 2、有 normal、注入位置合法且不含硬阈值的规则。
5. Rule integrator 每个子迭代选择问题最严重的一个维度，默认注入 1 条规则；候选先处于 pending，不消费 rule pool。
6. Micro gate 回放规则 evidence 与既有 B badcase；regular gate 再检查目标 badcase、normal 和跨维回归。
7. 所有门控通过后才 commit rule pool；是否覆盖 `final_prompt*` 还需要显式 `--promote-final`。

## 5. Key Technical Decisions

| 决策 | 当前实现 | 主要来源/归因 | 审计评价 |
|---|---|---|---|
| B/E 分治 | B 看 raw diff 的三维同向；E 看去 median 后的单维 residual | 初始作者无法确认；用户后续明确要求 B 稳定后只开发 E | 有辨识度，但 B 名称需谨慎 |
| median/MAD robust z | `residual / (1.4826*MAD+1e-6)`，E 阈值 1.5/2.5 | 代码存在于早期版本；无法确认由谁提出 | 合理启发式，未做 mean/std 消融 |
| E 层信任 analyzer | integrator 默认以 `compiled_rules.feasible_rules` 为合同，不重复做 strict-normal gate | 用户明确要求层间解耦、嵌入层信任 analyzer | 清晰模块边界，但上游错误会直达下游 |
| 暂不重构 origin prompt | 沿用现有 prompt 分区，从 consolidated 向后延伸 | 用户明确否决当时的 Prompt AST/origin 重构 | 控制范围、减少不必要改造 |
| 单维、小增量注入 | 一个子迭代选最严重单维的 1-2 条；runner 默认 1 条 | 用户明确提出大/子迭代和 1-2 条；Codex实现 | 便于归因和回滚 |
| Gate 采样 | 0-6 个目标维 badcase + 6 normal | 用户明确提出样本数量、顺序和置顶配置 | 可控但尚未校准；当前仍 soft 优先 |
| 全局 `index` 主键 | full origin 固定为权威源；sample、gate、scoring 以 index 匹配并校验正文 | 用户发现位置错配、否决 `source_data_index`/长文本主匹配后明确要求 | 本项目最明确的用户驱动修复 |
| Micro gate | outlier/normal 的目标维、其他维与 B badcase分别加权，并有硬回归保护 | 用户提出加权组成与配置化；Codex选择具体权重/阈值并实现 | 共同探索；权重无实验校准 |
| 事务 commit/rollback | 候选通过 micro 和 regular gate 后才消费 feasible rule | Codex review 发现提前消费等问题，用户要求修复并给测试 | 可靠性明显提升，代码由 Codex 主写 |
| 人工 promote | gate 通过仍是 `needs_human_review`，显式开关才覆盖 final | 用户明确要求人工决定覆盖 | 合理的人机边界 |

明确被否决或放弃的方案：

- 先重构 Prompt AST/重新分区 origin prompt：用户要求当前阶段不做。
- integrator 再做一层 strict normal-contrast 诊断：用户要求层级解耦，默认信任 analyzer 的 feasible rules。
- 按 `output.json` 列表位置补 teacher/meta：被用户发现会与随机划分错配。
- 临时引入 `source_data_index` 作为通用主键：用户追问其来源和语义，最终改为全局 `index`。
- gate sampler 生成重复的 `gate_test_set.json`：用户指出全量 origin 才应是权威源，要求移除。
- 以长作文全文作为主要 join key：实际发生 read timeout 后，用户为全链路追加 `index`。
- E 规则一生成就消费 rule pool：后续 review 发现 gate 失败无法恢复，改为 deferred commit。
- 继续叠加污染后的 E6：历史讨论建议回退稳定基线做单规则消融；当前仓库没有 E6 结果，不能声称已执行。

## 6. Iteration History

历史上在发现索引错配前产生的 B4/B5/E5 比较可能被错误 teacher 配对污染，不能作为最终量化结果。本文将当前可复算产物与旧讨论分开。

### V0 -> V3：B 类全局锚点迭代

```text
origin
  -> V1 增加“避免过严”和默认中位锚定
  -> V2 强化反过严校准和各维评分区间
  -> V3 加入 75% 起评下限、80%-90% 默认锚点和自检
```

修正后的 36 篇产物上，MAE 从 2.199 -> 1.477 -> 0.875 -> 0.755，B badcase 从 34 -> 23 -> 8 -> 5。

### V4：过度修正与回退

V4 同时加入反过宽约束、降低默认区间并重新拉开分差。结果 train MAE 回升至 1.116，holdout MAE 从 V3 的 0.556 升至 1.139，B severe 从 0 增至 3。最终 meta prompt 保留 V3。

### E0：转向局部 residual rule

用户明确把稳定 prompt 视为 B 基线，要求停止 origin 大改，只做 E 类开发；同时要求规则嵌入层沿 `contrastive_consolidated.feasible_rules` 向后延伸。此阶段形成 outlier + normal 对比和局部规则 schema。

### E1：小增量 rule integration 与 gate sampler

用户要求大迭代下拆子迭代，每次只选最严重单维的 1-2 条可行规则，并把已注入规则移出 pool、记录 injected log；随后又指定 0-6 badcase + 6 normal 的门控集和置顶配置区。Codex完成主要实现。

### Debug 1：随机划分后的 label/meta 错配

用户主动发现 `batch_scoring.save_results()` 可能按全量数组位置补齐随机 train/test 的 teacher。复核证实当时 train 36/36、holdout 12/12 的位置都不对应，历史评分结果可能被污染。

```text
position join（错误）
  -> essay exact match（正确但慢）
  -> source_data_index（语义依赖当前文件，仍不稳）
  -> 删除重复 gate_test_set，固定 full origin
  -> 全链路显式 global index + essay integrity check（当前实现）
```

这是最清楚的一次“用户发现 -> 质疑 Codex 修复 -> 否决中间方案 -> 统一数据合同”的迭代。

### E2：runner、micro gate 与事务语义

- 2026-06-03：用户确定 final prompt 为基底、每轮一条最小注入、自动 preprocess、gate sampler 门控、失败丢弃候选、人工决定是否覆盖 final；Codex实现 runner 与阈值方案。
- 2026-06-14：用户提出新增 micro-scoring 层，明确 outlier/normal 目标维、其他维和 B badcase 需要分别加权且权重集中配置；Codex选择具体 3/1/2/1 等权重、硬保护并编写测试。
- 同次 review 中，Codex发现提前消费 rule、gate 返回值、指定维度不一致、多 rule evidence 丢失和旧结果复用风险；用户要求修复并提供关键测试。最终形成 deferred commit/rollback 和 9 个单元测试。

## 7. My Contribution

“我的”指对话中的用户，不根据文件存在倒推作者。

### A. Problem Framing

- 把当前阶段限定为“B 类稳定后，只做 E 类 residual 规则开发与测试”。
- 要求一切以后提出的实际要求为准，交接上下文只作背景；发现冲突要提醒。
- 明确 stable/final prompt 不应被候选自动覆盖，最终由人工审核。

项目最初的 AES 目标与 B/C/D/E taxonomy 是否由用户独立原创：**无法确认，需要本人补充**。它们在用户提供的交接上下文和早期代码中已存在，但没有 Git 或更早原始会话证明来源。

### B. Architecture / Design

- 决定暂不重构 origin prompt/Prompt AST，保持既有分区。
- 决定 analyzer 与 integrator 严格解耦，由下游信任 feasible rule 合同。
- 定义 major/sub-iteration、小增量单维注入、已注入规则池与日志。
- 定义全量 origin 为唯一元数据源，train/test/gate 只传全局 index 和 essay。
- 定义 final 基线、自动 preprocess、候选隔离、gate 失败丢弃、人工 promote。

### C. Algorithmic Decisions

有明确用户输入：每个子迭代选择“问题最严重单维度”的 1-2 条最可行 rule；Gate 集由 0-6 个目标 badcase 与 6 个 normal 组成；Micro scoring 分开衡量 outlier/normal 的目标维、其他维以及 B badcase，并使用可配置权重。

不能直接归给用户：median/MAD 公式、1.4826、E 的 1.5/2.5 阈值、B 的 1.0/1.5 阈值、具体 micro 权重 3/1/2/1 和硬回归阈值。现有证据只表明这些在代码中或由 Codex实现，**是否由用户更早提出无法确认**。

### D. Debugging / Diagnosis

- 主动发现随机 train/test 与全量 `output.json` 按位置拼接的严重错配。
- 连续追问 `source_data_index` 从何而来、是否真的保证 gate 配对，要求严格复审。
- 识别 `gate_test_set` 是重复且容易误用的数据副本，要求删除并固定 origin 数据源。
- 在 essay 全文匹配 read timeout 后，为 full/train/test 追加稳定全局 `index`，并要求分析数据合同变化的连锁冲突。
- 主动要求比较多轮 train/holdout、检查 extreme raw diff、过拟合和过冲；但索引修复前的旧比较不能继续作为可靠结果。
- 指出 gate badcase 的严重度排序表述与实现可能相反；当前代码仍为 soft -> severe，说明问题被识别但未最终修正。

### E. Iteration / Trade-off

- 选择沿稳定基线局部延伸，而不是大规模 Prompt AST 重构。
- 选择一条/单维小步注入，而不是一次堆叠全部规则。
- 选择候选失败后丢弃、成功仍人工 review，而不是全自动自修改。
- 选择全局 index 作为数据身份，牺牲一次数据格式迁移，换取 join 稳定性和可审计性。
- 在 E 过冲讨论中考虑回退稳定 prompt，而不是继续用新 rule 修旧 rule；是否最终由用户决定并执行回退，当前证据不足。

### F. Implementation

历史会话明确显示，大部分新增/修改 Python 代码由 Codex直接编辑，包括 rule integrator、gate sampler、index join、E runner、micro gate、事务修复和测试。合理表述是：用户完成范围约束、接口合同、关键算法组成、风险审查和迭代验收；Codex承担大量具体编码、局部算法补全和测试实现。不要把这些模块的全部编码细节宣称为用户独立手写。

### AI-assisted engineering evidence

- 任务拆解：把 E 链路拆为 analyzer -> rule integration -> preprocess -> micro gate -> regular gate -> human promote。
- Context 与约束：反复指定 final 基线、信任边界、每轮规则数、数据权威源、候选生命周期。
- Review 与错误识别：发现位置 join、追问 Codex新增的 `source_data_index`、否决重复 gate 文件。
- Regression 意识：主动要求 train/holdout 对比、过冲检查、回退基线和最小注入。
- 多轮 refinement：同一数据配对问题经历至少四次方案收敛，最终形成 index + text integrity check。
- 架构一致性：要求 integrator 不越权重复 analyzer 判断，要求 gate 不污染 final。

不足：没有 Git diff 把每轮 decision、修改与测试绑定。AI 协作能力可以写，但应以“约束、review、验证和纠错”为证据，而不是代码量。

## 8. Codex Contribution

| 模块 | Codex Level | 判断依据 |
|---|---:|---|
| `sample_extractor.py` 与早期 scoring | Level 3 或无法确认 | 当前会话前已存在；缺最初创建记录 |
| `AESBadcaseMiner` 的 B/C/D/E taxonomy | Level 3 或无法确认 | 早期代码已存在；原始设计来源缺失 |
| `PromptOptimizer` / B pipeline | Level 3 | 用户推动实验和过拟合判断，Codex很可能完成主要实现；无原始创建会话 |
| `ContrastiveETypeAnalyzer` | Level 3 或无法确认 | 交接时已存在；用户后续决定它是上游合同 |
| Rule Integration Engine | Level 2-3 | 用户明确单维、1-2 条、子迭代、规则池/日志；Codex设计细节并写主要代码 |
| Gate Test Sampler | Level 2 | 用户给出样本构成、顺序和配置要求；Codex实现 |
| 数据 index 修复 | Level 2 | 用户发现根因、否决中间方案并定义最终合同；Codex编码与验证 |
| E Iteration Runner | Level 2-3 | 用户确认关键流程/人工边界；Codex补阈值、错误处理与编排 |
| Micro Scoring Gate | Level 3 | 用户提出加权结构；Codex选择具体权重、阈值、guards 并实现 |
| 事务 commit/rollback 与测试 | Level 2-3 | Codex主动 review 找问题，用户要求修复和测试；实现主要由 Codex完成 |
| Prompt 版本中的长规则文案 | Level 4 | `PromptOptimizer` 调 LLM 生成完整 modified prompt，用户主要验收 |

没有任何核心模块能从现有证据可靠归为 Level 0。局部业务判断、数据修复方向和架构约束体现用户能力，但代码作者身份不能由文件本身证明。

## 9. Difficult Problems & Debugging

### 9.1 数据身份错配

最严重 bug 不是语法错误，而是随机 split 后仍按列表位置 join。它会把 A 作文的 AI 分数与 B 作文的 teacher/meta 组合，进而污染 diff、badcase、规则抽取和所有优化结论。用户发现并推动最终 global index 修复；当前 `BatchEssayScorer._build_origin_index_lookup/_match_origin_item` 还会检查 index 唯一性、存在性和 essay 一致性。

### 9.2 双协议与硬编码冲突

历史会话中用户指出 meta/runtime prompt 会经 `preprocess_prompt` 去掉输出协议，而 `batch_scoring` 又拼接自己的 delimiter 协议；要求修复双协议叠加并统一 0-9 上下限配置。当前 scorer 在 request 尾部集中生成唯一输出协议。

### 9.3 Prompt 非单调回归

V3 已把整体偏严显著压低，V4 为防过宽加入平衡规则后反而全面回退。正确工程行为不是继续相信 optimizer，而是用 holdout gate 保留 V3。这里能讲清“为什么 prompt 修改不是单调优化”。

### 9.4 E rule 的局部收益与全局污染

`injected_prompt1` 的总 MAE只改善 0.009，但目标 structure MAE恶化 0.139，且目标维 8 篇恶化、3 篇改善。只看总均值会掩盖目标维回归，这解释了后续 target/normal/cross-dim/B-bias 多约束 gate 的必要性。

### 9.5 候选状态提前提交

Codex review 发现旧 runner 在 gate 前就从 feasible pool 移除 rule 并写 injected log；失败只删 prompt，无法恢复规则状态。修复后候选注入为 pending，所有 gate 通过才 commit，失败 rollback。测试覆盖 micro 失败、regular gate 失败和全通过三条路径。

### 9.6 仍未解决的问题

- `gate_test_sampler` 当前注释和代码明确 soft -> severe；如果真实意图是先测最危险样本，这仍是缺陷。
- E 链路部分组件仍使用 `data_index`（当前评分数组位置）而不是全局 `index`，数据重排后有错配风险。
- scorer 三次失败后写入 0 分，可能把基础设施失败误判为 severe badcase。
- 审计时的旧 scorer 只预测三主维，却把 technique/length 直接复制为 teacher，造成 C/D 恒为 0 的标签泄漏。2026-09-19 已删除该输出，并将两维正式排除在本项目主张之外。
- 源码硬编码有效形态的 API credential；发布前必须 revoke/rotate 并清理历史。

## 10. Results

### B 主迭代（本轮从 JSON 重算）

| 版本 | n | signed mean diff | 三维 MAE | Content MAE | Expression MAE | Structure MAE | B severe/soft |
|---|---:|---:|---:|---:|---:|---:|---:|
| train V0 | 36 | -2.199 | 2.199 | 1.806 | 2.764 | 2.028 | 28 / 6 |
| train V1 | 36 | -1.458 | 1.477 | 1.139 | 1.847 | 1.444 | 13 / 10 |
| train V2 | 36 | -0.718 | 0.875 | 0.639 | 1.292 | 0.694 | 5 / 3 |
| train V3 retained | 36 | -0.144 | 0.755 | 0.611 | 1.014 | 0.639 | 2 / 3 |
| train V4 rejected | 36 | -1.079 | 1.116 | 0.917 | 1.625 | 0.806 | 9 / 8 |
| holdout V3 | 12 | -0.028 | 0.556 | 0.458 | 0.417 | 0.792 | 0 / 2 |
| holdout V4 | 12 | -1.028 | 1.139 | 1.042 | 1.083 | 1.292 | 3 / 3 |

V0 -> retained V3：train MAE下降 65.7%，signed bias 绝对值下降 93.4%，B badcase 34 -> 5。只能写“训练集/留出门控结果”，不能写“测试集泛化提升”。

### 重复评分稳定性

同一 V3 prompt 的三份 36 篇结果相比，第二次有 content/expression/structure 各 1/1/5 篇变化，第三次有 4/4/5 篇变化；structure 最大变化 2 分。`temperature=0` 并未带来完全确定性，当前 gate 未纳入重复运行方差。

### E 注入

`train_scoring_results_injected1.json` 相比 V3：总 MAE 0.755 -> 0.745，但目标 structure MAE 0.639 -> 0.778。没有真实 micro/gate eval 文件，因此只能说“存在注入实验且暴露目标维回归”，不能说 E 闭环成功。

### 测试

- 本轮在可访问依赖的环境中执行 `python -m unittest -v`：9/9 通过。
- 顶层 Python 文件逐个 `py_compile`：全部通过。
- 未调用真实外部 LLM API，不宣称当前默认 pipeline 端到端成功；其默认 `final_train_scoring_results.json` / `final_aes_badcases.json` 目前不存在。

## 11. Resume Value

| 维度 | 1-5 | 理由 |
|---|---:|---|
| 技术深度 | 4 | 有误差分治、robust residual、对照规则、事务门控；统计严谨性不足 |
| 工程完整度 | 3 | 有完整脚本链和测试，但无 Git/README/deps/CI/真实 E gate |
| 原创设计程度 | 3 | 组合方式有辨识度；核心 taxonomy/公式原始作者无法确认，且未做文献对照 |
| 可量化结果 | 3 | B 路线数字可复算；样本小、holdout参与选型、E无成功报告 |
| 与候选项目区分度 | 4 | 比普通“调 prompt”更强调失败分类、局部规则和 regression control |
| 面试可讲性 | 5 | 有真实 bug、被否决方案、非单调回退和人机边界 |
| 我的真实贡献程度 | 4 | 设计约束、数据根因、review、取舍和迭代证据强；大部分代码由 Codex写 |
| 代码公开后的可信度 | 2 | 硬编码凭证、无 Git/README/deps、文件命名混乱、日志乱码和默认输入缺失 |

综合评级：**B**。

它适合写入简历，但应作为“AI-assisted、实验驱动的 LLM engineering 原型”，重点讲数据合同修复、误差分治、回归门控和人机审核。若先移除密钥、恢复可审计 Git、补独立 test 与真实 E gate 报告，可提升到 A；当前不够 S/A 的主要原因不是功能少，而是证据链和实验可信度不足。

不应作为个人核心能力的内容：

- Python JSON/Markdown 文件读写、`requests` 调用、重试和 delimiter parser。
- LLM 自动生成的长评分规则文案。
- Codex主写的 orchestrator 与测试代码本身的代码量。
- C/D detector 的存在，因为端到端 scorer 根本没有预测这两维。
- 9 轮 E consolidated 文件数量，因为多数没有 feasible rule，也没有真实 gate 成功证据。

## 12. Weaknesses / Risks

### P0

- 源码硬编码 API credential。需要立即撤销/轮换，改用环境变量，并在公开前确认历史副本未泄漏。
- [已关闭 2026-09-19] 删除 teacher -> AI 的 technique/length 复制；两维由上游确定性脚本评测，不再作为本项目闭环。
- 当前目录无 Git，不能证明作者、commit 演进或恢复覆盖过的产物。

### P1

- 仅 48 篇，36 train + 12 validation；无独立 test、交叉验证、置信区间或显著性检验。
- B optimizer 的 3 个代表样本不含作文正文，只能调评分尺度，不能归纳文本偏好。
- E evidence 主要由 LLM 自报；代码未严格验证 span 属于对应作文、索引与陈述一致。
- 阈值、normal 距离和 gate 权重是经验值，没有敏感性或消融。
- API/解析最终失败写 0，可能污染 badcase。
- 同 prompt 输出有波动，却没有重复评分聚合。
- E 默认 runner 输入缺失，且最新 consolidated_9 没有 feasible rule。

### P2

- B 的“系统性偏差”命名强于实际统计定义。
- MAD=0 用 `1e-6` 会把任何非零 residual 放大为极端 z。
- E 多处仍依赖列表 `data_index`。
- `iteration_history.json` 的 `analysis` 为 null，部分历史日志和 E 响应存在乱码。
- 平铺目录、`222`/`test2` 等文件名、无 run manifest，使复现实验成本高。

## 13. Resume Bullet Candidates

### Bullet 1：数据完整性与调试（最可防守）

**简历可能写法**  
“在 AI-assisted AES 迭代中定位随机 train/validation 与全量标签按位置拼接导致的系统性错配，统一以全局 index 建立数据合同并增加正文一致性校验，阻断错误标签继续污染 badcase mining 与 prompt 优化。”

**证据**  
`sample_extractor.py:26-55`; `batch_scoring.py:320-375`; 历史 task `019e58f7...` 中 turns `019e63f5`、`019e6402`、`019e6406`、`019e645c`。

**面试官可能追问**  
为什么位置 join 会错；为什么 essay 不是理想主键；如何处理重复文本、缺 index、重排和 gate 子集；旧结果如何判定失效。

**必须掌握**  
必须能够白板解释完整数据流和错误传播链。

**风险**  
修复代码由 Codex实现，但根因由用户主动发现并持续 review；可合理算调试与架构贡献，不要说“独立编写整个数据层”。

### Bullet 2：误差分治与 B 回退实验

**简历可能写法**  
“搭建教师反馈驱动的作文评分 prompt 迭代原型，将跨维同向偏差与去中心 residual 异常分流；36 篇训练数据上 MAE 由 2.199 降至 0.755，并通过 12 篇留出门控识别并回退一次非单调 regression。”

**证据**  
`aes_badcase_miner.py:87-138,233-300,404-479`; `pipeline_entry.py:120-238`; `train_scoring_results0-4.json`; `test_scoring_results3-4.json`; `final_prompt_meta.md`。

**面试官可能追问**  
B 与 E 定义、为什么 median/MAD、1.4826、阈值来源、为什么 holdout 不是 test、V4 为什么变差。

**必须掌握**  
必须能够白板解释公式、数据划分、指标口径和统计局限。

**风险**  
taxonomy/公式原始设计来源无法确认，代码大概率由 Codex辅助；建议用“搭建/共同设计”而非“原创算法”。65.7% 是 train 改善，不能写 test。

### Bullet 3：局部规则与分层回归控制

**简历可能写法**  
“共同设计 outlier + matched-normal 的局部规则提取与候选生命周期：单维小步注入，micro gate 同时约束目标样本、normal、跨维误差和全局 bias，全部通过后才事务提交并由人工晋升稳定 prompt。”

**证据**  
`etype_preference_analyzer.py:188-289,837-895`; `rule_integration_engine.py:164-260,506-566`; `micro_scoring_gate.py:18-41,263-417`; `etype_iteration_runner.py:480-685`; 9 个单元测试。

**面试官可能追问**  
normal 如何匹配；规则为何至少两个 evidence；权重和 guards 怎么定；为什么还需要 regular gate；commit 与 promote 有何区别。

**必须掌握**  
设计逻辑必须能解释；具体 Python API 只需知道用途；权重必须承认是经验值。

**风险**  
主要代码由 Codex生成，真实 gate 报告缺失。只能说“设计并实现原型/测试覆盖”，不能说“线上验证显著提升”。

### Bullet 4：AI-assisted engineering

**简历可能写法**  
“以 Codex 协作完成实验系统迭代，通过明确模块合同、候选/稳定状态边界和验证标准，持续 review 并否决不稳定 join、重复数据副本和提前提交方案，推动实现可回滚的 prompt 实验流程。”

**证据**  
2026-05-24、06-03、06-14 三条本地开发会话；当前 index join、deferred commit、gate stop-path 代码与测试。

**面试官可能追问**  
哪些决定由你给出；Codex 最初错在哪里；你如何验证；哪段代码你能自己重写；若 AI 再次引入回归怎么阻断。

**必须掌握**  
必须能逐个复述至少两个被否决方案和对应证据。

**风险**  
如果只说“使用 AI 提效”会很空；必须落到具体 constraint/review/test。

## 14. 2-minute Interview Story

这个项目不是完整作文评分产品，而是一个教师反馈驱动的 prompt 迭代实验。输入是 48 篇带教师多维评分的作文，模型只重评内容、表达和结构。项目先把 `AI - teacher` 的误差拆成两条路线：一类是一篇作文三个维度同向偏严或偏宽，用来调整整体评分锚点；另一类是扣掉每个维度中位偏差后仍异常的 residual，用 median/MAD 做稳健排序，再用 outlier 和 matched normal 对比抽取局部规则。

我在项目里的主要价值不是独立手写所有 Python。大量实现由 Codex辅助完成，我主要负责约束、review、调试和迭代判断。最典型的一次是我发现随机划分后的 train/test 结果仍按全量数组位置补 teacher 分，导致 AI 和教师标签不是同一篇作文。Codex先后给过全文匹配和 `source_data_index` 方案，我继续追问主键语义和 gate 数据来源，最后把全量数据固定为唯一权威源，所有子集都携带全局 index，并用正文做一致性校验。

在 prompt 迭代上，我要求停止大范围重构，基于稳定 prompt 做单维、单规则的小步 E 迭代；候选先过 micro 和 regular gate，失败回滚，成功也只能等待人工 promote。可复算的 B 实验中，36 篇训练集 MAE 从 2.199 降到第 3 轮 0.755；第 4 轮在 12 篇留出集上 MAE 从 0.556 恶化到 1.139，所以保留第 3 轮。这个项目最重要的经验是：LLM 生成代码和 prompt 都不是可信终点，数据身份、对照样本、回归门控和人工边界才决定实验是否可信。

我也会主动说明局限：留出集参与了选型，数据量小；prompt 范围只包含 content/expression/structure，technique/length 属于上游确定性脚本；E gate 只有代码和测试、没有真实成功报告；因此它是研究原型而不是生产系统。

## 15. Interview Questions

| 问题 | 应答核心 | 掌握级别 | 风险 |
|---|---|---|---|
| B 和 E 到底有什么区别？ | B 是单样本三维 raw diff 同向；E 是单维 diff 去 median 后的 residual outlier | 白板 | 不要把 B 说成严格统计系统偏差 |
| 为什么用 median/MAD？ | 抗少量极端值；1.4826 将正态下 MAD 缩放到标准差量级 | 白板 | n=36、离散分数、无消融 |
| MAD=0 怎么办？ | 当前用 1e-6，会夸大 z；可改 empirical quantile/最小样本或关闭该维检测 | 白板 | 现实现有缺陷 |
| 为什么不能按数组位置 join？ | split 打乱顺序；位置不是身份，错误会沿 scoring -> badcase -> rule 全链污染 | 白板 | 最强可讲点 |
| 为什么不一直用 essay 文本 join？ | 长文本成本高、可能重复/标准化差异；global index 才是稳定身份，文本用于 integrity check | 设计逻辑 | read timeout 是历史现象 |
| V4 为什么回退？ | 为限制过宽新增平衡规则，反而重新变严；只能支持相关性，不能做严格因果归因 | 设计逻辑 | 不要编造因果 |
| 为什么 test 不能叫 test？ | 它参与版本选择，是 validation/holdout；需要独立 untouched test | 白板 | 简历最易夸大 |
| normal 如何选？ | 同数据集中 raw diff/z 较小，再按题型、teacher score、长度距离匹配 | 设计逻辑 | 手工距离、无匹配质量验证 |
| 如何防止单样本规则过拟合？ | evidence>=2、有 normal/counter evidence、局部作用域、单维小步、micro/regular gate | 白板 | LLM evidence 未严格核验 |
| Micro gate 怎么算？ | baseline abs error - candidate abs error；按角色/维度加权，再加硬回归 guards | 白板 | 权重经验设定，无调参实验 |
| commit、rollback、promote 区别？ | commit 消费 rule pool/写注入状态；rollback 保留规则；promote 才覆盖 final | 设计逻辑 | 无完整 registry/Git |
| technique/length 怎么处理？ | 它们由上游确定性脚本评测，与 prompt 无关；本项目只负责三个主观维度 | 必须直说 | 不得把 C/D 包装为 prompt 能力 |
| API 失败怎么办？ | 当前有重试，但最终写 0 是缺陷；应标记 scoring_error 并排除统计 | 设计逻辑 | 不能声称已安全处理 |
| 为什么 `temperature=0` 仍会变？ | 服务端/模型推理仍可能非完全确定；重复产物有 1-2 分变化 | 设计逻辑 | 未做正式稳定性统计 |
| 哪些代码是你自己写的？ | 大量代码由 Codex写；本人主要是问题定义、约束、review、数据 bug 诊断、取舍和验收 | 必须直说 | 诚信核心问题 |

## 16. Evidence Map

| 技术点 | 对应文件 | 函数/类 | Commit | 实验/输出证据 | 对话证据 |
|---|---|---|---|---|---|
| 48 -> 36/12 固定划分 | `sample_extractor.py:14-73` | `EssayExtractor.run` | 无 Git，无法确定 | `origin_scoring_results.json`; `train_essays.json`; `test_essays.json` | 无原始设计证据 |
| 全局 index 数据合同 | `batch_scoring.py:320-375` | `_build_origin_index_lookup`; `_match_origin_item`; `save_results` | 无法确定 | 当前 48/36/12 index 唯一、无重叠、并集一致 | task `019e58f7...` turns `019e63f5` 至 `019e645c` |
| B detector | `aes_badcase_miner.py:233-300` | `_detect_class_b_bias` | 无法确定 | `aes_badcases0-4.json` | 原始作者无法确认 |
| E robust residual | `aes_badcase_miner.py:87-138,404-479` | `_compute_basic_statistics`; `_detect_class_e_residual` | 无法确定 | 各轮 `E_residual` 与 statistics | task `019e58f7...` 多轮阈值/极端 diff 讨论 |
| C/D detector 与标签泄漏 | `aes_badcase_miner.py:302-402`; `batch_scoring.py:369-375` | `_detect_class_c_technique`; `_detect_class_d_length`; `save_results` | 无法确定 | 原始数据可检出 C=11、D=6；scoring 产物全复制 teacher | 当前代码审计 |
| B prompt 迭代 | `prompt_optimizer.py:26-168,328-412`; `pipeline_entry.py:181-250` | `PromptOptimizer`; `run_pipeline` | 无法确定 | `optimized_prompt1-4*`; `iteration_history.json` | 历史用户持续要求比较 train/holdout 与过拟合 |
| V4 回退 | `pipeline_entry.py:120-145,222-238` | `should_gate`; `save_final_prompt` | 无法确定 | train V3/V4、holdout V3/V4；final meta = V3 | task `019e58f7...`，最终以当前产物为准 |
| Outlier + normal 对比 | `etype_preference_analyzer.py:188-289,291-489` | `select_outlier_samples`; `select_normal_samples`; `build_contrastive_analysis_request` | 无法确定 | `etype_analysis/*_contrastive_request/response.json` | task `019e58f7...` 明确 E-only 阶段 |
| Feasible rule filter | `etype_preference_analyzer.py:788-895` | `is_hard_threshold_rule`; `is_valid_injection_target`; `compile_all_rules` | 无法确定 | consolidated_2 structure feasible=1；consolidated_8 content feasible=1 | 用户要求 integrator 信任该合同 |
| 单维小步注入 | `rule_integration_engine.py:119-260,322-504` | `select_target_dimension`; `select_rules_for_sub_iteration`; `inject_rules` | 无法确定 | `injected_rules_*.json`; `injected_prompt1_meta.md` | turn `019e63c2...` |
| 事务提交/回滚 | `rule_integration_engine.py:506-566`; `etype_iteration_runner.py:655-685` | `commit`; `rollback`; `run` | 无法确定 | transaction/runner flow tests | session `019ec4bb...` review/fix |
| Gate sampler | `gate_test_sampler.py:19-278` | `GateTestSampler` | 无法确定 | 无当前 gate output | turns `019e63de...`、`019e63e1...` |
| Micro weighted gate | `micro_scoring_gate.py:18-41,119-417` | `Config`; `build_manifest`; `evaluate` | 无法确定 | 4 micro gate tests | session `019ec4bb...` 用户提出加权层 |
| Regular gate 与人工边界 | `etype_iteration_runner.py:480-653` | `evaluate_gate`; `discard_rejected_candidate`; `promote_to_final` | 无法确定 | 3 runner flow tests；无真实 gate eval | session `019e8c94...` 用户确认失败丢弃、人工覆盖 |
| 同 prompt 非确定性 | 三份 `train_scoring_results3*.json` | 数据对比 | 无法确定 | 变化数 1/1/5 与 4/4/5，最大 2 分 | 当前重算 |
| E 注入目标维回归 | `injected_prompt1_meta.md` | 规则文本 | 无法确定 | total MAE 0.755 -> 0.745；structure 0.639 -> 0.778 | 后续 gate 设计动机，非真实 gate 结论 |
| 测试状态 | 三个 `test_*.py` | 9 个 unittest | 无法确定 | 2026-09-14 本轮 9/9 PASS | 当前验证 |

### 证据缺口清单

- 谁最初提出 B/C/D/E taxonomy、median/MAD 和阈值：**无法确认，需要本人补充**。
- 哪些早期文件由用户自行编写：**无法确认，需要本人补充**。
- Git commit、author、branch、PR/issue、代码 review diff：当前目录没有 `.git`，**无法确认**。
- E micro/regular gate 是否在真实 API 数据上通过：无 eval/run report，**无法确认**。
- 当前代码是否曾部署或被真实教师使用：仓库无部署和使用记录，**无法确认**。
- 历史 B4/B5/E5 旧对比是否使用修复前错配数据：无法完整追溯，应视为不可靠；本文只采用当前可复算 V0-V4 产物。
