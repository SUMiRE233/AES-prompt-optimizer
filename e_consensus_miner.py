"""Two-run consensus re-mining for the E route (Phase 1, offline only).

Implements the frozen supplementary rules (E 类惰性补全补充指导 section 2.1/2.2):

E-consensus-robust-z-v1
    delta(i,d,r)    = AI(i,d,r) - Teacher(i,d)
    median(d,r)     = median_i delta(i,d,r)
    residual(i,d,r) = delta(i,d,r) - median(d,r)
    z(i,d,r)        = residual(i,d,r) / (1.4826 * MAD(d,r) + epsilon)

    E sample (per essay i, dimension d):
        z(i,d,1) * z(i,d,2) > 0                        (same direction in both runs)
        abs(mean(delta(i,d,1), delta(i,d,2))) >= 1.0   (business floor)

    severity:
        Severe: abs(mean(z1, z2)) > 2.5
        Soft:   1.5 < abs(mean(z1, z2)) <= 2.5

B consensus (b_consensus_samples)
    Membership and severity come from the frozen B rule applied to the
    two-run evidence (severe: abs(B) > 1.5 and consistency >= 3;
    soft: abs(B) > 1.0 and consistency >= 2).

    Adopted rule "mean_and_rerun_conservative" - an operational adjudication
    (NOT a natural symmetric statistical consensus):
        members  = essays flagged on the two-run mean profile
                   (final_train_scoring_results_mean.json)
        severity = Severe only when the mean profile AND the rerun both
                   flag the essay as Severe, otherwise Soft

    Rationale (reviewer ruling 2026-09-21): run2 plays the review/verification
    run in this project's precision-upgrade timeline, which is why it - not
    run1 - re-confirms Severe flags. It reproduces the guidance expectation
    2/6 (Severe={46,47}, Soft={13,15,16,18,22,28}). The five other variants
    remain auditable via b_severity_variants() for archiving only; they must
    not drive sample roles or gates.

The old single-run miner (aes_badcase_miner.py) is not modified. Its
statistical helpers are mirrored here with the same epsilon handling so the
two-run logic stays compatible with the historical semantics.

CLI (offline; no network, no credentials):
    python e_consensus_miner.py [--train-run1 PATH] [--train-run2 PATH]
                                [--mean-profile PATH] [--json-out PATH]
    (all B consensus variants are included in the JSON summary; the
     function-level audit table lives in b_severity_variants())
"""

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

DIMENSIONS: Tuple[str, ...] = ("content", "expression", "structure")

E_CONSENSUS_VERSION = "E-consensus-robust-z-v1"
CONSENSUS_MIN_ABS_MEAN_DELTA = 1.0
SEVERE_ABS_MEAN_Z = 2.5
SOFT_ABS_MEAN_Z_MIN = 1.5

# Frozen B rule constants (mirror of pipeline_entry; a test asserts equality).
B_SEVERE_ABS_BIAS = 1.5
B_SOFT_ABS_BIAS = 1.0
B_SEVERE_MIN_DIRECTION = 3
B_SOFT_MIN_DIRECTION = 2

B_CONSENSUS_RULE = "mean_and_rerun_conservative"
# Backward-compatible alias used by callers/tests.
B_CONSENSUS_SEVERITY_RULE = B_CONSENSUS_RULE

B_CONSENSUS_VARIANTS: Tuple[str, ...] = (
    "mean_and_rerun_conservative",
    "mean_and_run1_conservative",
    "mean_membership_only",
    "both_runs_mean_rule",
    "both_runs_conservative",
    "both_runs_either_severe",
)

DEFAULT_TRAIN_RUN1 = "train_scoring_results6.json"
DEFAULT_TRAIN_RUN2 = "train_scoring_results6_rerun.json"
DEFAULT_MEAN_PROFILE = "final_train_scoring_results_mean.json"


# ============================================================
# Loading and two-run alignment
# ============================================================
def load_rows(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        rows = json.load(f)
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"Scoring file is empty or not a list: {path}")
    return rows


def index_map(rows: List[Dict[str, Any]], source: str) -> Dict[int, Dict[str, Any]]:
    by_index: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        index = int(row["index"])
        if index in by_index:
            raise ValueError(f"Duplicate global index {index} in {source}")
        by_index[index] = row
    return by_index


