"""Phase-1 offline tests for the E route.

Covers the minimum test list of the E-route supplementary guidance section 5:

  1. two-run index mismatch is rejected
  2. two-run teacher mismatch is rejected
  3. opposite-direction residuals do not enter the E consensus
  4. Severe/Soft boundaries follow >2.5 and 1.5 < abs(z) <= 2.5 strictly
  5. V6 re-mine results: content 1/7, expression 0/0, structure 2/2
  6. B consensus result: 2 Severe / 6 Soft (and per-run 3/5, 3/8)
  7. validation two-run mean reproduces 1.5/1.0/Q 4.75
  8. hash anchor drift fails Gate 0 verification
  9. a delta of exactly +1.0 / -1.0 is classified as regression / improvement
 10. duplicated indices collapse to one scoring identity with multiple roles
 11. the .draft manifest is rejected by the frozen-manifest guard
 12. the Phase-1 entry point performs no network call and never reads the key

Tests that need the private V6 artifacts are skipped when those files are
absent (e.g. on CI); the synthetic tests always run.
"""

import argparse
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import e_consensus_miner
import e_phase1_prepare
import pipeline_entry
from decision_thresholds import (
    Q_SEVERE_WEIGHT,
    classify_delta_abs_error,
    is_clear_improvement,
    is_clear_regression,
    q_score,
)
from e_consensus_miner import (
    align_two_runs,
    b_consensus,
    b_run_counts,
    classify_mean_z,
    e_consensus,
    load_rows,
)
from e_phase1_prepare import (
    ROOT,
    TRAIN_MEAN,
    TRAIN_RUN1,
    TRAIN_RUN2,
    VALIDATION_RUN1,
    VALIDATION_RUN2,
    build_candidate_draft,
    build_validation_mean,
    require_frozen_manifest,
    run_phase1_prepare,
    verify_baseline_manifest,
)
from etype_iteration_runner import ETypeIterationRunner
from micro_scoring_gate import MicroScoringGate

DIMS = ("content", "expression", "structure")

REAL_DATA_REQUIRED = [
    ROOT / TRAIN_RUN1,
    ROOT / TRAIN_RUN2,
    ROOT / TRAIN_MEAN,
    ROOT / VALIDATION_RUN1,
    ROOT / VALIDATION_RUN2,
    ROOT / "final_prompt.md",
    ROOT / "final_prompt_meta.md",
    ROOT / "final_validation_scoring_results_mean.json",
    ROOT / "final_validation_scoring_results_mean.provenance.json",
    ROOT / "e_baseline_manifest.json",
    ROOT / "b_validation_report.json",
    ROOT / "b_protocol_state.json",
    ROOT / "final_evidence.json",
]


def scoring_row(index, deltas, base=5.0, essay_len=100):
    return {
        "index": index,
        "name": f"essay-{index}",
        "page": "page",
        "essay": "x" * essay_len,
        "teacher": {dim: base for dim in DIMS},
        "AI": {dim: base + deltas[dim] for dim in DIMS},
    }


def zero_deltas():
    return {dim: 0 for dim in DIMS}


class TwoRunAlignmentTest(unittest.TestCase):
    def test_index_mismatch_is_rejected(self):
        run1 = [scoring_row(1, zero_deltas())]
        run2 = [scoring_row(2, zero_deltas())]
        with self.assertRaises(ValueError):
            align_two_runs(run1, run2)

    def test_teacher_mismatch_is_rejected(self):
        run1 = [scoring_row(1, zero_deltas(), base=5.0)]
        run2 = [scoring_row(1, zero_deltas(), base=6.0)]
        with self.assertRaises(ValueError):
            align_two_runs(run1, run2)


