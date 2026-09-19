import json
import os
import time

from api_response import extract_response_text


class BatchEssayScorer:
    def __init__(self):
        self.api_url = os.getenv("AES_API_URL", "https://api.pateway.ai/v1/messages")
        self.api_key = os.getenv("AES_API_KEY")
        self.model = os.getenv("AES_SCORING_MODEL", "claude-sonnet-5")

        self.prompt_path = "origin_prompt.md"
        self.essays_path = "train_essays.json"
        self.output_path = "train_scoring_results0.json"
        self.origin_data_path = "origin_scoring_results.json"

        self.batch_size = 1
        
        # Score validation configuration
        self.score_min = 0
        self.score_max = 9

    def load_prompt(self):
        """Load prompt as-is from markdown file."""
        with open(self.prompt_path, 'r', encoding='utf-8') as f:
            return f.read().strip()

    def validate_prompt(self, prompt):
        """Validate prompt structure has all required sections."""
        required_sections = [

            '## 注意事项',


        ]
        
        missing_sections = [section for section in required_sections if section not in prompt]
        
        if missing_sections:
            raise ValueError(f"Prompt validation failed. Missing sections: {', '.join(missing_sections)}")
        
        return True

    def load_essays(self):
        with open(self.essays_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def load_origin_data(self):
        with open(self.origin_data_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _save_debug_prompt(self, prompt):
        """Save final prompt for debugging."""
        with open("debug_final_prompt.txt", 'w', encoding='utf-8') as f:
            f.write("=" * 80 + "\n")
            f.write("FINAL PROMPT SENT TO API\n")
            f.write("=" * 80 + "\n")
            f.write(prompt)
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"Prompt length: {len(prompt)} characters\n")
            f.write(f"Prompt lines: {prompt.count('\\n') + 1}\n")

    def build_batch_request(self, essays_batch, scoring_prompt):
        essays_content = ""
        
        for i, essay_item in enumerate(essays_batch, 1):
            essay_text = essay_item.get('essay', '')
            essays_content += (
                f"【作文{i}】\n"
                f"{essay_text}\n\n"
            )

        final_prompt = (
            f"{scoring_prompt}\n\n"
            "## 批量评分任务\n\n"
            "请对以下作文进行评分：\n\n"
            f"{essays_content}"
            "## 输出要求\n\n"
            "DO NOT OUTPUT JSON. DO NOT USE MARKDOWN CODE FENCES. DO NOT ADD EXPLANATION.\n\n"
            "OUTPUT ONLY IN THIS FORMAT:\n\n"
            "===BATCH_RESULT_START===\n"
            "===ESSAY_START===\n"
            "ID:1\n"
            "CONTENT:5\n"
            "EXPRESSION:6\n"
            "STRUCTURE:4\n"
            "COMMENT_START\n"
            "评语内容（不超过150字）\n"
            "COMMENT_END\n"
            "===ESSAY_END===\n"
            "===ESSAY_START===\n"
            "ID:2\n"
            "CONTENT:6\n"
            "EXPRESSION:5\n"
            "STRUCTURE:5\n"
            "COMMENT_START\n"
            "第二篇评语\n"
            "COMMENT_END\n"
            "===ESSAY_END===\n"
            "===BATCH_RESULT_END===\n\n"
            "IMPORTANT RULES:\n"
            "1. Each essay must have ID, CONTENT, EXPRESSION, STRUCTURE, and COMMENT\n"
            f"2. CONTENT/EXPRESSION/STRUCTURE must be integers between {self.score_min}-{self.score_max}\n"
            "3. COMMENT must be between COMMENT_START and COMMENT_END\n"
            "4. COMMENT must not exceed 150 characters\n"
            "5. NO text before ===BATCH_RESULT_START===\n"
            "6. NO text after ===BATCH_RESULT_END===\n"
            "7. DO NOT USE markdown code fences (```)\n"
            "8. DO NOT ADD any explanation or extra text\n"
        )

        self._save_debug_prompt(final_prompt)

        return {
            "model": self.model,
            "max_tokens": 8192,
            "messages": [
                {
                    "role": "user",
                    "content": final_prompt
                }
            ]
        }

    def send_api_request(self, request_data, max_retries=5, retry_delay=5):
        if not self.api_key:
            raise RuntimeError("AES_API_KEY is not set. Copy .env.example and export the value before running API scoring.")
        import requests

        headers = {
            "content-type": "application/json",
            "x-api-key": self.api_key
        }

        for attempt in range(max_retries):
            try:
                print(f"  Sending API request... (attempt {attempt + 1}/{max_retries})")

                response = requests.post(
                    self.api_url,
                    headers=headers,
                    json=request_data,
                    timeout=120
                )

                print("  Status Code:", response.status_code)

                if response.status_code != 200:
                    print("  Error Response:", response.text[:500])
                    if attempt < max_retries - 1:
                        print(f"  Retrying in {retry_delay} seconds...")
                        time.sleep(retry_delay)
                        continue
                    return None

                return response.json()

            except Exception as e:
                print(f"  Request failed (attempt {attempt + 1}): {str(e)}")
                if attempt < max_retries - 1:
                    print(f"  Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                    continue
                return None

        return None

    def extract_response_text(self, response):
        return extract_response_text(response)

    def _save_raw_dump(self, content, filename="raw_response_dump.txt"):
        """Save raw response for debugging when parsing fails."""
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write("=" * 60 + "\n")
                f.write("RAW API RESPONSE DUMP\n")
                f.write("=" * 60 + "\n")
                f.write(content)
            print(f"  [DEBUG] Raw dump saved to: {filename}")
        except Exception as e:
            print(f"  [DEBUG] Failed to save raw dump: {e}")

    def _is_truncated(self, text):
        """Check if the output is truncated (missing closing marker)."""
        return '===BATCH_RESULT_END===' not in text

    def _parse_single_essay(self, essay_block):
        """Parse a single essay block between ===ESSAY_START=== and ===ESSAY_END==="""
        result = {
            "id": 0,
            "content": 0,
            "expression": 0,
            "structure": 0,
            "comment": ""
        }

        comment_start = essay_block.find('COMMENT_START')
        comment_end = essay_block.find('COMMENT_END', comment_start)
        
        if comment_start != -1 and comment_end != -1:
            result['comment'] = essay_block[comment_start + len('COMMENT_START'):comment_end].strip()
        else:
            lines = essay_block.split('\n')
            comment_lines = []
            in_comment = False
            for line in lines:
                if line.strip() == 'COMMENT_START':
                    in_comment = True
                    continue
                if line.strip() == 'COMMENT_END':
                    break
                if in_comment:
                    comment_lines.append(line)
            result['comment'] = '\n'.join(comment_lines).strip()

        lines = essay_block.split('\n')
        for line in lines:
            line = line.strip()
            if not line or line.startswith('COMMENT_'):
                continue
            
            if line.startswith('ID:'):
                result['id'] = self._safe_parse_int(line[3:].strip())
            elif line.startswith('CONTENT:'):
                result['content'] = self._safe_parse_int(line[8:].strip())
            elif line.startswith('EXPRESSION:'):
                result['expression'] = self._safe_parse_int(line[11:].strip())
            elif line.startswith('STRUCTURE:'):
                result['structure'] = self._safe_parse_int(line[10:].strip())

        return result

    def _safe_parse_int(self, value):
        """Safely parse integer with fallback."""
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0

    def _validate_score(self, score):
        """Validate score is within configured range."""
        return isinstance(score, int) and self.score_min <= score <= self.score_max

    def parse_delimiter_results(self, response_text, expected_count=0):
        """Parse results using delimiter protocol."""
        if not response_text:
            print("  Error: Empty response text")
            return None

        if self._is_truncated(response_text):
            print("  Error: LLM output truncated (missing ===BATCH_RESULT_END===)")
            self._save_raw_dump(response_text)
            return None

        start_marker = '===BATCH_RESULT_START==='
        end_marker = '===BATCH_RESULT_END==='
        
        start_idx = response_text.find(start_marker)
        end_idx = response_text.find(end_marker)
        
        if start_idx == -1:
            print("  Error: Missing ===BATCH_RESULT_START===")
            self._save_raw_dump(response_text)
            return None
        
        content = response_text[start_idx + len(start_marker):end_idx]

        essay_blocks = content.split('===ESSAY_START===')
        
        results = []
        for block in essay_blocks:
            block = block.strip()
            if not block or '===ESSAY_END===' not in block:
                continue
            
            end_pos = block.find('===ESSAY_END===')
            essay_content = block[:end_pos].strip()
            
            essay_result = self._parse_single_essay(essay_content)
            
            # Validate scores
            if not self._validate_score(essay_result['content']):
                print(f"  Warning: Invalid content score {essay_result['content']} for essay {essay_result['id']}")
                return None
            if not self._validate_score(essay_result['expression']):
                print(f"  Warning: Invalid expression score {essay_result['expression']} for essay {essay_result['id']}")
                return None
            if not self._validate_score(essay_result['structure']):
                print(f"  Warning: Invalid structure score {essay_result['structure']} for essay {essay_result['id']}")
                return None
            
            if essay_result['id'] > 0 or essay_result['content'] > 0:
                results.append(essay_result)

        if expected_count > 0 and len(results) != expected_count:
            print(f"  Error: Essay count mismatch. Expected: {expected_count}, Got: {len(results)}")
            self._save_raw_dump(response_text)
            return None

        if not results:
            print("  Error: No valid essay results found")
            self._save_raw_dump(response_text)
            return None

        final_results = []
        for r in results:
            final_results.append({
                "content": r["content"],
                "expression": r["expression"],
                "structure": r["structure"],
                "comment": r["comment"]
            })

        return final_results

    def _build_origin_index_lookup(self, origin_data):
        """Build an index lookup for matching sampled essays back to full origin data."""
        index_lookup = {}

        for item in origin_data:
            if "index" not in item:
                raise ValueError("Origin data item is missing index.")

            origin_index = item["index"]
            if origin_index in index_lookup:
                raise ValueError(f"Duplicate index in origin data: {origin_index}")

            index_lookup[origin_index] = item

        return index_lookup

    def _match_origin_item(self, essay_item, index_lookup):
        """Match the scored essay item to its original metadata and teacher scores by index."""
        if "index" not in essay_item:
            raise ValueError("Essay item is missing index; cannot match origin data.")

        essay_index = essay_item["index"]
        if essay_index not in index_lookup:
            raise ValueError(f"Essay index not found in origin data: {essay_index}")

        origin_item = index_lookup[essay_index]

        # Lightweight integrity check. Index is authoritative, but a mismatch means files are out of sync.
        if essay_item.get("essay", "") != origin_item.get("essay", ""):
            raise ValueError(f"Essay text mismatch for index: {essay_index}")

        return origin_item

    def save_results(self, all_results, essays, origin_data):
        output = []
        index_lookup = self._build_origin_index_lookup(origin_data)

        for idx, result in enumerate(all_results):
            if idx >= len(essays):
                break

            origin_item = self._match_origin_item(essays[idx], index_lookup)

            output.append({
                "index": origin_item["index"],
                "name": origin_item["name"],
                "page": origin_item["page"],
                "essay": origin_item["essay"],
                "teacher": origin_item["teacher"],
                "AI": {
                    "content": result.get("content", 0),
                    "expression": result.get("expression", 0),
                    "structure": result.get("structure", 0),
                }
            })

        with open(self.output_path, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        print(f"\n[保存成功] {self.output_path}")

    def run(self):
        print("=" * 60)
        print("批量作文评分工具")
        print("=" * 60)

        print("\n[步骤1] 加载评分Prompt...")
        try:
            scoring_prompt = self.load_prompt()
            self.validate_prompt(scoring_prompt)
            print(f"  Prompt加载完成 (长度: {len(scoring_prompt)} 字符)")
        except Exception as e:
            print(f"  Prompt加载失败: {e}")
            return

        print("\n[步骤2] 加载作文...")
        essays = self.load_essays()
        print(f"  作文数量: {len(essays)}")

        print("\n[步骤3] 加载原始数据...")
        origin_data = self.load_origin_data()
        print(f"  原始数据数量: {len(origin_data)}")

        print("\n[步骤4] 开始批量评分...")

        all_results = []
        total_batches = (len(essays) + self.batch_size - 1) // self.batch_size

        for batch_idx in range(total_batches):
            start_idx = batch_idx * self.batch_size
            end_idx = min(start_idx + self.batch_size, len(essays))
            essays_batch = essays[start_idx:end_idx]
            expected_count = len(essays_batch)

            print(f"\n[批次 {batch_idx + 1}/{total_batches}] 作文 {start_idx + 1}-{end_idx}")

            max_retries = 3
            batch_results = None
            
            for retry_attempt in range(max_retries):
                request_data = self.build_batch_request(essays_batch, scoring_prompt)
                response = self.send_api_request(request_data)

                if response:
                    response_text = self.extract_response_text(response)

                    if response_text:
                        batch_results = self.parse_delimiter_results(response_text, expected_count)
                        
                        if batch_results:
                            break
                        else:
                            print(f"  Parse failed, retrying... ({retry_attempt + 1}/{max_retries})")
                    else:
                        print(f"  Empty response text, retrying... ({retry_attempt + 1}/{max_retries})")
                else:
                    print(f"  API request failed, retrying... ({retry_attempt + 1}/{max_retries})")

                if retry_attempt < max_retries - 1:
                    time.sleep(5)

            if batch_results:
                for local_idx, result in enumerate(batch_results):
                    global_idx = start_idx + local_idx
                    all_results.append(result)
                    print(f"  作文{global_idx + 1}: 内容={result['content']} 语言={result['expression']} 结构={result['structure']}")
            else:
                raise RuntimeError(
                    f"Scoring failed for batch {batch_idx + 1}/{total_batches}; "
                    "no output file was written, so infrastructure failures cannot enter badcase statistics as zero scores."
                )

            if batch_idx < total_batches - 1:
                time.sleep(3)

        print("\n[步骤5] 保存结果...")
        self.save_results(all_results, essays, origin_data)

        success_count = len(all_results)
        fail_count = 0

        print("\n" + "=" * 60)
        print("评分完成")
        print("=" * 60)
        print(f"总数: {len(all_results)}")
        print(f"成功: {success_count}")
        print(f"失败: {fail_count}")
        print(f"结果文件: {self.output_path}")


if __name__ == "__main__":
    scorer = BatchEssayScorer()
    scorer.run()
