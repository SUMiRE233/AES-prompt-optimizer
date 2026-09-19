"""
Gate Test Sampler

Build a small gate test set after rule integration:
- select 0-6 badcases for the improved dimension, ordered from low to high severity
- select 6 normal samples from train data
- concatenate badcases first, then normal samples
- emit an essays file for batch_scoring
"""

import json
import sys
from typing import Any, Dict, List, Optional, Set


# ============================================================
# 配置参数（显眼位置）
# ============================================================
class Config:
    """Configuration parameters for gate test sampling"""

    # ----------------------
    # 输入文件配置
    # ----------------------
    TRAIN_FILE = "final_train_scoring_results.json"                 # 输入：train 集评分结果
    BADCASE_FILE = "final_aes_badcases.json"                        # 输入：badcase 挖掘结果
    INJECTED_RULE_FILE = "etype_analysis/injected_rules_1.json" # 输入：已注入规则，用于识别本次改进维度

    # ----------------------
    # 输出文件配置
    # ----------------------
    OUTPUT_ESSAYS_FILE = "gate_test_essays.json" # 输出：作文列表，可作为 batch_scoring essays_path
    OUTPUT_MANIFEST_FILE = "gate_test_manifest.json"

    # ----------------------
    # 抽样参数配置
    # ----------------------
    IMPROVED_DIMENSION = None  # 可填 content / expression / structure；None 表示读取 injected 最新维度
    BADCASE_COUNT = 6          # 本次改进维度 badcase 抽取数，实际允许 0-6
    NORMAL_COUNT = 6           # 普通样本抽取数
    NORMAL_MAX_ABS_DIFF = 0.5  # 普通样本优先满足：该维度 AI 与 teacher 差值绝对值不超过该值

    MAIN_DIMS = ["content", "expression", "structure"]
    SEVERITY_ORDER = {
        "soft": 0,
        "severe": 1,
    }