class EConsensusTest(unittest.TestCase):
    def build_pair(self):
        # content deltas: +2, +1, +0.5, +0.5, -3/+3 (direction flip), 0
        d_vals = [2, 1, 0.5, 0.5, -3, 0]
        d2_vals = [2, 1, 0.5, 0.5, 3, 0]
        run1, run2 = [], []
        for pos, index in enumerate(range(0, 6)):
            deltas1 = zero_deltas()
            deltas2 = zero_deltas()
            deltas1["content"] = d_vals[pos]
            deltas2["content"] = d2_vals[pos]
            run1.append(scoring_row(index, deltas1))
            run2.append(scoring_row(index, deltas2))
        return run1, run2

    def test_opposite_direction_and_floor_exclusions(self):
        run1, run2 = self.build_pair()
        result = e_consensus(run1, run2)

        counts = result["counts"]["content"]
        self.assertEqual(counts, {"severe": 0, "soft": 1})
        soft_indices = [entry["index"] for entry in result["by_dim"]["content"]["soft"]]
        self.assertEqual(soft_indices, [0])

        all_indices = set()
        for severity in ("severe", "soft"):
            for entry in result["by_dim"]["content"][severity]:
                all_indices.add(entry["index"])
        self.assertNotIn(4, all_indices)  # direction flip across runs
        self.assertNotIn(5, all_indices)  # |mean delta| below the 1.0 floor
        self.assertEqual(result["by_dim"]["content"]["below_soft_count"], 1)  # index 1

    def test_severity_boundaries_are_strict(self):
        self.assertEqual(classify_mean_z(2.5), "soft")
        self.assertEqual(classify_mean_z(2.5000001), "severe")
        self.assertEqual(classify_mean_z(-2.5000001), "severe")
        self.assertEqual(classify_mean_z(1.5), "below_soft")
        self.assertEqual(classify_mean_z(1.5000001), "soft")
        self.assertEqual(classify_mean_z(-1.5000001), "soft")
        self.assertEqual(classify_mean_z(0.0), "below_soft")


class SharedThresholdTest(unittest.TestCase):
    def test_boundary_values(self):
        self.assertTrue(is_clear_regression(1.0))
        self.assertFalse(is_clear_regression(0.9999))
        self.assertTrue(is_clear_improvement(-1.0))
        self.assertFalse(is_clear_improvement(-0.9999))
        self.assertEqual(classify_delta_abs_error(1.0), "clear_regression")
        self.assertEqual(classify_delta_abs_error(-1.0), "clear_improvement")
        self.assertEqual(classify_delta_abs_error(0.5), "neutral")

    def test_mirrors_b_protocol_constants(self):
        self.assertEqual(Q_SEVERE_WEIGHT, pipeline_entry.Q_SEVERE_WEIGHT)
        self.assertEqual(q_score(2, 3), pipeline_entry.Q_SEVERE_WEIGHT * 2 + 3)
        self.assertEqual(
            e_consensus_miner.B_SEVERE_ABS_BIAS, pipeline_entry.B_SEVERE_ABS_BIAS
        )
        self.assertEqual(
            e_consensus_miner.B_SOFT_ABS_BIAS, pipeline_entry.B_SOFT_ABS_BIAS
        )
        self.assertEqual(
            e_consensus_miner.B_SEVERE_MIN_DIRECTION,
            pipeline_entry.B_SEVERE_MIN_DIRECTION,
        )
        self.assertEqual(
            e_consensus_miner.B_SOFT_MIN_DIRECTION,
            pipeline_entry.B_SOFT_MIN_DIRECTION,
        )


class HashAnchorTest(unittest.TestCase):
    def test_verifier_detects_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "artifact.json"
            target.write_text("one", encoding="utf-8")
            digest = hashlib.sha256(b"one").hexdigest()
            manifest = {"anchors": [{"path": "artifact.json", "sha256": digest}]}

            self.assertEqual(verify_baseline_manifest(manifest, root), [])
            target.write_text("two", encoding="utf-8")
            mismatches = verify_baseline_manifest(manifest, root)
            self.assertEqual(len(mismatches), 1)
            self.assertEqual(mismatches[0]["path"], "artifact.json")


