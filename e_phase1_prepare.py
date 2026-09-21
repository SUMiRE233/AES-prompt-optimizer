"""Phase-1 offline preparation for the E route (no network, no credentials).

Implements the Phase-1 checklist of the E-route supplementary guidance:

  1. two-run consensus re-mining (E + B) on the frozen V6 training runs;
  2. offline per-essay two-run mean evidence for the V6 validation set
     (final_validation_scoring_results_mean.json + .provenance.json) and
     reproduction check of the B aggregates (mean Severe 1.5 / mean Soft 1.0 /
     Q 4.75);
  3. e_protocol_manifest.json - frozen protocol constants, budgets, statuses;
  4. e_baseline_manifest.json - Gate-0 SHA-256 anchors (+ verifier helper);
  5. e_candidate_manifest.draft.json - the D10 sample list for human approval.

Safety properties (tested):
  * imports no network client and never reads AES_API_KEY or any environment
    variable;
  * writes nothing in --dry-run mode;
  * only reads/writes files relative to the repository root;
  * refuses to proceed when the recomputed numbers drift from the frozen
    expectations (raises RuntimeError with a [BLOCKED] prefix).
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from decision_thresholds import q_score
from e_consensus_miner import (
    B_CONSENSUS_RULE,
    B_CONSENSUS_VARIANTS,
    DIMENSIONS,
    E_CONSENSUS_VERSION,
    align_two_runs,
    b_consensus,
    b_run_counts,
    b_severity_variants,
    e_consensus,
    load_rows,
)

ROOT = Path(__file__).resolve().parent

TRAIN_RUN1 = "train_scoring_results6.json"
TRAIN_RUN2 = "train_scoring_results6_rerun.json"
TRAIN_MEAN = "final_train_scoring_results_mean.json"
TRAIN_MEAN_PROVENANCE = "final_train_scoring_results_mean.provenance.json"
VALIDATION_RUN1 = "test_scoring_results6.json"
VALIDATION_RUN2 = "test_scoring_results6_rerun.json"
VALIDATION_MEAN = "final_validation_scoring_results_mean.json"
VALIDATION_MEAN_PROVENANCE = "final_validation_scoring_results_mean.provenance.json"

E_CANDIDATE_DRAFT = "e_candidate_manifest.draft.json"
E_CANDIDATE_FROZEN = "e_candidate_manifest.json"
EVIDENCE_LOCAL = "e_candidate_evidence.local.json"

PROTOCOL_VERSION = "Q-mean-2.5-v1"

EXPECTED_VALIDATION_INDICES = {3, 4, 9, 10, 12, 21, 23, 24, 29, 30, 35, 39}
EXPECTED_E_CONSENSUS = {
    "content": {"severe": 1, "soft": 7},
    "expression": {"severe": 0, "soft": 0},
    "structure": {"severe": 2, "soft": 2},
}
EXPECTED_B_RUN_COUNTS = {
    "run1": {"severe": 3, "soft": 5},
    "run2": {"severe": 3, "soft": 8},
}
EXPECTED_B_CONSENSUS = {"severe": 2, "soft": 6}
EXPECTED_B_ROUTE_METRIC = {"mean_severe": 3.0, "mean_soft": 6.5, "q": 14.0}
EXPECTED_VALIDATION_RUN_COUNTS = {(2, 0), (1, 2)}  # as an unordered pair
EXPECTED_VALIDATION_MEAN = {"severe": 1.5, "soft": 1.0, "q": 4.75}

STATUS_ENUM = [
    "E_DEFERRED",
    "E_PREEXECUTION_AWAITING_SAMPLE_APPROVAL",
    "E_BLOCKED_INFRASTRUCTURE",
    "E_NO_VALID_CANDIDATE",
    "E_EVALUATED_REJECTED",
    "E_FIRST_PASS_PASSED_PENDING_BUDGET",
    "E_CANDIDATE_PASSED_AWAITING_HUMAN",
    "E_EXPLORATORY_PATCH_PROMOTED",
]
STOP_STATUS = "E_PREEXECUTION_AWAITING_SAMPLE_APPROVAL"

CANDIDATE_RULE_CONSTRAINTS = [
    "one dimension / one candidate / one rule / at most one non-semantic retry",
    "qualitative wording only: no fixed scores, percentages or score floors",
    "must not duplicate rules already present in the frozen final prompt",
    "must not alter other scoring dimensions",
    "must be injected under the existing 结构分特殊情形 anchor",
    "must not add new top-level sections",
    "must pass the existing prompt structure contract",
]


def sha256_file(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"[BLOCKED] {message}")


# ============================================================
# Step 1: two-run consensus on the frozen V6 training runs
# ============================================================
def mine_training_consensus() -> Dict[str, Any]:
    run1 = load_rows(str(ROOT / TRAIN_RUN1))
    run2 = load_rows(str(ROOT / TRAIN_RUN2))
    mean_rows = load_rows(str(ROOT / TRAIN_MEAN))

    e_result = e_consensus(run1, run2)
    b_result = b_consensus(run1, run2, mean_rows=mean_rows)
    variants = b_severity_variants(run1, run2, mean_rows=mean_rows)

    e_counts = e_result["counts"]
    for dim, expected in EXPECTED_E_CONSENSUS.items():
        _check(
            e_counts[dim] == expected,
            f"E consensus drift for {dim}: {e_counts[dim]} != {expected}",
        )

    b_counts = {"run1": b_run_counts(run1), "run2": b_run_counts(run2)}
    for label, expected in EXPECTED_B_RUN_COUNTS.items():
        got = b_counts[label]
        _check(
            got["severe"] == expected["severe"] and got["soft"] == expected["soft"],
            f"B run counts drift for {label}: {got} != {expected}",
        )

    _check(
        b_result["counts"]["severe"] == EXPECTED_B_CONSENSUS["severe"]
        and b_result["counts"]["soft"] == EXPECTED_B_CONSENSUS["soft"],
        f"B consensus drift: {b_result['counts']} != {EXPECTED_B_CONSENSUS}",
    )
    _check(
        not b_result["direction_conflicts"],
        f"B consensus direction conflicts: {b_result['direction_conflicts']}",
    )

    mean_severe = (b_counts["run1"]["severe"] + b_counts["run2"]["severe"]) / 2
    mean_soft = (b_counts["run1"]["soft"] + b_counts["run2"]["soft"]) / 2
    route_metric = {
        "mean_severe": mean_severe,
        "mean_soft": mean_soft,
        "q": q_score(mean_severe, mean_soft),
    }
    _check(
        route_metric == EXPECTED_B_ROUTE_METRIC,
        f"B route metric drift: {route_metric} != {EXPECTED_B_ROUTE_METRIC}",
    )

    return {
        "run1_rows": run1,
        "run2_rows": run2,
        "mean_rows": mean_rows,
        "e_consensus": e_result,
        "b_consensus": b_result,
        "b_run_counts": b_counts,
        "b_consensus_variants": variants,
        "b_route_metric": route_metric,
    }


# ============================================================
# Step 2: offline validation two-run mean evidence
# ============================================================
def build_validation_mean() -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    run1 = load_rows(str(ROOT / VALIDATION_RUN1))
    run2 = load_rows(str(ROOT / VALIDATION_RUN2))
    by1, by2 = align_two_runs(run1, run2, VALIDATION_RUN1, VALIDATION_RUN2)

    _check(
        set(by1) == EXPECTED_VALIDATION_INDICES,
        f"validation index set mismatch: {sorted(by1)}",
    )

    mean_rows = []
    for row in run1:  # keep run1 order
        index = int(row["index"])
        mean_rows.append(
            {
                "index": index,
                "name": row.get("name"),
                "page": row.get("page"),
                "essay": row.get("essay"),
                "teacher": dict(row["teacher"]),
                "AI": {
                    dim: (float(row["AI"][dim]) + float(by2[index]["AI"][dim])) / 2
                    for dim in DIMENSIONS
                },
            }
        )

    counts1 = b_run_counts(run1)
    counts2 = b_run_counts(run2)
    _check(
        {(counts1["severe"], counts1["soft"]), (counts2["severe"], counts2["soft"])}
        == EXPECTED_VALIDATION_RUN_COUNTS,
        f"validation run counts drift: {counts1} / {counts2}",
    )

    mean_severe = (counts1["severe"] + counts2["severe"]) / 2
    mean_soft = (counts1["soft"] + counts2["soft"]) / 2
    _check(
        mean_severe == EXPECTED_VALIDATION_MEAN["severe"]
        and mean_soft == EXPECTED_VALIDATION_MEAN["soft"]
        and q_score(mean_severe, mean_soft) == EXPECTED_VALIDATION_MEAN["q"],
        f"validation mean reproduction failed: {mean_severe}/{mean_soft}",
    )

    provenance = {
        "derived_from": [
            {"file": VALIDATION_RUN1, "sha256": sha256_file(ROOT / VALIDATION_RUN1)},
            {"file": VALIDATION_RUN2, "sha256": sha256_file(ROOT / VALIDATION_RUN2)},
        ],
        "method": (
            "per-essay per-dimension arithmetic mean of AI scores across the two "
            "fixed validation runs; teacher labels unchanged; aligned by global index"
        ),
        "iteration": 6,
        "w": 2.5,
        "protocol_version": PROTOCOL_VERSION,
        "created_at": _now(),
    }
    verification = {
        "run1_counts": counts1,
        "run2_counts": counts2,
        "mean_counts": {"severe": mean_severe, "soft": mean_soft},
        "q": q_score(mean_severe, mean_soft),
        "expected": EXPECTED_VALIDATION_MEAN,
        "indices": sorted(by1),
    }
    return mean_rows, provenance, verification


# ============================================================
# Step 3: protocol manifest
# ============================================================
def build_protocol_manifest(consensus: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "phase": "phase1_preexecution",
        "status": "draft_pending_human_approval",
        "created_at": _now(),
        "baseline": {
            "final_b_version": "V6",
            "active_prompt": "final_prompt.md",
            "train_mean_evidence": TRAIN_MEAN,
            "protocol_version": PROTOCOL_VERSION,
            "q_weight": 2.5,
        },
        "consensus_version": E_CONSENSUS_VERSION,
        "formulas": {
            "e_consensus": (
                "per run: residual = delta - median(delta), z = residual / "
                "(1.4826*MAD + eps); E sample: z1*z2 > 0 and "
                "abs(mean(delta1, delta2)) >= 1.0; severity: "
                "abs(mean(z1, z2)) > 2.5 -> severe, > 1.5 -> soft"
            ),
            "b_rule": (
                "severe: abs(B) > 1.5 and direction_consistency >= 3; "
                "soft: abs(B) > 1.0 and direction_consistency >= 2"
            ),
            "b_consensus": (
                f"rule '{B_CONSENSUS_RULE}' (operative adjudication, not a "
                "natural symmetric consensus): members = flagged by the frozen "
                "B rule on the two-run mean profile; severity = severe only "
                "when the mean profile and the rerun both flag severe; the "
                "rerun serves as the review run in this project's "
                "precision-upgrade timeline"
            ),
            "decision_thresholds": (
                "delta_abs_error = candidate_abs_error - baseline_abs_error; "
                "clear_improvement <= -1.0; clear_regression >= +1.0"
            ),
        },
        "b_layers": {
            "b_route_metric": {
                **consensus["b_route_metric"],
                "source": "arithmetic mean of per-run B counts (run1/run2)",
                "used_for": "Gate 3 global B regression",
            },
            "b_consensus_samples": {
                **consensus["b_consensus"]["counts"],
                "rule": B_CONSENSUS_RULE,
                "used_for": "micro sample identity",
                "description_kind": "operative_adjudication",
                "variants_usage": (
                    "archived for audit only; must not drive sample roles or gates"
                ),
            },
            "b_run_counts": consensus["b_run_counts"],
            "b_consensus_variants": consensus["b_consensus_variants"],
        },
        "budgets": {
            "max_successful_scoring_calls": 96,
            "max_successful_analysis_calls": 2,
            "max_successful_calls": 98,
            "max_request_attempts": 110,
            "derivation": "train 36*2 = 72, validation 12*2 = 24, analysis 1 + 1 retry",
            "reuse_key": "(prompt_sha256, model, global_index, repeat_id, scoring_protocol)",
            "models": {
                "AES_SCORING_MODEL": "claude-sonnet-5",
                "AES_OPTIMIZER_MODEL": "claude-sonnet-5",
                "AES_E_ANALYSIS_MODEL": "claude-sonnet-5",
            },
        },
        "status_enum": STATUS_ENUM,
        "phase1_stop_status": STOP_STATUS,
        "final_route_status": None,
        "legacy_labels": {
            "stop_reason_authority": "b_protocol_state.json",
            "b_validation_report_reason": (
                "legacy label 'cap_reached' is retained for provenance; "
                "B history is not rewritten"
            ),
        },
        "candidate_rule_constraints": CANDIDATE_RULE_CONSTRAINTS,
    }


# ============================================================
# Step 4: Gate-0 baseline anchors
# ============================================================
BASELINE_ANCHORS = [
    ("final_prompt_meta.md", "frozen B-final prompt metadata (V6)"),
    ("final_prompt.md", "frozen B-final prompt text (V6)"),
    (TRAIN_RUN1, "V6 train run1 scoring"),
    (TRAIN_RUN2, "V6 train run2 scoring (rerun)"),
    (TRAIN_MEAN, "V6 train two-run mean evidence"),
    (TRAIN_MEAN_PROVENANCE, "V6 train mean evidence provenance"),
    (VALIDATION_RUN1, "V6 validation run1 scoring"),
    (VALIDATION_RUN2, "V6 validation run2 scoring"),
    (VALIDATION_MEAN, "V6 validation two-run mean evidence (generated in Phase 1)"),
    (VALIDATION_MEAN_PROVENANCE, "V6 validation mean evidence provenance"),
    ("train_essays.json", "train split essays + teacher labels"),
    ("test_essays.json", "validation split essays + teacher labels"),
    ("b_protocol_state.json", "B stop reason authority"),
    ("b_validation_report.json", "B validation selection record (legacy label kept)"),
    ("final_evidence.json", "B freeze evidence index"),
]


def build_baseline_manifest(allow_missing: Tuple[str, ...] = ()) -> Dict[str, Any]:
    anchors = []
    missing = []
    for path_str, purpose in BASELINE_ANCHORS:
        path = ROOT / path_str
        digest = sha256_file(path)
        if digest is None and path_str in allow_missing:
            anchors.append(
                {
                    "path": path_str,
                    "sha256": None,
                    "status": "to_be_generated",
                    "purpose": purpose,
                }
            )
            continue
        if digest is None:
            missing.append(path_str)
        anchors.append({"path": path_str, "sha256": digest, "purpose": purpose})
    _check(not missing, f"baseline anchors missing: {missing}")
    return {
        "phase": "phase1_preexecution",
        "status": "draft_pending_human_approval",
        "created_at": _now(),
        "hash_algorithm": "sha256",
        "anchors": anchors,
    }


def verify_baseline_manifest(manifest: Dict[str, Any], root: Path = ROOT) -> List[Dict[str, Any]]:
    """Return a list of anchor mismatches (empty means Gate 0 passes)."""
    mismatches = []
    for anchor in manifest["anchors"]:
        path = root / anchor["path"]
        actual = sha256_file(path)
        if actual != anchor["sha256"]:
            mismatches.append(
                {"path": anchor["path"], "expected": anchor["sha256"], "actual": actual}
            )
    return mismatches


# ============================================================
# Step 5: D10 sample draft (for human approval)
# ============================================================
def _total(row: Dict[str, Any]) -> float:
    return sum(float(row["teacher"][d]) for d in DIMENSIONS)


def _band(structure_score: float) -> str:
    if structure_score <= 5.5:
        return "low"
    if structure_score < 7:
        return "mid"
    return "high"


def build_candidate_draft(consensus: Dict[str, Any]) -> Dict[str, Any]:
    run1 = consensus["run1_rows"]
    run2 = consensus["run2_rows"]
    mean_rows = consensus["mean_rows"]
    by1, by2 = align_two_runs(run1, run2)
    by_mean = {int(row["index"]): row for row in mean_rows}

    e_result = consensus["e_consensus"]
    b_result = consensus["b_consensus"]

    e_flagged_any = set()
    for dim in DIMENSIONS:
        for entry in e_result["by_dim"][dim]["severe"] + e_result["by_dim"][dim]["soft"]:
            e_flagged_any.add(entry["index"])

    roles: Dict[int, List[str]] = {}
    reasons: Dict[int, List[str]] = {}

    def add_role(index: int, role: str, reason: str) -> None:
        roles.setdefault(index, [])
        reasons.setdefault(index, [])
        if role not in roles[index]:
            roles[index].append(role)
        reasons[index].append(reason)

    structure_targets = sorted(
        entry["index"]
        for entry in e_result["by_dim"]["structure"]["severe"]
        + e_result["by_dim"]["structure"]["soft"]
    )
    for entry in e_result["by_dim"]["structure"]["severe"] + e_result["by_dim"]["structure"]["soft"]:
        add_role(
            entry["index"],
            "structure_e_target",
            f"structure E consensus {entry['severity']} (mean_z={entry['mean_z']:.3f}, "
            f"mean_delta={entry['mean_delta']:+.2f})",
        )

    b_members = sorted(
        entry["index"] for entry in b_result["severe"] + b_result["soft"]
    )
    for entry in b_result["severe"] + b_result["soft"]:
        add_role(
            entry["index"],
            f"b_consensus_{entry['severity']}",
            f"B consensus {entry['severity']} (bias_mean={entry['bias_mean']:+.3f}, "
            f"directions {entry['severity_run1']}/{entry['severity_run2']})",
        )

    # ---- normal controls (structure) ----
    excluded = e_flagged_any | set(b_members)
    pool = sorted(i for i in by1 if i not in excluded)
    _check(len(pool) >= 5, f"not enough clean normal-control candidates: {len(pool)}")

    lengths = {i: len(str(by1[i].get("essay") or "")) for i in by1}

    def cand_key(target_idx: int, cand_idx: int) -> Tuple[float, float, float, int]:
        t = by1[target_idx]
        c = by1[cand_idx]
        ds = abs(float(t["teacher"]["structure"]) - float(c["teacher"]["structure"]))
        dl = abs(lengths[target_idx] - lengths[cand_idx])
        dt = abs(_total(t) - _total(c))
        return (ds, dl, dt, cand_idx)

    def nearest_to_any_target(candidates: List[int]) -> Optional[int]:
        best = None
        best_key = None
        for cand in candidates:
            key = min(cand_key(t, cand) for t in structure_targets)
            if best_key is None or key < best_key:
                best, best_key = cand, key
        return best

    controls: List[int] = []
    for target in structure_targets:
        available = [c for c in pool if c not in controls]
        if not available:
            break
        pick = min(available, key=lambda c: cand_key(target, c))
        controls.append(pick)
        add_role(
            pick,
            "normal_control",
            f"nearest match to structure E target {target} "
            f"(d_structure={cand_key(target, pick)[0]:.1f}, d_len={cand_key(target, pick)[1]})",
        )

    target_bands = {_band(float(by1[t]["teacher"]["structure"])) for t in structure_targets}
    for band in ("low", "mid", "high"):
        if band not in target_bands:
            continue
        covered = any(_band(float(by1[c]["teacher"]["structure"])) == band for c in controls)
        if covered:
            continue
        candidates = [
            c for c in pool if c not in controls and _band(float(by1[c]["teacher"]["structure"])) == band
        ]
        pick = nearest_to_any_target(candidates)
        if pick is not None:
            controls.append(pick)
            add_role(pick, "normal_control", f"teacher structure band '{band}' coverage")

    while len(controls) < 6:
        pick = nearest_to_any_target([c for c in pool if c not in controls])
        if pick is None:
            break
        controls.append(pick)
        add_role(pick, "normal_control", "extra matched control (fill to 6)")
    _check(len(controls) >= 5, f"normal controls below minimum: {len(controls)}")

    # ---- cross-dimension watch samples ----
    content_entries = sorted(
        e_result["by_dim"]["content"]["severe"] + e_result["by_dim"]["content"]["soft"],
        key=lambda e: (-e["mean_abs_z"], e["index"]),
    )
    _check(len(content_entries) >= 2, "content E consensus below 2 samples")
    for entry in content_entries[:2]:
        add_role(
            entry["index"],
            "cross_dim_content",
            f"content E consensus boundary sample (|mean_z|={entry['mean_abs_z']:.3f})",
        )

    expr_delta = {}
    for index in by1:
        d1 = float(by1[index]["AI"]["expression"]) - float(by1[index]["teacher"]["expression"])
        d2 = float(by2[index]["AI"]["expression"]) - float(by2[index]["teacher"]["expression"])
        expr_delta[index] = (d1 + d2) / 2
    expr_sorted = sorted(by1, key=lambda i: (-abs(expr_delta[i]), i))
    expr_picks = [i for i in expr_sorted if i not in roles][:2]
    if len(expr_picks) < 2:
        expr_picks += [i for i in expr_sorted if i not in expr_picks][: 2 - len(expr_picks)]
    for index in expr_picks:
        add_role(
            index,
            "cross_dim_expression",
            f"largest |mean expression delta| (no expression E consensus; "
            f"leakage watch, delta={expr_delta[index]:+.2f})",
        )

    # ---- assemble ----
    samples = []
    for index in sorted(roles):
        row1, row2, row_mean = by1[index], by2[index], by_mean[index]
        diffs1 = {d: float(row1["AI"][d]) - float(row1["teacher"][d]) for d in DIMENSIONS}
        diffs2 = {d: float(row2["AI"][d]) - float(row2["teacher"][d]) for d in DIMENSIONS}
        mean_delta = {d: (diffs1[d] + diffs2[d]) / 2 for d in DIMENSIONS}

        z_values: Dict[str, Any] = {}
        for dim in DIMENSIONS:
            for entry in e_result["by_dim"][dim]["severe"] + e_result["by_dim"][dim]["soft"]:
                if entry["index"] == index:
                    z_values[dim] = {
                        "z_run1": entry["z_run1"],
                        "z_run2": entry["z_run2"],
                        "mean_z": entry["mean_z"],
                    }

        evidence: Dict[str, Any] = {
            "diffs_run1": diffs1,
            "diffs_run2": diffs2,
            "mean_delta": mean_delta,
            "e_consensus_z": z_values,
        }
        for entry in b_result["severe"] + b_result["soft"]:
            if entry["index"] == index:
                evidence["b_consensus"] = {
                    "bias_run1": entry["bias_run1"],
                    "bias_run2": entry["bias_run2"],
                    "bias_mean": entry["bias_mean"],
                    "severity_run1": entry["severity_run1"],
                    "severity_run2": entry["severity_run2"],
                    "severity_mean": entry["severity_mean"],
                    "severity": entry["severity"],
                    "direction": entry["direction"],
                }

        samples.append(
            {
                "global_index": index,
                "evidence_roles": sorted(roles[index]),
                "teacher_scores": {d: row1["teacher"][d] for d in DIMENSIONS},
                "v6_run1_scores": {d: row1["AI"][d] for d in DIMENSIONS},
                "v6_run2_scores": {d: row2["AI"][d] for d in DIMENSIONS},
                "v6_mean_scores": {d: row_mean["AI"][d] for d in DIMENSIONS},
                "evidence": evidence,
                "selection_reasons": reasons[index],
            }
        )

    role_counts: Dict[str, int] = {}
    for index in roles:
        for role in roles[index]:
            role_counts[role] = role_counts.get(role, 0) + 1

    manifest_samples = []
    evidence_samples: Dict[str, Any] = {}
    for sample in samples:
        manifest_samples.append(
            {
                "global_index": sample["global_index"],
                "evidence_roles": sample["evidence_roles"],
                "selection_reasons": sample["selection_reasons"],
            }
        )
        evidence_samples[str(sample["global_index"])] = {
            "teacher_scores": sample["teacher_scores"],
            "v6_run1_scores": sample["v6_run1_scores"],
            "v6_run2_scores": sample["v6_run2_scores"],
            "v6_mean_scores": sample["v6_mean_scores"],
            "evidence": sample["evidence"],
        }

    baseline_files = {
        path_str: sha256_file(ROOT / path_str) for path_str, _purpose in BASELINE_ANCHORS
    }

    manifest = {
        "phase": "phase1_preexecution",
        "status": "draft_pending_human_approval",
        "file_status": (
            "draft (no raw essays or raw per-dimension scores; derived z/delta "
            "values may appear inside selection_reasons for audit); after reviewer approval rename to "
            f"{E_CANDIDATE_FROZEN}, fill approval metadata and compute the "
            "frozen SHA-256 over the manifest body"
        ),
        "created_at": _now(),
        "consensus": {
            "version": E_CONSENSUS_VERSION,
            "b_consensus_rule": B_CONSENSUS_RULE,
            "b_consensus_description_kind": (
                "operative_adjudication (mean-profile membership + rerun "
                "re-confirmation); not a natural symmetric consensus"
            ),
        },
        "sources": {
            "train_run1": TRAIN_RUN1,
            "train_run2": TRAIN_RUN2,
            "train_mean": TRAIN_MEAN,
        },
        "selection_rules": [
            "structure targets: all structure E consensus samples",
            "normal controls: >=5, not flagged as E (any dimension) or B in either source;"
            " nearest matches to E targets, then band coverage (low/mid/high), then fill to 6",
            "cross-dim content: top-2 content E consensus by |mean_z| (boundary/leakage watch)",
            "cross-dim expression: top-2 by |mean expression delta| (no expression E supply)",
            "B roles: all B consensus samples (membership rule "
            f"'{B_CONSENSUS_RULE}')",
            "one scoring identity per unique global index; roles are shared",
            "no sample list changes after candidate rule or scores are seen",
        ],
        "counts": {
            "structure_targets": len(structure_targets),
            "normal_controls": len(controls),
            "cross_dim_content": 2,
            "cross_dim_expression": len(expr_picks),
            "b_consensus": len(b_members),
            "unique_indices": len(roles),
            "role_counts": role_counts,
        },
        "scoring_dedup_note": (
            "micro/regular/full-train/validation reuse one candidate score per "
            "(prompt_sha256, model, global_index, repeat_id, scoring_protocol)"
        ),
        "baseline_files": baseline_files,
        "approval": {
            "status": "pending",
            "approved_by": None,
            "approved_at": None,
            "frozen_sha256": None,
        },
        "unique_global_indices": sorted(roles),
        "samples": manifest_samples,
    }

    evidence = {
        "phase": "phase1_preexecution",
        "status": "local_only_never_committed",
        "note": (
            "per-sample teacher/model scores, diffs and z-scores for review; "
            f"gitignored ({EVIDENCE_LOCAL}); do not commit"
        ),
        "created_at": _now(),
        "samples": evidence_samples,
    }

    return {"manifest": manifest, "evidence": evidence}


def require_frozen_manifest(path: str) -> str:
    """Guard for Phase-2 entry points.

    Only the frozen ``e_candidate_manifest.json`` may be consumed: drafts and
    the local per-sample evidence file are rejected, as is any other name
    (reviewer ruling 2026-09-21).
    """
    name = Path(path).name
    if name.endswith(".draft.json") or name.endswith(".local.json"):
        raise ValueError(
            f"Draft/local manifest must not enter analyzers or scoring entry points: {path}"
        )
    if name != E_CANDIDATE_FROZEN:
        raise ValueError(
            f"E entry points accept only the frozen {E_CANDIDATE_FROZEN}: {path}"
        )
    return path


# ============================================================
# Orchestration
# ============================================================
def run_phase1_prepare(dry_run: bool = False) -> Dict[str, Any]:
    consensus = mine_training_consensus()
    validation_rows, provenance, verification = build_validation_mean()

    written = []
    if not dry_run:
        write_json(ROOT / VALIDATION_MEAN, validation_rows)
        write_json(ROOT / VALIDATION_MEAN_PROVENANCE, provenance)
        written += [VALIDATION_MEAN, VALIDATION_MEAN_PROVENANCE]

    protocol = build_protocol_manifest(consensus)
    if not dry_run:
        write_json(ROOT / "e_protocol_manifest.json", protocol)
        written.append("e_protocol_manifest.json")

    baseline = build_baseline_manifest(
        allow_missing=()
        if not dry_run
        else (VALIDATION_MEAN, VALIDATION_MEAN_PROVENANCE)
    )
    if not dry_run:
        write_json(ROOT / "e_baseline_manifest.json", baseline)
        written.append("e_baseline_manifest.json")

    draft = build_candidate_draft(consensus)
    if not dry_run:
        write_json(ROOT / E_CANDIDATE_DRAFT, draft["manifest"])
        write_json(ROOT / EVIDENCE_LOCAL, draft["evidence"])
        written += [E_CANDIDATE_DRAFT, EVIDENCE_LOCAL]

    if not dry_run:
        dump = {
            "created_at": _now(),
            "e_consensus": consensus["e_consensus"],
            "b_consensus": {
                "rule": consensus["b_consensus"]["rule"],
                "counts": consensus["b_consensus"]["counts"],
                "severe": consensus["b_consensus"]["severe"],
                "soft": consensus["b_consensus"]["soft"],
            },
            "b_run_counts": consensus["b_run_counts"],
            "b_consensus_variants": consensus["b_consensus_variants"],
            "b_route_metric": consensus["b_route_metric"],
        }
        write_json(ROOT / "etype_analysis" / "e_consensus_v6.json", dump)
        written.append("etype_analysis/e_consensus_v6.json")

    return {
        "dry_run": dry_run,
        "status": STOP_STATUS,
        "e_consensus_counts": consensus["e_consensus"]["counts"],
        "b_run_counts": consensus["b_run_counts"],
        "b_consensus_counts": consensus["b_consensus"]["counts"],
        "b_route_metric": consensus["b_route_metric"],
        "validation_verification": verification,
        "draft_counts": draft["manifest"]["counts"],
        "written": written,
        "network_calls": 0,
    }


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="compute everything in memory; write no files")
    args = parser.parse_args(argv)

    try:
        summary = run_phase1_prepare(dry_run=args.dry_run)
    except RuntimeError as exc:
        print(str(exc))
        return 2

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
