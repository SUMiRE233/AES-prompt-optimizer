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
| Missing regression coverage for completion checks | Added unit tests for duplicate indices, text mismatch, teacher mismatch, secret detection, scorer boundaries, active dimensions, model configuration, API responses, B stopping policy, three-candidate cap comparison, E gate rounding, aggregate gates, direction sampling, and manifests | 46 tests pass |
| Complete B-route rerun | Re-ran through the V6 safety cap with `claude-sonnet-5`; the final cap compared V4/V5/V6 using `5*severe+soft`, then severe, then earlier version | Validation Scores V4=3, V5=11, V6=7; V4 promoted to `final_prompt*`; all 36-row train and 12-row validation artifacts aligned |
| E-route validation design | Profiled V4 residuals and defined structure-first, one-rule iterations with micro, target/control, full-train, validation, and human-promotion layers | `E_VALIDATION_PLAN.md`; 5-case 50% threshold corrected to require 3 improvements |
| No Git repository | Initialized a new `main` repository | Git can track changes from 2026-09-18 onward; it cannot recover prior authorship |
| Positional identity remained in E runtime | New analyzer output, micro manifests, and regular gate manifests use stable global `index`; legacy positional artifacts are converted on read | Shuffled-order and compatibility tests pass |
| Content direction coverage was accidental | Regular gate sampling now deterministically includes strict and lenient cases when both exist and at least two slots are available | Direction-coverage test passes |
| Layer-3/4 decisions lacked direct tests | Added passing and rejecting tests for full-train B/E aggregation and validation B/target guards | Four aggregate-gate tests pass |
| No public end-to-end fixture | Added an explicitly synthetic six-row scoring fixture and `offline_smoke.py` | Smoke completes without a credential or network request |
| Missing run identity | Added `run_manifest.py` for input hashes, model identifiers, thresholds, and repeat number; secret-like fields are rejected | Manifest tests pass |
| Missing CI | Added an offline GitHub Actions workflow | Remote model/API evaluation is intentionally excluded until Phase 2 |
| Real E gate evidence | Ran structure and content candidates against B-final V4; both were rejected and rolled back at the earliest failing layer | `STRUCTURE_E_VALIDATION_REPORT.md`; `CONTENT_E_VALIDATION_REPORT.md` |

## Phase 1 status: offline engineering complete

The no-network implementation work is complete. Remaining local maintenance is
limited to keeping reports synchronized with code and reviewing each commit for
private artifacts. No further prompt-rule expansion is required for Phase 1.

## Blocked on external action or human decisions

| Item | Why automation must stop |
|---|---|
| Revoke/rotate the exposed API credential | Only the credential owner/provider console can invalidate it |
| Freeze an untouched independent test set | Requires a decision about data allocation, sample sufficiency, and future access |
| Run repeated real-model evaluation | Phase 2 only: incurs external API usage and requires the temporary credential; acceptance thresholds must be frozen first |
| Approve a real version-gate report | This is the project’s explicit human promotion boundary |
| Validate E evidence spans and rule semantics | Requires human review of essay evidence and teacher intent |
| Claim statistical or generalization improvement | Current sample size and validation reuse do not support that claim |

## Stop condition

The repository now has its initial commit, global-index runtime contracts, a
synthetic offline smoke path, real rejection reports, and offline CI. Feature
work should remain stopped. Phase 2 is limited to freezing an independent
evaluation protocol, repeated real scoring, credential revocation, and the
resulting human acceptance decision. Until Phase 2 is complete, describe the
project as an AI-assisted, experiment-driven prototype.
