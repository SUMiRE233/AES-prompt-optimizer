import json
import os
from datetime import datetime

from api_response import extract_response_text


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
            'priority_dimension': priority_dimension
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

    def build_api_request(self, origin_prompt, analysis, samples):
        system_prompt = """You are a scoring prompt optimization expert for essay evaluation.

## Your Task
Analyze the bias pattern in the current scoring system and modify the prompt to reduce it.
You have FULL autonomy over what to change — do NOT be guided by any specific direction.

## Structural Constraints (MANDATORY)

The prompt you output must strictly follow these rules:

REQUIRED sections (must exist, must not be renamed or deleted):
- ## 评分标准
- ## 注意事项
- ## 待评作文
- ## 输出格式

ALLOWED operations:
- Modify wording of existing items in ## 注意事项
- Add new rules or edit existing rules under ## 注意事项 using format: **[rule name]**：[content]
- Add new top-level sections ## [name] or edit existing sections BETWEEN ## 注意事项 and ## 待评作文 ONLY IF NECESSARY
- Add sub-items or edit existing sub-items under existing sections using **bold title** format

NOT ALLOWED:
- Delete or rename required sections
- Add scoring logic inside ## 待评作文 or ## 输出格式
- Use third-level headers ###

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
            "2. Modify ## 注意事项 or add new sections to address the root cause\n"
            "3. Do not modify ## 待评作文 or ## 输出格式\n"
            "4. OUTPUT ONLY IN THE FORMAT SPECIFIED ABOVE\n"
            "5. DO NOT USE MARKDOWN CODE FENCES\n"
            "6. DO NOT ADD ANY EXPLANATION TEXT\n"
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

    def run(self):
        iteration_num = self.get_next_iteration_number()
        
        print("=" * 60)
        print(f"Prompt Optimizer - 迭代 #{iteration_num}")
        print("=" * 60)
        
        print("\n" + "="*40)
        print("当前配置")
        print("="*40)
        print(f"  当前Prompt: {self.CURRENT_PROMPT}")
        print(f"  目标Prompt: {self.TARGET_PROMPT}")
        print(f"  Badcase文件: {self.BADCASE_FILE}")
        print(f"  迭代历史: {self.ITERATION_LOG}")
        print("="*40)

        print("\nStep 1: load badcase")
        badcase_data = self.load_badcase_data(self.BADCASE_FILE)

        print("Step 2: load prompt")
        origin_prompt = self.load_prompt(self.CURRENT_PROMPT)

        print("Step 3: analyze bias")
        analysis = self.analyze_B_bias_distribution(badcase_data)
        
        print("\n" + "="*40)
        print("偏差分析摘要")
        print("="*40)
        print(f"  B类badcase总数: {analysis['total_badcases']}")
        print(f"  偏严/偏宽分布: {analysis['bias_distribution']['strict']}/{analysis['bias_distribution']['lenient']}")
        print(f"  平均偏差分数: {analysis['avg_bias_score']}")
        print(f"  重点改进维度: {analysis['priority_dimension']}")
        print("="*40)

        print("\nStep 4: select samples")
        samples = self.select_representative_samples(badcase_data)

        print("Step 5: build request")
        request_data = self.build_api_request(origin_prompt, analysis, samples)

        self.save_json(request_data, "api_request_debug.json")

        print("Step 6: call API")
        response = self.send_api_request(request_data)

        print("Step 7: save result")
        self.save_json(response, "optimized_result.json")

        print("Step 8: extract optimized prompt")
        self.extract_optimized_prompt(
            "optimized_result.json",
            output_path=self.TARGET_PROMPT
        )

        print("\n" + "="*60)
        print("迭代完成")
        print("="*60)
        print(f"  下次迭代建议: 将 {self.TARGET_PROMPT} 设为 CURRENT_PROMPT")
        print("="*60)


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
