"""E-route Phase-2 orchestrator (official run).

Subcommands (all thresholds come from ``decision_thresholds`` /
``e_gate_engine`` only; no private numbers):

  freeze-manifest --approved-by X   approve the D10 draft -> frozen manifest
  generate-rule                     <=2 analysis calls -> candidate rule
  select-rule                       apply lazy sequential Gate 0 to the
                                    archived response (no API call)
  approve-rule --decision approve|reject --approved-by X
                                    optional veto (admission is automatic
                                    once Gate 0 passes)
  execute                           scoring stages + gates + report
  execute --first-pass-only         cheap screen: stop after the first-pass gate
  export-public-rule                write the sanitized (span-free) rule copy
  status                            print the current run state

Rule selection policy (ruled by the project owner on 2026-09-21, lazy
completion): take the returned rules in their ORIGINAL order; run the Gate 0
hard rejections ON the taken rule (injection-location match, scope, hard
thresholds, evidence pool, duplication, contract/budget dry-run); on
rejection fall through to the next rule; stop at the first pass. No ranking,
no pre-screening. Every attempt is recorded in the payload. An analysis
response that is already archived is never re-sampled: use ``select-rule``
instead of ``generate-rule`` in that case.

Budget guard (frozen protocol): successful scoring calls <= 96, successful
analysis calls <= 2, total successful <= 98, request attempts <= 110.
State lives in ``etype_analysis/e_budget_state.json`` (gitignored dir).

Admission policy (ruled 2026-09-21): a rule that passes the lazy sequential
Gate 0 hard review is admitted automatically and enters the testing flow --
there is NO human pass at this node. Human veto (approve-rule --decision
reject) remains available until execution starts. The only remaining human
stop is E_CANDIDATE_PASSED_AWAITING_HUMAN after every automatic gate passed;
``--promote-final`` is intentionally NOT implemented here, the candidate can
never overwrite the frozen final prompt.

Integrity (review fixes 2026-09-21): the frozen manifest is verified against
its ``approval.frozen_sha256`` canonical digest at every execution entry
point that depends on it (generate-rule / select-rule / execute); ``status``
additionally reports ``manifest_integrity: valid|invalid``. Stage-result
reuse validates a per-stage identity sidecar (prompt_sha256 / model /
global_indices / repeat_id / scoring_protocol); mismatches block.
"""

import argparse
import copy
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from decision_thresholds import (
    B_SEVERE_PENALTY_WEIGHT,
    B_SOFT_PENALTY_WEIGHT,
    FIRST_PASS_CATASTROPHIC_DELTA,
    MICRO_MIN_U,
    NON_TARGET_WEIGHT,
    TARGET_WEIGHT,
)
from e_consensus_miner import e_consensus, load_rows, run_dimension_stats
from e_gate_engine import (
    DIMENSIONS,
    TARGET_DIM,
    classify_manifest,
    first_pass_check,
    full_train_evaluate,
    mean_rows,
    micro_evaluate,
    regular_evaluate,
    validation_evaluate,
)
from e_phase1_prepare import sha256_file, verify_baseline_manifest, write_json

ROOT = Path(__file__).resolve().parent
ANALYSIS_DIR = ROOT / "etype_analysis"

MANIFEST_DRAFT = "e_candidate_manifest.draft.json"
MANIFEST_FROZEN = "e_candidate_manifest.json"
EVIDENCE_LOCAL = "e_candidate_evidence.local.json"
RULE_FILE = "e_candidate_rule.json"
BASELINE_MANIFEST = "e_baseline_manifest.json"
PROTOCOL_MANIFEST = "e_protocol_manifest.json"
REPORT_FILE = "e_evaluation_report.json"

CANDIDATE_META = "e_candidate_prompt_meta.md"
CANDIDATE_PROMPT = "e_candidate_prompt.md"

TRAIN_RUN1 = "train_scoring_results6.json"
TRAIN_RUN2 = "train_scoring_results6_rerun.json"
TRAIN_MEAN = "final_train_scoring_results_mean.json"
VAL_RUN1 = "test_scoring_results6.json"
VAL_RUN2 = "test_scoring_results6_rerun.json"
FINAL_META = "final_prompt_meta.md"

BUDGET_STATE = ANALYSIS_DIR / "e_budget_state.json"

# Budget amended 2026-09-21 (project owner ruling): candidate 1 was rejected
# at the first-pass gate after 17 scoring calls; the second candidate is
# authorised to run the complete chain -> 17 + 96 = 113 scoring successes;
# attempts doubled to 220 to absorb the observed SSL transport retries.
# Recorded in e_protocol_manifest.json -> budgets (coherence enforced by tests).
MAX_SCORING_SUCCESS = 113
MAX_ANALYSIS_SUCCESS = 2
MAX_TOTAL_SUCCESS = 115
MAX_REQUEST_ATTEMPTS = 220

# Identity string recorded in the per-stage sidecar so that stage-result reuse
# (reuse-key philosophy) can verify prompt/model/indices/run/protocol.
SCORING_PROTOCOL_ID = "batch_size=1;dims=3;score_max=9;single_pass_json;v1"


class BudgetExceeded(RuntimeError):
    pass


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ============================================================
# Budget guard
# ============================================================
class BudgetGuard:
    def __init__(self, path: Path = BUDGET_STATE):
        self.path = path
        if path.exists():
            self.state = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.state = {
                "scoring_success": 0,
                "analysis_success": 0,
                "attempts": 0,
                "updated_at": None,
            }
            self._save()

    def _save(self) -> None:
        self.state["updated_at"] = now()
        write_json(self.path, self.state)

    def note_attempt(self) -> None:
        if self.state["attempts"] + 1 > MAX_REQUEST_ATTEMPTS:
            raise BudgetExceeded(
                f"request attempt cap reached ({MAX_REQUEST_ATTEMPTS})"
            )
        self.state["attempts"] += 1
        self._save()

    def _check_total(self) -> None:
        total = self.state["scoring_success"] + self.state["analysis_success"]
        if total > MAX_TOTAL_SUCCESS:
            raise BudgetExceeded(f"total successful call cap reached ({MAX_TOTAL_SUCCESS})")

    def note_scoring_success(self) -> None:
        if self.state["scoring_success"] + 1 > MAX_SCORING_SUCCESS:
            raise BudgetExceeded(
                f"scoring success cap reached ({MAX_SCORING_SUCCESS})"
            )
        self.state["scoring_success"] += 1
        self._check_total()
        self._save()

    def note_analysis_success(self) -> None:
        if self.state["analysis_success"] + 1 > MAX_ANALYSIS_SUCCESS:
            raise BudgetExceeded(
                f"analysis success cap reached ({MAX_ANALYSIS_SUCCESS})"
            )
        self.state["analysis_success"] += 1
        self._check_total()
        self._save()

    def summary(self) -> Dict[str, Any]:
        return dict(self.state)