def align_two_runs(
    run1: List[Dict[str, Any]],
    run2: List[Dict[str, Any]],
    run1_name: str = "run1",
    run2_name: str = "run2",
) -> Tuple[Dict[int, Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    """Align two runs by global index and validate identity invariants."""
    by1 = index_map(run1, run1_name)
    by2 = index_map(run2, run2_name)

    if set(by1) != set(by2):
        only1 = sorted(set(by1) - set(by2))
        only2 = sorted(set(by2) - set(by1))
        raise ValueError(
            f"Two-run index mismatch: only in {run1_name}={only1}, "
            f"only in {run2_name}={only2}"
        )

    for index in sorted(by1):
        teacher1 = by1[index]["teacher"]
        teacher2 = by2[index]["teacher"]
        for dim in DIMENSIONS:
            if float(teacher1[dim]) != float(teacher2[dim]):
                raise ValueError(
                    f"Teacher score mismatch at index {index} dimension {dim}: "
                    f"{teacher1[dim]} vs {teacher2[dim]}"
                )
    return by1, by2


# ============================================================
# Robust per-run statistics (mirror of aes_badcase_miner)
# ============================================================
def _safe_mad(abs_deviations: List[float]) -> float:
    mad = statistics.median(abs_deviations) if abs_deviations else 0.0
    return max(mad, 1e-6)


def run_dimension_stats(rows: List[Dict[str, Any]], dim: str) -> Dict[str, Dict[str, float]]:
    """Per-run median / MAD / robust-z for one dimension, keyed by index."""
    diffs = [float(row["AI"][dim]) - float(row["teacher"][dim]) for row in rows]
    median_diff = statistics.median(diffs)
    residuals = [d - median_diff for d in diffs]
    mad = _safe_mad([abs(r) for r in residuals])
    scale_factor = 1.4826 * mad + 1e-6
    stats: Dict[str, Dict[str, float]] = {}
    for row, delta, residual in zip(rows, diffs, residuals):
        stats[int(row["index"])] = {
            "delta": delta,
            "residual": residual,
            "z_score": residual / scale_factor,
        }
    return stats


# ============================================================
# E consensus: two-run same-direction residual anomalies
# ============================================================
def classify_mean_z(mean_z: float) -> str:
    """Severity band for the mean of the two robust z-scores."""
    abs_mean_z = abs(mean_z)
    if abs_mean_z > SEVERE_ABS_MEAN_Z:
        return "severe"
    if abs_mean_z > SOFT_ABS_MEAN_Z_MIN:
        return "soft"
    return "below_soft"


def e_consensus(run1: List[Dict[str, Any]], run2: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Two-run E consensus for all dimensions (per essay per dimension)."""
    by1, by2 = align_two_runs(run1, run2)

    stats1 = {dim: run_dimension_stats(run1, dim) for dim in DIMENSIONS}
    stats2 = {dim: run_dimension_stats(run2, dim) for dim in DIMENSIONS}

    result: Dict[str, Any] = {
        "version": E_CONSENSUS_VERSION,
        "min_abs_mean_delta": CONSENSUS_MIN_ABS_MEAN_DELTA,
        "severe_abs_mean_z": SEVERE_ABS_MEAN_Z,
        "soft_abs_mean_z_min": SOFT_ABS_MEAN_Z_MIN,
        "counts": {},
        "by_dim": {},
    }

    for dim in DIMENSIONS:
        severe: List[Dict[str, Any]] = []
        soft: List[Dict[str, Any]] = []
        below_soft = 0
        for index in sorted(by1):
            s1 = stats1[dim][index]
            s2 = stats2[dim][index]
            z1, z2 = s1["z_score"], s2["z_score"]
            delta1, delta2 = s1["delta"], s2["delta"]

            if z1 * z2 <= 0:
                continue
            mean_delta = (delta1 + delta2) / 2
            if abs(mean_delta) < CONSENSUS_MIN_ABS_MEAN_DELTA:
                continue

            mean_z = (z1 + z2) / 2
            severity = classify_mean_z(mean_z)
            if severity == "below_soft":
                below_soft += 1
                continue

            entry = {
                "index": index,
                "dimension": dim,
                "severity": severity,
                "direction": "lenient" if mean_z > 0 else "strict",
                "delta_run1": delta1,
                "delta_run2": delta2,
                "mean_delta": mean_delta,
                "residual_run1": s1["residual"],
                "residual_run2": s2["residual"],
                "z_run1": z1,
                "z_run2": z2,
                "mean_z": mean_z,
                "mean_abs_z": abs(mean_z),
            }
            (severe if severity == "severe" else soft).append(entry)

        result["by_dim"][dim] = {
            "severe": severe,
            "soft": soft,
            "below_soft_count": below_soft,
        }
        result["counts"][dim] = {"severe": len(severe), "soft": len(soft)}

    return result


# ============================================================
# B consensus: same-direction B flags in both runs
# ============================================================
def b_row_evidence(row: Dict[str, Any]) -> Dict[str, Any]:
    """Frozen B rule on one scoring row (overall score minuses)."""
    diffs = {dim: float(row["AI"][dim]) - float(row["teacher"][dim]) for dim in DIMENSIONS}
    bias_score = (diffs["content"] + diffs["expression"] + diffs["structure"]) / 3

    sign = 1 if bias_score > 0 else -1 if bias_score < 0 else 0
    consistency = 0
    if sign != 0:
        for dim in DIMENSIONS:
            if diffs[dim] * sign > 0:
                consistency += 1

    if bias_score > 0:
        direction = "lenient"
    elif bias_score < 0:
        direction = "strict"
    else:
        direction = "neutral"

    if abs(bias_score) > B_SEVERE_ABS_BIAS and consistency >= B_SEVERE_MIN_DIRECTION:
        severity = "severe"
    elif abs(bias_score) > B_SOFT_ABS_BIAS and consistency >= B_SOFT_MIN_DIRECTION:
        severity = "soft"
    else:
        severity = None

    return {
        "bias_score": bias_score,
        "direction": direction,
        "consistency": consistency,
        "severity": severity,
    }


def b_run_counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """Per-run B severe/soft counts (mirror of the frozen protocol counting)."""
    severe = soft = 0
    for row in rows:
        evidence = b_row_evidence(row)
        if evidence["severity"] == "severe":
            severe += 1
        elif evidence["severity"] == "soft":
            soft += 1
    return {"severe": severe, "soft": soft, "total": severe + soft}


def _mean_rows_from_runs(
    by1: Dict[int, Dict[str, Any]], by2: Dict[int, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Per-essay two-run mean profile (teacher unchanged)."""
    rows = []
    for index in sorted(by1):
        row1, row2 = by1[index], by2[index]
        rows.append(
            {
                "index": index,
                "name": row1.get("name"),
                "page": row1.get("page"),
                "essay": row1.get("essay"),
                "teacher": dict(row1["teacher"]),
                "AI": {
                    dim: (float(row1["AI"][dim]) + float(row2["AI"][dim])) / 2
                    for dim in DIMENSIONS
                },
            }
        )
    return rows


def _b_severity_for_rule(
    rule: str,
    ev1: Dict[str, Any],
    ev2: Dict[str, Any],
    ev_mean: Dict[str, Any],
) -> str:
    if rule == "mean_and_rerun_conservative":
        if ev_mean["severity"] == "severe" and ev2["severity"] == "severe":
            return "severe"
        return "soft"
    if rule == "mean_and_run1_conservative":
        if ev_mean["severity"] == "severe" and ev1["severity"] == "severe":
            return "severe"
        return "soft"
    if rule == "mean_membership_only":
        return "severe" if ev_mean["severity"] == "severe" else "soft"
    if rule == "both_runs_mean_rule":
        mean_bias = (ev1["bias_score"] + ev2["bias_score"]) / 2
        if (
            abs(mean_bias) > B_SEVERE_ABS_BIAS
            and min(ev1["consistency"], ev2["consistency"]) >= B_SEVERE_MIN_DIRECTION
        ):
            return "severe"
        return "soft"
    if rule == "both_runs_conservative":
        if ev1["severity"] == "severe" and ev2["severity"] == "severe":
            return "severe"
        return "soft"
    if rule == "both_runs_either_severe":
        return "severe" if "severe" in (ev1["severity"], ev2["severity"]) else "soft"
    raise ValueError(f"Unknown B consensus rule: {rule}")


_MEAN_BASED_RULES = (
    "mean_and_rerun_conservative",
    "mean_and_run1_conservative",
    "mean_membership_only",
)


def b_consensus(
    run1: List[Dict[str, Any]],
    run2: List[Dict[str, Any]],
    severity_rule: str = B_CONSENSUS_RULE,
    mean_rows: List[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """B consensus under an auditable membership/severity rule."""
    if severity_rule not in B_CONSENSUS_VARIANTS:
        raise ValueError(f"Unknown B consensus rule: {severity_rule}")

    by1, by2 = align_two_runs(run1, run2)
    if mean_rows is None:
        mean_rows = _mean_rows_from_runs(by1, by2)
    by_mean = index_map(mean_rows, "mean profile")
    if set(by_mean) != set(by1):
        raise ValueError("Mean profile index set differs from the two runs")
    for index in sorted(by1):
        teacher1 = by1[index]["teacher"]
        teacher_mean = by_mean[index]["teacher"]
        for dim in DIMENSIONS:
            if float(teacher1[dim]) != float(teacher_mean[dim]):
                raise ValueError(
                    f"Teacher mismatch between runs and mean profile at "
                    f"index {index} dimension {dim}"
                )

    ev1 = {i: b_row_evidence(by1[i]) for i in sorted(by1)}
    ev2 = {i: b_row_evidence(by2[i]) for i in sorted(by1)}
    ev_mean = {i: b_row_evidence(by_mean[i]) for i in sorted(by1)}

    if severity_rule in _MEAN_BASED_RULES:
        members = [i for i in sorted(by1) if ev_mean[i]["severity"] is not None]
    else:
        members = [
            i
            for i in sorted(by1)
            if ev1[i]["severity"] is not None and ev2[i]["severity"] is not None
        ]

    entries: List[Dict[str, Any]] = []
    direction_conflicts: List[int] = []
    for index in members:
        directions = {
            ev_mean[index]["direction"],
            ev2[index]["direction"],
            ev1[index]["direction"],
        }
        directions.discard("neutral")
        if len(directions) > 1:
            direction_conflicts.append(index)
            continue
        severity = _b_severity_for_rule(
            severity_rule, ev1[index], ev2[index], ev_mean[index]
        )
        entries.append(
            {
                "index": index,
                "severity": severity,
                "direction": next(iter(directions), "neutral"),
                "bias_run1": ev1[index]["bias_score"],
                "bias_run2": ev2[index]["bias_score"],
                "bias_mean": ev_mean[index]["bias_score"],
                "severity_run1": ev1[index]["severity"],
                "severity_run2": ev2[index]["severity"],
                "severity_mean": ev_mean[index]["severity"],
                "consistency_run1": ev1[index]["consistency"],
                "consistency_run2": ev2[index]["consistency"],
                "consistency_mean": ev_mean[index]["consistency"],
            }
        )

    severe = [e for e in entries if e["severity"] == "severe"]
    soft = [e for e in entries if e["severity"] == "soft"]
    return {
        "rule": severity_rule,
        "counts": {"severe": len(severe), "soft": len(soft), "total": len(entries)},
        "severe": severe,
        "soft": soft,
        "direction_conflicts": direction_conflicts,
    }


def b_severity_variants(
    run1: List[Dict[str, Any]],
    run2: List[Dict[str, Any]],
    mean_rows: List[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Counts for every auditable B consensus rule variant."""
    out = {}
    for variant in B_CONSENSUS_VARIANTS:
        consensus = b_consensus(run1, run2, severity_rule=variant, mean_rows=mean_rows)
        out[variant] = consensus["counts"]
    return out


# ============================================================
# CLI
# ============================================================
def build_summary(run1_path: str, run2_path: str, mean_path: str = None) -> Dict[str, Any]:
    run1 = load_rows(run1_path)
    run2 = load_rows(run2_path)
    mean_rows = load_rows(mean_path) if mean_path else None
    consensus = e_consensus(run1, run2)
    b_cons = b_consensus(run1, run2, mean_rows=mean_rows)
    return {
        "e_consensus_version": E_CONSENSUS_VERSION,
        "sources": {"run1": run1_path, "run2": run2_path, "mean_profile": mean_path},
        "e_consensus_counts": consensus["counts"],
        "b_run_counts": {"run1": b_run_counts(run1), "run2": b_run_counts(run2)},
        "b_mean_counts": b_run_counts(mean_rows) if mean_rows else None,
        "b_consensus_rule": b_cons["rule"],
        "b_consensus_counts": b_cons["counts"],
        "b_consensus_direction_conflicts": b_cons["direction_conflicts"],
        "b_consensus_variants": b_severity_variants(run1, run2, mean_rows=mean_rows),
    }


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--train-run1", default=DEFAULT_TRAIN_RUN1)
    parser.add_argument("--train-run2", default=DEFAULT_TRAIN_RUN2)
    parser.add_argument("--mean-profile", default=DEFAULT_MEAN_PROFILE)
    parser.add_argument("--json-out", default=None,
                        help="Optional path to write the full summary JSON")
    args = parser.parse_args(argv)

    summary = build_summary(args.train_run1, args.train_run2, args.mean_profile)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
