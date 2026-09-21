import json
import os
import tempfile
from datetime import datetime

from api_response import extract_response_text
from prompt_structure_contract import (
    AMBIGUOUS_RESTRICTION_KEYWORDS,
    DIMENSION_MARKERS,
    HARD_COMPARISON_MARKERS,
    MAX_ADDED_CHARS_PER_ITERATION,
    MAX_BOLD_SECTIONS,
    MAX_NEW_BOLD_SECTIONS_PER_ROUND,
    MAX_PROMPT_CHARS,
    OPERATIONAL_NAME_MARKERS,
    PromptContractError,
    SPECIAL_CASE_KEYWORD,
    UNAMBIGUOUS_RESTRICTION_KEYWORDS,
    bold_section_bodies,
    parse_bold_sections,
    section_diff,
    top_level_headings,
    validate_input_contract,
    validate_optimizer_edit,
)


# 候选被拒后允许重新请求的次数（大纲 §7.3）。
MAX_OPTIMIZER_ATTEMPTS = 3
# 每次迭代的结构决策记录（接受/拒绝、原因、结构统计）。
DECISION_LOG = "prompt_candidate_decisions.json"


class CandidateRejectedError(RuntimeError):
    """候选 prompt 未通过结构契约/权限/预算校验，且重试已耗尽。"""

    def __init__(self, attempts):
        self.attempts = list(attempts)
        detail = "；".join(
            f"第{item['attempt']}次: {'; '.join(item['reasons'])}"
            for item in self.attempts
        )
        super().__init__(
            f"候选 prompt 全部被拒绝（{len(self.attempts)} 次尝试）：{detail}"
        )


class NoOptimizationTargetError(RuntimeError):
    """该版本没有可供优化的 B severe/soft badcase（大纲 §10）。"""


