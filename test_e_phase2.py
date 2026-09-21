"""Offline tests for the E-route Phase-2 build (gate engine + orchestrator)."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import decision_thresholds as dt
from e_gate_engine import (
    classify_manifest,
    first_pass_check,
    full_train_evaluate,
    micro_evaluate,
    regular_evaluate,
    validation_evaluate,
)

import e_phase2_run as p2

ROOT = Path(__file__).resolve().parent


def _anchor_files_present() -> bool:
    manifest_path = ROOT / "e_baseline_manifest.json"
    if not manifest_path.exists():
        return False
    anchors = json.loads(manifest_path.read_text(encoding="utf-8")).get("anchors", [])
    return all((ROOT / anchor["path"]).exists() for anchor in anchors)


LOCAL_ANCHORS_AVAILABLE = _anchor_files_present()

DIMS = ("content", "expression", "structure")


def row(index, teacher=(6, 6, 6), ai=(6, 6, 6)):
    return {
        "index": index,
        "name": f"essay-{index}",
        "page": "p1",
        "essay": "text",
        "teacher": dict(zip(DIMS, teacher)),
        "AI": dict(zip(DIMS, ai)),
    }


def manifest_with(roles_by_index):
    """roles_by_index: {index: [roles]} -> manifest-shaped dict."""
    return {
        "samples": [
            {
                "global_index": index,
                "evidence_roles": roles,
                "selection_reasons": [],
            }
            for index, roles in sorted(roles_by_index.items())
        ]
    }


def consensus(structure=((), ()), content=((), ()), expression=((), ())):
    members = {"structure": structure, "content": content, "expression": expression}
    by_dim = {}
    counts = {}
    for dim in DIMS:
        severe, soft = members[dim]
        by_dim[dim] = {
            "severe": [{"index": index} for index in severe],
            "soft": [{"index": index} for index in soft],
            "below_soft_count": 0,
        }
        counts[dim] = {"severe": len(severe), "soft": len(soft)}
    return {"counts": counts, "by_dim": by_dim}


class ThresholdTests(unittest.TestCase):
    def test_delta_boundaries(self):
        self.assertTrue(dt.is_clear_improvement(-1.0))
        self.assertFalse(dt.is_clear_improvement(-0.99))
        self.assertTrue(dt.is_clear_regression(1.0))
        self.assertFalse(dt.is_clear_regression(0.99))
        self.assertEqual(dt.classify_delta_abs_error(-1.0), "clear_improvement")
        self.assertEqual(dt.classify_delta_abs_error(1.0), "clear_regression")

    def test_required_improvement_count(self):
        self.assertEqual(dt.required_improvement_count(4), 2)
        self.assertEqual(dt.required_improvement_count(5), 3)
        self.assertEqual(dt.required_improvement_count(1), 1)
        self.assertEqual(dt.required_improvement_count(0), 0)

    def test_approved_weights(self):
        self.assertEqual(dt.TARGET_WEIGHT, 1.0)
        self.assertAlmostEqual(dt.NON_TARGET_WEIGHT, 1 / 3)
        self.assertAlmostEqual(dt.B_SOFT_PENALTY_WEIGHT, 1 / 3)
        self.assertAlmostEqual(dt.B_SEVERE_PENALTY_WEIGHT, 2 / 3)
        self.assertEqual(dt.MICRO_MIN_U, 0.01)
        self.assertEqual(dt.FIRST_PASS_CATASTROPHIC_DELTA, 2.0)


class FirstPassTests(unittest.TestCase):
    def test_catastrophic_boundary_inclusive(self):
        baseline = [row(1)]
        candidate_ok = [row(1, ai=(6, 6, 6 + 1.99))]
        candidate_bad = [row(1, ai=(6, 6, 6 + 2.0))]
        self.assertTrue(first_pass_check(candidate_ok, baseline, [1])["passed"])
        result = first_pass_check(candidate_bad, baseline, [1])
        self.assertFalse(result["passed"])
        self.assertEqual(result["violations"][0]["delta"], 2.0)


class MicroGateTests(unittest.TestCase):
    def setUp(self):
        self.manifest = manifest_with(
            {
                1: ["structure_e_target"],
                2: ["structure_e_target"],
                3: ["normal_control"],
                4: ["cross_dim_content"],
                5: ["b_consensus_severe"],
            }
        )
        # baseline errors: target1 structure err 2, target2 err 1; others clean.
        self.baseline = [
            row(1, ai=(6, 6, 4)),
            row(2, ai=(6, 6, 7)),
            row(3),
            row(4, ai=(6.5, 6, 6)),
            row(5),
        ]

    def test_pass_case_and_u_math(self):
        candidate = [
            row(1, ai=(6, 6, 6)),      # structure delta -2 -> u_target +2
            row(2, ai=(6, 6, 6)),      # structure delta -1 -> u_target +1
            row(3),
            row(4, ai=(6.8, 6, 6)),    # content delta +0.3 -> u_other sample -0.05
            row(5, ai=(6.6, 6.6, 6.6)),  # m_B 0 -> 0.6 -> u_b -0.4
        ]
        result = micro_evaluate(self.manifest, self.baseline, candidate)
        self.assertTrue(result["passed"])
        self.assertAlmostEqual(result["u"]["target"], 1.5)
        # sample4: -0.05 ; sample5: -(1/3)*0.6 = -0.2 ; mean over 5 samples
        self.assertAlmostEqual(result["u"]["other"], (-0.05 - 0.2) / 5)
        self.assertAlmostEqual(result["u"]["b"], -0.4)
        self.assertEqual(sorted(result["improvements"]), [1, 2])
        self.assertEqual(result["violations"], [])

    def test_improvement_but_tiny_u_fails(self):
        candidate = [
            row(1, ai=(6, 6, 5)),      # delta -1.0 -> +1.0
            row(2, ai=(6, 6, 7.99)),   # delta +0.99 -> -0.99
            row(3),
            row(4, ai=(6.5, 6, 6)),    # unchanged non-target dims -> no U_other credit
            row(5),
        ]
        result = micro_evaluate(self.manifest, self.baseline, candidate)
        self.assertFalse(result["passed"])
        self.assertGreater(result["u"]["total"], 0.0)
        self.assertLessEqual(result["u"]["total"], dt.MICRO_MIN_U)
        self.assertEqual(result["improvements"], [1])

    def test_clear_regressions_are_violations(self):
        candidate = [
            row(1, ai=(6, 6, 6)),
            row(2, ai=(6, 6, 6)),      # delta -1 improvement
            row(3, ai=(6, 6, 7.0)),    # control delta +1.0 -> violation
            row(4, ai=(7.5, 6, 6)),    # content delta +1.0 -> violation
            row(5),
        ]
        result = micro_evaluate(self.manifest, self.baseline, candidate)
        self.assertFalse(result["passed"])
        types = sorted(item["type"] for item in result["violations"])
        self.assertIn("normal_control_clear_regression", types)
        self.assertIn("cross_dim_clear_regression", types)

    def test_b_hard_regression_rejects(self):
        candidate = [
            row(1, ai=(6, 6, 6)),
            row(2, ai=(6, 6, 5)),
            row(3),
            row(4),
            row(5, ai=(7.0, 7.0, 7.0)),  # delta_B = 1.0 -> hard reject
        ]
        result = micro_evaluate(self.manifest, self.baseline, candidate)
        self.assertFalse(result["passed"])
        self.assertIn("b_clear_regression", [item["type"] for item in result["violations"]])

    def test_b_improvement_yields_no_reward(self):
        baseline = list(self.baseline)
        baseline[4] = row(5, ai=(6.6, 6.6, 6.6))  # baseline m_B already 0.6
        candidate = [
            row(1, ai=(6, 6, 6)),
            row(2, ai=(6, 6, 6)),
            row(3),
            row(4),
            row(5),  # m_B 0.6 -> 0.0: improvement must not earn positive reward
        ]
        result = micro_evaluate(self.manifest, baseline, candidate)
        self.assertAlmostEqual(result["u"]["b"], 0.0)


class RegularGateTests(unittest.TestCase):
    def setUp(self):
        self.manifest = manifest_with(
            {
                1: ["structure_e_target"],
                2: ["structure_e_target"],
                3: ["structure_e_target"],
                4: ["structure_e_target"],
                5: ["normal_control"],
                6: ["cross_dim_content"],
                7: ["b_consensus_soft"],
                8: ["normal_control"],
            }
        )
        self.baseline = [
            row(1, ai=(6, 6, 4)),
            row(2, ai=(6, 6, 4)),
            row(3),
            row(4),
            row(5),
            row(6),
            row(7),
            row(8),
        ]

    def test_two_improvements_pass(self):
        candidate = [
            row(1, ai=(6, 6, 6)),
            row(2, ai=(6, 6, 6)),
            row(3, ai=(6, 6, 6.5)),
            row(4, ai=(6, 6, 5.5)),
            row(5),
            row(6, ai=(6.9, 6, 6)),  # cross delta +0.9 within tolerance
            row(7),
            row(8),
        ]
        result = regular_evaluate(self.manifest, self.baseline, candidate)
        self.assertTrue(result["passed"])
        self.assertEqual(result["required_improvements"], 2)
        self.assertEqual(sorted(result["improvements"]), [1, 2])

    def test_one_improvement_fails(self):
        candidate = [
            row(1, ai=(6, 6, 6)),
            row(2, ai=(6, 6, 4)),  # delta 0 -> not an improvement
            row(3),
            row(4),
            row(5),
            row(6),
            row(7),
            row(8),
        ]
        result = regular_evaluate(self.manifest, self.baseline, candidate)
        self.assertFalse(result["passed"])
        self.assertEqual(result["improvements"], [1])

    def test_budgets(self):
        two_cross = [
            row(1, ai=(6, 6, 6)),
            row(2, ai=(6, 6, 6)),
            row(3),
            row(4),
            row(5),
            row(6, ai=(7.1, 6, 6)),
            row(7, ai=(6, 7.1, 6)),
            row(8),
        ]
        result = regular_evaluate(self.manifest, self.baseline, two_cross)
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["cross_dim_regressions"]), 2)

        one_control = [
            row(1, ai=(6, 6, 6)),
            row(2, ai=(6, 6, 6)),
            row(3),
            row(4),
            row(5, ai=(6, 6, 7.1)),
            row(6),
            row(7),
            row(8),
        ]
        self.assertTrue(regular_evaluate(self.manifest, self.baseline, one_control)["passed"])

        two_controls = list(one_control)
        two_controls[7] = row(8, ai=(6, 6, 7.1))
        self.assertFalse(
            regular_evaluate(self.manifest, self.baseline, two_controls)["passed"]
        )

        b_reg = [
            row(1, ai=(6, 6, 6)),
            row(2, ai=(6, 6, 6)),
            row(3),
            row(4),
            row(5),
            row(6),
            row(7, ai=(7.0, 7.0, 7.0)),
            row(8),
        ]
        result = regular_evaluate(self.manifest, self.baseline, b_reg)
        self.assertFalse(result["passed"])
        self.assertEqual(result["b_regressions"], [7])


class FullTrainGateTests(unittest.TestCase):
    def setUp(self):
        self.rows = [row(index) for index in range(1, 7)]
        self.baseline_runs = (list(self.rows), list(self.rows))
        self.baseline_consensus = consensus(
            structure=((1, 2), (3,)), content=((), (4,))
        )

    def evaluate(self, candidate_consensus, mutate=None):
        candidate_runs = [list(self.rows), list(self.rows)]
        if mutate is not None:
            candidate_runs = mutate(candidate_runs)
        return full_train_evaluate(
            self.baseline_runs[0],
            self.baseline_runs[1],
            self.baseline_consensus,
            candidate_runs[0],
            candidate_runs[1],
            candidate_consensus=candidate_consensus,
        )

    def test_target_severe_down_passes(self):
        candidate = consensus(structure=((1,), (3,)), content=((), (4,)))
        result = self.evaluate(candidate)
        self.assertTrue(result["passed"])
        self.assertTrue(result["target"]["passed"])

    def test_target_soft_down_with_equal_severe_passes(self):
        candidate = consensus(structure=((1, 2), ()))
        result = self.evaluate(candidate)
        self.assertTrue(result["target"]["passed"])

    def test_target_flat_fails(self):
        candidate = consensus(structure=((1, 2), (3,)), content=((), (4,)))
        result = self.evaluate(candidate)
        self.assertFalse(result["passed"])
        self.assertFalse(result["target"]["passed"])

    def test_new_non_target_member_rejects(self):
        candidate = consensus(structure=((1,), (3,)), content=((5,), (4,)))
        result = self.evaluate(candidate)
        self.assertFalse(result["passed"])
        self.assertEqual(
            result["non_target_e"]["new_members"],
            [{"index": 5, "dimension": "content", "severity": "severe"}],
        )

    def test_soft_to_severe_upgrade_rejects(self):
        candidate = consensus(structure=((1,), (3,)), content=((4,), ()))
        result = self.evaluate(candidate)
        self.assertFalse(result["passed"])
        self.assertEqual(
            result["non_target_e"]["soft_to_severe"], [{"index": 4, "dimension": "content"}]
        )

    def test_cross_dim_clear_regression_rejects(self):
        def mutate(runs):
            runs[0][1] = row(2, ai=(6, 7.2, 6))
            runs[1][1] = row(2, ai=(6, 7.2, 6))
            return runs

        candidate = consensus(structure=((1,), (3,)), content=((), (4,)))
        result = self.evaluate(candidate, mutate=mutate)
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["cross_dim_regressions"]), 1)
        self.assertEqual(result["cross_dim_regressions"][0]["dimension"], "expression")

    def test_b_guard_rejects(self):
        def mutate(runs):
            shifted = [row(index, ai=(8.5, 8.5, 8.5)) for index in range(1, 7)]
            return [shifted, list(shifted)]

        candidate = consensus(structure=((1,), (3,)), content=((), (4,)))
        result = self.evaluate(candidate, mutate=mutate)
        self.assertFalse(result["passed"])
        self.assertFalse(result["b"]["passed"])
        self.assertGreater(result["b"]["mean_severe"], dt.FULL_TRAIN_B_SEVERE_MAX)


class ValidationGateTests(unittest.TestCase):
    def setUp(self):
        self.baseline_runs = (
            [row(index) for index in range(1, 4)],
            [row(index) for index in range(1, 4)],
        )

    def test_clean_passes(self):
        rows = [row(index) for index in range(1, 4)]
        result = validation_evaluate(
            self.baseline_runs[0], self.baseline_runs[1], rows, rows
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["clear_regressions"], [])

    def test_one_clear_regression_rejects(self):
        rows = [row(index) for index in range(1, 4)]
        bad = list(rows)
        bad[1] = row(2, ai=(6, 6, 7.2))
        result = validation_evaluate(
            self.baseline_runs[0], self.baseline_runs[1], bad, bad
        )
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["clear_regressions"]), 1)

    def test_b_guard_rejects(self):
        rows = [row(index, ai=(8.5, 8.5, 8.5)) for index in range(1, 4)]
        result = validation_evaluate(
            self.baseline_runs[0], self.baseline_runs[1], rows, rows
        )
        self.assertFalse(result["b"]["passed"])
        self.assertFalse(result["passed"])


class ManifestRoleTests(unittest.TestCase):
    def test_classify_manifest_roles(self):
        manifest = manifest_with(
            {
                1: ["structure_e_target"],
                2: ["normal_control"],
                3: ["b_consensus_severe", "cross_dim_expression"],
            }
        )
        roles = classify_manifest(manifest)
        self.assertEqual(roles["targets"], [1])
        self.assertEqual(roles["controls"], [2])
        self.assertEqual(roles["b_roles"], {3: "severe"})
        self.assertEqual(roles["cross"]["expression"], [3])
        self.assertEqual(roles["all_indices"], [1, 2, 3])

    def test_duplicate_indices_rejected(self):
        manifest = manifest_with({1: ["structure_e_target"]})
        manifest["samples"].append(dict(manifest["samples"][0]))
        with self.assertRaises(ValueError):
            classify_manifest(manifest)


class BudgetCoherenceTests(unittest.TestCase):
    """Code caps must match the amended budgets recorded in the protocol manifest."""

    def test_manifest_budgets_match_code(self):
        manifest = json.loads(
            (ROOT / "e_protocol_manifest.json").read_text(encoding="utf-8")
        )
        budgets = manifest["budgets"]
        self.assertEqual(budgets["max_successful_scoring_calls"], p2.MAX_SCORING_SUCCESS)
        self.assertEqual(budgets["max_successful_analysis_calls"], p2.MAX_ANALYSIS_SUCCESS)
        self.assertEqual(budgets["max_successful_calls"], p2.MAX_TOTAL_SUCCESS)
        self.assertEqual(budgets["max_request_attempts"], p2.MAX_REQUEST_ATTEMPTS)

    def test_amendment_recorded(self):
        manifest = json.loads(
            (ROOT / "e_protocol_manifest.json").read_text(encoding="utf-8")
        )
        amendments = manifest.get("budget_amendments") or []
        self.assertTrue(amendments)
        self.assertEqual(amendments[-1]["scoring_success_cap"]["after"], p2.MAX_SCORING_SUCCESS)

    def test_first_pass_stop_status_recorded(self):
        from e_phase1_prepare import STATUS_ENUM

        manifest = json.loads(
            (ROOT / "e_protocol_manifest.json").read_text(encoding="utf-8")
        )
        self.assertIn("E_FIRST_PASS_PASSED_PENDING_BUDGET", STATUS_ENUM)
        self.assertIn("E_FIRST_PASS_PASSED_PENDING_BUDGET", manifest["status_enum"])

class StageIdentityTests(unittest.TestCase):
    """Stage reuse must validate the identity sidecar (review fix 2026-09-21)."""

    def write_rows(self, path):
        path.write_text(json.dumps([row(1)]), encoding="utf-8")

    def test_reuse_with_matching_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "rows.json"
            self.write_rows(out)
            identity = p2._stage_identity("micro_r1", [1])
            (Path(tmp) / "rows.meta.json").write_text(
                json.dumps(identity, ensure_ascii=False), encoding="utf-8"
            )
            report = {}
            rows = p2.stage_rows(None, "micro_r1", [1], "unused.json", out, report)
            self.assertEqual(len(rows), 1)
            self.assertEqual(report["reused_stages"], ["rows.json"])

    def test_missing_sidecar_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "rows.json"
            self.write_rows(out)
            with self.assertRaises(RuntimeError):
                p2.stage_rows(None, "micro_r1", [1], "unused.json", out, {})

    def test_identity_mismatch_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "rows.json"
            self.write_rows(out)
            identity = p2._stage_identity("micro_r1", [1])
            identity["prompt_sha256"] = "deadbeef"
            (Path(tmp) / "rows.meta.json").write_text(
                json.dumps(identity, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaises(RuntimeError):
                p2.stage_rows(None, "micro_r1", [1], "unused.json", out, {})

    def test_fresh_stage_writes_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "rows.json"

            def fake_score_stage(*args, **kwargs):
                self.write_rows(out)
                return [row(1)]

            original = p2.score_stage
            p2.score_stage = fake_score_stage
            try:
                rows = p2.stage_rows(None, "micro_r1", [1], "unused.json", out, {})
            finally:
                p2.score_stage = original
            self.assertEqual(len(rows), 1)
            sidecar = json.loads(
                (Path(tmp) / "rows.meta.json").read_text(encoding="utf-8")
            )
            self.assertEqual(sidecar["repeat_id"], "micro_r1")
            self.assertEqual(sidecar["global_indices"], [1])
            self.assertIn("prompt_sha256", sidecar)

    def test_sanitize_rule_removes_spans(self):
        rule = {
            "trigger_condition": "x",
            "positive_evidence_spans": [{"span": "quote"}],
            "counter_evidence_spans": [{"span": "q2"}],
        }
        clean = p2.sanitize_rule_for_public(rule)
        self.assertEqual(clean["positive_evidence_spans"], {"removed_for_privacy": 1})
        self.assertNotIn("quote", json.dumps(clean, ensure_ascii=False))


class FrozenManifestIntegrityTests(unittest.TestCase):
    def test_frozen_manifest_approved_and_hash_stable(self):
        path = ROOT / p2.MANIFEST_FROZEN
        self.assertTrue(path.exists(), "frozen manifest missing; run freeze-manifest")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["approval"]["status"], "approved")
        self.assertEqual(manifest["status"], "approved")
        body = dict(manifest)
        recorded = body["approval"]["frozen_sha256"]
        body["approval"] = dict(body["approval"])
        body["approval"]["frozen_sha256"] = None
        canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        import hashlib

        self.assertEqual(
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(), recorded
        )
        self.assertFalse((ROOT / p2.MANIFEST_DRAFT).exists(), "draft must be removed")
        self.assertEqual(len(manifest["samples"]), 17)

    def test_require_frozen_manifest_accepts(self):
        manifest = p2.require_frozen_manifest()
        self.assertEqual(manifest["approval"]["status"], "approved")

    def test_require_frozen_manifest_detects_tampering(self):
        manifest = json.loads((ROOT / p2.MANIFEST_FROZEN).read_text(encoding="utf-8"))
        tampered = json.loads(json.dumps(manifest))
        tampered["samples"][0]["selection_reasons"].append("tampered")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                p2.require_frozen_manifest(path)

    def test_status_reports_manifest_integrity(self):
        buffer = io.StringIO()
        original = sys.stdout
        sys.stdout = buffer
        try:
            p2.status()
        finally:
            sys.stdout = original
        output = json.loads(buffer.getvalue())
        self.assertEqual(output["manifest_integrity"], "valid")


class StructureInjectionTests(unittest.TestCase):
    def test_injection_is_contract_valid(self):
        from prompt_structure_contract import validate_optimizer_edit

        meta = (ROOT / p2.FINAL_META).read_text(encoding="utf-8")
        rule = {
            "should_be_injected_at": "**结构分特殊情形**",
            "trigger_condition": "高考作文若以抒情串联但段落顺序随意",
            "scoring_adjustment": "存在有效收束时不因段落顺序随意而额外减分",
            "anti_overfit_boundary": "仅限抒情类记叙文",
            "forbidden_generalization": ["不得外推到议论文"],
        }
        injected, rendered = p2.structure_injection(meta, rule)
        self.assertEqual(validate_optimizer_edit(meta, injected), [])
        self.assertIn("触发条件：", rendered)
        self.assertIn("- 触发条件", injected)
        self.assertLess(len(injected) - len(meta), 800)


class Gate0Tests(unittest.TestCase):
    def valid_rule(self):
        return {
            "should_be_injected_at": "**结构分特殊情形**",
            "trigger_condition": "高考作文若以抒情串联但段落顺序随意",
            "scoring_adjustment": "存在有效收束时不因段落顺序随意而额外减分",
            "anti_overfit_boundary": "仅限抒情类记叙文",
            "forbidden_generalization": ["不得外推到议论文"],
            "expected_scope": ["structure"],
            "evidence": {"outlier_indices": [13, 18]},
            "evidence_count": 2,
            "confidence": 0.8,
        }

    def test_valid_rule_passes_gate0(self):
        meta = (ROOT / p2.FINAL_META).read_text(encoding="utf-8")
        result = p2.gate0_checks(self.valid_rule(), meta)
        self.assertTrue(result["passed"], result["checks"])

    def test_do_not_inject_fails_anchor(self):
        meta = (ROOT / p2.FINAL_META).read_text(encoding="utf-8")
        rule = self.valid_rule()
        rule["should_be_injected_at"] = "do_not_inject"
        self.assertFalse(p2.gate0_checks(rule, meta)["passed"])

    def test_scope_leak_fails(self):
        meta = (ROOT / p2.FINAL_META).read_text(encoding="utf-8")
        rule = self.valid_rule()
        rule["expected_scope"] = ["content"]
        result = p2.gate0_checks(rule, meta)
        self.assertFalse(result["passed"])
        self.assertFalse(
            next(item for item in result["checks"] if item["code"] == "G0-scope")["ok"]
        )

    def test_hard_threshold_fails(self):
        meta = (ROOT / p2.FINAL_META).read_text(encoding="utf-8")
        rule = self.valid_rule()
        rule["scoring_adjustment"] = "结构分不得低于同类平均"
        self.assertFalse(p2.gate0_checks(rule, meta)["passed"])

    def test_missing_evidence_fails(self):
        meta = (ROOT / p2.FINAL_META).read_text(encoding="utf-8")
        rule = self.valid_rule()
        rule["evidence"] = {"outlier_indices": []}
        result = p2.gate0_checks(rule, meta)
        self.assertFalse(result["passed"])
        self.assertFalse(
            next(item for item in result["checks"] if item["code"] == "G0-evidence")["ok"]
        )

    def test_out_of_pool_evidence_fails(self):
        meta = (ROOT / p2.FINAL_META).read_text(encoding="utf-8")
        rule = self.valid_rule()
        rule["evidence"] = {"outlier_indices": [99], "normal_indices": []}
        rule["evidence_count"] = 1
        result = p2.gate0_checks(rule, meta)
        self.assertFalse(result["passed"])
        codes = {item["code"]: item["ok"] for item in result["checks"]}
        self.assertFalse(codes["G0-evidence-pool"])

    def test_evidence_count_mismatch_fails(self):
        meta = (ROOT / p2.FINAL_META).read_text(encoding="utf-8")
        rule = self.valid_rule()
        rule["evidence_count"] = 3
        result = p2.gate0_checks(rule, meta)
        self.assertFalse(result["passed"])
        codes = {item["code"]: item["ok"] for item in result["checks"]}
        self.assertFalse(codes["G0-evidence-pool"])


class RuleSelectionSequenceTests(unittest.TestCase):
    """Lazy sequential selection ruled 2026-09-21 (no ranking/pre-screening)."""

    def meta(self):
        return (ROOT / p2.FINAL_META).read_text(encoding="utf-8")

    def valid_rule(self, marker="甲"):
        return {
            "should_be_injected_at": "**结构分特殊情形**",
            "trigger_condition": f"当作文以某一独特场景{marker}反复出现并形成前后呼应时",
            "scoring_adjustment": "将此类呼应视为结构完整性的正向证据",
            "anti_overfit_boundary": "仅限抒情记叙文",
            "forbidden_generalization": ["不得外推为通用加分依据"],
            "expected_scope": ["structure"],
            "evidence": {"outlier_indices": [13, 18]},
            "evidence_count": 2,
            "confidence": 0.7,
        }

    def test_first_fails_then_falls_through(self):
        bad = self.valid_rule("甲")
        bad["should_be_injected_at"] = "do_not_inject"
        good = self.valid_rule("乙")
        sequence = p2.select_candidate_rule([bad, good], self.meta())
        self.assertTrue(sequence["passed"])
        self.assertEqual(sequence["selected_index"], 1)
        self.assertEqual(len(sequence["attempts"]), 2)
        self.assertFalse(sequence["attempts"][0]["passed"])
        self.assertTrue(sequence["attempts"][1]["passed"])

    def test_first_passes_stops_immediately(self):
        sequence = p2.select_candidate_rule(
            [self.valid_rule("甲"), self.valid_rule("乙")], self.meta()
        )
        self.assertTrue(sequence["passed"])
        self.assertEqual(sequence["selected_index"], 0)
        self.assertEqual(len(sequence["attempts"]), 1)
        block = p2._selection_block(sequence, 2)
        self.assertEqual(block["policy"], "lazy_sequential_first_pass")
        self.assertEqual(block["untried"], 1)
        self.assertEqual(block["selected_index"], 0)

    def test_all_fail_no_candidate(self):
        bad1 = self.valid_rule("甲")
        bad1["should_be_injected_at"] = "do_not_inject"
        bad2 = self.valid_rule("乙")
        bad2["expected_scope"] = ["content"]
        sequence = p2.select_candidate_rule([bad1, bad2], self.meta())
        self.assertFalse(sequence["passed"])
        self.assertIsNone(sequence["selected_index"])
        self.assertEqual(len(sequence["attempts"]), 2)
        block = p2._selection_block(sequence, 2)
        self.assertEqual(block["untried"], 0)

    def test_start_index_skips_consumed_candidate(self):
        rules = [self.valid_rule("甲"), self.valid_rule("乙"), self.valid_rule("丙")]
        sequence = p2.select_candidate_rule(rules, self.meta(), start_index=1)
        self.assertTrue(sequence["passed"])
        self.assertEqual(sequence["selected_index"], 1)
        self.assertEqual(sequence["start_index"], 1)
        block = p2._selection_block(sequence, 3)
        self.assertEqual(block["scan_start_index"], 1)
        self.assertEqual(block["source"], "human_directed_start")
        self.assertEqual(block["untried"], 1)

class FinalizeSelectionTests(unittest.TestCase):
    """Gate 0 pass auto-admits (ruled 2026-09-21); no human pass at this node."""

    def sequence_pass(self):
        return {
            "passed": True,
            "selected_index": 0,
            "rule": {"should_be_injected_at": "**结构分特殊情形**"},
            "gate0": {"passed": True, "checks": []},
            "attempts": [{"index": 0, "passed": True, "checks": []}],
        }

    def sequence_fail(self):
        return {
            "passed": False,
            "selected_index": None,
            "rule": None,
            "gate0": None,
            "attempts": [
                {"index": 0, "passed": False, "checks": []},
                {"index": 1, "passed": False, "checks": []},
            ],
        }

    def test_pass_auto_admits_without_human(self):
        payload = p2.finalize_selected_rule({"stub": True}, self.sequence_pass(), 3)
        self.assertEqual(payload["status"], "approved_pending_execution")
        self.assertEqual(payload["human_review"]["status"], "not_required")
        self.assertEqual(payload["rule_index"], 0)
        self.assertEqual(payload["selection"]["untried"], 2)
        self.assertEqual(payload["selection"]["policy"], "lazy_sequential_first_pass")

    def test_fail_marks_no_candidate(self):
        payload = p2.finalize_selected_rule({"stub": True}, self.sequence_fail(), 2)
        self.assertEqual(payload["status"], "E_NO_VALID_CANDIDATE")
        self.assertIsNone(payload["rule"])
        self.assertEqual(payload["selection"]["untried"], 0)


class VetoTests(unittest.TestCase):
    def write_payload(self, path, status, human_status="not_required"):
        path.write_text(
            json.dumps(
                {"status": status, "human_review": {"status": human_status}, "rule": {"x": 1}}
            ),
            encoding="utf-8",
        )

    def test_reject_veto_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            rule_path = Path(tmp) / "rule.json"
            report_path = Path(tmp) / "report.json"
            self.write_payload(rule_path, "approved_pending_execution")
            rc = p2.approve_rule(
                "reject", "项目负责人", "veto", rule_path=rule_path, report_path=report_path
            )
            payload = json.loads(rule_path.read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            self.assertEqual(payload["status"], "E_NO_VALID_CANDIDATE")
            self.assertEqual(payload["human_review"]["status"], "rejected")

    def test_reject_blocked_after_execution_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            rule_path = Path(tmp) / "rule.json"
            report_path = Path(tmp) / "report.json"
            self.write_payload(rule_path, "approved_pending_execution")
            report_path.write_text(
                json.dumps({"stages": {"micro": {}}}), encoding="utf-8"
            )
            rc = p2.approve_rule(
                "reject", "项目负责人", "veto", rule_path=rule_path, report_path=report_path
            )
            payload = json.loads(rule_path.read_text(encoding="utf-8"))
            self.assertEqual(rc, 3)
            self.assertEqual(payload["status"], "approved_pending_execution")

    def test_approve_noop_when_auto_admitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            rule_path = Path(tmp) / "rule.json"
            self.write_payload(rule_path, "approved_pending_execution")
            rc = p2.approve_rule(
                "approve",
                "项目负责人",
                "",
                rule_path=rule_path,
                report_path=Path(tmp) / "missing.json",
            )
            payload = json.loads(rule_path.read_text(encoding="utf-8"))
            self.assertEqual(rc, 0)
            self.assertEqual(payload["status"], "approved_pending_execution")

class BudgetGuardTests(unittest.TestCase):
    def test_caps_enforced(self):
        originals = (
            p2.MAX_SCORING_SUCCESS,
            p2.MAX_ANALYSIS_SUCCESS,
            p2.MAX_TOTAL_SUCCESS,
            p2.MAX_REQUEST_ATTEMPTS,
        )
        p2.MAX_SCORING_SUCCESS = 1
        p2.MAX_ANALYSIS_SUCCESS = 1
        p2.MAX_TOTAL_SUCCESS = 2
        p2.MAX_REQUEST_ATTEMPTS = 2
        try:
            with tempfile.TemporaryDirectory() as tmp:
                guard = p2.BudgetGuard(Path(tmp) / "state.json")
                guard.note_attempt()
                guard.note_attempt()
                with self.assertRaises(p2.BudgetExceeded):
                    guard.note_attempt()

                guard2 = p2.BudgetGuard(Path(tmp) / "state2.json")
                guard2.note_scoring_success()
                with self.assertRaises(p2.BudgetExceeded):
                    guard2.note_scoring_success()

                guard3 = p2.BudgetGuard(Path(tmp) / "state3.json")
                guard3.note_analysis_success()
                with self.assertRaises(p2.BudgetExceeded):
                    guard3.note_analysis_success()

                guard4 = p2.BudgetGuard(Path(tmp) / "state4.json")
                guard4.note_scoring_success()
                guard4.note_analysis_success()
                with self.assertRaises(p2.BudgetExceeded):
                    guard4.note_scoring_success()
        finally:
            (
                p2.MAX_SCORING_SUCCESS,
                p2.MAX_ANALYSIS_SUCCESS,
                p2.MAX_TOTAL_SUCCESS,
                p2.MAX_REQUEST_ATTEMPTS,
            ) = originals

    def test_state_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            guard = p2.BudgetGuard(path)
            guard.note_attempt()
            reloaded = p2.BudgetGuard(path)
            self.assertEqual(reloaded.state["attempts"], 1)


class OfflineStaticTests(unittest.TestCase):
    def test_gate_engine_has_no_network_tokens(self):
        source = (ROOT / "e_gate_engine.py").read_text(encoding="utf-8")
        for token in ("requests", "urllib", "http", "api_key", "api_url"):
            self.assertNotIn(token, source, f"unexpected token {token!r} in e_gate_engine.py")

    @unittest.skipUnless(
        LOCAL_ANCHORS_AVAILABLE, "V6 anchor artifacts are local-only (CI-safe skip)"
    )
    def test_execute_requires_approved_rule(self):
        original_exists = Path.exists

        def fake_exists(self):
            if self.name == p2.RULE_FILE:
                return False
            return original_exists(self)

        Path.exists = fake_exists
        try:
            with self.assertRaises(RuntimeError):
                p2.execute()
        finally:
            Path.exists = original_exists


if __name__ == "__main__":
    unittest.main()
