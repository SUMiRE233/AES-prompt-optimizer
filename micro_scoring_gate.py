"""
Micro scoring gate for a newly injected E-type rule.

The gate scores only the evidence carried by the latest injected rule plus
existing B-bias badcases. Positive score means the candidate prompt moved
closer to teacher scores; negative score means regression.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


# ============================================================
# Micro gate weights and decision thresholds
# ============================================================
class Config:
    DIMENSIONS = ("content", "expression", "structure")

    # E-type evidence weights.
    OUTLIER_TARGET_DIM_WEIGHT = 3.0
    OUTLIER_OTHER_DIM_WEIGHT = 1.0
    NORMAL_TARGET_DIM_WEIGHT = 2.0
    NORMAL_OTHER_DIM_WEIGHT = 1.0

    # B-bias badcase weights. Severe B cases contribute more than soft cases.
    B_BIAS_WEIGHT = 2.0
    B_SEVERITY_WEIGHTS = {
        "severe": 2.0,
        "soft": 1.0,
    }

    # The weighted average improvement must reach this value.
    PASS_SCORE_THRESHOLD = 0.01

    # Hard guards prevent a positive aggregate from hiding a large regression.
    MAX_OUTLIER_TARGET_REGRESSION = 0.0
    MAX_NORMAL_TARGET_REGRESSION = 0.5
    MAX_SINGLE_DIM_REGRESSION = 1.0
    MAX_B_BIAS_REGRESSION = 0.5


class MicroScoringGate:
    def __init__(
        self,
        baseline_path: str,
        badcase_path: str,
        injected_rule_path: str,
        essays_output_path: str,
        manifest_output_path: str,
        candidate_path: str,
        eval_output_path: str,
        injected_rules: List[Dict[str, Any]] = None,
    ):
        self.baseline_path = baseline_path
        self.badcase_path = badcase_path
        self.injected_rule_path = injected_rule_path
        self.essays_output_path = essays_output_path
        self.manifest_output_path = manifest_output_path
        self.candidate_path = candidate_path
        self.eval_output_path = eval_output_path
        self.injected_rules = injected_rules

    @staticmethod
    def load_json(path: str) -> Any:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def save_json(data: Any, path: str) -> None:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with open(output, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @staticmethod
    def score_lookup(rows: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
        return {int(item["index"]): item for item in rows}

    @staticmethod
    def resolve_rules(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        rules = payload.get("injected_rules", [])
        if not rules:
            raise ValueError("Injected rule file has no injected_rules.")
        return rules

    @staticmethod
    def normalize_data_indices(values: Any) -> List[int]:
        if not isinstance(values, list):
            return []
        result = []
        seen = set()
        for value in values:
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue
            if index not in seen:
                seen.add(index)
                result.append(index)
        return result

    @staticmethod
    def b_bias_entries(badcases: Dict[str, Any]) -> List[Dict[str, Any]]:
        entries = []
        for severity in ("severe", "soft"):
            for item in badcases.get("B_bias", {}).get(severity, []):
                if "data_index" not in item:
                    continue
                entries.append(
                    {
                        "data_index": int(item["data_index"]),
                        "severity": item.get("severity", severity),
                    }
                )
        return entries

    def build_manifest(self) -> Dict[str, Any]:
        baseline = self.load_json(self.baseline_path)
        badcases = self.load_json(self.badcase_path)
        if self.injected_rules is not None:
            rules = self.injected_rules
        else:
            injected = self.load_json(self.injected_rule_path)
            rules = [self.resolve_rules(injected)[-1]]
        if not rules:
            raise ValueError("No injected rules were provided for micro scoring.")

        dimensions = {rule.get("dimension") for rule in rules}
        if len(dimensions) != 1:
            raise ValueError(f"Micro scoring rules span multiple dimensions: {dimensions}")
        dimension = next(iter(dimensions))
        if dimension not in Config.DIMENSIONS:
            raise ValueError(f"Invalid injected rule dimension: {dimension}")

        outlier_indices = []
        normal_indices = []
        for rule in rules:
            evidence = rule.get("evidence", {})
            outlier_indices.extend(
                self.normalize_data_indices(evidence.get("outlier_indices"))
            )
            normal_indices.extend(
                self.normalize_data_indices(evidence.get("normal_indices"))
            )
        outlier_indices = self.normalize_data_indices(outlier_indices)
        normal_indices = self.normalize_data_indices(normal_indices)
        b_entries = self.b_bias_entries(badcases)

        sample_roles: Dict[int, Dict[str, Any]] = {}

        def ensure_role(data_index: int) -> Dict[str, Any]:
            if data_index < 0 or data_index >= len(baseline):
                raise IndexError(f"Micro scoring data_index out of range: {data_index}")
            return sample_roles.setdefault(
                data_index,
                {
                    "data_index": data_index,
                    "evidence_roles": [],
                    "b_bias_severities": [],
                },
            )

        for data_index in outlier_indices:
            ensure_role(data_index)["evidence_roles"].append("outlier")
        for data_index in normal_indices:
            ensure_role(data_index)["evidence_roles"].append("normal")
        for entry in b_entries:
            ensure_role(entry["data_index"])["b_bias_severities"].append(entry["severity"])

        if not sample_roles:
            raise ValueError("Latest injected rule has no usable evidence or B-bias samples.")

        samples = []
        essays = []
        for data_index in sorted(sample_roles):
            baseline_item = baseline[data_index]
            if "index" not in baseline_item:
                raise ValueError(f"Baseline row {data_index} is missing index.")
            role = sample_roles[data_index]
            sample = {
                **role,
                "index": int(baseline_item["index"]),
                "dimension": dimension,
                "name": baseline_item.get("name"),
                "page": baseline_item.get("page"),
            }
            samples.append(sample)
            essays.append(
                {
                    "index": int(baseline_item["index"]),
                    "essay": baseline_item.get("essay", ""),
                }
            )

        manifest = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "dimension": dimension,
            "baseline_file": self.baseline_path,
            "badcase_file": self.badcase_path,
            "injected_rule_file": self.injected_rule_path,
            "candidate_file": self.candidate_path,
            "rules": [
                {
                    "major_iteration": rule.get("major_iteration"),
                    "sub_iteration": rule.get("sub_iteration"),
                    "source_rule_index": rule.get("source_rule_index"),
                    "trigger_condition": rule.get("trigger_condition"),
                    "scoring_adjustment": rule.get("scoring_adjustment"),
                }
                for rule in rules
            ],
            "counts": {
                "unique_samples": len(samples),
                "outliers": len(outlier_indices),
                "normals": len(normal_indices),
                "b_bias": len(b_entries),
            },
            "samples": samples,
        }
        self.save_json(essays, self.essays_output_path)
        self.save_json(manifest, self.manifest_output_path)
        return manifest

    @staticmethod
    def dimension_improvement(
        baseline_item: Dict[str, Any],
        candidate_item: Dict[str, Any],
        dimension: str,
    ) -> Dict[str, float]:
        teacher_score = float(baseline_item["teacher"][dimension])
        baseline_error = abs(float(baseline_item["AI"][dimension]) - teacher_score)
        candidate_error = abs(float(candidate_item["AI"][dimension]) - teacher_score)
        return {
            "baseline_abs_error": baseline_error,
            "candidate_abs_error": candidate_error,
            "improvement": baseline_error - candidate_error,
        }

    @staticmethod
    def b_bias_magnitude(item: Dict[str, Any]) -> float:
        diffs = [
            float(item["AI"][dim]) - float(item["teacher"][dim])
            for dim in Config.DIMENSIONS
        ]
        return abs(sum(diffs) / len(diffs))

    @staticmethod
    def evidence_weight(role: str, dimension: str, target_dimension: str) -> float:
        if role == "outlier":
            return (
                Config.OUTLIER_TARGET_DIM_WEIGHT
                if dimension == target_dimension
                else Config.OUTLIER_OTHER_DIM_WEIGHT
            )
        return (
            Config.NORMAL_TARGET_DIM_WEIGHT
            if dimension == target_dimension
            else Config.NORMAL_OTHER_DIM_WEIGHT
        )

    def evaluate(self) -> Dict[str, Any]:
        manifest = self.load_json(self.manifest_output_path)
        baseline = self.score_lookup(self.load_json(self.baseline_path))
        candidate = self.score_lookup(self.load_json(self.candidate_path))
        target_dimension = manifest["dimension"]

        weighted_sum = 0.0
        total_weight = 0.0
        violations: List[Dict[str, Any]] = []
        rows = []

        for sample in manifest["samples"]:
            index = int(sample["index"])
            if index not in baseline or index not in candidate:
                raise ValueError(f"Micro scoring result is missing essay index: {index}")
            baseline_item = baseline[index]
            candidate_item = candidate[index]
            dim_details = {}
            weighted_components = []

            for dimension in Config.DIMENSIONS:
                details = self.dimension_improvement(
                    baseline_item,
                    candidate_item,
                    dimension,
                )
                dim_details[dimension] = details

                if details["improvement"] < -Config.MAX_SINGLE_DIM_REGRESSION:
                    violations.append(
                        {
                            "type": "single_dimension_regression",
                            "index": index,
                            "dimension": dimension,
                            "improvement": details["improvement"],
                        }
                    )

                for role in sample["evidence_roles"]:
                    weight = self.evidence_weight(role, dimension, target_dimension)
                    contribution = details["improvement"] * weight
                    weighted_sum += contribution
                    total_weight += weight
                    weighted_components.append(
                        {
                            "source": role,
                            "dimension": dimension,
                            "improvement": details["improvement"],
                            "weight": weight,
                            "contribution": contribution,
                        }
                    )

                    max_target_regression = (
                        Config.MAX_OUTLIER_TARGET_REGRESSION
                        if role == "outlier"
                        else Config.MAX_NORMAL_TARGET_REGRESSION
                    )
                    if (
                        dimension == target_dimension
                        and details["improvement"] < -max_target_regression
                    ):
                        violations.append(
                            {
                                "type": f"{role}_target_regression",
                                "index": index,
                                "dimension": dimension,
                                "improvement": details["improvement"],
                            }
                        )

            b_details = None
            if sample["b_bias_severities"]:
                baseline_magnitude = self.b_bias_magnitude(baseline_item)
                candidate_magnitude = self.b_bias_magnitude(candidate_item)
                improvement = baseline_magnitude - candidate_magnitude
                severity_weight = max(
                    Config.B_SEVERITY_WEIGHTS.get(severity, 1.0)
                    for severity in sample["b_bias_severities"]
                )
                weight = Config.B_BIAS_WEIGHT * severity_weight
                contribution = improvement * weight
                weighted_sum += contribution
                total_weight += weight
                b_details = {
                    "baseline_magnitude": baseline_magnitude,
                    "candidate_magnitude": candidate_magnitude,
                    "improvement": improvement,
                    "severity_weight": severity_weight,
                    "weight": weight,
                    "contribution": contribution,
                }
                weighted_components.append(
                    {
                        "source": "B_bias",
                        "dimension": "aggregate",
                        "improvement": improvement,
                        "weight": weight,
                        "contribution": contribution,
                    }
                )
                if improvement < -Config.MAX_B_BIAS_REGRESSION:
                    violations.append(
                        {
                            "type": "b_bias_regression",
                            "index": index,
                            "improvement": improvement,
                        }
                    )

            rows.append(
                {
                    **sample,
                    "dimension_details": dim_details,
                    "b_bias_details": b_details,
                    "weighted_components": weighted_components,
                }
            )

        weighted_average = weighted_sum / total_weight if total_weight else 0.0
        passed = (
            total_weight > 0
            and weighted_average >= Config.PASS_SCORE_THRESHOLD
            and not violations
        )
        payload = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "gate_passed": passed,
            "decision": "continue_pipeline" if passed else "reject_candidate",
            "dimension": target_dimension,
            "score": {
                "weighted_sum": weighted_sum,
                "total_weight": total_weight,
                "weighted_average": weighted_average,
                "pass_threshold": Config.PASS_SCORE_THRESHOLD,
            },
            "weights": {
                "outlier_target_dimension": Config.OUTLIER_TARGET_DIM_WEIGHT,
                "outlier_other_dimension": Config.OUTLIER_OTHER_DIM_WEIGHT,
                "normal_target_dimension": Config.NORMAL_TARGET_DIM_WEIGHT,
                "normal_other_dimension": Config.NORMAL_OTHER_DIM_WEIGHT,
                "b_bias": Config.B_BIAS_WEIGHT,
                "b_severity": Config.B_SEVERITY_WEIGHTS,
            },
            "hard_guards": {
                "max_outlier_target_regression": Config.MAX_OUTLIER_TARGET_REGRESSION,
                "max_normal_target_regression": Config.MAX_NORMAL_TARGET_REGRESSION,
                "max_single_dimension_regression": Config.MAX_SINGLE_DIM_REGRESSION,
                "max_b_bias_regression": Config.MAX_B_BIAS_REGRESSION,
            },
            "violations": violations,
            "rows": rows,
        }
        self.save_json(payload, self.eval_output_path)
        return payload