class PromptOptimizer:
    def __init__(self):
        self.api_url = os.getenv("AES_API_URL", "https://api.pateway.ai/v1/messages")
        self.api_key = os.getenv("AES_API_KEY")
        self.model = os.getenv("AES_OPTIMIZER_MODEL", "claude-sonnet-5")
        
        self.CURRENT_PROMPT = "origin_prompt_meta.md"
        self.TARGET_PROMPT = "optimized_prompt1_meta.md"
        self.BADCASE_FILE = "aes_badcases0.json"
        self.ITERATION_LOG = "iteration_history.json"
        
    def load_badcase_data(self, file_path):
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def load_prompt(self, file_path):
        """Load prompt as-is from markdown file."""
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read().strip()

    def analyze_B_bias_distribution(self, badcase_data):
        statistics = badcase_data['statistics']['B_bias']
        severe_cases = badcase_data['B_bias']['severe']
        soft_cases = badcase_data['B_bias']['soft']

        all_B_cases = severe_cases + soft_cases

        # 空 B-badcase 是合法状态：必须显式报告，而不是抛除零异常（大纲 §10）。
        if not all_B_cases:
            return {
                'total_badcases': 0,
                'severe_count': int(statistics.get('severe_count', 0) or 0),
                'soft_count': int(statistics.get('soft_count', 0) or 0),
                'bias_distribution': {'strict': 0, 'lenient': 0},
                'avg_bias_score': 0.0,
                'dimension_analysis': {
                    'content': 0.0, 'expression': 0.0, 'structure': 0.0
                },
                'priority_dimension': None,
                'has_targets': False,
            }

        strict_count = sum(1 for case in all_B_cases if case['direction'] == 'strict')
        lenient_count = sum(1 for case in all_B_cases if case['direction'] == 'lenient')

        avg_bias_score = sum(case['bias_score'] for case in all_B_cases) / len(all_B_cases)

        dimension_diffs = {'content': [], 'expression': [], 'structure': []}

        for case in all_B_cases:
            dimension_diffs['content'].append(case['diffs']['content'])
            dimension_diffs['expression'].append(case['diffs']['expression'])
            dimension_diffs['structure'].append(case['diffs']['structure'])

        avg_dimension_diffs = {
            dim: sum(vals) / len(vals) for dim, vals in dimension_diffs.items()
        }

        priority_dimension = min(avg_dimension_diffs, key=avg_dimension_diffs.get)

        return {
            'total_badcases': len(all_B_cases),
            'severe_count': statistics['severe_count'],
            'soft_count': statistics['soft_count'],
            'bias_distribution': {
                'strict': strict_count,
                'lenient': lenient_count
            },
            'avg_bias_score': round(avg_bias_score, 2),
            'dimension_analysis': avg_dimension_diffs,
            'priority_dimension': priority_dimension,
            'has_targets': True,
        }

    def select_representative_samples(self, badcase_data, count=3):
        all_cases = badcase_data['B_bias']['severe'] + badcase_data['B_bias']['soft']
        sorted_cases = sorted(all_cases, key=lambda x: abs(x['bias_score']), reverse=True)

        selected = []
        for case in sorted_cases[:count]:
            selected.append({
                'name': case['name'],
                'teacher': {k: v for k, v in case['teacher'].items()
                            if k in ['content', 'expression', 'structure']},
                'AI': {k: v for k, v in case['AI'].items()
                       if k in ['content', 'expression', 'structure']},
                'diffs': {k: v for k, v in case['diffs'].items()
                          if k in ['content', 'expression', 'structure']},
                'bias_score': case['bias_score'],
                'direction': case['direction']
            })

        return selected

    def contract_block(self) -> str:
        """从校验器本身渲染 R5 词表，使提示词与判定器无法互相漂移（§13.3）。

        此前这些规则只以英文散文形式写在 system prompt 里，模型仍产出了
        `**评分前锚定校准**` 这类命中词表但语义为操作分点的标题；把**实际生效的
        匹配词**直接渲染出来，可以显著减少无效尝试（实测单轮浪费 3 次调用）。
        """
        dimensions = " / ".join(
            marker
            for dimension in ("content", "expression", "structure")
            for marker in DIMENSION_MARKERS[dimension]
        )
        return (
            "### Machine-checked word lists (these ARE the matchers)\n\n"
            "A NEW bold point is REJECTED when its TITLE contains any of:\n"
            f"- always forbidden: {'、'.join(UNAMBIGUOUS_RESTRICTION_KEYWORDS)}\n"
            f"- any scored-dimension word: {dimensions}\n"
            f"- forbidden UNLESS the title also carries an operation marker: "
            f"{'、'.join(AMBIGUOUS_RESTRICTION_KEYWORDS)}\n\n"
            "Operation markers that neutralise the ambiguous words above, so that a\n"
            "title such as `评分前锚定校准` is ACCEPTED:\n"
            f"- {'、'.join(OPERATIONAL_NAME_MARKERS)}\n\n"
            "A NEW bold point is ALSO REJECTED when its BODY contains a bullet where a\n"
            "dimension word is IMMEDIATELY followed by a hard comparison\n"
            "(e.g. `内容分不得低于 6`). Merely mentioning dimension names elsewhere\n"
            "in the bullet is fine:\n"
            f"- hard comparisons: {'、'.join(HARD_COMPARISON_MARKERS)}\n"
            "- adding an operation marker to the title does NOT license the body.\n"
            f"- at most {MAX_NEW_BOLD_SECTIONS_PER_ROUND} new bold points per iteration.\n\n"
            f"Dimension restrictions belong as bullet items UNDER the matching "
            f"`{SPECIAL_CASE_KEYWORD}` bold point.\n"
        )

    def budget_block(self) -> str:
        """Render the §13.4 budget so the prompt text cannot drift from the validator."""
        return (
            "### Budget\n\n"
            f"- At most {MAX_BOLD_SECTIONS} bold points in total.\n"
            f"- At most {MAX_PROMPT_CHARS} characters in total.\n"
            f"- At most {MAX_ADDED_CHARS_PER_ITERATION} characters added by this iteration.\n"
            "- When the bold-point budget is reached, you may only edit bullet items.\n"
        )

    def build_api_request(self, origin_prompt, analysis, samples, previous_reasons=None):
        system_prompt = """You are a scoring prompt optimization expert for essay evaluation.

## Your Task
Analyze the bias pattern in the current scoring system and modify the prompt to reduce it.
You have FULL autonomy over what to change — do NOT be guided by any specific direction.

## Structural Contract (MANDATORY — MACHINE-CHECKED)

Your output is validated against the rules below before it is scored. A candidate
that violates any rule is discarded, so a rule-breaking edit wastes the iteration.

### Vocabulary

- Required section: `## 注意事项` — the only section that is deployed and scored.
- **Bold point** (`**评分原则**`) — a rule carrier. Its TITLE is frozen.
- Bullet item (`- ...`) — the content beneath a bold point.
- `###` third-level headers are FORBIDDEN.

### Frozen (never change)

- The `## 注意事项` heading, and the set, titles and order of the existing bold points.
  Do not rename, delete, merge, split or reorder them.
- Renaming a bold point breaks the downstream E-route anchor registry, which
  addresses rules by title.

### Required input structure (must be preserved)

`## 注意事项` must keep exactly one "特殊情形" bold point for EACH scored dimension:
内容 / 表达 / 结构. Do not add a second one for a dimension and do not remove one.

### Allowed operations, in priority order

1. HIGHEST PRIORITY — edit the bullet items under an existing bold point, especially
   under `**内容分特殊情形**` / `**表达分特殊情形**` / `**结构分特殊情形**`. Rewrite,
   add, tighten or drop individual `- ...` items. You may also rewrite the descriptive
   prose beneath a bold point; only its TITLE is frozen.
2. LOW PRIORITY — add a NEW bold point, and only if it describes an OPERATION:
   a scoring procedure step, or a pre-output self-check. At most 2 new bold points
   per iteration. If item 1 could carry the idea, use item 1 instead.

Examples of ALLOWED new bold points:
- a scoring step, e.g. "先判断是否存在重大缺陷，再确定档次起点"
- a pre-output self-check, e.g. "输出前核对是否已引用至少两处原文证据"

Examples of FORBIDDEN new bold points:
- any dimension-scoring restriction or numeric anchor, e.g. "**语言分强制锚点**",
  "**结构分强制锚点**", "**评分锚点校准**", "**扣分依据具体化**". Anchors, cutoffs,
  floors, ceilings, percentage bands and deduction caps must be written as bullet
  items UNDER the matching "特殊情形" bold point, never as a bold point of their own.
- any bold point whose title contains 内容 / 表达 / 语言 / 结构.
- an operation-flavoured title that hides the restriction inside its own body.

__CONTRACT_BLOCK__

__BUDGET_BLOCK__

### NOT ALLOWED

- Rename, delete or reorder any existing bold point; change `## 注意事项`.
- Add, remove or rename top-level `##` sections.
- Use `###` third-level headers.

## OUTPUT FORMAT - MANDATORY (READ CAREFULLY)

OUTPUT ONLY THE FOLLOWING STRUCTURE. DO NOT ADD ANY EXPLANATION, COMMENT, OR ADDITIONAL TEXT.

===MODIFIED_PROMPT_START===
[PUT THE COMPLETE MODIFIED PROMPT HERE - INCLUDING ALL MARKDOWN HEADERS AND CONTENT]
===MODIFIED_PROMPT_END===

===METADATA_START===
{
    "changed_sections": ["list of section names that were modified"],
    "added_sections": ["list of new section names, empty array if none"],
    "modification_reasons": "explanation of why these changes reduce the observed bias",
    "expected_impact": "description of which metrics should improve and how",
    "confidence_score": 0.0
}
===METADATA_END===

## CRITICAL RULES:
1. DO NOT use markdown code fences (```) anywhere
2. DO NOT add any text before or after the blocks above
3. The prompt between ===MODIFIED_PROMPT_START=== and ===MODIFIED_PROMPT_END=== must be the complete prompt text
4. The metadata JSON must be valid and properly formatted
5. If you cannot complete the thought, output what you have
"""

        user_content = (
            "## Current Prompt\n\n"
            f"{origin_prompt}\n\n"
            "## Bias Analysis\n\n"
            f"- Total badcases: {analysis['total_badcases']}\n"
            f"- Severe: {analysis['severe_count']} / Soft: {analysis['soft_count']}\n"
            f"- Strict bias: {analysis['bias_distribution']['strict']}\n"
            f"- Lenient bias: {analysis['bias_distribution']['lenient']}\n"
            f"- Average bias score: {analysis['avg_bias_score']}\n"
            f"- Priority dimension: {analysis['priority_dimension']}\n"
            f"- Dimension analysis:\n"
            f"{json.dumps(analysis['dimension_analysis'], ensure_ascii=False, indent=2)}\n\n"
            "## Representative Samples\n\n"
            f"{json.dumps(samples, ensure_ascii=False, indent=2)}\n\n"
            "## Instructions\n\n"
            "1. Identify what in the current prompt causes the observed bias pattern\n"
            "2. Prefer editing bullet items under the existing \"特殊情形\" bold points;\n"
            "   add a new bold point only for an operation (a scoring step or a self-check)\n"
            "3. Never rename, delete or reorder an existing bold point\n"
            "4. OUTPUT ONLY IN THE FORMAT SPECIFIED ABOVE\n"
            "5. DO NOT USE MARKDOWN CODE FENCES\n"
            "6. DO NOT ADD ANY EXPLANATION TEXT\n"
        )

        system_prompt = system_prompt.replace(
            "__CONTRACT_BLOCK__", self.contract_block()
        ).replace(
            "__BUDGET_BLOCK__", self.budget_block()
        )

        if previous_reasons:
            bullets = "\n".join(f"- {reason}" for reason in previous_reasons)
            user_content += (
                "\n\n## Your Previous Attempt Was Rejected\n\n"
                "The machine validator discarded your last candidate for:\n"
                f"{bullets}\n\n"
                "Fix exactly these violations. Do not rename the offending bold point\n"
                "and resubmit it — either make it a genuine operation step (a scoring\n"
                "procedure or a pre-output self-check, with no dimension restriction\n"
                "anywhere in its body) or move the content under the matching\n"
                "\"特殊情形\" bold point as a bullet item.\n"
            )

        return {
            "model": self.model,
            "max_tokens": 8192,
            "system": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": user_content.strip()
                }
            ]
        }

    def send_api_request(self, request_data, max_retries=3):
        if not self.api_key:
            raise RuntimeError("AES_API_KEY is not set. Copy .env.example and export the value before running prompt optimization.")
        import requests

        headers = {
            "content-type": "application/json",
            "x-api-key": self.api_key
        }

        for attempt in range(max_retries):
            print(f"Sending API request (attempt {attempt + 1}/{max_retries})...")
            
            try:
                response = requests.post(
                    self.api_url,
                    headers=headers,
                    json=request_data,
                    timeout=90
                )

                print("Status Code:", response.status_code)

                if response.status_code == 200:
                    return response.json()
                
                print(f"API failed with status: {response.status_code}")
                print("Error Response:", response.text[:500])
                
            except requests.exceptions.RequestException as e:
                print(f"Request failed: {e}")
            
            if attempt < max_retries - 1:
                import time
                wait_time = (attempt + 1) * 5
                print(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)

        raise Exception(f"API failed after {max_retries} attempts")

    def save_json(self, data, path):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def save_prompt(self, prompt_content, path):
        """Save prompt as markdown text (no conversion)."""
        with open(path, 'w', encoding='utf-8') as f:
            f.write(prompt_content)
    
    def save_iteration_log(self, iteration_num, analysis, modification_reasons,
                           expected_impact, changed_sections=None, added_sections=None,
                           confidence_score=None):
        log_entry = {
            "iteration": iteration_num,
            "timestamp": datetime.now().isoformat(),
            "current_prompt": self.CURRENT_PROMPT,
            "target_prompt": self.TARGET_PROMPT,
            "badcase_file": self.BADCASE_FILE,
            "analysis": analysis,
            "changed_sections": changed_sections or [],
            "added_sections": added_sections or [],
            "confidence_score": confidence_score,
            "modification_reasons": modification_reasons,
            "expected_impact": expected_impact
        }

        if os.path.exists(self.ITERATION_LOG):
            with open(self.ITERATION_LOG, 'r', encoding='utf-8') as f:
                history = json.load(f)
        else:
            history = []

        history.append(log_entry)

        with open(self.ITERATION_LOG, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

        print(f"[INFO] Iteration {iteration_num} logged")

    def get_next_iteration_number(self):
        if os.path.exists(self.ITERATION_LOG):
            with open(self.ITERATION_LOG, 'r', encoding='utf-8') as f:
                history = json.load(f)
            return len(history) + 1
        return 1
    
    def show_iteration_history(self):
        if not os.path.exists(self.ITERATION_LOG):
            print("[INFO] No iteration history found")
            return
        
        with open(self.ITERATION_LOG, 'r', encoding='utf-8') as f:
            history = json.load(f)
        
        print("\n" + "="*60)
        print("迭代历史记录")
        print("="*60)
        for entry in history:
            print(f"\n迭代 #{entry['iteration']} ({entry['timestamp'][:19]})")
            print(f"  当前Prompt: {entry['current_prompt']}")
            print(f"  目标Prompt: {entry['target_prompt']}")
            print(f"  Badcase文件: {entry.get('badcase_file', 'N/A')}")
            if entry.get('analysis'):
                print(f"  平均偏差: {entry['analysis']['avg_bias_score']}")
                print(f"  重点维度: {entry['analysis']['priority_dimension']}")

    def _strip_markdown_fences(self, text):
        text = text.strip()

        for fence in ['```json\n', '```json', '```\n', '```']:
            if text.startswith(fence):
                text = text[len(fence):]
                break

        if text.strip().endswith('```'):
            text = text.strip()[:-3]

        return text.strip()

    def _extract_prompt_block(self, text):
        start_marker = '===MODIFIED_PROMPT_START==='
        end_marker = '===MODIFIED_PROMPT_END==='
        
        start_idx = text.find(start_marker)
        if start_idx == -1:
            return None
        
        start_pos = start_idx + len(start_marker)
        
        end_idx = text.find(end_marker, start_pos)
        if end_idx == -1:
            return None
        
        return text[start_pos:end_idx].strip()

    def _extract_metadata(self, text):
        start_marker = '===METADATA_START==='
        end_marker = '===METADATA_END==='
        
        start_idx = text.find(start_marker)
        if start_idx == -1:
            return {}
        
        start_pos = start_idx + len(start_marker)
        
        end_idx = text.find(end_marker, start_pos)
        if end_idx == -1:
            return {}
        
        json_text = text[start_pos:end_idx].strip()
        
        if json_text:
            try:
                return json.loads(json_text)
            except json.JSONDecodeError:
                pass
        
        return {}

    def extract_optimized_prompt(self, result_path, output_path=None):
        if output_path is None:
            output_path = self.TARGET_PROMPT

        print(f"\n[Extract] Loading result from {result_path}...")

        try:
            with open(result_path, 'r', encoding='utf-8') as f:
                result = json.load(f)
        except Exception as e:
            print(f"[ERROR] Failed to load result file: {e}")
            return None

        text_content = extract_response_text(result)

        if not text_content:
            print("[ERROR] No text content found")
            return None

        text_content = self._strip_markdown_fences(text_content)

        prompt_block = self._extract_prompt_block(text_content)
        
        if prompt_block is None:
            print("[ERROR] Prompt block not found")
            return None

        self.save_prompt(prompt_block, output_path)
        print(f"[OK] Optimized prompt saved to {output_path}")

        metadata = self._extract_metadata(text_content)

        print("\n[Change Summary]")
        print("  Changed sections:", metadata.get('changed_sections', []))
        print("  Added sections:", metadata.get('added_sections', []))
        print("  Confidence:", metadata.get('confidence_score', 0))
        print("\n[Modification Reasons]")
        print(metadata.get('modification_reasons', 'N/A'))
        print("\n[Expected Impact]")
        print(metadata.get('expected_impact', 'N/A'))

        self.save_iteration_log(
            self.get_next_iteration_number(),
            None,
            metadata.get('modification_reasons', ''),
            metadata.get('expected_impact', '')
        )

        return prompt_block

    # ------------------------------------------------------------------
    # 候选校验与事务化写入（大纲 §6 / §7）
    # ------------------------------------------------------------------
    @staticmethod
    def structure_stats(text):
        return {
            "chars": len(text),
            "top_level_headings": top_level_headings(text),
            "bold_sections": [section.name for section in parse_bold_sections(text)],
        }

    def extract_candidate_from_response(self, response):
        """从 API 响应中取出候选 prompt 与 metadata（不落盘）。"""
        text = extract_response_text(response)
        if not text:
            return None, {}
        text = self._strip_markdown_fences(text)
        return self._extract_prompt_block(text), self._extract_metadata(text)

    @staticmethod
    def validate_candidate(before_text, candidate_text):
        """返回拒绝原因列表；空列表表示候选合格。"""
        if not candidate_text:
            return ["未能从响应中提取 ===MODIFIED_PROMPT_START/END=== 区块"]
        reasons = [
            str(item) for item in validate_optimizer_edit(before_text, candidate_text)
        ]
        reasons += [str(item) for item in validate_input_contract(candidate_text)]
        return reasons

    @staticmethod
    def describe_candidate(before_text, candidate_text):
        """记录被拒候选的结构信息，使"拒绝"这一决定事后可审计。

        此前只记录 reasons，无法回看被判非法的分点究竟长什么样，
        因此"控制词过宽造成的误杀"无法从决策日志中发现（V1 实测）。
        """
        if not candidate_text:
            return {}
        try:
            diff = section_diff(before_text, candidate_text)
            bodies = bold_section_bodies(candidate_text)
            return {
                "added_sections": diff["added"],
                "added_section_bodies": {
                    name: bodies.get(name, "")[:400] for name in diff["added"]
                },
                "changed_sections": diff["changed"],
            }
        except Exception as exc:  # 审计信息缺失不应影响主流程
            return {"describe_error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def atomic_save_prompt(text, path):
        """原子写入：先写同目录临时文件，再 os.replace 覆盖目标。"""
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(
            dir=directory, prefix=".candidate_", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(temp_path, path)
        except BaseException:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    def record_decision(self, payload):
        history = []
        if os.path.exists(DECISION_LOG):
            try:
                with open(DECISION_LOG, "r", encoding="utf-8") as handle:
                    history = json.load(handle)
            except json.JSONDecodeError:
                history = []
        history.append(payload)
        with open(DECISION_LOG, "w", encoding="utf-8") as handle:
            json.dump(history, handle, ensure_ascii=False, indent=2)

    def run(self):
        iteration_num = self.get_next_iteration_number()

        print("=" * 60)
        print(f"Prompt Optimizer - 迭代 #{iteration_num}")
        print("=" * 60)

        print("\n" + "=" * 40)
        print("当前配置")
        print("=" * 40)
        print(f"  当前Prompt: {self.CURRENT_PROMPT}")
        print(f"  目标Prompt: {self.TARGET_PROMPT}")
        print(f"  Badcase文件: {self.BADCASE_FILE}")
        print(f"  迭代历史: {self.ITERATION_LOG}")
        print("=" * 40)

        print("\nStep 1: load badcase")
        badcase_data = self.load_badcase_data(self.BADCASE_FILE)

        print("Step 2: load and validate current prompt")
        current_text = self.load_prompt(self.CURRENT_PROMPT)
        current_violations = validate_input_contract(current_text)
        if current_violations:
            raise PromptContractError(current_violations)
        current_stats = self.structure_stats(current_text)
        print(
            f"  [OK] 输入契约通过：{current_stats['chars']} 字符，"
            f"{len(current_stats['bold_sections'])} 个加粗分点"
        )

        print("Step 3: analyze bias")
        analysis = self.analyze_B_bias_distribution(badcase_data)

        print("\n" + "=" * 40)
        print("偏差分析摘要")
        print("=" * 40)
        print(f"  B类badcase总数: {analysis['total_badcases']}")
        print(f"  偏严/偏宽分布: {analysis['bias_distribution']['strict']}/{analysis['bias_distribution']['lenient']}")
        print(f"  平均偏差分数: {analysis['avg_bias_score']}")
        print(f"  重点改进维度: {analysis['priority_dimension']}")
        print("=" * 40)

        if not analysis["has_targets"]:
            message = (
                f"{self.BADCASE_FILE} 中没有 B severe/soft badcase，"
                "没有可供优化的目标。"
            )
            self.record_decision(
                {
                    "iteration": iteration_num,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "current_prompt": self.CURRENT_PROMPT,
                    "target_prompt": self.TARGET_PROMPT,
                    "status": "no_target",
                    "reason": message,
                }
            )
            raise NoOptimizationTargetError(message)

        print("\nStep 4: select samples")
        samples = self.select_representative_samples(badcase_data)

        attempts = []
        previous_reasons = None
        for attempt in range(1, MAX_OPTIMIZER_ATTEMPTS + 1):
            print(f"\nStep 5.{attempt}: build request and call API")
            if previous_reasons:
                print(f"  [RETRY] 回喂上一次拒绝原因（{len(previous_reasons)} 项）")
            request_data = self.build_api_request(
                current_text, analysis, samples, previous_reasons=previous_reasons
            )
            self.save_json(request_data, "api_request_debug.json")
            response = self.send_api_request(request_data)
            self.save_json(response, "optimized_result.json")

            try:
                candidate_text, metadata = self.extract_candidate_from_response(response)
            except Exception as exc:
                # 响应格式异常只消耗一次机会，不应中断整轮迭代。
                reasons = [
                    f"响应无法解析为候选 prompt：{type(exc).__name__}: {exc}"
                ]
                print(f"  [REJECT] {reasons[0]}")
                attempts.append({"attempt": attempt, "reasons": reasons})
                previous_reasons = reasons
                continue

            reasons = self.validate_candidate(current_text, candidate_text)
            if reasons:
                print(f"  [REJECT] 候选未通过校验（{len(reasons)} 项）：")
                for reason in reasons:
                    print(f"    - {reason}")
                entry = {"attempt": attempt, "reasons": reasons}
                entry.update(self.describe_candidate(current_text, candidate_text))
                attempts.append(entry)
                previous_reasons = reasons
                continue

            candidate_stats = self.structure_stats(candidate_text)
            diff = section_diff(current_text, candidate_text)
            self.atomic_save_prompt(candidate_text, self.TARGET_PROMPT)

            print(f"[OK] 候选通过全部校验并写入 {self.TARGET_PROMPT}")
            print("\n[Structure]")
            print(f"  字符数: {current_stats['chars']} -> {candidate_stats['chars']} "
                  f"({candidate_stats['chars'] - current_stats['chars']:+d})")
            print(f"  顶层章节数: {len(candidate_stats['top_level_headings'])}")
            print(f"  加粗分点数: {len(current_stats['bold_sections'])} -> "
                  f"{len(candidate_stats['bold_sections'])}")
            print(f"  新增分点: {diff['added']}")
            print(f"  正文被改的分点: {diff['changed']}")
            print("\n[Change Summary]")
            print("  Modification reasons:", metadata.get('modification_reasons', 'N/A'))
            print("  Expected impact:", metadata.get('expected_impact', 'N/A'))

            self.save_iteration_log(
                iteration_num,
                None,
                metadata.get('modification_reasons', ''),
                metadata.get('expected_impact', ''),
                changed_sections=diff['changed'],
                added_sections=diff['added'],
                confidence_score=metadata.get('confidence_score'),
            )
            self.record_decision(
                {
                    "iteration": iteration_num,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "current_prompt": self.CURRENT_PROMPT,
                    "target_prompt": self.TARGET_PROMPT,
                    "status": "accepted",
                    "attempt": attempt,
                    "current_stats": current_stats,
                    "candidate_stats": candidate_stats,
                    "changed_sections": diff['changed'],
                    "added_sections": diff['added'],
                    "removed_sections": diff['removed'],
                }
            )

            print("\n" + "=" * 60)
            print("迭代完成")
            print("=" * 60)
            print(f"  下次迭代建议: 将 {self.TARGET_PROMPT} 设为 CURRENT_PROMPT")
            print("=" * 60)
            return candidate_text

        self.record_decision(
            {
                "iteration": iteration_num,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "current_prompt": self.CURRENT_PROMPT,
                "target_prompt": self.TARGET_PROMPT,
                "status": "rejected",
                "attempts": attempts,
                "current_stats": current_stats,
            }
        )
        print(f"\n[ABORT] {MAX_OPTIMIZER_ATTEMPTS} 次候选均被拒绝，未写入 {self.TARGET_PROMPT}")
        raise CandidateRejectedError(attempts)


if __name__ == "__main__":
    import sys
    
    optimizer = PromptOptimizer()
    
    if len(sys.argv) > 1:
        if sys.argv[1] == "--extract":
            result_path = sys.argv[2] if len(sys.argv) > 2 else "optimized_result.json"
            optimizer.extract_optimized_prompt(result_path)
        elif sys.argv[1] == "--history":
            optimizer.show_iteration_history()
        elif sys.argv[1] == "--config":
            print("\n当前配置:")
            print(f"  CURRENT_PROMPT: {optimizer.CURRENT_PROMPT}")
            print(f"  TARGET_PROMPT: {optimizer.TARGET_PROMPT}")
            print(f"  BADCASE_FILE: {optimizer.BADCASE_FILE}")
            print(f"  ITERATION_LOG: {optimizer.ITERATION_LOG}")
        elif sys.argv[1] == "--set-prompt":
            if len(sys.argv) > 3:
                if sys.argv[2] == "current":
                    optimizer.CURRENT_PROMPT = sys.argv[3]
                    print(f"CURRENT_PROMPT 已设置为: {optimizer.CURRENT_PROMPT}")
                elif sys.argv[2] == "target":
                    optimizer.TARGET_PROMPT = sys.argv[3]
                    print(f"TARGET_PROMPT 已设置为: {optimizer.TARGET_PROMPT}")
                elif sys.argv[2] == "badcase":
                    optimizer.BADCASE_FILE = sys.argv[3]
                    print(f"BADCASE_FILE 已设置为: {optimizer.BADCASE_FILE}")
        else:
            print("用法:")
            print("  python prompt_optimizer.py                              - 运行完整优化流程")
            print("  python prompt_optimizer.py --extract [result.json]      - 提取优化结果")
            print("  python prompt_optimizer.py --history                    - 查看迭代历史")
            print("  python prompt_optimizer.py --config                     - 查看当前配置")
            print("  python prompt_optimizer.py --set-prompt current <file>  - 设置当前prompt")
            print("  python prompt_optimizer.py --set-prompt target <file>   - 设置目标prompt")
            print("  python prompt_optimizer.py --set-prompt badcase <file>  - 设置badcase文件")
    else:
        optimizer.run()
