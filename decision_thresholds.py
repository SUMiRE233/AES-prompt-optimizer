"""Single shared decision thresholds for the E-route gates (Phase 1 unification).

The E-route supplementary guidance (section 4.5) unifies every
improvement/regression decision onto one boundary so the former
inconsistencies cannot reappear:

    micro gate : flagged only "improvement < -1.0" (boundary exclusive)
    runner     : counted "delta > 0.5"
    badcase    : counted "delta > 0.0"

Unified rule (all gate call sites MUST use these helpers):

    delta_abs_error = candidate_abs_error - baseline_abs_error
    clear_improvement : delta_abs_error <= -1.0
    clear_regression  : delta_abs_error >= +1.0

Anything in between is neutral (measurement noise) and must not be counted as
either an improvement or a regression.

This module is offline-only: it reads no environment variables, performs no
network access, and has no third-party dependencies.
"""

from typing import Literal

import math

# ---------------------------------------------------------------
# Unified boundary (E-route supplementary guidance section 4.5)
# ---------------------------------------------------------------
CLEAR_IMPROVEMENT_DELTA = -1.0
CLEAR_REGRESSION_DELTA = 1.0

# ---------------------------------------------------------------
# Global B-regression objective (B protocol "Q-mean-2.5-v1").
# Mirrors pipeline_entry.Q_SEVERE_WEIGHT; a test asserts the two
# constants stay equal so the E gates cannot drift from the frozen
# B protocol.
# ---------------------------------------------------------------
Q_SEVERE_WEIGHT = 2.5

# ---------------------------------------------------------------
# Layered budgets: allowed counts of clear regressions per stage
# (thresholds are uniform; only the tolerated counts differ).
# ---------------------------------------------------------------
MICRO_ALLOWED_CLEAR_REGRESSIONS = {
    "e_target_target_dim": 0,
    "normal_control_target_dim": 0,
    "any_sample_cross_dim": 0,
}
REGULAR_ALLOWED_CLEAR_REGRESSIONS = {
    "e_target_target_dim": 0,
    "normal_control_target_dim": 1,
    "any_sample_cross_dim": 1,
}

# ---------------------------------------------------------------
# Approved E gate policy (project owner, 2026-09-21).
# Recorded in e_protocol_manifest.json; every gate call site must use
# these constants instead of private numbers.
# ---------------------------------------------------------------
FIRST_PASS_CATASTROPHIC_DELTA = 2.0
MICRO_MIN_U = 0.01

# Contribution weights (sample-level E pools; B is penalty-only).
TARGET_WEIGHT = 1.0
NON_TARGET_WEIGHT = 1.0 / 3.0
B_SOFT_PENALTY_WEIGHT = 1.0 / 3.0
B_SEVERE_PENALTY_WEIGHT = 2.0 / 3.0

# Regular gate default behaviour: rate 0.5 with ceil (4 targets -> 2).
REGULAR_REQUIRED_IMPROVE_RATE = 0.5

# Full-train B route-metric guards (Q-mean-2.5-v1 caliber).
FULL_TRAIN_B_SEVERE_MAX = 3.0
FULL_TRAIN_B_Q_MAX = 14.0
FULL_TRAIN_ALLOWED_CROSS_DIM_CLEAR_REGRESSIONS = 0

# Validation guards (double-eval mean caliber).
VALIDATION_B_MEAN_SEVERE_MAX = 1.5
VALIDATION_B_Q_MAX = 4.75
VALIDATION_ALLOWED_CLEAR_REGRESSIONS = 0

Decision = Literal["clear_improvement", "neutral", "clear_regression"]


def classify_delta_abs_error(delta_abs_error: float) -> Decision:
    """Classify a candidate-minus-baseline absolute-error delta."""
    if delta_abs_error <= CLEAR_IMPROVEMENT_DELTA:
        return "clear_improvement"
    if delta_abs_error >= CLEAR_REGRESSION_DELTA:
        return "clear_regression"
    return "neutral"


def is_clear_improvement(delta_abs_error: float) -> bool:
    """True when the delta counts as a clear improvement (<= -1.0)."""
    return delta_abs_error <= CLEAR_IMPROVEMENT_DELTA


def is_clear_regression(delta_abs_error: float) -> bool:
    """True when the delta counts as a clear regression (>= +1.0)."""
    return delta_abs_error >= CLEAR_REGRESSION_DELTA


def q_score(severe: float, soft: float) -> float:
    """Global B-regression objective Q = 2.5 * severe + soft."""
    return Q_SEVERE_WEIGHT * severe + soft


def required_improvement_count(
    sample_count: int, minimum_rate: float = REGULAR_REQUIRED_IMPROVE_RATE
) -> int:
    """Default behaviour: ceil(count * rate), at least 1 (4 targets -> 2)."""
    if sample_count <= 0:
        return 0
    return max(1, math.ceil(sample_count * minimum_rate))