# ============================================================
# 主类
# ============================================================
class GateTestSampler:
    """Build gate test samples for the latest improved scoring dimension."""

    def __init__(
        self,
        train_path: str,
        badcase_path: str,
        injected_rule_path: str,
        output_essays_path: str,
        output_manifest_path: str,
        improved_dimension: Optional[str] = None,
        badcase_count: int = Config.BADCASE_COUNT,
        normal_count: int = Config.NORMAL_COUNT,
    ):
        self.train_path = train_path
        self.badcase_path = badcase_path
        self.injected_rule_path = injected_rule_path
        self.output_essays_path = output_essays_path
        self.output_manifest_path = output_manifest_path
        self.improved_dimension = improved_dimension
        self.badcase_count = max(0, min(6, badcase_count))
        self.normal_count = max(0, normal_count)

        self.train_data: List[Dict[str, Any]] = []
        self.badcase_data: Dict[str, Any] = {}

    def load_json(self, path: str) -> Any:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_json(self, data: Any, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_inputs(self) -> None:
        self.train_data = self.load_json(self.train_path)
        self.badcase_data = self.load_json(self.badcase_path)

        if not isinstance(self.train_data, list) or not self.train_data:
            raise ValueError("Train data is empty or not a list.")

    def resolve_improved_dimension(self) -> str:
        if self.improved_dimension:
            dimension = self.improved_dimension
        else:
            injected_payload = self.load_json(self.injected_rule_path)
            injected_rules = injected_payload.get("injected_rules", [])
            if not injected_rules:
                raise ValueError("Injected rule file has no injected_rules.")
            dimension = injected_rules[-1].get("dimension")

        if dimension not in Config.MAIN_DIMS:
            raise ValueError(f"Invalid improved dimension: {dimension}")

        return dimension

    def get_train_item(self, data_index: int) -> Dict[str, Any]:
        if data_index < 0 or data_index >= len(self.train_data):
            raise IndexError(f"Badcase data_index out of range: {data_index}")
        return self.train_data[data_index]

    def severity_value(self, badcase: Dict[str, Any], dimension: str) -> float:
        z_scores = badcase.get("z_scores", {})
        z_score = z_scores.get(dimension, badcase.get("z_score", 0))
        try:
            return abs(float(z_score))
        except (TypeError, ValueError):
            return 0.0

    def collect_dimension_badcases(self, dimension: str) -> List[Dict[str, Any]]:
        e_result = self.badcase_data.get("E_residual", {}).get(dimension, {})
        candidates = []

        for severity in ["soft", "severe"]:
            for badcase in e_result.get(severity, []):
                if "data_index" not in badcase:
                    continue
                candidates.append(
                    {
                        "data_index": badcase["data_index"],
                        "severity": badcase.get("severity", severity),
                        "severity_value": self.severity_value(badcase, dimension),
                    }
                )

        candidates.sort(
            key=lambda item: (
                Config.SEVERITY_ORDER.get(item["severity"], 99),
                item["severity_value"],
                item["data_index"],
            )
        )
        return candidates[: self.badcase_count]

    def build_badcase_samples(self, dimension: str) -> List[Dict[str, Any]]:
        selected = self.collect_dimension_badcases(dimension)
        samples = []

        for rank, item in enumerate(selected, start=1):
            train_item = self.get_train_item(item["data_index"]).copy()
            train_item["gate_sample_type"] = "badcase"
            train_item["gate_dimension"] = dimension
            train_item["gate_rank"] = rank
            train_item["gate_severity"] = item["severity"]
            train_item["gate_severity_value"] = item["severity_value"]
            train_item["source_data_index"] = item["data_index"]
            samples.append(train_item)

        return samples

    def dimension_abs_diff(self, item: Dict[str, Any], dimension: str) -> float:
        return abs(float(item["AI"][dimension]) - float(item["teacher"][dimension]))

    def build_normal_samples(self, dimension: str, excluded_indices: Set[int]) -> List[Dict[str, Any]]:
        normal_candidates = []
        fallback_candidates = []

        for idx, item in enumerate(self.train_data):
            if idx in excluded_indices:
                continue

            abs_diff = self.dimension_abs_diff(item, dimension)
            candidate = {
                "data_index": idx,
                "abs_diff": abs_diff,
                "total_abs_diff": sum(self.dimension_abs_diff(item, dim) for dim in Config.MAIN_DIMS),
            }

            if abs_diff <= Config.NORMAL_MAX_ABS_DIFF:
                normal_candidates.append(candidate)
            else:
                fallback_candidates.append(candidate)

        normal_candidates.sort(key=lambda item: (item["abs_diff"], item["total_abs_diff"], item["data_index"]))
        fallback_candidates.sort(key=lambda item: (item["abs_diff"], item["total_abs_diff"], item["data_index"]))

        selected = (normal_candidates + fallback_candidates)[: self.normal_count]
        samples = []

        for rank, item in enumerate(selected, start=1):
            train_item = self.get_train_item(item["data_index"]).copy()
            train_item["gate_sample_type"] = "normal"
            train_item["gate_dimension"] = dimension
            train_item["gate_rank"] = rank
            train_item["gate_abs_diff"] = item["abs_diff"]
            train_item["source_data_index"] = item["data_index"]
            samples.append(train_item)

        return samples

    def build_essays_view(self, samples: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        essays_view = []

        for item in samples:
            if "index" not in item:
                raise ValueError("Gate sample is missing index; cannot build batch_scoring input.")

            essays_view.append({
                "index": item["index"],
                "essay": item.get("essay", ""),
            })

        return essays_view

    def build_manifest(
        self,
        dimension: str,
        badcase_samples: List[Dict[str, Any]],
        normal_samples: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return {
            "improved_dimension": dimension,
            "train_file": self.train_path,
            "badcase_file": self.badcase_path,
            "injected_rule_file": self.injected_rule_path,
            "output_essays_file": self.output_essays_path,
            "badcase_count": len(badcase_samples),
            "normal_count": len(normal_samples),
            "total_count": len(badcase_samples) + len(normal_samples),
            "samples": [
                {
                    "source_data_index": item.get("source_data_index"),
                    "index": item.get("index"),
                    "sample_type": item.get("gate_sample_type"),
                    "dimension": item.get("gate_dimension"),
                    "severity": item.get("gate_severity"),
                    "severity_value": item.get("gate_severity_value"),
                    "abs_diff": item.get("gate_abs_diff"),
                    "name": item.get("name"),
                    "page": item.get("page"),
                }
                for item in badcase_samples + normal_samples
            ],
        }

    def run(self) -> Dict[str, Any]:
        self.load_inputs()
        dimension = self.resolve_improved_dimension()

        badcase_samples = self.build_badcase_samples(dimension)
        excluded_indices = {item["source_data_index"] for item in badcase_samples}
        normal_samples = self.build_normal_samples(dimension, excluded_indices)
        gate_samples = badcase_samples + normal_samples

        essays_view = self.build_essays_view(gate_samples)
        manifest = self.build_manifest(dimension, badcase_samples, normal_samples)

        self.save_json(essays_view, self.output_essays_path)
        self.save_json(manifest, self.output_manifest_path)

        return manifest

    def print_summary(self, manifest: Dict[str, Any]) -> None:
        print("=" * 60)
        print("Gate Test Sampler")
        print("=" * 60)
        print(f"Improved dimension: {manifest['improved_dimension']}")
        print(f"Badcases: {manifest['badcase_count']}")
        print(f"Normals:   {manifest['normal_count']}")
        print(f"Total:     {manifest['total_count']}")
        print(f"Essays file: {manifest['output_essays_file']}")
        print(f"Manifest:    {self.output_manifest_path}")
        print("=" * 60)


# ============================================================
# 命令行接口
# ============================================================
def main() -> None:
    print("=" * 60)
    print("Gate Test Sampler")
    print("=" * 60)

    train_file = Config.TRAIN_FILE
    badcase_file = Config.BADCASE_FILE
    injected_rule_file = Config.INJECTED_RULE_FILE
    output_essays_file = Config.OUTPUT_ESSAYS_FILE
    output_manifest_file = Config.OUTPUT_MANIFEST_FILE
    improved_dimension = Config.IMPROVED_DIMENSION

    if len(sys.argv) > 1:
        # 支持：
        # python gate_test_sampler.py <dimension>
        # python gate_test_sampler.py <dimension> <output_essays.json>
        improved_dimension = sys.argv[1]
        if len(sys.argv) > 2:
            output_essays_file = sys.argv[2]

    print("\n当前配置:")
    print(f"  TRAIN_FILE:          {train_file}")
    print(f"  BADCASE_FILE:        {badcase_file}")
    print(f"  INJECTED_RULE_FILE:  {injected_rule_file}")
    print(f"  OUTPUT_ESSAYS_FILE:  {output_essays_file}")
    print(f"  OUTPUT_MANIFEST:     {output_manifest_file}")
    print(f"  BADCASE_COUNT:       {Config.BADCASE_COUNT}")
    print(f"  NORMAL_COUNT:        {Config.NORMAL_COUNT}")
    print("-" * 60)

    try:
        sampler = GateTestSampler(
            train_path=train_file,
            badcase_path=badcase_file,
            injected_rule_path=injected_rule_file,
            output_essays_path=output_essays_file,
            output_manifest_path=output_manifest_file,
            improved_dimension=improved_dimension,
            badcase_count=Config.BADCASE_COUNT,
            normal_count=Config.NORMAL_COUNT,
        )
        manifest = sampler.run()
        sampler.print_summary(manifest)
    except Exception as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
