> 历史状态：V4 时代 E 类探索证据。当前正式 B baseline 已升级为 V6。
> 本文中的残差数量、候选规则、阈值结果和晋升结论不得直接复用于 V6；
> 仅保留失败模式、事务回滚和门禁设计证据。

# Structure E Validation — Brief Report

Date: 2026-09-19

## Outcome

After strengthening the rule-writing prohibition, the structure retry produced
safe qualitative rules and reached the micro gate. The selected rule was then
rejected by two hard regression guards. The candidate was deleted, its pending
rule transaction was rolled back, and the B-final V4 prompt was not changed.

## Inputs

- Baseline: B-final V4
- Structure residuals: 2 severe / 3 soft
- Contrastive evidence: all 5 structure outliers plus 3 matched normal samples
- Analysis model: `claude-sonnet-5`

## Analysis attempts

### Attempt 1

The analyzer produced:

- 2 validated preferences
- 2 localized candidate rules
- average rule confidence: 0.66
- 0 feasible rules

Both raw rules passed the safety, evidence-count, normal-control, confidence,
and injection-target checks. Both were rejected because their proposed scoring
adjustments contained hard percentage anchors (`70%-85%` / `85%`), while the E
route explicitly prohibits fixed numerical thresholds and percentage-based
score floors.

### Attempt 2

The strengthened prompt was used, but the response stopped at the 4096-token
output limit after spending most of the budget on model thinking. The JSON was
truncated and was not repaired or admitted.

### Attempt 3

The E analysis output budget was raised to 8192. The analyzer produced two
feasible qualitative rules with no numeric anchors. The higher-confidence rule
was injected provisionally:

> Evaluate paragraph arrangement and opening/closing structure independently
> from language errors; do not infer poor structure solely from poor language.

The 13-sample micro gate had a positive weighted average improvement of
`0.0476`, above the `0.01` aggregate threshold, but failed two hard guards:

- Normal control index 41: structure absolute error regressed from 0 to 1.
- Severe B/outlier index 18: structure did not improve, while content and
  expression each regressed by 1; B-bias magnitude worsened from 2.500 to
  3.167, a regression of 0.667 beyond the permitted 0.5.

## Cost containment

The final retry stopped immediately after the micro gate and therefore avoided:

- the 5-badcase + 6-normal regular gate;
- 36-essay full-train scoring;
- 12-essay validation scoring.

The accepted retry used one successful contrastive-analysis request and 13
successful micro-scoring requests. Failed TLS attempts were retried without
creating duplicate result rows.

## Decision

This is a rejected structure iteration, not an infrastructure failure. The
numeric-anchor failure is retained in `contrastive_consolidated_1.json`; the
truncated attempt is retained in `contrastive_consolidated_2.json`; and the
micro-rejected qualitative rule is retained in
`contrastive_consolidated_3.json` plus
`micro_scoring_eval_structure_attempt3.json`.

The prompt hardening worked, so it should remain. The rule itself should not be
retried unchanged: it improved the aggregate slightly but destabilized a normal
control and a severe B case.
