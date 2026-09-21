import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# =========================
# 手动配置区
# =========================

# E 类分析器输出的 consolidated 文件。compiled_rules.*.feasible_rules 被视为待注入队列。
DEFAULT_SOURCE_FILE = "etype_analysis/contrastive_consolidated_8.json"

# 当前子迭代的输入 prompt。连续子迭代时，建议传入上一轮输出 prompt。
DEFAULT_PROMPT_FILE = "optimized_prompt2_meta.md"

# 当前子迭代后的 prompt 输出。
DEFAULT_OUTPUT_PROMPT_FILE = "test_injected_prompt2_meta.md"

# 更新后的 consolidated。默认覆盖写回 source，使已注入 rule 从 feasible_rules 中移除。
DEFAULT_SOURCE_OUTPUT_FILE = None

# 已注入 rule 的累计记录文件。
DEFAULT_INJECTED_FILE = "etype_analysis/injected_rules_3.json"

# 每次子迭代最多注入几条。业务约束：1 到 2 条。
DEFAULT_MAX_RULES_PER_SUB_ITERATION = 1

# 维度顺序必须与 prompt 中“特殊判例”下三个三级标题顺序一致。
DIMENSION_ORDER = ["content", "expression", "structure"]

SEVERE_WEIGHT = 5
SOFT_WEIGHT = 1

DIMENSION_CHINESE = {
    "content": "内容",
    "expression": "语言",
    "structure": "结构"
}

DEFAULT_FORBIDDEN_GENERALIZATION = [
    "不得外推为全局评分尺度",
    "不得设置固定分数下限",
    "不得影响其他评分维度"
]


