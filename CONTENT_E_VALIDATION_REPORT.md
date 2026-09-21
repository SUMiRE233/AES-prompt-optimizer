> 历史状态：V4 时代 E 类探索证据。当前正式 B baseline 已升级为 V6。
> 本文中的残差数量、候选规则、阈值结果和晋升结论不得直接复用于 V6；
> 仅保留失败模式、事务回滚和门禁设计证据。

# Content E Validation — Brief Report

Date: 2026-09-19

## Outcome

The content candidate passed the evidence micro gate but failed the regular
target/control gate. It was deleted, its pending rule transaction was rolled
back, and the B-final V4 prompt was not changed.

This candidate does **not** enter human annotation or human acceptance.

## Assumption under test

The run retained the current E-route hypothesis:

- derive a narrow qualitative E rule from residual errors under the frozen
  B-final V4 prompt;
- use one rule and one dimension per sub-iteration;
- prohibit hard numerical score anchors;
- require the candidate to pass automated regression gates before any human
  review or final-prompt promotion.

The provisional content rule targeted essays dominated by repetitive or
generic statements without concrete events, examples, or causal detail. The
rule explicitly excluded short but specific and logically complete essays.

## Gate results

| Layer | Requirement | Result | Decision |
|---|---|---|---|
| 1. Evidence micro gate | weighted improvement at least `+0.01`; zero hard violations | `+0.1858`; 0 violations across 17 samples | Pass |
| 2. Regular target/control gate | at least 3/6 badcases improve; 0 badcases worsen; at most 1 normal target regression; at most 1 cross-dimension regression | 4/6 improved; 0 worsened; 2 normal regressions; 5 cross-dimension regressions | **Fail** |
| 3. Full training regression gate | preserve B score and reduce weighted target-E errors | Not run after Layer 2 rejection | No evidence |
| 4. Validation/holdout guard | preserve validation B severe = 0 and B Score <= 3, plus target-dimension guard | Not run after Layer 2 rejection | No evidence |
| 5. Human acceptance | Layers 1–4 must all pass | Preconditions not met | **Do not enter** |

## Failure pattern

The content rule showed useful local signal, but it did not generalize safely:

- badcase indices 0 and 34 each acquired an expression-error regression of 1;
- normal index 20 acquired an expression-error regression of 1;
- normal indices 38 and 14 each regressed by 1 on both content and expression;
- therefore the candidate exceeded both permitted counts: 2 normal target
  regressions instead of at most 1, and 5 cross-dimension regressions instead
  of at most 1.

The failure is not an infrastructure failure. TLS interruptions were retried,
all 17 micro samples and all 12 regular-gate samples completed successfully,
and the rejection was produced by the declared gate criteria.

## Acceptance comparison

The E-route acceptance plan requires every automated layer to pass before a
candidate is sent to human review. The project completion standard also
requires claims to be supported by reproducible evaluations and prohibits
claiming stable improvement from a single model run. This run supports a real
gate-and-rollback claim, but it does not support a claim that the content rule
improves the final scorer.

Therefore:

1. keep B-final V4 frozen;
2. retain this candidate as a documented failure example;
3. do not annotate or promote this candidate;
4. do not rerun the unchanged rule—the next E attempt, if any, needs a narrower
   trigger or an explicit boundary that prevents expression-score coupling.

## Integrity checks

- `final_prompt_meta.md` is byte-identical to `optimized_prompt4_meta.md`.
- `final_prompt.md` is byte-identical to `optimized_prompt4.md`.
- provisional `injected_prompt_next` artifacts no longer exist.
- the runner was invoked without `--promote-final`.
