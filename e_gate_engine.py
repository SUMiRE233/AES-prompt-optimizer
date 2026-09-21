"""Gate math for E-route Phase 2 (approved policy 2026-09-21).

Pure functions over scoring rows plus the frozen candidate manifest. No file
writes, no network. Every threshold comes from ``decision_thresholds`` only.

Delta definition (per sample ``i``, dimension ``d``; both sides use double-eval
per-essay per-dimension means when a gate is at the double-eval stage):

    delta_abs_error(i,d) =
        |AI_candidate(i,d) - Teacher(i,d)| - |AI_baseline(i,d) - Teacher(i,d)|

    clear_improvement : delta <= -1.0
    clear_regression  : delta >= +1.0
    first-pass catastrophic regression: delta >= +2.0 (any sample, any dimension)

Contribution model (sample-level pools):

    U_target = mean over E-target samples of (-TARGET_WEIGHT * delta(target dim))
    U_other  = mean over all scored samples of
               (-NON_TARGET_WEIGHT * mean of delta over the non-target dims)
    U_B      = mean over B-consensus samples of
               (-penalty_weight * max(0, delta_B)),
               penalty_weight = 2/3 (Severe) or 1/3 (Soft); no positive reward

B bias magnitude uses the frozen B definition:
    m_B = |(diff_content + diff_expression + diff_structure) / 3|,
    diff_d = AI_d - Teacher_d ;  delta_B = m_B(candidate) - m_B(baseline)
"""

from typing import Any, Dict, Iterable, List, Optional, Tuple

from decision_thresholds import (
    B_SEVERE_PENALTY_WEIGHT,
    B_SOFT_PENALTY_WEIGHT,
    FIRST_PASS_CATASTROPHIC_DELTA,
    FULL_TRAIN_ALLOWED_CROSS_DIM_CLEAR_REGRESSIONS,
    FULL_TRAIN_B_Q_MAX,
    FULL_TRAIN_B_SEVERE_MAX,
    MICRO_MIN_U,
    NON_TARGET_WEIGHT,
    REGULAR_ALLOWED_CLEAR_REGRESSIONS,
    TARGET_WEIGHT,
    VALIDATION_ALLOWED_CLEAR_REGRESSIONS,
    VALIDATION_B_MEAN_SEVERE_MAX,
    VALIDATION_B_Q_MAX,
    is_clear_improvement,
    is_clear_regression,
    q_score,
    required_improvement_count,
)
from e_consensus_miner import b_run_counts, e_consensus

DIMENSIONS: Tuple[str, ...] = ("content", "expression", "structure")
TARGET_DIM = "structure"
NON_TARGET_DIMS: Tuple[str, ...] = tuple(d for d in DIMENSIONS if d != TARGET_DIM)

B_ROLE_PREFIX = "b_consensus_"
SEVERE_ROLE = B_ROLE_PREFIX + "severe"
SOFT_ROLE = B_ROLE_PREFIX + "soft"


# ============================================================
# Loading and alignment helpers
# ============================================================
def index_map(rows: List[Dict[str, Any]], label: str = "rows") -> Dict[int, Dict[str, Any]]:
    by_index: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        index = int(row["index"])
        if index in by_index:
            raise ValueError(f"Duplicate index {index} in {label}")
        by_index[index] = row
    return by_index


def mean_rows(
    run1: List[Dict[str, Any]], run2: List[Dict[str, Any]], label: str = "candidate"
) -> List[Dict[str, Any]]:
    """Per-essay per-dimension arithmetic mean of AI scores (run1 order)."""
    by1 = index_map(run1, f"{label} run1")
    by2 = index_map(run2, f"{label} run2")
    if set(by1) != set(by2):
        raise ValueError(f"{label}: run1/run2 index sets differ")
    rows = []
    for row in run1:
        index = int(row["index"])
        other = by2[index]
        rows.append(
            {
                "index": index,
                "name": row.get("name"),
                "page": row.get("page"),
                "essay": row.get("essay"),
                "teacher": dict(row["teacher"]),
                "AI": {
                    dim: (float(row["AI"][dim]) + float(other["AI"][dim])) / 2
                    for dim in DIMENSIONS
                },
            }
        )
    return rows