class RuleIntegrationEngine:
    """
    E-rule 嵌入层。

    每次 run 表示一次大迭代下的子迭代：
    - 只选择“问题最严重”的一个维度
    - 在该维度 feasible_rules 中选择最可行的 1 到 2 条
    - 追加注入 prompt 对应特殊判例区
    - 从 contrastive consolidated 的 feasible_rules 队列移除这些 rule
    - 将已注入 rule 追加记录到 injected 文件

    本层信任 E 类分析器产出的 feasible_rules，不做二次证据过滤或冲突诊断。
    """

    def __init__(
        self,
        source_file: str = DEFAULT_SOURCE_FILE,
        prompt_file: str = DEFAULT_PROMPT_FILE,
        output_prompt_file: str = DEFAULT_OUTPUT_PROMPT_FILE,
        source_output_file: Optional[str] = DEFAULT_SOURCE_OUTPUT_FILE,
        injected_file: str = DEFAULT_INJECTED_FILE,
        major_iteration: Optional[int] = None,
        sub_iteration: Optional[int] = None,
        max_rules_per_sub_iteration: int = DEFAULT_MAX_RULES_PER_SUB_ITERATION,
        target_dimension: Optional[str] = None,
        defer_commit: bool = False,
    ):
        self.source_file = source_file
        self.prompt_file = prompt_file
        self.output_prompt_file = output_prompt_file
        self.source_output_file = source_output_file or source_file
        self.injected_file = injected_file
        self.major_iteration = major_iteration
        self.sub_iteration = sub_iteration
        self.max_rules_per_sub_iteration = min(max(1, max_rules_per_sub_iteration), 2)
        self.target_dimension = target_dimension
        self.defer_commit = defer_commit
        self.pending_transaction: Optional[Dict[str, Any]] = None

    def load_json(self, path: str) -> Any:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_json(self, data: Any, path: str) -> None:
        parent = Path(path).parent
        if str(parent) != ".":
            parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_text(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def save_text(self, text: str, path: str) -> None:
        parent = Path(path).parent
        if str(parent) != ".":
            parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def get_major_iteration(self, payload: Dict[str, Any]) -> Optional[int]:
        if self.major_iteration is not None:
            return self.major_iteration
        return payload.get("iteration")

    def get_feasible_rules(self, payload: Dict[str, Any], dimension: str) -> List[Dict[str, Any]]:
        return payload.get("compiled_rules", {}).get(dimension, {}).get("feasible_rules", [])

    def dimension_severity(self, payload: Dict[str, Any], dimension: str) -> Tuple[int, int, int, int]:
        stats = payload.get("stats_summary", {}).get(dimension, {})
        severe_count = int(stats.get("severe_count", 0) or 0)
        soft_count = int(stats.get("soft_count", 0) or 0)
        total_outliers = int(stats.get("total_outliers", 0) or 0)
        score = severe_count * SEVERE_WEIGHT + soft_count * SOFT_WEIGHT
        return score, total_outliers, severe_count, soft_count

    def rule_feasibility(self, rule: Dict[str, Any], source_index: int) -> Tuple[int, float, int, int, int, int]:
        evidence = rule.get("evidence", {}) or {}
        confidence_level = 1 if rule.get("confidence_level") == "trusted" else 0
        confidence = float(rule.get("confidence", 0) or 0)
        evidence_count = int(rule.get("evidence_count", 0) or 0)
        normal_count = len(evidence.get("normal_indices", []) or [])
        safe_flag = 1 if rule.get("safe_for_global_bias") else 0
        earlier_rule_bonus = -source_index
        return confidence_level, confidence, evidence_count, normal_count, safe_flag, earlier_rule_bonus

    def resolve_injection_anchor(self, rule: dict, dimension: str) -> dict:
        """解析规则的注入位置，直接返回规则指定的位置（后续在注入时验证）"""
        should_be_injected_at = rule.get("should_be_injected_at", "").strip()
        
        # 检查是否为 do_not_inject
        if should_be_injected_at == "do_not_inject":
            return {
                "resolved": "do_not_inject",
                "was_downgraded": False,
                "reason": "do_not_inject"
            }
        
        # 返回规则指定的位置（应为注意事项下的加粗子标题，如 **内容分特殊情形**）
        if should_be_injected_at:
            return {
                "resolved": should_be_injected_at,
                "was_downgraded": False,
                "reason": "specified_by_rule"
            }
        
        # 如果未指定位置，返回 None（注入时会报错）
        return {
            "resolved": None,
            "was_downgraded": False,
            "reason": "not_specified"
        }

    def select_rules_for_sub_iteration(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """选择本次子迭代的规则，软过滤无效规则并记录跳过原因"""
        dimension = self.select_target_dimension(payload)
        feasible_rules = self.get_feasible_rules(payload, dimension)
        
        # 软过滤规则，记录跳过原因
        filtered_rules = []
        skipped_rules = []
        
        for idx, rule in enumerate(feasible_rules):
            injection_result = self.resolve_injection_anchor(rule, dimension)
            
            # 跳过 do_not_inject
            if injection_result["resolved"] == "do_not_inject":
                skipped_rules.append({
                    "source_index": idx,
                    "reason": "do_not_inject",
                    "rule": rule
                })
                continue
            
            # 跳过缺少必要字段的规则
            trigger_condition = rule.get("trigger_condition", "").strip()
            scoring_adjustment = rule.get("scoring_adjustment", "").strip()
            if not trigger_condition or not scoring_adjustment:
                skipped_rules.append({
                    "source_index": idx,
                    "reason": "missing_required_fields",
                    "rule": rule
                })
                continue
            
            # 添加注入位置信息
            rule["_injection_info"] = injection_result
            filtered_rules.append((idx, rule))
        
        # 对过滤后的规则排序
        ranked = sorted(
            filtered_rules,
            key=lambda item: self.rule_feasibility(item[1], item[0]),
            reverse=True,
        )
        selected = ranked[: self.max_rules_per_sub_iteration]

        return {
            "dimension": dimension,
            "dimension_severity": self.dimension_severity(payload, dimension),
            "rules": [
                {
                    "source_rule_index": source_index + 1,
                    "queue_index": source_index,
                    "rule": rule,
                }
                for source_index, rule in selected
            ],
            "skipped_rules": skipped_rules
        }

    def select_target_dimension(self, payload: Dict[str, Any]) -> str:
        if self.target_dimension is not None:
            if self.target_dimension not in DIMENSION_ORDER:
                raise ValueError(f"Invalid target dimension: {self.target_dimension}")
            if not self.get_feasible_rules(payload, self.target_dimension):
                raise ValueError(
                    f"No feasible_rules for target dimension: {self.target_dimension}"
                )
            return self.target_dimension

        candidate_dimensions = [
            dimension
            for dimension in DIMENSION_ORDER
            if self.get_feasible_rules(payload, dimension)
        ]
        if not candidate_dimensions:
            raise ValueError("没有可注入的 feasible_rules")

        return max(
            candidate_dimensions,
            key=lambda dimension: (
                *self.dimension_severity(payload, dimension),
                -DIMENSION_ORDER.index(dimension),
            ),
        )

    def remove_selected_rules(self, payload: Dict[str, Any], selection: Dict[str, Any]) -> None:
        rules = self.get_feasible_rules(payload, selection["dimension"])
        for selected in sorted(selection["rules"], key=lambda item: item["queue_index"], reverse=True):
            rules.pop(selected["queue_index"])

    def load_injected_payload(self, major_iteration: Optional[int]) -> Dict[str, Any]:
        if Path(self.injected_file).exists():
            return self.load_json(self.injected_file)
        return {
            "major_iteration": major_iteration,
            "updated_at": None,
            "injected_rules": [],
        }

    def next_sub_iteration(self, injected_payload: Dict[str, Any], major_iteration: Optional[int]) -> int:
        if self.sub_iteration is not None:
            return self.sub_iteration

        existing = [
            int(item.get("sub_iteration", 0) or 0)
            for item in injected_payload.get("injected_rules", [])
            if item.get("major_iteration") == major_iteration
        ]
        return max(existing, default=0) + 1

    def build_injected_records(
        self,
        selection: Dict[str, Any],
        major_iteration: Optional[int],
        sub_iteration: int,
    ) -> List[Dict[str, Any]]:
        records = []
        for selected in selection["rules"]:
            rule = selected["rule"]
            injection_info = rule.get("_injection_info", {})
            
            records.append(
                {
                    "major_iteration": major_iteration,
                    "sub_iteration": sub_iteration,
                    "dimension": selection["dimension"],
                    "source_file": self.source_file,
                    "source_rule_index": selected["source_rule_index"],
                    "trigger_condition": rule.get("trigger_condition", "").strip(),
                    "scoring_adjustment": rule.get("scoring_adjustment", "").strip(),
                    "counter_examples": rule.get("counter_examples", "").strip(),
                    "confidence": rule.get("confidence"),
                    "evidence_count": rule.get("evidence_count"),
                    "confidence_level": rule.get("confidence_level"),
                    "evidence": rule.get("evidence", {}),
                    "evidence_index_type": rule.get("evidence_index_type"),
                    # 新增字段
                    "should_be_injected_at": rule.get("should_be_injected_at", "local_residual_calibration"),
                    "resolved_injection_anchor": injection_info.get("resolved", "local_residual_calibration"),
                    "injection_strength": rule.get("injection_strength", "soft"),
                    "expected_scope": rule.get("expected_scope", []),
                    "was_anchor_downgraded": injection_info.get("was_downgraded", False),
                    "skip_reason": None,
                    "anti_overfit_boundary": rule.get("anti_overfit_boundary", "").strip(),
                    "forbidden_generalization": rule.get("forbidden_generalization", DEFAULT_FORBIDDEN_GENERALIZATION),
                }
            )
        return records

    def append_injected_records(
        self,
        records: List[Dict[str, Any]],
        major_iteration: Optional[int],
    ) -> None:
        injected_payload = self.load_injected_payload(major_iteration)
        injected_payload["major_iteration"] = injected_payload.get("major_iteration", major_iteration)
        injected_payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
        injected_payload.setdefault("injected_rules", []).extend(records)
        self.save_json(injected_payload, self.injected_file)

    def render_rules_for_prompt(self, selected_rules: List[Dict[str, Any]], injection_mode: str = "append_nested_bullet") -> str:
        """
        渲染规则到prompt，支持两种模式：
        - append_nested_bullet: 嵌套 bullet 格式（维度误判提醒）
        - append_rule_block: 规则块格式（局部残差校准）
        """
        lines = []
        for selected in selected_rules:
            rule = selected["rule"]
            dimension = selected.get("dimension", "")
            trigger = rule.get("trigger_condition", "").strip()
            adjustment = rule.get("scoring_adjustment", "").strip()
            
            if not trigger or not adjustment:
                raise ValueError("待注入 rule 缺少 trigger_condition 或 scoring_adjustment")
            
            # 获取边界和禁止外推信息
            anti_overfit_boundary = rule.get("anti_overfit_boundary", "").strip()
            if not anti_overfit_boundary:
                anti_overfit_boundary = rule.get("counter_examples", "").strip()
            
            forbidden_generalization = rule.get("forbidden_generalization", DEFAULT_FORBIDDEN_GENERALIZATION)
            if isinstance(forbidden_generalization, list):
                forbidden_generalization = "；".join(forbidden_generalization)
            elif not isinstance(forbidden_generalization, str):
                forbidden_generalization = "；".join(DEFAULT_FORBIDDEN_GENERALIZATION)
            
            if injection_mode == "append_rule_block":
                # local_residual_calibration 格式
                dimension_cn = DIMENSION_CHINESE.get(dimension, dimension)
                lines.append(f"- **{dimension_cn}｜局部残差校准规则**：")
                lines.append(f"  - 触发条件：{trigger}")
                lines.append(f"  - 调整方向：{adjustment}")
                if anti_overfit_boundary:
                    lines.append(f"  - 不适用边界：{anti_overfit_boundary}")
                if forbidden_generalization:
                    lines.append(f"  - 禁止外推：{forbidden_generalization}")
            else:
                # append_nested_bullet 格式（维度误判提醒）
                lines.append(f"  - 触发条件：{trigger}")
                lines.append(f"    调整方向：{adjustment}")
                if anti_overfit_boundary:
                    lines.append(f"    不适用边界：{anti_overfit_boundary}")
                if forbidden_generalization:
                    lines.append(f"    禁止外推：{forbidden_generalization}")
        
        return "\n".join(lines)

    def ensure_local_residual_section(self, prompt_text: str) -> str:
        """确保 prompt 中存在 E类局部残差校准区块（保留此方法但不再使用）"""
        anchor_text = "**E类局部残差校准**"
        
        if anchor_text in prompt_text:
            return prompt_text
        
        # 在 ## 注意事项 中、## 待评作文 之前插入
        note_section_pattern = re.compile(r"(?ms)(## 注意事项.*?)(?=^## 待评作文|\Z)")
        match = note_section_pattern.search(prompt_text)
        
        if match:
            section_start = match.start()
            section_end = match.end()
            section_content = match.group(1)
            
            # 构建新的局部残差校准区块
            residual_section = f"""

{anchor_text}：
以下规则仅用于修正已验证的局部残差，不得作为全局评分尺度。只有当触发条件、调整方向和不适用边界同时满足时才可参考；若与评分标准、严重硬伤规则或全局偏差校准冲突，以评分标准和严重硬伤规则为准。

"""
            
            # 在注意事项末尾、待评作文之前插入
            new_content = section_content.rstrip() + "\n" + residual_section
            return prompt_text[:section_start] + new_content + prompt_text[section_end:]
        
        return prompt_text

    def find_special_case_section(self, prompt_text: str) -> Tuple[int, int]:
        section_pattern = re.compile(r"(?ms)^## .+?(?=^## |\Z)")
        for match in section_pattern.finditer(prompt_text):
            section = match.group(0)
            if len(re.findall(r"(?m)^### ", section)) >= 3:
                return match.start(), match.end()
        raise ValueError("未找到包含至少三个三级标题的特殊判例区")

    def split_dimension_subsections(self, section_text: str) -> List[Tuple[int, int, str]]:
        heading_matches = list(re.finditer(r"(?m)^### .*$", section_text))
        if len(heading_matches) < 3:
            raise ValueError("特殊判例区内三级标题不足三个")

        subsections = []
        for i, heading in enumerate(heading_matches[:3]):
            start = heading.start()
            end = heading_matches[i + 1].start() if i + 1 < len(heading_matches) else len(section_text)
            subsections.append((start, end, section_text[start:end]))
        return subsections

    def append_rules_to_subsection(self, subsection_text: str, rendered_rules: str) -> str:
        subsection_text = subsection_text.rstrip()
        if not rendered_rules:
            return subsection_text + "\n"
        if rendered_rules in subsection_text:
            return subsection_text + "\n"
        return f"{subsection_text}\n\n{rendered_rules}\n"

    def inject_rules(self, prompt_text: str, selection: Dict[str, Any]) -> str:
        """根据规则指定的加粗子标题进行注入"""
        if not selection["rules"]:
            return prompt_text
        
        target_dimension = selection["dimension"]
        
        # 获取规则的注入位置（应为注意事项下的加粗子标题）
        first_rule = selection["rules"][0]["rule"]
        injection_info = first_rule.get("_injection_info", {})
        injection_anchor = injection_info.get("resolved")
        
        if not injection_anchor:
            raise ValueError("规则未指定注入位置 (should_be_injected_at)")
        
        # 构建带维度信息的规则列表
        rules_with_dimension = []
        for rule_item in selection["rules"]:
            rules_with_dimension.append({
                "rule": rule_item["rule"],
                "dimension": target_dimension
            })
        
        rendered_rules = self.render_rules_for_prompt(rules_with_dimension, "append_nested_bullet")
        
        # 在注意事项中查找指定的加粗子标题并注入
        return self.inject_under_bold_subheading(prompt_text, injection_anchor, rendered_rules)
    
    def inject_under_bold_subheading(self, prompt_text: str, target_heading: str, rendered_rules: str) -> str:
        """
        在注意事项中查找指定的加粗子标题并在其下注入规则
        
        Args:
            prompt_text: prompt 文本
            target_heading: 目标加粗子标题，如 **内容分特殊情形**
            rendered_rules: 渲染后的规则文本
        
        Returns:
            注入后的 prompt 文本
        
        Raises:
            ValueError: 如果找不到目标子标题
        """
        # 找到注意事项区块
        note_section_pattern = re.compile(r"(?ms)(## 注意事项.*?)(?=^## 待评作文|\Z)")
        match = note_section_pattern.search(prompt_text)
        
        if not match:
            raise ValueError("未找到注意事项区块")
        
        section_start = match.start()
        section_end = match.end()
        section_text = match.group(1)
        
        # 检查目标子标题是否存在
        if target_heading not in section_text:
            raise ValueError(f"在注意事项中找不到目标子标题: {target_heading}")
        
        # 在目标子标题后插入规则
        # 找到子标题的结束位置（考虑子标题后可能有换行和内容）
        heading_end = section_text.find(target_heading) + len(target_heading)
        
        # 找到下一个加粗子标题或段落结束
        next_bold_pattern = r"(?=\n\*\*[^*]+\*\*)"
        next_bold_match = re.search(next_bold_pattern, section_text[heading_end:])
        
        if next_bold_match:
            # 在当前子标题和下一个子标题之间插入
            insert_pos = heading_end + next_bold_match.start()
        else:
            # 在当前子标题后面直接插入（到区块末尾）
            insert_pos = heading_end
        
        # 在插入位置添加规则
        new_section_text = section_text[:insert_pos] + "\n" + rendered_rules + "\n" + section_text[insert_pos:]
        
        return prompt_text[:section_start] + new_section_text + prompt_text[section_end:]
    
    def commit(self) -> Dict[str, Any]:
        if self.pending_transaction is None:
            raise RuntimeError("No pending rule-injection transaction to commit.")

        transaction = self.pending_transaction
        self.save_json(transaction["payload"], self.source_output_file)
        self.append_injected_records(
            transaction["records"],
            transaction["major_iteration"],
        )
        self.save_skipped_rules(
            transaction["skipped_rules"],
            transaction["major_iteration"],
            transaction["sub_iteration"],
        )
        self.pending_transaction = None
        return {
            "source_output_file": self.source_output_file,
            "injected_file": self.injected_file,
            "inserted_rule_count": len(transaction["records"]),
        }

    def rollback(self) -> None:
        self.pending_transaction = None

    def run(self) -> Dict[str, Any]:
        payload = self.load_json(self.source_file)
        major_iteration = self.get_major_iteration(payload)
        injected_payload = self.load_injected_payload(major_iteration)
        sub_iteration = self.next_sub_iteration(injected_payload, major_iteration)

        selection = self.select_rules_for_sub_iteration(payload)
        prompt_text = self.load_text(self.prompt_file)
        output_text = self.inject_rules(prompt_text, selection)
        self.save_text(output_text, self.output_prompt_file)

        self.remove_selected_rules(payload, selection)
        records = self.build_injected_records(selection, major_iteration, sub_iteration)
        self.pending_transaction = {
            "payload": payload,
            "records": records,
            "skipped_rules": selection.get("skipped_rules", []),
            "major_iteration": major_iteration,
            "sub_iteration": sub_iteration,
        }

        if not self.defer_commit:
            self.commit()

        return {
            "major_iteration": major_iteration,
            "sub_iteration": sub_iteration,
            "dimension": selection["dimension"],
            "inserted_rules": records,
            "skipped_rules": selection.get("skipped_rules", []),
            "commit_status": "pending" if self.defer_commit else "committed",
            "remaining_feasible_rules": {
                dimension: len(self.get_feasible_rules(payload, dimension))
                for dimension in DIMENSION_ORDER
            },
        }
    
    def save_skipped_rules(self, skipped_rules: List[Dict[str, Any]], major_iteration: Optional[int], sub_iteration: int) -> None:
        """保存跳过的规则记录"""
        if not skipped_rules:
            return
        
        injected_payload = self.load_injected_payload(major_iteration)
        injected_payload.setdefault("skipped_rules", []).extend([
            {
                "major_iteration": major_iteration,
                "sub_iteration": sub_iteration,
                "source_index": item["source_index"],
                "skip_reason": item["reason"],
                "rule_summary": {
                    "trigger_condition": item["rule"].get("trigger_condition", "")[:50] + "..." if len(item["rule"].get("trigger_condition", "")) > 50 else item["rule"].get("trigger_condition", ""),
                    "confidence": item["rule"].get("confidence"),
                    "should_be_injected_at": item["rule"].get("should_be_injected_at", "local_residual_calibration")
                }
            }
            for item in skipped_rules
        ])
        self.save_json(injected_payload, self.injected_file)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按子迭代将 E 类 feasible rules 注入 prompt")
    parser.add_argument("--source", default=DEFAULT_SOURCE_FILE)
    parser.add_argument("--source-output", default=DEFAULT_SOURCE_OUTPUT_FILE)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT_FILE)
    parser.add_argument("--output-prompt", default=DEFAULT_OUTPUT_PROMPT_FILE)
    parser.add_argument("--injected", default=DEFAULT_INJECTED_FILE)
    parser.add_argument("--major-iteration", type=int, default=None)
    parser.add_argument("--sub-iteration", type=int, default=None)
    parser.add_argument("--max-rules-per-sub-iteration", type=int, default=DEFAULT_MAX_RULES_PER_SUB_ITERATION)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    engine = RuleIntegrationEngine(
        source_file=args.source,
        source_output_file=args.source_output,
        prompt_file=args.prompt,
        output_prompt_file=args.output_prompt,
        injected_file=args.injected,
        major_iteration=args.major_iteration,
        sub_iteration=args.sub_iteration,
        max_rules_per_sub_iteration=args.max_rules_per_sub_iteration,
    )
    report = engine.run()

    print("规则子迭代嵌入完成")
    print(f"  大迭代: {report['major_iteration']}")
    print(f"  子迭代: {report['sub_iteration']}")
    print(f"  目标维度: {report['dimension']}")
    print(f"  注入规则数: {len(report['inserted_rules'])}")
    print(f"  剩余feasible_rules: {report['remaining_feasible_rules']}")
    print(f"  输出prompt: {args.output_prompt}")
    print(f"  已注入记录: {args.injected}")
    print(f"  更新后的consolidated: {args.source_output or args.source}")


if __name__ == "__main__":
    main()
