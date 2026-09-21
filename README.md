# AES-prompt-optimizer

An AI-assisted research prototype for iterating an essay-scoring prompt against teacher labels. The project separates broad, same-direction scoring bias (B route) from local residual anomalies (E route), and uses candidate gates plus explicit human promotion to keep experimental prompts from silently replacing the stable version.

## Evidence status

- **Reproducible offline:** prompt preprocessing, badcase mining, deterministic data-contract checks, gate calculations, candidate commit/rollback, a public synthetic smoke path, and 251 unit tests.
- **Historical experiment evidence:** 48 essays split with seed 42 into 36 train and 12 validation/holdout samples. The holdout participated in version selection and is **not** an independent test set.
- **Real rejection evidence:** structure reached the micro gate and was rejected; content passed micro and was rejected by the regular gate. Both candidates rolled back without changing the then-current B final. No E candidate has passed every layer.
- **Out of scope by design:** `technique` and `length` are evaluated by upstream deterministic scripts. They are not prompt outputs, optimization targets, or project claims in this repository.
- **Not claimed:** production deployment, large-scale generalization, statistically significant improvement, or autonomous prompt promotion.

See [PROJECT_AUDIT.md](PROJECT_AUDIT.md) for the complete evidence and contribution audit.

## Latest B-route rerun (2026-09-19)

### Current B status (2026-09-21, exploratory freeze)

The B route was re-frozen under the unified objective `Q = 2.5*mean(Severe)+mean(Soft)` with a
two-tier precision protocol. The official route was rebuilt offline from the existing artifacts
(no new API calls): first warning at V3, refuted by the double-eval boundary check; V4/V5 passed
at high precision; the run stopped at a **high-precision gate at the V6 cap**
(`high_precision_gate_at_cap`). Validation (12 holdout essays, two fixed runs per candidate)
selected **V6** over V5 (Q 4.75 vs 5.25 — a noise-level margin; train means point the other way).
Status: `B_EXPLORATORY_PROTOCOL_FROZEN`. Evidence: `B_REFACTOR_REPORT.md` §12.7,
`final_evidence.json`, `b_rebuilt_route.json`. There is **no independent test set**; V5 and V6
are not claimed to be distinguishable.

Using `claude-sonnet-5` for both scoring and prompt optimization, the exploratory first run was extended through the last complete version, V5:

| Split/version | n | Signed mean diff | MAE | B severe/soft |
|---|---:|---:|---:|---:|
| Train V0 | 36 | -1.681 | 1.708 | 18 / 9 |
| Train V1 | 36 | -1.310 | 1.394 | 13 / 7 |
| Train V2 | 36 | -1.218 | 1.319 | 13 / 8 |
| Train V3 | 36 | -0.810 | 1.106 | 6 / 8 |
| Train V4 | 36 | -0.514 | 1.060 | 5 / 7 |
| Train V5 | 36 | -0.384 | 0.912 | 3 / 6 |
| Train V6 | 36 | -0.097 | 0.838 | 4 / 5 |
| Validation V1 | 12 | -1.389 | 1.556 | 5 / 3 |
| Validation V2 | 12 | -1.111 | 1.278 | 2 / 4 |
| Validation V3 | 12 | -0.722 | 1.167 | 3 / 1 |
| Validation V4 best B candidate | 12 | -0.389 | 0.889 | 0 / 3 |
| Validation V5 | 12 | -0.278 | 0.833 | 2 / 1 |
| Validation V6 | 12 | -0.139 | 0.806 | 1 / 2 |

Under `Score = 5 * Severe + Soft`, the V6 safety-cap comparison produced V4=3, V5=11, and V6=7. V4 was selected and promoted as the then-current B final (superseded by the 2026-09-21 re-freeze above). V5 is retained as a late-iteration regression example: its MAE improves while severe B cases return. The 12-sample validation set participated in version comparison, so these numbers are not independent-test evidence. See [SONNET_FIRST_RUN_REPORT.md](SONNET_FIRST_RUN_REPORT.md) for the first-run report and limitations.

## Architecture

```text
teacher-labelled essays
  -> deterministic train/validation split
  -> remote LLM scoring (content/expression/structure only)
  -> B bias and E residual mining
  -> prompt/rule candidate
  -> micro gate -> regular gate -> full-train gate -> validation guard
  -> pending human review -> explicit promotion
```