# ============================================================
# Budget-aware API clients
# ============================================================
def _budgeted_send(self, request_data, max_retries, retry_delay, guard, kind):
    import requests

    if not self.api_key:
        raise RuntimeError("AES_API_KEY is not set.")
    headers = {"content-type": "application/json", "x-api-key": self.api_key}
    for attempt in range(max_retries):
        guard.note_attempt()
        try:
            response = requests.post(
                self.api_url, headers=headers, json=request_data, timeout=150
            )
            if response.status_code != 200:
                print(f"  [budgeted] HTTP {response.status_code}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                return None
            try:
                data = response.json()
            except ValueError:
                print("  [budgeted] response is not valid JSON")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                return None
            if kind == "analysis":
                guard.note_analysis_success()
            else:
                guard.note_scoring_success()
            return data
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - transport errors are retried
            print(f"  [budgeted] request failed: {exc}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
            return None
    return None


def make_budgeted_analyzer(guard: BudgetGuard):
    from etype_preference_analyzer import ContrastiveETypeAnalyzer

    class BudgetedAnalyzer(ContrastiveETypeAnalyzer):
        def send_api_request(self, request_data, max_retries=3, retry_delay=10):
            return _budgeted_send(
                self, request_data, max_retries, retry_delay, guard, "analysis"
            )

    return BudgetedAnalyzer()


def make_budgeted_scorer(guard: BudgetGuard):
    from batch_scoring import BatchEssayScorer

    class BudgetedScorer(BatchEssayScorer):
        def send_api_request(self, request_data, max_retries=5, retry_delay=5):
            return _budgeted_send(
                self, request_data, max_retries, retry_delay, guard, "scoring"
            )

    return BudgetedScorer()


# ============================================================
# Shared helpers
# ============================================================
def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_report(report: Dict[str, Any]) -> None:
    write_json(ROOT / REPORT_FILE, report)


def load_report() -> Dict[str, Any]:
    path = ROOT / REPORT_FILE
    if path.exists():
        return read_json(path)
    return {"created_at": now(), "stages": {}}


def _report_status() -> Optional[str]:
    path = ROOT / REPORT_FILE
    if not path.exists():
        return None
    return read_json(path).get("status")


def load_fresh_report(rule_index: int) -> Dict[str, Any]:
    """Fresh report for a new candidate.

    Archives the previous run's report and candidate-prompt copies into the
    gitignored ``etype_analysis/`` folder so that candidate 1 evidence is never
    overwritten by candidate 2 runs.
    """
    path = ROOT / REPORT_FILE
    if path.exists():
        previous = read_json(path)
        if previous.get("stages"):
            previous_index = int(previous.get("rule_index", 0))
            write_json(
                ANALYSIS_DIR / f"e_evaluation_report_candidate{previous_index + 1}.json",
                previous,
            )
            for name in (CANDIDATE_META, CANDIDATE_PROMPT):
                source = ROOT / name
                if source.exists():
                    target = ANALYSIS_DIR / f"{Path(name).stem}_cand{previous_index + 1}.md"
                    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return {"created_at": now(), "rule_index": rule_index, "stages": {}}


def require_baseline_ok() -> None:
    manifest = read_json(ROOT / BASELINE_MANIFEST)
    mismatches = verify_baseline_manifest(manifest, ROOT)
    if mismatches:
        raise RuntimeError(f"[BLOCKED] baseline drift: {mismatches}")


def frozen_manifest_digest(manifest: Dict[str, Any]) -> str:
    """Canonical digest of the manifest body (frozen_sha256 field nulled)."""
    body = copy.deepcopy(manifest)
    approval = body.setdefault("approval", {})
    approval["frozen_sha256"] = None
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def require_frozen_manifest(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the frozen manifest AND verify its integrity (review fix 2026-09-21).

    Recomputes the canonical digest and compares it with
    ``approval.frozen_sha256``; any mismatch blocks the run, so a manifest
    edited after approval cannot silently enter execution.
    """
    manifest_path = path or (ROOT / MANIFEST_FROZEN)
    if not manifest_path.exists():
        raise RuntimeError(
            "[BLOCKED] e_candidate_manifest.json missing; run freeze-manifest first"
        )
    manifest = read_json(manifest_path)
    approval = manifest.get("approval") or {}
    if approval.get("status") != "approved":
        raise RuntimeError("[BLOCKED] frozen manifest is not approved")
    recorded = approval.get("frozen_sha256")
    actual = frozen_manifest_digest(manifest)
    if not recorded or actual != recorded:
        raise RuntimeError(
            "[BLOCKED] frozen manifest hash mismatch "
            f"(recorded={recorded}, actual={actual}); refusing to run"
        )
    return manifest


def structure_injection(prompt_text: str, rule: Dict[str, Any]) -> Tuple[str, str]:
    """Append the rule as nested bullets under the frozen structure anchor."""
    from prompt_structure_contract import dimension_anchor_map, note_section_body

    anchor = dimension_anchor_map(prompt_text).get("structure")
    if not anchor:
        raise ValueError("structure anchor missing from prompt")
    body = note_section_body(prompt_text)
    if body is None:
        raise ValueError("## 注意事项 missing from prompt")
    note_start = prompt_text.find(body)
    note_end = note_start + len(body)
    section_start = prompt_text.find(anchor)
    next_bold = prompt_text.find("\n**", section_start + len(anchor))
    insert_at = next_bold if 0 <= next_bold <= note_end else note_end

    import re as _re

    forbidden = rule.get("forbidden_generalization") or []
    if isinstance(forbidden, list):
        forbidden_text = "；".join(str(item) for item in forbidden)
    else:
        forbidden_text = str(forbidden)
    boundary = (rule.get("anti_overfit_boundary") or rule.get("counter_examples") or "").strip()

    rendered_lines = [
        f"  - 触发条件：{rule.get('trigger_condition', '').strip()}",
        f"    调整方向：{rule.get('scoring_adjustment', '').strip()}",
    ]
    if boundary:
        rendered_lines.append(f"    不适用边界：{boundary}")
    if forbidden_text:
        rendered_lines.append(f"    禁止外推：{forbidden_text}")
    rendered = "\n".join(rendered_lines)

    before = prompt_text[:insert_at].rstrip("\n")
    after = prompt_text[insert_at:]
    if after.startswith("\n"):
        after = after[1:]
    new_text = before + "\n\n" + rendered + "\n\n" + after
    new_text = _re.sub(r"\n{3,}", "\n\n", new_text)
    return new_text, rendered


def freeze_manifest(approved_by: str) -> Dict[str, Any]:
    require_baseline_ok()
    draft_path = ROOT / MANIFEST_DRAFT
    if not draft_path.exists():
        raise RuntimeError("[BLOCKED] draft manifest missing")
    manifest = read_json(draft_path)
    manifest["status"] = "approved"
    manifest["approval"] = {
        "status": "approved",
        "approved_by": approved_by,
        "approved_at": now(),
        "frozen_sha256": None,
    }
    digest = frozen_manifest_digest(manifest)
    manifest["approval"]["frozen_sha256"] = digest
    write_json(ROOT / MANIFEST_FROZEN, manifest)
    draft_path.unlink()

    protocol_path = ROOT / PROTOCOL_MANIFEST
    if protocol_path.exists():
        protocol = read_json(protocol_path)
        protocol["status"] = "approved_2026-09-21"
        protocol["approved_gate_policy"] = {
            "target_weight": TARGET_WEIGHT,
            "non_target_weight": NON_TARGET_WEIGHT,
            "b_soft_penalty_weight": B_SOFT_PENALTY_WEIGHT,
            "b_severe_penalty_weight": B_SEVERE_PENALTY_WEIGHT,
            "first_pass_catastrophic_delta": FIRST_PASS_CATASTROPHIC_DELTA,
            "micro_min_u": MICRO_MIN_U,
            "full_train": {
                "b_mean_severe_max": 3.0,
                "b_q_max": 14.0,
                "cross_dim_clear_regressions_allowed": 0,
                "non_target_new_e_members": 0,
                "non_target_soft_to_severe": 0,
            },
            "validation": {
                "b_mean_severe_max": 1.5,
                "b_q_max": 4.75,
                "clear_regressions_allowed": 0,
            },
            "regular": {
                "required_improve_rate": 0.5,
                "ceil_rule": True,
                "target_regressions_allowed": 0,
                "control_regressions_allowed": 1,
                "cross_dim_regressions_allowed": 1,
            },
            "source": "e_gate_engine.py / decision_thresholds.py",
        }
        write_json(protocol_path, protocol)
    return manifest


# ============================================================
# Rule generation
# ============================================================
def build_explicit_samples(manifest: Dict[str, Any]) -> Dict[str, Any]:
    evidence = read_json(ROOT / EVIDENCE_LOCAL)["samples"]
    train_rows = {int(row["index"]): row for row in load_rows(str(ROOT / TRAIN_RUN1))}
    mean_rows_all = load_rows(str(ROOT / TRAIN_MEAN))
    stats = {
        dim: run_dimension_stats(mean_rows_all, dim) for dim in DIMENSIONS
    }
    roles = classify_manifest(manifest)

    def entry(index: int, z_key: str = "mean_z") -> Dict[str, Any]:
        ev = evidence[str(index)]
        row = train_rows[index]
        mean_delta = ev["evidence"]["mean_delta"]
        z_values = ev["evidence"]["e_consensus_z"].get(TARGET_DIM)
        z_score = z_values[z_key] if z_values else stats[TARGET_DIM][index]["z_score"]
        return {
            "name": row.get("name") or f"essay-{index}",
            "essay": row.get("essay", ""),
            "teacher": {dim: float(row["teacher"][dim]) for dim in DIMENSIONS},
            "AI": {dim: float(ev["v6_mean_scores"][dim]) for dim in DIMENSIONS},
            "diff": mean_delta[TARGET_DIM],
            "residual": stats[TARGET_DIM][index]["residual"],
            "z_score": z_score,
            "direction": "lenient" if z_score > 0 else "strict",
            "index": index,
        }

    outliers = [entry(index) for index in roles["targets"]]
    normals = [entry(index) for index in roles["controls"]]
    return {"outliers": outliers, "normals": normals}


def gate0_checks(
    rule: Dict[str, Any], final_meta_text: str, pools: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    from prompt_structure_contract import (
        MAX_ADDED_CHARS_PER_ITERATION,
        MAX_PROMPT_CHARS,
        MAX_NEW_BOLD_SECTIONS_PER_ROUND,
        dimension_anchor_map,
        validate_optimizer_edit,
    )

    checks: List[Dict[str, Any]] = []

    def check(code: str, ok: bool, detail: str) -> None:
        checks.append({"code": code, "ok": bool(ok), "detail": detail})

    if pools is None:
        manifest_path = ROOT / MANIFEST_FROZEN
        if manifest_path.exists():
            roles = classify_manifest(read_json(manifest_path))
            pools = {"outliers": set(roles["targets"]), "normals": set(roles["controls"])}
        else:
            pools = None
    pool_note = (
        f"outlier pool {sorted(pools['outliers'])}, normal pool {sorted(pools['normals'])}"
        if pools
        else "skipped: frozen manifest unavailable"
    )

    anchor = dimension_anchor_map(final_meta_text).get("structure")
    anchor_ok = rule.get("should_be_injected_at", "").strip() == anchor
    check("G0-anchor", anchor_ok, f"should_be_injected_at={rule.get('should_be_injected_at')!r} vs {anchor!r}")

    from etype_preference_analyzer import ContrastiveETypeAnalyzer

    analyzer = ContrastiveETypeAnalyzer()
    hard = analyzer.is_hard_threshold_rule(rule)
    check("G0-hard-threshold", not hard, "rule must not contain fixed numeric anchors")

    scope = rule.get("expected_scope") or []
    scope_ok = set(scope) <= {TARGET_DIM} and bool(scope)
    check("G0-scope", scope_ok, f"expected_scope={scope} must be a subset of ['{TARGET_DIM}']")

    evidence = rule.get("evidence") or {}
    outlier_indices = evidence.get("outlier_indices") or []
    normal_indices = evidence.get("normal_indices") or []
    check("G0-evidence", bool(outlier_indices), f"outlier_indices={outlier_indices}")
    if pools is not None:
        count_value = rule.get("evidence_count")
        pool_ok = (
            set(outlier_indices) <= pools["outliers"]
            and set(normal_indices) <= pools["normals"]
            and count_value == len(outlier_indices)
        )
        check(
            "G0-evidence-pool",
            pool_ok,
            f"outlier={outlier_indices}, normal={normal_indices}, "
            f"evidence_count={count_value} vs {len(outlier_indices)}; {pool_note}",
        )
    else:
        check("G0-evidence-pool", True, pool_note)

    core = (
        str(rule.get("trigger_condition", "")) + str(rule.get("scoring_adjustment", ""))
    ).replace(" ", "").replace("，", "").replace("、", "")
    existing = final_meta_text.replace(" ", "").replace("，", "").replace("、", "")
    duplicate = len(core) >= 10 and core in existing
    check("G0-duplicate", not duplicate, "rule must not be a rewrite of existing prompt items")

    try:
        injected, rendered = structure_injection(final_meta_text, rule)
        edit_violations = validate_optimizer_edit(final_meta_text, injected)
        added = len(injected) - len(final_meta_text)
        check(
            "G0-structure-contract",
            not edit_violations,
            f"violations={[str(v) for v in edit_violations]}",
        )
        check(
            "G0-budgets",
            0 <= added <= MAX_ADDED_CHARS_PER_ITERATION and len(injected) <= MAX_PROMPT_CHARS,
            f"added_chars={added} (<= {MAX_ADDED_CHARS_PER_ITERATION}), "
            f"total_chars={len(injected)} (<= {MAX_PROMPT_CHARS}), "
            f"max_new_bold={MAX_NEW_BOLD_SECTIONS_PER_ROUND}",
        )
    except Exception as exc:  # noqa: BLE001 - recorded as a failed check
        check("G0-structure-contract", False, f"dry-run failed: {exc}")
        check("G0-budgets", False, "dry-run failed")

    passed = all(item["ok"] for item in checks)
    return {"passed": passed, "checks": checks}


def select_candidate_rule(
    rules: List[Dict[str, Any]],
    final_meta_text: str,
    pools: Optional[Dict[str, Any]] = None,
    start_index: int = 0,
) -> Dict[str, Any]:
    """Lazy sequential selection (ruled 2026-09-21).

    Take the rules in their original response order; run the Gate 0 hard
    rejections ON the taken rule; on rejection fall through to the next rule;
    stop at the first pass. No ranking or pre-screening; every attempt is kept
    for the audit trail. ``start_index`` lets the project owner direct the scan
    past an already-consumed candidate (e.g. candidate 1 evaluated & rejected
    -> start at 1); the scan still falls through lazily from there.
    """
    attempts: List[Dict[str, Any]] = []
    for index, rule in enumerate(rules[start_index:], start=start_index):
        gate = gate0_checks(rule, final_meta_text, pools)
        attempts.append({"index": index, "passed": gate["passed"], "checks": gate["checks"]})
        if gate["passed"]:
            return {
                "passed": True,
                "selected_index": index,
                "rule": rule,
                "gate0": gate,
                "attempts": attempts,
                "start_index": start_index,
            }
    return {
        "passed": False,
        "selected_index": None,
        "rule": None,
        "gate0": None,
        "attempts": attempts,
        "start_index": start_index,
    }


def _selection_block(sequence: Dict[str, Any], total_rules: int) -> Dict[str, Any]:
    start_index = sequence.get("start_index", 0)
    tried = sequence["attempts"]
    return {
        "policy": "lazy_sequential_first_pass",
        "ruling": (
            "2026-09-21 惰性补全裁定：按序取第一条；Gate 0 硬拒绝在选取之后执行；"
            "未通过则顺延至下一条；不做前筛选排序"
        ),
        "scan_start_index": start_index,
        "source": "human_directed_start" if start_index else "auto_first_pass",
        "total_rules_returned": total_rules,
        "tried": tried,
        "untried": max(total_rules - (start_index + len(tried)), 0),
        "selected_index": sequence["selected_index"],
    }


AUTO_APPROVAL_RULING = (
    "2026-09-21 裁定：硬审查（Gate 0 串行检查）通过即进入测试流程，此节点不设人工通过点；"
    "人工否决权保留至执行开始前"
)


def finalize_selected_rule(
    payload: Dict[str, Any], sequence: Dict[str, Any], total_rules: int
) -> Dict[str, Any]:
    """Apply the lazy-selection outcome to a rule payload (pure).

    Ruled 2026-09-21: a Gate 0 pass admits the rule automatically
    (status ``approved_pending_execution``, no human pass required). A full
    rejection sweep yields ``E_NO_VALID_CANDIDATE``.
    """
    payload["selection"] = _selection_block(sequence, total_rules)
    payload["selected_at"] = now()
    if sequence["passed"]:
        payload["status"] = "approved_pending_execution"
        payload["rule"] = sequence["rule"]
        payload["rule_index"] = sequence["selected_index"]
        payload["gate0"] = sequence["gate0"]
        payload["human_review"] = {
            "status": "not_required",
            "approved_by": None,
            "approved_at": None,
            "note": AUTO_APPROVAL_RULING,
            "veto_available_until": "execution start",
        }
    else:
        payload["status"] = "E_NO_VALID_CANDIDATE"
        payload["rule"] = None
        payload["rule_index"] = None
        payload["gate0"] = None
    return payload


def record_selection_ruling(protocol_path: Optional[Path] = None) -> None:
    """Record the lazy-sequential selection ruling in the protocol manifest."""
    path = protocol_path or (ROOT / PROTOCOL_MANIFEST)
    if not path.exists():
        return
    protocol = read_json(path)
    protocol["rule_selection_policy"] = {
        "policy": "lazy_sequential_first_pass",
        "description_kind": "operative_adjudication",
        "ruled_by": "项目负责人",
        "ruled_at": "2026-09-21",
        "description": (
            "按响应数组原序取第 1 条，在其上执行 Gate 0 硬拒绝检查（注入位置/范围/硬阈值/"
            "证据池/重复/契约预算）；未通过则顺延至下一条；全部未通过则 E_NO_VALID_CANDIDATE；"
            "不做前筛选排序；已归档响应不得重发请求"
        ),
        "admission": "hard_review_pass_auto_admits",
        "admission_ruling": (
            "2026-09-21 裁定：硬审查通过即进入测试流程，此节点不设人工通过点；"
            "人工否决权保留至执行开始前；全部门禁通过后仍停在 "
            "E_CANDIDATE_PASSED_AWAITING_HUMAN，无自动晋升"
        ),
        "rationale": "惰性补全：最小机械干预，避免引入人工权重与选择偏差",
    }
    write_json(path, protocol)


def generate_rule() -> int:
    require_baseline_ok()
    manifest = require_frozen_manifest()
    guard = BudgetGuard()

    rule_path = ROOT / RULE_FILE
    attempts = 0
    if rule_path.exists():
        previous = read_json(rule_path)
        attempts = int(previous.get("generation_attempts", 1))
        if previous.get("status") in (
            "awaiting_human_semantic_approval",
            "approved_pending_execution",
        ) or previous.get("human_review", {}).get("status") == "approved":
            print("[ok] candidate rule already generated; nothing to do")
            return 0
        archived_rules = ((previous.get("analysis") or {}).get("localized_prompt_rules")) or []
        if archived_rules:
            print(
                "[blocked] archived analysis response exists; apply `select-rule` "
                "instead of re-calling (no re-sampling allowed)"
            )
            return 3
        if attempts >= 2:
            print("[blocked] non-semantic retry budget exhausted")
            return 3

    record_selection_ruling()

    samples = build_explicit_samples(manifest)
    final_meta_text = (ROOT / FINAL_META).read_text(encoding="utf-8")

    analyzer = make_budgeted_analyzer(guard)
    analyzer.OUTPUT_DIR = str(ANALYSIS_DIR)
    analyzer.SCORING_PROMPT_PATH = FINAL_META
    analyzer.ALL_DATA_FILE = TRAIN_MEAN
    analyzer.EXPLICIT_SAMPLES = {TARGET_DIM: samples}

    badcase_data = {
        "statistics": {
            "total_count": 36,
            "E_residual": {
                dim: {"severe_count": 0, "soft_count": 0, "filtered_count": 0, "remaining_count": 36}
                for dim in DIMENSIONS
            },
        },
        "E_residual": {
            dim: {"severe": [], "soft": []} for dim in DIMENSIONS
        },
    }

    try:
        result, log = analyzer.analyze_dimension(badcase_data, final_meta_text, TARGET_DIM)
    except BudgetExceeded as exc:
        payload = {
            "status": "E_BLOCKED_INFRASTRUCTURE",
            "error": str(exc),
            "generation_attempts": attempts + 1,
            "budget": guard.summary(),
            "created_at": now(),
        }
        write_json(rule_path, payload)
        print(f"[blocked] {exc}")
        return 3

    if not result:
        payload = {
            "status": "E_BLOCKED_INFRASTRUCTURE",
            "error": "analysis response missing or unparsable (non-semantic)",
            "generation_attempts": attempts + 1,
            "budget": guard.summary(),
            "created_at": now(),
        }
        write_json(rule_path, payload)
        print("[blocked] analysis failed; one non-semantic retry is allowed by rerunning generate-rule")
        return 3

    rules = result.get("localized_prompt_rules") or []
    if not rules:
        payload = {
            "status": "E_NO_VALID_CANDIDATE",
            "error": "analysis returned no localized_prompt_rules",
            "generation_attempts": attempts + 1,
            "analysis": result,
            "budget": guard.summary(),
            "created_at": now(),
        }
        write_json(rule_path, payload)
        print("[stop] no valid candidate (no rules returned)")
        return 4

    sequence = select_candidate_rule(rules, final_meta_text)
    selection = _selection_block(sequence, len(rules))
    if not sequence["passed"]:
        payload = {
            "status": "E_NO_VALID_CANDIDATE",
            "error": "every returned rule failed the Gate 0 hard checks",
            "generation_attempts": attempts + 1,
            "analysis": result,
            "selection": selection,
            "budget": guard.summary(),
            "created_at": now(),
        }
        write_json(rule_path, payload)
        print(
            json.dumps(
                {"status": "E_NO_VALID_CANDIDATE", "tried": len(sequence["attempts"])},
                ensure_ascii=False,
            )
        )
        return 4

    payload = {
        "created_at": now(),
        "generation_attempts": attempts + 1,
        "source": {
            "consensus_version": "E-consensus-robust-z-v1",
            "manifest": MANIFEST_FROZEN,
            "manifest_sha256": sha256_file(ROOT / MANIFEST_FROZEN),
        },
        "analysis_stats": (log or {}).get("analysis_stats"),
        "budget": guard.summary(),
    }
    payload = finalize_selected_rule(payload, sequence, len(rules))
    write_json(rule_path, payload)
    print(
        json.dumps(
            {"status": payload["status"], "selected_index": sequence["selected_index"]},
            ensure_ascii=False,
        )
    )
    return 0


def approve_rule(
    decision: str,
    approved_by: str,
    note: str,
    rule_path: Optional[Path] = None,
    report_path: Optional[Path] = None,
) -> int:
    """Optional human veto; admission itself is automatic after Gate 0 (2026-09-21).

    ``approve`` is a no-op when the rule was already admitted automatically;
    legacy payloads in ``awaiting_human_semantic_approval`` are still accepted.
    ``reject`` is the veto path and is available only before execution starts.
    """
    rule_path = rule_path or (ROOT / RULE_FILE)
    if not rule_path.exists():
        print("[blocked] no candidate rule to approve")
        return 3
    payload = read_json(rule_path)
    status = payload.get("status")
    if decision == "approve":
        if status == "approved_pending_execution":
            print("[ok] already admitted by the Gate 0 hard review; approval not required")
            return 0
        if status != "awaiting_human_semantic_approval":
            print(f"[blocked] rule status is {status}")
            return 3
        payload["human_review"] = {
            "status": "approved",
            "approved_by": approved_by,
            "approved_at": now(),
            "note": note,
        }
        payload["status"] = "approved_pending_execution"
    else:
        report_path = report_path or (ROOT / REPORT_FILE)
        if report_path.exists() and (read_json(report_path).get("stages") or {}):
            print("[blocked] execution already started; the veto window is closed")
            return 3
        if status not in ("awaiting_human_semantic_approval", "approved_pending_execution"):
            print(f"[blocked] rule status is {status}")
            return 3
        payload["human_review"] = {
            "status": "rejected",
            "approved_by": approved_by,
            "approved_at": now(),
            "note": note,
        }
        payload["status"] = "E_NO_VALID_CANDIDATE"
    write_json(rule_path, payload)
    print(json.dumps({"status": payload["status"]}, ensure_ascii=False))
    return 0


def select_rule(start_index: int = 0) -> int:
    """Apply the lazy sequential Gate 0 to the ARCHIVED analysis response.

    A passing rule is admitted automatically (ruled 2026-09-21); no API call
    is made and no human pass is required at this node. ``start_index`` is the
    human-directed scan start (e.g. 1 = the second rule after candidate 1 was
    evaluated and rejected).
    """
    require_baseline_ok()
    require_frozen_manifest()
    record_selection_ruling()
    rule_path = ROOT / RULE_FILE
    if not rule_path.exists():
        print("[blocked] no archived e_candidate_rule.json to select from")
        return 3
    payload = read_json(rule_path)
    human = (payload.get("human_review") or {}).get("status")
    if human in ("approved", "rejected"):
        print(f"[blocked] human review already recorded: {human}")
        return 3
    rules = ((payload.get("analysis") or {}).get("localized_prompt_rules")) or []
    if not rules:
        print("[blocked] archived payload contains no parsed rules")
        return 3
    if start_index < 0 or start_index >= len(rules):
        print(f"[blocked] start-index {start_index} outside 0..{len(rules) - 1}")
        return 3
    final_meta_text = (ROOT / FINAL_META).read_text(encoding="utf-8")
    sequence = select_candidate_rule(rules, final_meta_text, start_index=start_index)
    history = payload.get("history") or []
    if payload.get("rule_index") is not None or payload.get("selected_at"):
        history.append(
            {
                "rule_index": payload.get("rule_index"),
                "rule_status": payload.get("status"),
                "selected_at": payload.get("selected_at"),
                "evaluation_status": _report_status(),
            }
        )
    payload["history"] = history
    payload = finalize_selected_rule(payload, sequence, len(rules))
    write_json(rule_path, payload)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "selected_index": sequence["selected_index"],
                "start_index": start_index,
                "tried": len(sequence["attempts"]),
            },
            ensure_ascii=False,
        )
    )
    return 0 if sequence["passed"] else 4


# ============================================================
# Execution: candidate prompt + scoring stages + gates
# ============================================================
def build_candidate_prompt(rule: Dict[str, Any]) -> Tuple[str, str]:
    final_meta_text = (ROOT / FINAL_META).read_text(encoding="utf-8")
    injected, rendered = structure_injection(final_meta_text, rule)
    from prompt_structure_contract import validate_optimizer_edit

    violations = validate_optimizer_edit(final_meta_text, injected)
    if violations:
        raise RuntimeError(f"[BLOCKED] candidate prompt violates contract: {violations}")
    (ROOT / CANDIDATE_META).write_text(injected, encoding="utf-8")
    from preprocess_prompt import PromptPreprocessor

    PromptPreprocessor().process(
        input_path=str(ROOT / CANDIDATE_META), output_path=str(ROOT / CANDIDATE_PROMPT)
    )
    return rendered, injected


def subset_rows(source_rows: List[Dict[str, Any]], indices: List[int]) -> List[Dict[str, Any]]:
    wanted = set(indices)
    return [row for row in source_rows if int(row["index"]) in wanted]


def score_stage(
    guard: BudgetGuard,
    label: str,
    indices: List[int],
    origin_path: str,
    output_path: Path,
) -> List[Dict[str, Any]]:
    origin_rows = load_rows(str(ROOT / origin_path))
    subset = subset_rows(origin_rows, indices)
    essays_path = ANALYSIS_DIR / f"e_essays_{label}.json"
    write_json(essays_path, subset)

    scorer = make_budgeted_scorer(guard)
    scorer.prompt_path = str(ROOT / CANDIDATE_PROMPT)
    scorer.essays_path = str(essays_path)
    scorer.output_path = str(output_path)
    scorer.origin_data_path = str(ROOT / origin_path)
    scorer.run()
    if not output_path.exists():
        raise RuntimeError(f"[BLOCKED] scoring produced no file for stage {label}")
    return load_rows(str(output_path))


def _stage_identity(label: str, indices: List[int]) -> Dict[str, Any]:
    """Identity of a scoring stage per the frozen reuse key."""
    return {
        "prompt_sha256": sha256_file(ROOT / CANDIDATE_PROMPT),
        "model": os.getenv("AES_SCORING_MODEL", "claude-sonnet-5"),
        "global_indices": sorted({int(index) for index in indices}),
        "repeat_id": label,
        "scoring_protocol": SCORING_PROTOCOL_ID,
    }


def stage_rows(
    guard: BudgetGuard,
    label: str,
    indices: List[int],
    origin_path: str,
    output_path: Path,
    report: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Score a stage, or reuse a saved file ONLY when its identity sidecar
    matches the frozen reuse key (prompt_sha256 / model / global_indices /
    repeat_id / scoring_protocol). A missing sidecar or any mismatch blocks
    the run instead of silently reusing foreign results (review fix
    2026-09-21)."""
    identity = _stage_identity(label, indices)
    sidecar = output_path.with_suffix(".meta.json")
    if output_path.exists():
        recorded = read_json(sidecar) if sidecar.exists() else None
        if recorded is None:
            raise RuntimeError(
                f"[BLOCKED] stage file {output_path.name} has no identity sidecar; "
                "refusing silent reuse (archive or remove the file)"
            )
        mismatch = next(
            (key for key in identity if recorded.get(key) != identity[key]), None
        )
        if mismatch is not None:
            raise RuntimeError(
                f"[BLOCKED] stage file {output_path.name} identity mismatch on "
                f"{mismatch!r}; refusing silent reuse (archive or remove the file)"
            )
        report.setdefault("reused_stages", []).append(output_path.name)
        return load_rows(str(output_path))
    rows = score_stage(guard, label, indices, origin_path, output_path)
    write_json(sidecar, {**identity, "written_at": now()})
    return rows


def sanitize_rule_for_public(rule: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Rule copy with essay excerpts removed (review privacy policy 2026-09-21)."""
    if rule is None:
        return None
    clean: Dict[str, Any] = {}
    for key, value in rule.items():
        if key in ("positive_evidence_spans", "counter_evidence_spans"):
            clean[key] = {"removed_for_privacy": len(value or [])}
        else:
            clean[key] = value
    return clean


def export_public_rule() -> int:
    """Write e_candidate_rule.public.json (span-free committable copy)."""
    rule_path = ROOT / RULE_FILE
    if not rule_path.exists():
        print("[blocked] no archived e_candidate_rule.json")
        return 3
    payload = read_json(rule_path)
    rules = ((payload.get("analysis") or {}).get("localized_prompt_rules")) or []
    public = {
        "note": (
            "e_candidate_rule.json 的公开脱敏副本；证据引文 span（作文原文片段）"
            "已删除，其余审计字段保留"
        ),
        "created_at": now(),
        "status": payload.get("status"),
        "generation_attempts": payload.get("generation_attempts"),
        "source": payload.get("source"),
        "analysis_stats": payload.get("analysis_stats"),
        "selection": payload.get("selection"),
        "history": payload.get("history"),
        "human_review": payload.get("human_review"),
        "budget": payload.get("budget"),
        "selected_at": payload.get("selected_at"),
        "rule_index": payload.get("rule_index"),
        "rule": sanitize_rule_for_public(payload.get("rule")),
        "rules_returned": [sanitize_rule_for_public(rule) for rule in rules],
    }
    write_json(ROOT / "e_candidate_rule.public.json", public)
    print(json.dumps({"written": "e_candidate_rule.public.json"}, ensure_ascii=False))
    return 0


def execute(first_pass_only: bool = False) -> int:
    require_baseline_ok()
    manifest = require_frozen_manifest()
    rule_payload = read_json(ROOT / RULE_FILE) if (ROOT / RULE_FILE).exists() else None
    if not rule_payload or rule_payload.get("status") != "approved_pending_execution":
        raise RuntimeError("[BLOCKED] candidate rule is not approved for execution")

    guard = BudgetGuard()
    rule_index = int(rule_payload.get("rule_index", 0))
    report = load_fresh_report(rule_index)
    report["manifest"] = MANIFEST_FROZEN
    report["manifest_sha256"] = sha256_file(ROOT / MANIFEST_FROZEN)
    report["rule_status"] = rule_payload["status"]

    rendered, injected = build_candidate_prompt(rule_payload["rule"])
    report["candidate_prompt"] = {"meta": CANDIDATE_META, "prompt": CANDIDATE_PROMPT}
    candidate_tag = rule_index + 1

    roles = classify_manifest(manifest)
    micro_indices = roles["all_indices"]
    train_all = [int(row["index"]) for row in load_rows(str(ROOT / TRAIN_RUN1))]
    rest_indices = [index for index in train_all if index not in set(micro_indices)]
    val_indices = [int(row["index"]) for row in load_rows(str(ROOT / VAL_RUN1))]

    baseline_train_r1 = load_rows(str(ROOT / TRAIN_RUN1))
    baseline_train_r2 = load_rows(str(ROOT / TRAIN_RUN2))
    baseline_mean_all = load_rows(str(ROOT / TRAIN_MEAN))
    baseline_train_mean = subset_rows(baseline_mean_all, micro_indices)

    def finish(status: str) -> int:
        report["status"] = status
        report["budget"] = guard.summary()
        report["updated_at"] = now()
        write_report(report)
        print(json.dumps({"status": status, "budget": guard.summary()}, ensure_ascii=False))
        return 0

    try:
        micro_r1 = stage_rows(
            guard, "micro_r1", micro_indices, TRAIN_RUN1,
            ANALYSIS_DIR / f"e_scoring_cand{candidate_tag}_micro_r1.json",
            report,
        )
        first_pass = first_pass_check(
            micro_r1, subset_rows(baseline_train_r1, micro_indices), micro_indices
        )
        report["stages"]["first_pass"] = first_pass
        write_report(report)
        if not first_pass["passed"]:
            return finish("E_EVALUATED_REJECTED")
        if first_pass_only:
            report["first_pass_only"] = True
            return finish("E_FIRST_PASS_PASSED_PENDING_BUDGET")

        micro_r2 = stage_rows(
            guard, "micro_r2", micro_indices, TRAIN_RUN1,
            ANALYSIS_DIR / f"e_scoring_cand{candidate_tag}_micro_r2.json",
            report,
        )
        candidate_micro_mean = mean_rows(micro_r1, micro_r2, "candidate micro")

        micro = micro_evaluate(manifest, baseline_train_mean, candidate_micro_mean)
        report["stages"]["micro"] = micro
        write_report(report)
        if not micro["passed"]:
            return finish("E_EVALUATED_REJECTED")

        regular = regular_evaluate(manifest, baseline_train_mean, candidate_micro_mean)
        report["stages"]["regular"] = regular
        write_report(report)
        if not regular["passed"]:
            return finish("E_EVALUATED_REJECTED")

        rest_r1 = stage_rows(
            guard, "train_rest_r1", rest_indices, TRAIN_RUN1,
            ANALYSIS_DIR / f"e_scoring_cand{candidate_tag}_train_rest_r1.json",
            report,
        )
        rest_r2 = stage_rows(
            guard, "train_rest_r2", rest_indices, TRAIN_RUN1,
            ANALYSIS_DIR / f"e_scoring_cand{candidate_tag}_train_rest_r2.json",
            report,
        )
        candidate_train_r1 = micro_r1 + rest_r1
        candidate_train_r2 = micro_r2 + rest_r2
        write_json(
            ANALYSIS_DIR / f"e_candidate_train_cand{candidate_tag}_run1.json",
            candidate_train_r1,
        )
        write_json(
            ANALYSIS_DIR / f"e_candidate_train_cand{candidate_tag}_run2.json",
            candidate_train_r2,
        )

        baseline_consensus = e_consensus(baseline_train_r1, baseline_train_r2)
        full = full_train_evaluate(
            baseline_train_r1,
            baseline_train_r2,
            baseline_consensus,
            candidate_train_r1,
            candidate_train_r2,
        )
        report["stages"]["full_train"] = full
        write_report(report)
        if not full["passed"]:
            return finish("E_EVALUATED_REJECTED")

        val_r1 = stage_rows(
            guard, "val_r1", val_indices, VAL_RUN1,
            ANALYSIS_DIR / f"e_scoring_cand{candidate_tag}_val_r1.json",
            report,
        )
        val_r2 = stage_rows(
            guard, "val_r2", val_indices, VAL_RUN1,
            ANALYSIS_DIR / f"e_scoring_cand{candidate_tag}_val_r2.json",
            report,
        )
        validation = validation_evaluate(
            load_rows(str(ROOT / VAL_RUN1)),
            load_rows(str(ROOT / VAL_RUN2)),
            val_r1,
            val_r2,
        )
        report["stages"]["validation"] = validation
        write_report(report)
        if not validation["passed"]:
            return finish("E_EVALUATED_REJECTED")

        return finish("E_CANDIDATE_PASSED_AWAITING_HUMAN")

    except BudgetExceeded as exc:
        report["stages"]["budget_abort"] = {"error": str(exc), "at": now()}
        return finish("E_BLOCKED_INFRASTRUCTURE")
    except RuntimeError as exc:
        report["stages"]["infrastructure_abort"] = {"error": str(exc), "at": now()}
        return finish("E_BLOCKED_INFRASTRUCTURE")


def status() -> int:
    state: Dict[str, Any] = {"files": {}}
    for name in (MANIFEST_DRAFT, MANIFEST_FROZEN, RULE_FILE, REPORT_FILE):
        path = ROOT / name
        state["files"][name] = path.exists()
    if (ROOT / MANIFEST_FROZEN).exists():
        manifest = read_json(ROOT / MANIFEST_FROZEN)
        state["manifest_approval"] = manifest.get("approval")
        recorded = (manifest.get("approval") or {}).get("frozen_sha256")
        actual = frozen_manifest_digest(manifest)
        state["manifest_integrity"] = (
            "valid"
            if recorded and actual == recorded
            else f"invalid (recorded={recorded}, actual={actual})"
        )
    if (ROOT / RULE_FILE).exists():
        rule_payload = read_json(ROOT / RULE_FILE)
        state["rule_status"] = rule_payload.get("status")
        state["rule_human_review"] = rule_payload.get("human_review")
        state["rule_selection"] = rule_payload.get("selection")
    if BUDGET_STATE.exists():
        state["budget"] = read_json(BUDGET_STATE)
    if (ROOT / REPORT_FILE).exists():
        state["run_status"] = read_json(ROOT / REPORT_FILE).get("status")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    freeze = sub.add_parser("freeze-manifest")
    freeze.add_argument("--approved-by", required=True)

    sub.add_parser("generate-rule")
    select = sub.add_parser("select-rule")
    select.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="response position where the lazy scan starts (0 = first rule)",
    )

    approve = sub.add_parser("approve-rule")
    approve.add_argument("--decision", choices=["approve", "reject"], required=True)
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--note", default="")

    execute_parser = sub.add_parser("execute")
    execute_parser.add_argument(
        "--first-pass-only",
        action="store_true",
        help="cheap screen authorised by ruling: stop after the first-pass gate",
    )
    sub.add_parser("export-public-rule")
    sub.add_parser("status")

    args = parser.parse_args(argv)

    try:
        if args.command == "freeze-manifest":
            manifest = freeze_manifest(args.approved_by)
            print(json.dumps({"frozen_sha256": manifest["approval"]["frozen_sha256"]},
                             ensure_ascii=False))
            return 0
        if args.command == "generate-rule":
            return generate_rule()
        if args.command == "select-rule":
            return select_rule(args.start_index)
        if args.command == "approve-rule":
            return approve_rule(args.decision, args.approved_by, args.note)
        if args.command == "execute":
            return execute(args.first_pass_only)
        if args.command == "export-public-rule":
            return export_public_rule()
        return status()
    except RuntimeError as exc:
        print(str(exc))
        return 3


if __name__ == "__main__":
    sys.exit(main())