def delta_abs_error(candidate_row: Dict[str, Any], baseline_row: Dict[str, Any], dim: str) -> float:
    teacher = float(baseline_row["teacher"][dim])
    candidate_error = abs(float(candidate_row["AI"][dim]) - teacher)
    baseline_error = abs(float(baseline_row["AI"][dim]) - teacher)
    return candidate_error - baseline_error


def bias_magnitude(row: Dict[str, Any]) -> float:
    """Frozen B bias magnitude m_B = |mean(diffs)| over the three dimensions."""
    diffs = [float(row["AI"][dim]) - float(row["teacher"][dim]) for dim in DIMENSIONS]
    return abs(sum(diffs) / len(diffs))


def classify_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Split the frozen manifest samples by evidence role."""
    targets: List[int] = []
    controls: List[int] = []
    b_roles: Dict[int, str] = {}
    cross: Dict[str, List[int]] = {"content": [], "expression": []}
    all_indices: List[int] = []
    for sample in manifest["samples"]:
        index = int(sample["global_index"])
        all_indices.append(index)
        roles = set(sample.get("evidence_roles", []))
        if "structure_e_target" in roles:
            targets.append(index)
        if "normal_control" in roles:
            controls.append(index)
        if SEVERE_ROLE in roles:
            b_roles[index] = "severe"
        elif SOFT_ROLE in roles:
            b_roles[index] = "soft"
        for dim in ("content", "expression"):
            if f"cross_dim_{dim}" in roles:
                cross[dim].append(index)
    if len(set(all_indices)) != len(all_indices):
        raise ValueError("Manifest contains duplicate global indices")
    return {
        "targets": sorted(targets),
        "controls": sorted(controls),
        "b_roles": dict(sorted(b_roles.items())),
        "cross": cross,
        "all_indices": sorted(all_indices),
    }


def _require_indices(rows_by_index: Dict[int, Dict[str, Any]], indices: Iterable[int], label: str) -> None:
    missing = [index for index in indices if index not in rows_by_index]
    if missing:
        raise ValueError(f"{label}: missing indices {missing}")


# ============================================================
# Gate 1a: first pass (single run1 vs V6 run1)
# ============================================================
def first_pass_check(
    candidate_run1: List[Dict[str, Any]],
    baseline_run1: List[Dict[str, Any]],
    indices: Iterable[int],
) -> Dict[str, Any]:
    baseline = index_map(baseline_run1, "baseline run1")
    candidate = index_map(candidate_run1, "candidate run1")
    _require_indices(candidate, indices, "candidate run1")
    violations = []
    for index in sorted(indices):
        for dim in DIMENSIONS:
            delta = delta_abs_error(candidate[index], baseline[index], dim)
            if delta >= FIRST_PASS_CATASTROPHIC_DELTA:
                violations.append({"index": index, "dimension": dim, "delta": delta})
    return {
        "rule": f"reject when any (sample, dim) delta >= +{FIRST_PASS_CATASTROPHIC_DELTA}",
        "passed": not violations,
        "violations": violations,
    }


# ============================================================
# Gate 1b: micro (double-eval means)
# ============================================================
def _b_delta_details(
    baseline: Dict[int, Dict[str, Any]],
    candidate: Dict[int, Dict[str, Any]],
    b_roles: Dict[int, str],
) -> List[Dict[str, Any]]:
    details = []
    for index, severity in sorted(b_roles.items()):
        delta_b = bias_magnitude(candidate[index]) - bias_magnitude(baseline[index])
        weight = B_SEVERE_PENALTY_WEIGHT if severity == "severe" else B_SOFT_PENALTY_WEIGHT
        penalty = max(0.0, delta_b)
        details.append(
            {
                "index": index,
                "severity": severity,
                "delta_b": delta_b,
                "penalty_weight": weight,
                "u_b": -weight * penalty,
            }
        )
    return details


def micro_evaluate(
    manifest: Dict[str, Any],
    baseline_mean: List[Dict[str, Any]],
    candidate_mean: List[Dict[str, Any]],
) -> Dict[str, Any]:
    roles = classify_manifest(manifest)
    baseline = index_map(baseline_mean, "baseline mean")
    candidate = index_map(candidate_mean, "candidate mean")
    indices = roles["all_indices"]
    _require_indices(candidate, indices, "candidate mean")

    u_target_entries = []
    for index in roles["targets"]:
        delta = delta_abs_error(candidate[index], baseline[index], TARGET_DIM)
        contribution = -TARGET_WEIGHT * delta
        u_target_entries.append({"index": index, "delta": delta, "u": contribution})

    u_other_entries = []
    for index in indices:
        dim_deltas = {
            dim: delta_abs_error(candidate[index], baseline[index], dim) for dim in NON_TARGET_DIMS
        }
        mean_delta = sum(dim_deltas.values()) / len(NON_TARGET_DIMS)
        contribution = -NON_TARGET_WEIGHT * mean_delta
        u_other_entries.append(
            {"index": index, "deltas": dim_deltas, "mean_delta": mean_delta, "u": contribution}
        )

    b_entries = _b_delta_details(baseline, candidate, roles["b_roles"])

    def pool_mean(entries: List[Dict[str, Any]], key: str = "u") -> float:
        if not entries:
            return 0.0
        return sum(item[key] for item in entries) / len(entries)

    u_target = pool_mean(u_target_entries)
    u_other = pool_mean(u_other_entries)
    u_b = pool_mean(b_entries, "u_b")
    total_u = u_target + u_other + u_b

    improvements = [
        entry["index"] for entry in u_target_entries if is_clear_improvement(entry["delta"])
    ]

    violations: List[Dict[str, Any]] = []
    for entry in u_target_entries:
        if is_clear_regression(entry["delta"]):
            violations.append(
                {"type": "e_target_clear_regression", "index": entry["index"], "dimension": TARGET_DIM, "delta": entry["delta"]}
            )
    for index in roles["controls"]:
        delta = delta_abs_error(candidate[index], baseline[index], TARGET_DIM)
        if is_clear_regression(delta):
            violations.append(
                {"type": "normal_control_clear_regression", "index": index, "dimension": TARGET_DIM, "delta": delta}
            )
    for index in indices:
        for dim in NON_TARGET_DIMS:
            delta = delta_abs_error(candidate[index], baseline[index], dim)
            if is_clear_regression(delta):
                violations.append(
                    {"type": "cross_dim_clear_regression", "index": index, "dimension": dim, "delta": delta}
                )
    for entry in b_entries:
        if entry["delta_b"] >= 1.0:
            violations.append(
                {"type": "b_clear_regression", "index": entry["index"], "delta_b": entry["delta_b"]}
            )

    passed = bool(total_u > MICRO_MIN_U and improvements and not violations)
    return {
        "rule": (
            f"U = mean(U_target) + mean(U_other) + mean(U_B) > {MICRO_MIN_U}; "
            ">=1 E-target clear improvement; zero clear regressions "
            "(E target / normal control / cross dim / B)"
        ),
        "passed": passed,
        "u": {
            "target": u_target,
            "other": u_other,
            "b": u_b,
            "total": total_u,
        },
        "u_target_entries": u_target_entries,
        "u_other_entries": u_other_entries,
        "u_b_entries": b_entries,
        "improvements": improvements,
        "violations": violations,
        "allowed_clear_regressions": 0,
    }


# ============================================================
# Gate 2: regular target/control (same 17-sample set)
# ============================================================
def regular_evaluate(
    manifest: Dict[str, Any],
    baseline_mean: List[Dict[str, Any]],
    candidate_mean: List[Dict[str, Any]],
) -> Dict[str, Any]:
    roles = classify_manifest(manifest)
    baseline = index_map(baseline_mean, "baseline mean")
    candidate = index_map(candidate_mean, "candidate mean")
    indices = roles["all_indices"]
    _require_indices(candidate, indices, "candidate mean")

    target_deltas = {
        index: delta_abs_error(candidate[index], baseline[index], TARGET_DIM)
        for index in roles["targets"]
    }
    improvements = [
        index for index, delta in sorted(target_deltas.items()) if is_clear_improvement(delta)
    ]
    required = required_improvement_count(len(roles["targets"]))

    target_regressions = [
        {"index": index, "delta": delta}
        for index, delta in sorted(target_deltas.items())
        if is_clear_regression(delta)
    ]
    control_regressions = []
    for index in roles["controls"]:
        delta = delta_abs_error(candidate[index], baseline[index], TARGET_DIM)
        if is_clear_regression(delta):
            control_regressions.append({"index": index, "delta": delta})
    cross_regressions = []
    for index in indices:
        for dim in NON_TARGET_DIMS:
            delta = delta_abs_error(candidate[index], baseline[index], dim)
            if is_clear_regression(delta):
                cross_regressions.append({"index": index, "dimension": dim, "delta": delta})
    b_entries = _b_delta_details(baseline, candidate, roles["b_roles"])
    b_regressions = [entry for entry in b_entries if entry["delta_b"] >= 1.0]

    budget = REGULAR_ALLOWED_CLEAR_REGRESSIONS
    passed = bool(
        len(improvements) >= required
        and len(target_regressions) <= budget["e_target_target_dim"]
        and len(control_regressions) <= budget["normal_control_target_dim"]
        and len(cross_regressions) <= budget["any_sample_cross_dim"]
        and not b_regressions
    )
    return {
        "rule": (
            f"default behaviour: >= {required} clear improvements among the "
            f"{len(roles['targets'])} E targets; clear-regression budgets "
            f"target<=0 / control<=1 / cross<=1; B delta_B >= 1.0 rejects"
        ),
        "passed": passed,
        "improvements": improvements,
        "required_improvements": required,
        "target_regressions": target_regressions,
        "control_regressions": control_regressions,
        "cross_dim_regressions": cross_regressions,
        "b_entries": b_entries,
        "b_regressions": [entry["index"] for entry in b_regressions],
    }


# ============================================================
# Gate 3: full train (36 essays, double-eval means where applicable)
# ============================================================
def _e_member_map(consensus: Dict[str, Any], dim: str) -> Dict[int, str]:
    members: Dict[int, str] = {}
    for entry in consensus["by_dim"][dim]["severe"]:
        members[int(entry["index"])] = "severe"
    for entry in consensus["by_dim"][dim]["soft"]:
        members[int(entry["index"])] = "soft"
    return members


def full_train_evaluate(
    baseline_run1: List[Dict[str, Any]],
    baseline_run2: List[Dict[str, Any]],
    baseline_consensus: Dict[str, Any],
    candidate_run1: List[Dict[str, Any]],
    candidate_run2: List[Dict[str, Any]],
    candidate_consensus: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    baseline_mean = mean_rows(baseline_run1, baseline_run2, "baseline train")
    candidate_mean = mean_rows(candidate_run1, candidate_run2, "candidate train")
    baseline = index_map(baseline_mean, "baseline train mean")
    candidate = index_map(candidate_mean, "candidate train mean")
    indices = sorted(baseline)
    _require_indices(candidate, indices, "candidate train mean")

    if candidate_consensus is None:
        candidate_consensus = e_consensus(candidate_run1, candidate_run2)

    # Target dimension requirement (frozen outline): Severe decreases, or
    # Severe stays equal and Soft decreases.
    base_target = baseline_consensus["counts"][TARGET_DIM]
    cand_target = candidate_consensus["counts"][TARGET_DIM]
    target_ok = bool(
        cand_target["severe"] < base_target["severe"]
        or (
            cand_target["severe"] == base_target["severe"]
            and cand_target["soft"] < base_target["soft"]
        )
    )

    # B route-metric guards (mean of per-run counts; Q-mean-2.5-v1 caliber).
    b1 = b_run_counts(candidate_run1)
    b2 = b_run_counts(candidate_run2)
    mean_severe = (b1["severe"] + b2["severe"]) / 2
    mean_soft = (b1["soft"] + b2["soft"]) / 2
    q_candidate = q_score(mean_severe, mean_soft)
    b_ok = mean_severe <= FULL_TRAIN_B_SEVERE_MAX and q_candidate <= FULL_TRAIN_B_Q_MAX

    # Non-target dimensions: no new E consensus members, no Soft -> Severe upgrade.
    new_e: List[Dict[str, Any]] = []
    upgrades: List[Dict[str, Any]] = []
    for dim in NON_TARGET_DIMS:
        base_members = _e_member_map(baseline_consensus, dim)
        cand_members = _e_member_map(candidate_consensus, dim)
        for index, severity in sorted(cand_members.items()):
            if index not in base_members:
                new_e.append({"index": index, "dimension": dim, "severity": severity})
            elif base_members[index] == "soft" and severity == "severe":
                upgrades.append({"index": index, "dimension": dim})
    e_ok = not new_e and not upgrades

    # Cross-dimension clear regressions on the double-eval means.
    cross_regressions = []
    for index in indices:
        for dim in NON_TARGET_DIMS:
            delta = delta_abs_error(candidate[index], baseline[index], dim)
            if is_clear_regression(delta):
                cross_regressions.append({"index": index, "dimension": dim, "delta": delta})
    cross_ok = len(cross_regressions) <= FULL_TRAIN_ALLOWED_CROSS_DIM_CLEAR_REGRESSIONS

    mae_baseline = _mae(baseline_mean)
    mae_candidate = _mae(candidate_mean)

    passed = bool(target_ok and b_ok and e_ok and cross_ok)
    return {
        "rule": (
            "structure Severe down, or equal & Soft down; B mean Severe <= 3.0; "
            "B Q <= 14.0; no new non-target E consensus; no Soft->Severe upgrade; "
            "cross-dim clear regressions == 0"
        ),
        "passed": passed,
        "target": {
            "baseline": base_target,
            "candidate": cand_target,
            "passed": target_ok,
        },
        "b": {
            "candidate_run_counts": {"run1": b1, "run2": b2},
            "mean_severe": mean_severe,
            "mean_soft": mean_soft,
            "q": q_candidate,
            "limits": {"mean_severe_max": FULL_TRAIN_B_SEVERE_MAX, "q_max": FULL_TRAIN_B_Q_MAX},
            "passed": b_ok,
        },
        "non_target_e": {"new_members": new_e, "soft_to_severe": upgrades, "passed": e_ok},
        "cross_dim_regressions": cross_regressions,
        "cross_dim_passed": cross_ok,
        "mae_record": {
            "baseline": mae_baseline,
            "candidate": mae_candidate,
            "warning": mae_candidate > mae_baseline,
        },
    }


def _mae(rows: List[Dict[str, Any]]) -> float:
    total = 0.0
    count = 0
    for row in rows:
        for dim in DIMENSIONS:
            total += abs(float(row["AI"][dim]) - float(row["teacher"][dim]))
            count += 1
    return total / count if count else 0.0


# ============================================================
# Gate 4: validation regression guard (12 essays, double-eval mean)
# ============================================================
def validation_evaluate(
    baseline_run1: List[Dict[str, Any]],
    baseline_run2: List[Dict[str, Any]],
    candidate_run1: List[Dict[str, Any]],
    candidate_run2: List[Dict[str, Any]],
) -> Dict[str, Any]:
    baseline_mean = mean_rows(baseline_run1, baseline_run2, "baseline validation")
    candidate_mean = mean_rows(candidate_run1, candidate_run2, "candidate validation")
    baseline = index_map(baseline_mean, "baseline validation mean")
    candidate = index_map(candidate_mean, "candidate validation mean")
    indices = sorted(baseline)
    _require_indices(candidate, indices, "candidate validation mean")

    b1 = b_run_counts(candidate_run1)
    b2 = b_run_counts(candidate_run2)
    mean_severe = (b1["severe"] + b2["severe"]) / 2
    mean_soft = (b1["soft"] + b2["soft"]) / 2
    q_candidate = q_score(mean_severe, mean_soft)
    b_ok = mean_severe <= VALIDATION_B_MEAN_SEVERE_MAX and q_candidate <= VALIDATION_B_Q_MAX

    regressions = []
    for index in indices:
        for dim in DIMENSIONS:
            delta = delta_abs_error(candidate[index], baseline[index], dim)
            if is_clear_regression(delta):
                regressions.append({"index": index, "dimension": dim, "delta": delta})
    regression_ok = len(regressions) <= VALIDATION_ALLOWED_CLEAR_REGRESSIONS

    mae_baseline = _mae(baseline_mean)
    mae_candidate = _mae(candidate_mean)

    passed = bool(b_ok and regression_ok)
    return {
        "rule": (
            "B mean Severe <= 1.5; B Q <= 4.75; clear regressions (all dims) == 0"
        ),
        "passed": passed,
        "b": {
            "candidate_run_counts": {"run1": b1, "run2": b2},
            "mean_severe": mean_severe,
            "mean_soft": mean_soft,
            "q": q_candidate,
            "limits": {
                "mean_severe_max": VALIDATION_B_MEAN_SEVERE_MAX,
                "q_max": VALIDATION_B_Q_MAX,
            },
            "passed": b_ok,
        },
        "clear_regressions": regressions,
        "regression_passed": regression_ok,
        "mae_record": {
            "baseline": mae_baseline,
            "candidate": mae_candidate,
            "warning": mae_candidate > mae_baseline,
        },
    }
