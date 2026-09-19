# Sonnet First-Run Report

Date: 2026-09-19

## Run configuration

- Scoring model: `claude-sonnet-5`
- Prompt optimizer: `claude-sonnet-5`
- Training set: 36 essays
- Validation/holdout set: 12 essays
- Primary comparison metric: `Score = 5 * Severe + Soft` (lower is better)
- Tie-break order: fewer severe cases, then earlier version
- MAE policy: record only; it does not trigger the gate or choose the final prompt

The files retain the historical `test_*` names, but this 12-essay split has
participated in candidate evaluation and is therefore validation/holdout data,
not an untouched independent test set.

## Results

| Version | Train signed diff | Train MAE | Train severe/soft | Train Score | Validation signed diff | Validation MAE | Validation severe/soft | Validation Score |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| V0 | -1.681 | 1.708 | 18 / 9 | 99 | - | - | - | - |
| V1 | -1.310 | 1.394 | 13 / 7 | 72 | -1.389 | 1.556 | 5 / 3 | 28 |
| V2 | -1.218 | 1.319 | 13 / 8 | 73 | -1.111 | 1.278 | 2 / 4 | 14 |
| V3 | -0.810 | 1.106 | 6 / 8 | 38 | -0.722 | 1.167 | 3 / 1 | 16 |
| V4 | -0.514 | 1.060 | 5 / 7 | 32 | -0.389 | 0.889 | 0 / 3 | **3** |
| V5 | -0.384 | 0.912 | 3 / 6 | **21** | -0.278 | **0.833** | 2 / 1 | 11 |
| V6 | -0.097 | **0.838** | 4 / 5 | 25 | -0.139 | **0.806** | 1 / 2 | 7 |

The first V6 scoring attempt stopped at essay 33/36 after an HTTP 402. That
attempt wrote no partial result. After the test balance was restored, V6 was
rescored completely and admitted to the comparison.

## Interpretation

The all-Sonnet route produced strong overall improvement rather than a general
train-only collapse. From V0 to V5, train Score fell from 99 to 21. On the
validation split, V4 achieved the best primary result: zero severe cases and a
Score of 3.

The path was not monotonic on validation:

- V3 improved train Score from 73 to 38 while validation Score moved from 14
  to 16. This is a small one-version regression.
- V5 further improved train Score from 32 to 21 and validation MAE from 0.889
  to 0.833, but validation B Score regressed from 3 to 11 because severe cases
  rose from 0 to 2.

V5 is therefore retained as a useful late-iteration regression example. MAE
did not warn about it, which demonstrates why MAE must remain observational and
why the severe-weighted B metric controls selection.

## Candidate conclusion

Under the declared comparison rule, V4 is the selected Sonnet candidate. The
V6 safety-cap comparison evaluated V4, V5, and V6; their validation Scores were
3, 11, and 7 respectively. V4 has been promoted to `final_prompt*` as the B
route final.

The safety-cap implementation now compares the last three versions only when
V6 is reached. This retained V4 without repeatedly consulting the validation
split during optimization.

## Evidence limitations

- Each version has one stochastic scoring pass; no repeated-run variance is
  available.
- The validation split was used for version comparison, so it cannot support
  an independent generalization claim.
- These results support an all-Sonnet first-run report, but not a production or
  statistically significant performance claim.
