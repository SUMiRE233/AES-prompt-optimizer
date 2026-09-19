# E-route Validation Plan after B Final

Date: 2026-09-19

## Frozen baseline

- Baseline prompt: B-route final V4 (`final_prompt_meta.md` / `final_prompt.md`)
- Baseline training scores: `final_train_scoring_results.json`
- Baseline badcases: `final_aes_badcases.json`
- B acceptance baseline on validation: 0 severe / 3 soft, weighted Score 3
- E changes must not silently replace the B final. Promotion remains an explicit
  final step after every automated gate passes.

## Residual situation

| Dimension | Severe | Soft | Direction | B-overlap | Route decision |
|---|---:|---:|---|---:|---|
| content | 0 | 9 | 3 strict / 6 lenient | 4 | second priority |
| expression | 0 | 0 | none | 0 | do not iterate |
| structure | 2 | 3 | 2 strict / 3 lenient | 2 | first priority |

There are ten unique E samples across content and structure. Four samples occur
in both dimensions. Both severe structure residuals are also severe B cases,
so improving structure without reopening B bias is the primary risk.

## Iteration order

1. Run one structure rule only.
2. If it passes and is promoted, regenerate the full baseline scoring and
   badcases before considering content.
3. Run one content rule only against that refreshed baseline.
4. Do not create an expression rule unless a later promoted candidate produces
   real expression E cases.

Each sub-iteration injects at most one rule and one dimension. A rejected rule
is preserved in its evaluation report but is not committed or promoted.

## Validation layers

### Layer 1: evidence micro gate

Score the rule's contrastive outliers and normal evidence together with every
current B badcase. Reject immediately if any of the following occurs:

- an E outlier worsens on the target dimension;
- a normal target case worsens by more than 0.5;
- any single dimension worsens by more than 1.0;
- B-bias magnitude worsens by more than 0.5;
- the weighted improvement is not positive.

This is a cheap rule-level rejection gate, not evidence of generalization.

### Layer 2: regular target/control gate

For structure, include all five E badcases plus six low-error normal controls.
At least 3/5 E badcases must improve by 0.5 or more. No E badcase may worsen;
at most one normal may regress by more than 0.5; and at most one cross-dimension
regression greater than 0.5 is allowed.

For content, sample six of the nine E badcases with direction coverage and six
normal controls. At least 3/6 must improve. Because the current sampler sorts
by severity rather than direction, direction-stratified selection should be
added before executing the content iteration.

The required-improvement calculation uses `ceil(n * rate)`, so 50% of five
means three cases rather than two.

### Layer 3: full training-set regression gate

Only after Layers 1 and 2 pass, score all 36 training essays and remine B/E.
Require all of the following:

- target E severe count does not increase;
- target E weighted count (`5 * severe + soft`) decreases;
- B weighted Score does not exceed the V4 training baseline of 32;
- no new E dimension appears solely because of the injected rule;
- MAE is recorded only; an increase emits a warning but does not decide.

The current runner can create the full-train scoring artifact but does not yet
enforce this aggregate Layer-3 decision. Add that evaluator before executing a
real E promotion.

### Layer 4: validation/holdout guard

After the full-training gate passes, score the 12 validation essays once.
The candidate must retain zero severe B cases and a B Score no greater than 3.
For the target dimension, compare absolute error on the predeclared challenge
and control samples; do not rediscover a rule from this split. Record MAE and
warn on rebound, but keep it outside the decision rule.

Because this split already selected B V4, it is a regression guard rather than
independent test evidence.

### Layer 5: human acceptance and promotion

Automated success produces a candidate and compact evaluation report. Human
review checks whether the injected rule is semantically narrow, non-duplicative,
and faithful to the scoring rubric. Only then may `--promote-final` replace the
stable prompt.

## Required implementation before the first E API run

- Add the Layer-3 aggregate B/E evaluator.
- Add the Layer-4 validation B guard.
- Make `gate-and-train` evaluate the regular gate before spending a full-train
  scoring run.
- Add direction-stratified content sampling before the content iteration.
- Keep the structure-first run at `--max-rules-per-sub-iteration 1` and do not
  pass `--promote-final` during initial validation.
