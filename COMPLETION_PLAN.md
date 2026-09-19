# AES Prompt Optimization Completion Plan

Date: 2026-09-18  
Inputs: `PROJECT_COMPLETION_STANDARD.md` and `PROJECT_AUDIT.md`

The project is still an experimental prototype. Completion work is prioritized by evidence integrity and reproducibility, not by adding features.

## Completed in this pass

| Gap | Action | Verification |
|---|---|---|
| Hard-coded API credential | Removed all embedded values; runtime now reads `AES_API_KEY` and optional `AES_API_URL` | Source credential scan passes |
| Infrastructure failures becoming 0 scores | Exhausted API/parsing retries now abort before an output file is written | Code path changed; Python compile and unit suite pass |
| Teacher-label leakage into AI technique/length | New scoring outputs and active badcase reports contain only content/expression/structure | Contract test passes |
| Technique/length scope ambiguity | Recorded the project decision that both belong to upstream deterministic scripts and are excluded from prompt outputs, optimization targets, and project claims | README and audit decision note updated |
| Hard-coded model identifiers | Added separate environment configuration for scoring, B optimization, and E analysis models | Configuration and scoring-payload tests cover model injection |
| Missing reproducibility docs | Added README, dependency file, environment example, evidence boundaries, run and offline-check commands | Documentation review plus executable commands |
| Missing repository hygiene | Added ignore rules for credentials, private essays, labels, raw API material, caches, and generated reports | `git status --ignored` separates source from local artifacts |
| Missing deterministic integrity check | Added source-secret, global-index, split, essay-text, teacher-pairing, and score-range checks | 48 unique origin rows; 36/12 disjoint complete split; 13 artifacts aligned |
| Missing regression coverage for completion checks | Added unit tests for duplicate indices, text mismatch, teacher mismatch, secret detection, scorer boundaries, active dimensions, model configuration, API responses, B stopping policy, three-candidate cap comparison, and E gate rounding | 32 tests pass |
| Complete B-route rerun | Re-ran through the V6 safety cap with `claude-sonnet-5`; the final cap compared V4/V5/V6 using `5*severe+soft`, then severe, then earlier version | Validation Scores V4=3, V5=11, V6=7; V4 promoted to `final_prompt*`; all 36-row train and 12-row validation artifacts aligned |
| E-route validation design | Profiled V4 residuals and defined structure-first, one-rule iterations with micro, target/control, full-train, validation, and human-promotion layers | `E_VALIDATION_PLAN.md`; 5-case 50% threshold corrected to require 3 improvements |
| No Git repository | Initialized a new `main` repository | Git can track changes from 2026-09-18 onward; it cannot recover prior authorship |

## Next work that remains automatable

1. Implement the E-route Layer-3 aggregate B/E evaluator and Layer-4 validation B guard specified in `E_VALIDATION_PLAN.md`.
2. Make `gate-and-train` evaluate the regular gate before spending a full-train API run.
3. Add direction-stratified content E sampling.
4. Replace positional `data_index` references in E analyzer, micro gate, and regular gate manifests with the stable global `index`, while retaining a compatibility reader for historical artifacts.
5. Add a synthetic, explicitly non-evaluative public smoke fixture and an offline smoke command that never calls the model.
6. Add a run manifest containing prompt hash, dataset fingerprint, model identifier, thresholds, repeat number, and output hashes.
7. Add CI for offline checks and unit tests after the first reviewed commit.

## Blocked on external action or human decisions

| Item | Why automation must stop |
|---|---|
| Revoke/rotate the exposed API credential | Only the credential owner/provider console can invalidate it |
| Freeze an untouched independent test set | Requires a decision about data allocation, sample sufficiency, and future access |
| Run repeated real-model evaluation | Incurs external API usage and requires the rotated credential; acceptance thresholds should be frozen first |
| Approve a real version-gate report | This is the project’s explicit human promotion boundary |
| Validate E evidence spans and rule semantics | Requires human review of essay evidence and teacher intent |
| Claim statistical or generalization improvement | Current sample size and validation reuse do not support that claim |

## Stop condition

Stop feature work when the repository has a reviewed initial commit, global-index-only runtime contracts, a synthetic offline smoke path, one frozen independent evaluation protocol, repeated real scoring with a retained gate report, and explicit human approval or rejection of the candidate. Until then, describe the project as an AI-assisted, experiment-driven prototype.