The full origin dataset is the metadata authority. Sampled files carry a stable global `index`; essay text is checked as an integrity guard. Array position is not treated as identity across files.

## Setup

Requires Python 3.10+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Before any remote call, rotate the credential that was previously embedded in the source, then expose the replacement only through the environment:

```powershell
$env:AES_API_KEY = Read-Host -MaskInput "Enter rotated AES API key"
$env:AES_API_URL = "https://api.pateway.ai/v1/messages"  # optional override
$env:AES_SCORING_MODEL = "provider-model-id-for-scoring"
$env:AES_OPTIMIZER_MODEL = "provider-model-id-for-b-optimization"
$env:AES_E_ANALYSIS_MODEL = "provider-model-id-for-e-analysis"
```

Never put the real value in `.env.example`, source code, JSON artifacts, or Git history.

The variables above affect only the current PowerShell process and programs launched from it. Set them in the same terminal before running a test. The three model variables are intentionally separate: updating the scoring model does not silently change the B optimizer or E analyzer.

Confirm the selected models without printing the key or making an API call:

```powershell
python -c "from batch_scoring import BatchEssayScorer; from prompt_optimizer import PromptOptimizer; from etype_preference_analyzer import ContrastiveETypeAnalyzer; print(BatchEssayScorer().model, PromptOptimizer().model, ContrastiveETypeAnalyzer().model)"
```

Configuration is read in these constructors:

- `batch_scoring.py::BatchEssayScorer.__init__` — `AES_SCORING_MODEL`
- `prompt_optimizer.py::PromptOptimizer.__init__` — `AES_OPTIMIZER_MODEL`
- `etype_preference_analyzer.py::ContrastiveETypeAnalyzer.__init__` — `AES_E_ANALYSIS_MODEL`

## Offline verification

These commands do not call an external model:

```powershell
python -m unittest -v
python project_checks.py
Get-ChildItem -Filter *.py | ForEach-Object { python -m py_compile $_.FullName }
python offline_smoke.py
```

`project_checks.py` verifies source credential hygiene and, when the local private artifacts are present, index uniqueness, split disjointness/completeness, essay integrity, teacher-label pairing, and scoring-artifact alignment.

`offline_smoke.py` uses only `fixtures/synthetic_scoring_results.json`. It runs
the public scoring/badcase contract without reading private essays, requiring a
credential, or making a network request.

Create a secret-free run manifest before a real experiment:

```powershell
python run_manifest.py `
  --input final_prompt.md `
  --input train_essays.json `
  --model scoring=claude-sonnet-5 `
  --threshold b_score=3 `
  --repeat 1
```

The generated `run_manifest*.json` is local by default. It records hashes,
model identifiers, thresholds, and repeat number, and rejects secret-like
fields.

## Running the prototype

Dry-run the E route without API calls:

```powershell
python etype_iteration_runner.py --skip-analysis --consolidated etype_analysis/contrastive_consolidated_1.json --score none
```

The B pipeline makes remote scoring and optimizer calls:

```powershell
python pipeline_entry.py --origin-file origin_scoring_results.json
```

Remote-model evaluation is Phase 2 work. It is deliberately absent from the
offline unit suite, smoke command, and CI workflow; running the command above
requires explicit credential and cost authorization.

The historical filenames `test_essays.json` and `test_scoring_results*.json` are retained for compatibility. Semantically they are **validation/holdout** artifacts. Do not report them as an untouched test set.

## Data and artifact policy

- Real essays, teacher labels, raw requests/responses, scoring outputs, badcase dumps, run reports, and local credentials are ignored by Git.
- A public synthetic fixture may be added separately, but it must be labelled synthetic and cannot support model-accuracy claims.
- Generated candidates never overwrite `final_prompt*` unless `--promote-final` is explicitly supplied after human review.
- API or parsing exhaustion aborts the run without writing zero scores into downstream statistics.

## Known completion blockers

The following are intentionally not auto-resolved by this repository cleanup:

1. Revoke the temporary evaluation credential in the provider console after final acceptance.
2. Freeze a new untouched independent test set and decide its evaluation protocol.
3. Run repeated real-API scoring under the frozen Phase 2 protocol.
4. Human-review only a candidate that passes every automated gate.

`technique` and `length` are not completion blockers here: the project decision is final that they belong to upstream deterministic evaluation and are excluded from this prompt-optimization project.
