# AES-prompt-optimizer

面向教师标注的作文评分提示词迭代研究原型（AI 辅助）。项目将大范围、同向的评分偏置（B 路线）与局部残差异常（E 路线）分离处理，并通过候选门禁 + 显式人工晋升，防止实验性提示词静默替换稳定版本。

## 证据状态

- **可离线复现：** 提示词预处理、badcase 挖掘、确定性数据契约检查、门禁计算、候选提交/回滚、公开合成冒烟路径，以及 251 项单元测试。
- **历史实验证据：** 48 篇作文以种子 42 划分为 36 篇训练 + 12 篇验证/留出（holdout）。留出集参与了版本选择，**不是**独立测试集。
- **真实拒绝证据：** 结构维候选曾进入 micro 门并被拒；内容维候选通过 micro 后又被 regular 门拒绝。两次候选均回滚，未改动当时的 B 终版。至今没有任何 E 候选通过全部层级。
- **设计上排除：** `technique` 与 `length` 由上游确定性脚本评测，不是提示词输出、优化目标，也不属于本仓库的项目主张。
- **不作声称：** 生产部署、大规模泛化、统计显著改善、或自主提示词晋升。

完整证据与贡献审计留档于本地 `PROJECT_AUDIT.md`；按项目策略，除本 README 与评分 prompt 外，说明性文档不随仓库分发。

## B 路线最近一次重跑（2026-09-19）

### B 当前状态（2026-09-21，探索性冻结）

B 路线在统一目标 `Q = 2.5*mean(Severe)+mean(Soft)` 与两级精度协议下重新冻结。官方路线基于既有工件离线重建（无新增 API 调用）：V3 首次告警、被双评边界检查推翻；V4/V5 以高精度通过；运行停在 **V6 上限处的高精度门**（`high_precision_gate_at_cap`）。验证（12 篇留出作文、每候选两次固定重跑）在 V5 与 **V6** 之间选择了 V6（Q 4.75 对 5.25 —— 噪声级差距；训练均值指向相反方向）。状态：`B_EXPLORATORY_PROTOCOL_FROZEN`。证据（均为本地留档）：`B_REFACTOR_REPORT.md` §12.7、`final_evidence.json`、`b_rebuilt_route.json`。**不存在独立测试集**；不声称 V5 与 V6 可区分。

评分与提示词优化均使用 `claude-sonnet-5`；探索性首轮运行已扩展到最后一个完整版本 V5：

| 划分/版本 | n | 带符号均值差 | MAE | B Severe/Soft |
|---|---:|---:|---:|---:|
| 训练 V0 | 36 | -1.681 | 1.708 | 18 / 9 |
| 训练 V1 | 36 | -1.310 | 1.394 | 13 / 7 |
| 训练 V2 | 36 | -1.218 | 1.319 | 13 / 8 |
| 训练 V3 | 36 | -0.810 | 1.106 | 6 / 8 |
| 训练 V4 | 36 | -0.514 | 1.060 | 5 / 7 |
| 训练 V5 | 36 | -0.384 | 0.912 | 3 / 6 |
| 训练 V6 | 36 | -0.097 | 0.838 | 4 / 5 |
| 验证 V1 | 12 | -1.389 | 1.556 | 5 / 3 |
| 验证 V2 | 12 | -1.111 | 1.278 | 2 / 4 |
| 验证 V3 | 12 | -0.722 | 1.167 | 3 / 1 |
| 验证 V4（最佳 B 候选） | 12 | -0.389 | 0.889 | 0 / 3 |
| 验证 V5 | 12 | -0.278 | 0.833 | 2 / 1 |
| 验证 V6 | 12 | -0.139 | 0.806 | 1 / 2 |

在 `Score = 5 * Severe + Soft` 口径下，V6 安全上限比较得到 V4=3、V5=11、V6=7。V4 曾获选并晋升为当时的 B 终版（已被上述 2026-09-21 重新冻结取代）。V5 作为后期迭代的回归示例保留：其 MAE 改善的同时严重 B 案例回归。12 篇验证样本参与了版本比较，因此这些数字不构成独立测试证据。首轮报告与局限见本地留档 `SONNET_FIRST_RUN_REPORT.md`。

## 架构

```text
教师标注作文
  -> 确定性训练/验证划分
  -> 远程 LLM 评分（仅内容/表达/结构）
  -> B 偏置与 E 残差挖掘
  -> 提示词/规则候选
  -> micro 门 -> regular 门 -> 全量训练门 -> 验证守卫
  -> 待人工复核 -> 显式晋升
```

