"""Prompt 结构契约（ETYPE_ITERATION_V2_DESIGN.md §13）。

两个职责：

1. **输入结构契约**（§13.2）：B 类优化器的输入必须满足 IN-1/IN-2/IN-3。
   不满足则拒绝运行，而不是让 E 类迭代在注入阶段才失败。
2. **优化器权限校验**（§13.3 / §13.4）：把"只冻结分点标题、不冻结分点内容"
   这一约定做成机器可检验的规则。

E 类迭代的注入锚点由 `build_anchor_registry()` 从 final prompt **运行时解析**得到，
不做静态硬编码（P7）。因此分点标题一旦漂移，失败发生在启动时而非注入时。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


DIMENSION_ORDER: Tuple[str, ...] = ("content", "expression", "structure")

# 维度 → 该维"特殊情形"分点标题必须包含的关键词之一
DIMENSION_MARKERS: Dict[str, Tuple[str, ...]] = {
    "content": ("内容",),
    "expression": ("表达", "语言"),
    "structure": ("结构",),
}

SPECIAL_CASE_KEYWORD = "特殊情形"

NOTE_HEADING = "## 注意事项"

# §13.4 预算。分点数量是唯一的有效约束（膨胀由新增分点驱动，
# 不是由条目丰富度驱动）；字符预算初版刻意宽松。
MAX_BOLD_SECTIONS = 12
MAX_PROMPT_CHARS = 4200
MAX_ADDED_CHARS_PER_ITERATION = 800
# §5.3：单轮新增加粗分点最多 2 个。
MAX_NEW_BOLD_SECTIONS_PER_ROUND = 2

# §13.3 规则 5：新增分点不得是"维度评分限制"类。
#
# 关键词分两层，因为中文里有些词既能构成"维度限制"也能构成"操作步骤"。
# 实测（V1 第 1/2 次尝试）`**评分前锚定校准**`、`**打分前档位校准**`
# 被判为 R5，但它们是"评分前操作步骤"，属于允许类型 —— 单一关键词表过宽。
#
#   - 无歧义类：出现即必然是数值/门槛类限制，任何情况下都不许作分点标题。
#   - 有歧义类：单独出现时按限制类处理；但若标题同时含"操作标记"，
#               则判为操作分点放行。
UNAMBIGUOUS_RESTRICTION_KEYWORDS: Tuple[str, ...] = (
    "下限",
    "上限",
    "阈值",
    "门槛",
    "起评",
    "封顶",
    "区间",
    "不得低于",
    "不低于",
    "不超过",
    "不高于",
    "百分比",
)

AMBIGUOUS_RESTRICTION_KEYWORDS: Tuple[str, ...] = (
    "锚点",
    "校准",
)

# 操作分点标记：标题含其一，即表明该分点描述的是"评分步骤 / 结束前自查"，
# 因而可以中和有歧义的限制词。
OPERATIONAL_NAME_MARKERS: Tuple[str, ...] = (
    "评分前",
    "打分前",
    "赋分前",
    "结束前",
    "输出前",
    "提交前",
    "自查",
    "自检",
    "复核",
    "核对",
    "步骤",
    "流程",
    "清单",
    "顺序",
)

# 硬比较标记：与维度词出现在同一条目时，说明正文在给某个维度设硬性限制。
HARD_COMPARISON_MARKERS: Tuple[str, ...] = (
    "不得低于",
    "不低于",
    "不得超过",
    "不超过",
    "不高于",
    "至少",
    "至多",
    "≥",
    "≤",
    ">=",
    "<=",
)

# 向后兼容别名：两层关键词的并集。
FORBIDDEN_NEW_SECTION_KEYWORDS: Tuple[str, ...] = (
    UNAMBIGUOUS_RESTRICTION_KEYWORDS + AMBIGUOUS_RESTRICTION_KEYWORDS
)

_NOTE_SECTION_RE = re.compile(r"(?ms)^##[ \t]*注意事项[ \t]*$(?P<body>.*?)(?=^##[ \t]|\Z)")
_BOLD_RE = re.compile(r"(?m)^\*\*(?P<name>[^*\n]+?)\*\*")
_TOP_HEADING_RE = re.compile(r"(?m)^##[ \t]*(?P<name>[^\n]+?)[ \t]*$")
_THIRD_LEVEL_RE = re.compile(r"(?m)^###[ \t]")


def has_operational_marker(name: str) -> bool:
    """标题是否表明该分点是"操作步骤 / 结束前自查"。"""
    return any(marker in name for marker in OPERATIONAL_NAME_MARKERS)


def restriction_keyword_hits(name: str) -> List[str]:
    """返回标题命中的"维度评分限制"关键词。

    无歧义词一旦命中即返回；有歧义词（锚点/校准）在标题含操作标记时被中和。
    """
    unambiguous = [word for word in UNAMBIGUOUS_RESTRICTION_KEYWORDS if word in name]
    if unambiguous:
        return unambiguous
    if has_operational_marker(name):
        return []
    return [word for word in AMBIGUOUS_RESTRICTION_KEYWORDS if word in name]


# 维度词与硬比较必须**紧邻**才算“把维度限制写进正文”。
# 同行共现不够：V1->V2 实测中，合法的操作型自查
# 「至少两处具体证据（如错别字位置、病句内容、结构缺陷描述）」
# 里的 `内容`/`结构` 只是示例文字，同行共现规则会把它误杀。
# 两个词表都由上面的常量派生，避免与提示词中渲染的列表漂移。
_DIMENSION_WORD_PATTERN = "|".join(
    re.escape(marker)
    for dimension in DIMENSION_ORDER
    for marker in DIMENSION_MARKERS[dimension]
)
_DIMENSION_HARD_LIMIT_RE = re.compile(
    "(?:%s)分?\\s*(?:%s)"
    % (
        _DIMENSION_WORD_PATTERN,
        "|".join(re.escape(marker) for marker in HARD_COMPARISON_MARKERS),
    )
)


def body_dimension_restriction_hits(body: str) -> List[str]:
    """找出正文中“维度词紧邻硬比较”的条目（如 `内容分不得低于 6`）。

    新增的"操作分点"若把某个维度的硬性限制写进正文，等同于抢占维度表述，
    因此与标题命中同等处理。但必须是紧邻匹配，否则会误杀仅仅**提及**
    维度名的操作型自查。
    """
    hits: List[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _DIMENSION_HARD_LIMIT_RE.search(stripped):
            hits.append(stripped[:70])
    return hits


class PromptContractError(ValueError):
    """输入结构契约或优化器权限校验失败。"""

    def __init__(self, violations: Sequence["ContractViolation"]):
        self.violations = list(violations)
        summary = "；".join(str(item) for item in self.violations)
        super().__init__(f"Prompt 结构契约校验失败：{summary}")


@dataclass(frozen=True)
class ContractViolation:
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True)
class BoldSection:
    """一个加粗分点。标题冻结，标题下的内容允许修改。"""

    name: str
    order: int
    char_offset: int

    @property
    def heading(self) -> str:
        return f"**{self.name}**"


def prompt_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def note_section_body(text: str) -> Optional[str]:
    """返回 `## 注意事项` 的正文（不含标题行）；不存在时返回 None。"""
    match = _NOTE_SECTION_RE.search(text)
    return match.group("body") if match else None


def top_level_headings(text: str) -> List[str]:
    return [item.group("name") for item in _TOP_HEADING_RE.finditer(text)]


def parse_bold_sections(text: str) -> List[BoldSection]:
    """解析 `## 注意事项` 下的加粗分点，按出现顺序返回。"""
    body = note_section_body(text)
    if body is None:
        return []
    offset_base = text.find(body)
    sections: List[BoldSection] = []
    for order, match in enumerate(_BOLD_RE.finditer(body)):
        sections.append(
            BoldSection(
                name=match.group("name").strip(),
                order=order,
                char_offset=offset_base + match.start(),
            )
        )
    return sections


def bold_section_bodies(text: str) -> Dict[str, str]:
    """加粗分点名 → 该分点标题之后、下一个分点之前的正文（用于变更审计）。"""
    body = note_section_body(text)
    if body is None:
        return {}
    matches = list(_BOLD_RE.finditer(body))
    bodies: Dict[str, str] = {}
    for position, match in enumerate(matches):
        start = match.end()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(body)
        name = match.group("name").strip()
        bodies.setdefault(name, body[start:end].strip())
    return bodies


def section_diff(before: str, after: str) -> Dict[str, List[str]]:
    """比较两版 prompt 的加粗分点：哪些正文被改、哪些新增、哪些消失。

    用于补回 `iteration_history.json` 里长期为空的 `changed_sections` /
    `added_sections`（设计稿 §11.2 的 B3）。
    """
    before_bodies = bold_section_bodies(before)
    after_bodies = bold_section_bodies(after)

    added = [name for name in after_bodies if name not in before_bodies]
    removed = [name for name in before_bodies if name not in after_bodies]
    changed = [
        name
        for name, content in before_bodies.items()
        if name in after_bodies and after_bodies[name] != content
    ]
    return {"changed": changed, "added": added, "removed": removed}


def dimension_of(section_name: str) -> Optional[str]:
    """若该分点标题是某维度的"特殊情形"分点，返回维度名。"""
    if SPECIAL_CASE_KEYWORD not in section_name:
        return None
    for dimension in DIMENSION_ORDER:
        if any(marker in section_name for marker in DIMENSION_MARKERS[dimension]):
            return dimension
    return None


def dimension_anchor_map(text: str) -> Dict[str, str]:
    """维度 → 加粗分点标题（含 `**`）。缺失的维度不出现在结果中。"""
    anchors: Dict[str, str] = {}
    for section in parse_bold_sections(text):
        dimension = dimension_of(section.name)
        if dimension and dimension not in anchors:
            anchors[dimension] = section.heading
    return anchors


def validate_input_contract(text: str) -> List[ContractViolation]:
    """§13.2：IN-1 存在 `## 注意事项`；IN-2 三维"特殊情形"分点各恰好一个；IN-3 分点名称唯一。"""
    violations: List[ContractViolation] = []

    if note_section_body(text) is None:
        violations.append(ContractViolation("IN-1", f"缺少 `{NOTE_HEADING}`"))
        return violations

    sections = parse_bold_sections(text)

    found: Dict[str, List[str]] = {}
    for section in sections:
        dimension = dimension_of(section.name)
        if dimension:
            found.setdefault(dimension, []).append(section.name)

    for dimension in DIMENSION_ORDER:
        names = found.get(dimension, [])
        if not names:
            markers = "/".join(DIMENSION_MARKERS[dimension])
            violations.append(
                ContractViolation(
                    "IN-2",
                    f"缺少 {dimension} 维的“{SPECIAL_CASE_KEYWORD}”分点"
                    f"（标题需同时包含 {markers} 与“{SPECIAL_CASE_KEYWORD}”）",
                )
            )
        elif len(names) > 1:
            violations.append(
                ContractViolation(
                    "IN-2",
                    f"{dimension} 维存在 {len(names)} 个“{SPECIAL_CASE_KEYWORD}”分点：{names}",
                )
            )

    seen: Dict[str, int] = {}
    for section in sections:
        seen[section.name] = seen.get(section.name, 0) + 1
    duplicated = sorted(name for name, count in seen.items() if count > 1)
    if duplicated:
        violations.append(
            ContractViolation("IN-3", f"加粗分点名称重复：{duplicated}")
        )

    return violations


def require_input_contract(text: str) -> None:
    violations = validate_input_contract(text)
    if violations:
        raise PromptContractError(violations)


def validate_optimizer_edit(before: str, after: str) -> List[ContractViolation]:
    """§13.3 权限矩阵 + §13.4 预算。

    允许：改既有分点的**内容**、在既有分点下增改条目、新增"操作分点"。
    禁止：改既有分点的**标题**、删除分点、变更顶层 `##` 集合、
          新增维度评分限制类分点、使用 `###`。
    """
    violations: List[ContractViolation] = []

    # 规则 6：顶层二级标题集合不可变
    before_headings = top_level_headings(before)
    after_headings = top_level_headings(after)
    if before_headings != after_headings:
        violations.append(
            ContractViolation(
                "R6",
                f"顶层 `##` 标题被变更：{before_headings} -> {after_headings}",
            )
        )

    before_sections = parse_bold_sections(before)
    after_sections = parse_bold_sections(after)
    before_names = [section.name for section in before_sections]
    after_names = [section.name for section in after_sections]
    after_name_set = set(after_names)

    # 规则 1：既有分点的标题冻结。改名与删除都会破坏 E 侧注册表。
    for name in before_names:
        if name not in after_name_set:
            violations.append(
                ContractViolation(
                    "R1",
                    f"既有加粗分点被改名或删除：**{name}**（标题必须冻结）",
                )
            )

    # 规则 1b：既有分点的相对顺序必须保持不变（§5.1）。
    order_index = {name: position for position, name in enumerate(after_names)}
    surviving = [name for name in before_names if name in order_index]
    positions = [order_index[name] for name in surviving]
    if positions != sorted(positions):
        violations.append(
            ContractViolation(
                "R1",
                f"既有加粗分点的顺序被改变：{[name for name in surviving]}",
            )
        )

    # 规则 4b：单轮新增加粗分点不超过上限（§5.3）。
    new_names = [name for name in after_names if name not in before_names]
    if len(new_names) > MAX_NEW_BOLD_SECTIONS_PER_ROUND:
        violations.append(
            ContractViolation(
                "R4",
                f"单轮新增加粗分点 {len(new_names)} 个，超过上限 "
                f"{MAX_NEW_BOLD_SECTIONS_PER_ROUND}：{new_names}",
            )
        )

    # 规则 4 / 5：新增分点只能是"操作分点"，不得抢占维度表述
    after_bodies = bold_section_bodies(after)
    for name in new_names:
        hits = restriction_keyword_hits(name)
        if hits:
            violations.append(
                ContractViolation(
                    "R5",
                    f"新增分点 **{name}** 属维度评分限制类（命中 {hits}），"
                    f"应下沉为 `{SPECIAL_CASE_KEYWORD}` 分点下的条目",
                )
            )
            continue
        dimension_hits = [
            dimension
            for dimension in DIMENSION_ORDER
            if any(marker in name for marker in DIMENSION_MARKERS[dimension])
        ]
        if dimension_hits:
            violations.append(
                ContractViolation(
                    "R5",
                    f"新增分点 **{name}** 含维度表述 {dimension_hits}，"
                    "维度类内容只能写在既有的“特殊情形”分点下",
                )
            )
            continue
        body_hits = body_dimension_restriction_hits(after_bodies.get(name, ""))
        if body_hits:
            violations.append(
                ContractViolation(
                    "R5",
                    f"新增分点 **{name}** 的正文把维度限制写成条目 {body_hits}，"
                    f"维度限制应下沉为 `{SPECIAL_CASE_KEYWORD}` 分点下的条目",
                )
            )

    # 规则 7：不使用三级标题
    if _THIRD_LEVEL_RE.search(after):
        violations.append(ContractViolation("R7", "输出中出现 `###` 三级标题"))

    # §13.4 预算
    if len(after_sections) > MAX_BOLD_SECTIONS:
        violations.append(
            ContractViolation(
                "BUDGET",
                f"加粗分点数 {len(after_sections)} 超过上限 {MAX_BOLD_SECTIONS}",
            )
        )
    if len(after) > MAX_PROMPT_CHARS:
        violations.append(
            ContractViolation(
                "BUDGET", f"字符数 {len(after)} 超过上限 {MAX_PROMPT_CHARS}"
            )
        )
    added = len(after) - len(before)
    if added > MAX_ADDED_CHARS_PER_ITERATION:
        violations.append(
            ContractViolation(
                "BUDGET",
                f"单轮新增字符 {added} 超过上限 {MAX_ADDED_CHARS_PER_ITERATION}",
            )
        )

    return violations


def build_anchor_registry(prompt_text: str, prompt_path: str = "") -> Dict[str, Any]:
    """§13.5：E 类注入锚点由 final prompt 运行时注册，不做静态硬编码。"""
    require_input_contract(prompt_text)
    sections = parse_bold_sections(prompt_text)
    anchors = dimension_anchor_map(prompt_text)
    return {
        "identity": "bold_section_name",
        "schema_version": 1,
        "prompt_path": prompt_path,
        "prompt_sha256": prompt_sha256(prompt_text),
        "note_heading": NOTE_HEADING,
        "sections": [
            {
                "name": section.name,
                "heading": section.heading,
                "order": section.order,
                "dimension": dimension_of(section.name),
            }
            for section in sections
        ],
        "dimension_anchors": {dim: anchors[dim] for dim in DIMENSION_ORDER if dim in anchors},
    }


def write_anchor_registry(prompt_path: str, output_dir: str = "etype_analysis") -> Path:
    """读取 prompt 文件、校验契约、写出注册表，返回注册表路径。"""
    text = Path(prompt_path).read_text(encoding="utf-8")
    registry = build_anchor_registry(text, prompt_path=prompt_path)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f"anchor_registry_{registry['prompt_sha256'][:12]}.json"
    output.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return output


def validate_prompt_file(prompt_path: str) -> List[ContractViolation]:
    return validate_input_contract(Path(prompt_path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    import sys

    targets = sys.argv[1:] or ["origin_prompt_meta.md", "final_prompt_meta.md"]
    for target in targets:
        path = Path(target)
        if not path.exists():
            print(f"[SKIP] {target} 不存在")
            continue
        text = path.read_text(encoding="utf-8")
        problems = validate_input_contract(text)
        sections = parse_bold_sections(text)
        print(f"== {target}  chars={len(text)}  bold_sections={len(sections)}")
        for section in sections:
            dimension = dimension_of(section.name) or "-"
            print(f"   [{section.order}] {section.heading}   dim={dimension}")
        if problems:
            print("   [FAIL]")
            for problem in problems:
                print(f"      {problem}")
        else:
            print("   [OK] IN-1/IN-2/IN-3 全部满足")