class MicroGateBoundaryTest(unittest.TestCase):
    """The unified threshold must be visible at the real micro-gate wiring."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.paths = {
            name: root / name
            for name in (
                "baseline.json",
                "badcases.json",
                "injected.json",
                "essays.json",
                "manifest.json",
                "candidate.json",
                "eval.json",
            )
        }
        baseline = [
            scoring_row(10, {"content": -2, "expression": 0, "structure": 0}, base=7.0),
            scoring_row(11, zero_deltas(), base=6.0),
            scoring_row(12, {"content": -2, "expression": -2, "structure": -2}, base=7.0),
        ]
        self.write("baseline.json", baseline)
        self.write(
            "badcases.json",
            {
                "B_bias": {
                    "severe": [{"data_index": 2, "severity": "severe"}],
                    "soft": [],
                }
            },
        )
        self.write(
            "injected.json",
            {
                "injected_rules": [
                    {
                        "major_iteration": 1,
                        "sub_iteration": 1,
                        "source_rule_index": 0,
                        "dimension": "content",
                        "evidence": {"outlier_indices": [0, 2], "normal_indices": [1]},
                    }
                ]
            },
        )
        self.gate = MicroScoringGate(
            baseline_path=str(self.paths["baseline.json"]),
            badcase_path=str(self.paths["badcases.json"]),
            injected_rule_path=str(self.paths["injected.json"]),
            essays_output_path=str(self.paths["essays.json"]),
            manifest_output_path=str(self.paths["manifest.json"]),
            candidate_path=str(self.paths["candidate.json"]),
            eval_output_path=str(self.paths["eval.json"]),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def write(self, name, payload):
        with open(self.paths[name], "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def test_exact_one_point_regression_is_flagged(self):
        self.gate.build_manifest()
        candidate = [
            scoring_row(10, {"content": -3, "expression": 0, "structure": 0}, base=7.0),
            scoring_row(11, zero_deltas(), base=6.0),
            scoring_row(12, {"content": -2, "expression": -2, "structure": -2}, base=7.0),
        ]
        self.write("candidate.json", candidate)

        result = self.gate.evaluate()

        flagged = [
            v
            for v in result["violations"]
            if v.get("index") == 10 and v.get("dimension") == "content"
        ]
        self.assertTrue(any(v["type"] == "single_dimension_regression" for v in flagged))
        self.assertFalse(result["gate_passed"])

    def test_half_point_regression_is_neutral_now(self):
        self.gate.build_manifest()
        candidate = [
            scoring_row(10, {"content": -2.5, "expression": 0, "structure": 0}, base=7.0),
            scoring_row(11, zero_deltas(), base=6.0),
            scoring_row(12, {"content": -2, "expression": -2, "structure": -2}, base=7.0),
        ]
        self.write("candidate.json", candidate)

        result = self.gate.evaluate()

        self.assertFalse(any(v.get("index") == 10 for v in result["violations"]))


class RunnerValidationBoundaryTest(unittest.TestCase):
    def test_exact_one_point_delta_counts_as_clear_regression(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline_path = root / "baseline.json"
            candidate_path = root / "candidate.json"
            eval_path = root / "eval.json"
            with open(baseline_path, "w", encoding="utf-8") as f:
                json.dump(
                    [scoring_row(1, {"content": -1, "expression": 0, "structure": 0}, base=5.0)],
                    f,
                )
            with open(candidate_path, "w", encoding="utf-8") as f:
                json.dump(
                    [scoring_row(1, {"content": -2, "expression": 0, "structure": 0}, base=5.0)],
                    f,
                )

            args = argparse.Namespace(
                analysis_dir=str(root),
                execute=True,
                dimension="content",
                validation_baseline=str(baseline_path),
                test_output=str(candidate_path),
                validation_eval=str(eval_path),
            )
            runner = ETypeIterationRunner(args)
            runner.rule_report = None
            runner.discard_rejected_candidate = Mock(return_value=[])

            passed = runner.evaluate_validation()

            self.assertFalse(passed)  # MAE guard fails on purpose
            report = json.loads(eval_path.read_text(encoding="utf-8"))
            self.assertEqual(report["target_samples_clear_regressions"], 1)


class DraftManifestGuardTest(unittest.TestCase):
    def test_draft_is_rejected_and_frozen_name_is_accepted(self):
        with self.assertRaises(ValueError):
            require_frozen_manifest("e_candidate_manifest.draft.json")
        with self.assertRaises(ValueError):
            require_frozen_manifest("e_candidate_evidence.local.json")
        with self.assertRaises(ValueError):
            require_frozen_manifest("some_other_manifest.json")
        self.assertEqual(
            require_frozen_manifest("e_candidate_manifest.json"),
            "e_candidate_manifest.json",
        )


class DraftSampleListTest(unittest.TestCase):
    def test_roles_deduplicate_to_one_identity_per_index(self):
        deltas = {
            100: {"content": 0.5, "expression": 2.0, "structure": 3.0},
            101: {"content": 0.0, "expression": 0.0, "structure": 2.0},
            102: {"content": 6.0, "expression": 0.0, "structure": 0.0},
            103: {"content": 5.0, "expression": 0.0, "structure": 0.0},
            104: {"content": 0.0, "expression": 3.0, "structure": 2.0},
            105: {"content": -2.0, "expression": -2.0, "structure": -1.0},
        }
        for index in range(106, 112):
            deltas[index] = zero_deltas()

        run1 = [scoring_row(index, deltas[index]) for index in sorted(deltas)]
        run2 = [scoring_row(index, deltas[index]) for index in sorted(deltas)]
        mean_rows = [
            {
                **scoring_row(index, deltas[index]),
                "AI": {dim: 5.0 + deltas[index][dim] for dim in DIMS},
            }
            for index in sorted(deltas)
        ]

        e_result = e_consensus(run1, run2)
        b_result = b_consensus(run1, run2, mean_rows=mean_rows)
        bundle = build_candidate_draft(
            {
                "run1_rows": run1,
                "run2_rows": run2,
                "mean_rows": mean_rows,
                "e_consensus": e_result,
                "b_consensus": b_result,
            }
        )
        manifest = bundle["manifest"]
        evidence = bundle["evidence"]

        unique = manifest["unique_global_indices"]
        self.assertEqual(unique, sorted(set(unique)))
        self.assertEqual(len(unique), len(manifest["samples"]))

        sample100 = next(s for s in manifest["samples"] if s["global_index"] == 100)
        self.assertIn("structure_e_target", sample100["evidence_roles"])
        self.assertTrue(
            any(role.startswith("b_consensus_") for role in sample100["evidence_roles"])
        )

        self.assertGreaterEqual(manifest["counts"]["normal_controls"], 5)
        total_roles = sum(len(s["evidence_roles"]) for s in manifest["samples"])
        self.assertEqual(total_roles, sum(manifest["counts"]["role_counts"].values()))

        # Split guarantee: the committable manifest carries no raw essays or
        # raw per-dimension scores (derived z/delta text may appear inside
        # selection_reasons for audit), while the local evidence file carries
        # the per-sample raw numbers.
        for sample in manifest["samples"]:
            self.assertTrue(sample["selection_reasons"])
            self.assertNotIn("teacher_scores", sample)
            self.assertNotIn("v6_mean_scores", sample)
        self.assertEqual(sorted(int(k) for k in evidence["samples"]), unique)
        evidence100 = evidence["samples"]["100"]
        self.assertIn("teacher_scores", evidence100)
        self.assertIn("v6_mean_scores", evidence100)
        self.assertEqual(manifest["approval"]["status"], "pending")


class OfflineOnlySourceTest(unittest.TestCase):
    def test_phase1_modules_have_no_network_or_env_access(self):
        forbidden = ("requests", "urllib", "http.client", "socket", "os.environ", "getenv")
        for name in (
            "e_phase1_prepare.py",
            "e_consensus_miner.py",
            "decision_thresholds.py",
        ):
            source = (ROOT / name).read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, source, f"{name} contains forbidden token: {token}")


@unittest.skipUnless(
    all(path.exists() for path in REAL_DATA_REQUIRED),
    "private V6 artifacts / Phase-1 outputs not available in this checkout",
)
class RealDataPhase1Test(unittest.TestCase):
    def test_e_and_b_consensus_reproduction(self):
        run1 = load_rows(str(ROOT / TRAIN_RUN1))
        run2 = load_rows(str(ROOT / TRAIN_RUN2))
        mean_rows = load_rows(str(ROOT / TRAIN_MEAN))

        e_result = e_consensus(run1, run2)
        self.assertEqual(e_result["counts"]["content"], {"severe": 1, "soft": 7})
        self.assertEqual(e_result["counts"]["expression"], {"severe": 0, "soft": 0})
        self.assertEqual(e_result["counts"]["structure"], {"severe": 2, "soft": 2})

        self.assertEqual(b_run_counts(run1), {"severe": 3, "soft": 5, "total": 8})
        self.assertEqual(b_run_counts(run2), {"severe": 3, "soft": 8, "total": 11})

        b_result = b_consensus(run1, run2, mean_rows=mean_rows)
        self.assertEqual(b_result["counts"], {"severe": 2, "soft": 6, "total": 8})

    def test_validation_mean_reproduction_matches_report(self):
        mean_rows, _provenance, verification = build_validation_mean()

        self.assertEqual(len(mean_rows), 12)
        self.assertEqual(verification["mean_counts"], {"severe": 1.5, "soft": 1.0})
        self.assertEqual(verification["q"], 4.75)

        report = json.loads(
            (ROOT / "b_validation_report.json").read_text(encoding="utf-8")
        )
        v6 = next(item for item in report["candidates"] if item["iteration"] == 6)
        self.assertEqual(v6["mean_severe"], 1.5)
        self.assertEqual(v6["mean_soft"], 1.0)
        self.assertEqual(v6["q"], 4.75)

    def test_baseline_manifest_verifies_without_drift(self):
        manifest = json.loads(
            (ROOT / "e_baseline_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(verify_baseline_manifest(manifest, ROOT), [])

    def test_phase1_entry_dry_run_needs_no_key_and_no_network(self):
        with patch.dict(os.environ, {"AES_API_KEY": "SENTINEL_DO_NOT_USE"}):
            summary = run_phase1_prepare(dry_run=True)

        self.assertEqual(summary["network_calls"], 0)
        self.assertEqual(summary["status"], "E_PREEXECUTION_AWAITING_SAMPLE_APPROVAL")
        self.assertNotIn("SENTINEL", json.dumps(summary))
        self.assertEqual(summary["written"], [])
        self.assertEqual(summary["draft_counts"]["unique_indices"], 17)


if __name__ == "__main__":
    unittest.main()