完整源数据集是元数据权威。抽样文件携带稳定的全局 `index`；正文文本作为完整性守卫校验。跨文件不把数组位置当作身份。

## 环境准备

需要 Python 3.10+。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

任何远程调用之前，先吊销历史曾嵌入源码的凭据，并仅通过环境变量暴露替换后的值：

```powershell
$env:AES_API_KEY = Read-Host -MaskInput "输入已轮换的 AES API 密钥"
$env:AES_API_URL = "https://api.pateway.ai/v1/messages"  # 可选覆盖
$env:AES_SCORING_MODEL = "provider-model-id-for-scoring"
$env:AES_OPTIMIZER_MODEL = "provider-model-id-for-b-optimization"
$env:AES_E_ANALYSIS_MODEL = "provider-model-id-for-e-analysis"
```

切勿把真实值写入 `.env.example`、源码、JSON 工件或 Git 历史。

上述变量仅影响当前 PowerShell 进程及其启动的程序。运行测试前在同一终端设置。三个模型变量刻意分离：更新评分模型不会静默改变 B 优化器或 E 分析器。

在不打印密钥、不发起 API 调用的前提下确认所选模型：

```powershell
python -c "from batch_scoring import BatchEssayScorer; from prompt_optimizer import PromptOptimizer; from etype_preference_analyzer import ContrastiveETypeAnalyzer; print(BatchEssayScorer().model, PromptOptimizer().model, ContrastiveETypeAnalyzer().model)"
```

配置在这三个构造函数中读取：

- `batch_scoring.py::BatchEssayScorer.__init__` — `AES_SCORING_MODEL`
- `prompt_optimizer.py::PromptOptimizer.__init__` — `AES_OPTIMIZER_MODEL`
- `etype_preference_analyzer.py::ContrastiveETypeAnalyzer.__init__` — `AES_E_ANALYSIS_MODEL`

## 离线验证

以下命令不会调用外部模型：

```powershell
python -m unittest -v
python project_checks.py
Get-ChildItem -Filter *.py | ForEach-Object { python -m py_compile $_.FullName }
python offline_smoke.py
```

`project_checks.py` 校验源码凭据卫生；当本地私密工件存在时，还会校验索引唯一性、划分互斥/完备、作文完整性、师评配对与评分工件对齐。

`offline_smoke.py` 仅使用 `fixtures/synthetic_scoring_results.json`，在不读取私密作文、不要求凭据、不发起网络请求的前提下运行公开的评分/badcase 契约。

正式实验前生成无密钥的运行清单（run manifest）：

```powershell
python run_manifest.py `
  --input final_prompt.md `
  --input train_essays.json `
  --model scoring=claude-sonnet-5 `
  --threshold b_score=3 `
  --repeat 1
```

生成的 `run_manifest*.json` 默认仅本地保留，记录哈希、模型标识、阈值与重复次数，并拒绝疑似密钥字段。

## 运行原型

在无 API 调用下对 E 路线做 dry-run：

```powershell
python etype_iteration_runner.py --skip-analysis --consolidated etype_analysis/contrastive_consolidated_1.json --score none
```

B 流水线会发起远程评分与优化器调用：

```powershell
python pipeline_entry.py --origin-file origin_scoring_results.json
```

远程模型评测属于 Phase 2 工作，刻意不进入离线单测套件、冒烟命令与 CI 工作流；运行上述命令需要凭据与成本的显式授权。

历史文件名 `test_essays.json` 与 `test_scoring_results*.json` 为兼容性保留；语义上它们是**验证/留出**工件，不得作为未动过的测试集报告。

## 数据与工件策略

- 真实作文、师评标签、原始请求/响应、评分输出、badcase 转储、运行报告与本地凭据均被 Git 忽略。
- 公开合成夹具可另行添加，但必须标注为合成，且不得用于支撑模型准确率主张。
- 生成的候选永不覆盖 `final_prompt*`，除非人工复核后显式提供 `--promote-final`。
- API 或解析资源耗尽可能中止运行，且不会把 0 分写入下游统计。

## 已知的结项阻塞项

以下事项刻意不由此仓库清理自动解决：

1. 最终验收后，在服务商控制台吊销临时评测凭据。
2. 冻结一套新的、未动过的独立测试集，并确定其评测协议。
3. 在冻结的 Phase 2 协议下进行重复的真实 API 评分。
4. 仅对通过全部自动门禁的候选执行人工复核。

`technique` 与 `length` 在此不构成结项阻塞：项目决定为最终——它们属于上游确定性评测，且被排除在本提示词优化项目之外。
