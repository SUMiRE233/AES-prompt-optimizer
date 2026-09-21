import json
import os
import re
import random
from datetime import datetime

from api_response import extract_response_text


class ContrastiveETypeAnalyzer:
    """
    Contrastive Residual Preference Extraction System
    
    核心功能：
    - 对比式分析 E类残差异常样本 vs 正常对齐样本
    - 提取教师隐式评分偏好（validated）
    - 识别可泛化的局部scoring rules
    - 过滤 hallucinations 和个例现象
    
    与B类优化完全分离：
    - B类 = 全局偏差优化
    - E类 = 局部残差对齐优化
    """
    
    def __init__(self):
        self.api_url = os.getenv("AES_API_URL", "https://api.pateway.ai/v1/messages")
        self.api_key = os.getenv("AES_API_KEY")
        self.model = os.getenv("AES_E_ANALYSIS_MODEL", "claude-sonnet-5")
        
        self.MAIN_DIMS = ['content', 'expression', 'structure']
        self.FROZEN_DIMS = []  # 冻结的维度，不参与迭代分析
        self.MAX_OUTLIER_SAMPLES = 5
        self.MAX_NORMAL_SAMPLES = 3
        self.NORMAL_ZSCORE_THRESHOLD = 0.8
        self.NORMAL_DIFF_THRESHOLD = 1.0
        
        self.BADCASE_FILE = "aes_badcases3.json"
        self.SCORING_PROMPT_PATH = "final_prompt_meta.md"
        self.ALL_DATA_FILE = "train_scoring_results3.json"
        self.OUTPUT_DIR = "etype_analysis"
        self.ITERATION_LOG = "etype_iteration_history.json"
        self.CONTRASTIVE_LOG = "etype_contrastive_log.json"
        self.FROZEN_LOG = "etype_frozen_dims.json"  # 记录冻结状态
        self.RANDOM_SEED = 42
        self._legacy_position_to_index = None
        
        if not os.path.exists(self.OUTPUT_DIR):
            os.makedirs(self.OUTPUT_DIR)
        
        self._load_frozen_dims()
    
    def _load_frozen_dims(self):
        """加载已冻结的维度"""
        if os.path.exists(self.FROZEN_LOG):
            try:
                with open(self.FROZEN_LOG, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.FROZEN_DIMS = data.get('frozen_dims', [])
            except Exception as e:
                print(f"  [WARN] Failed to load frozen dims: {e}")
    
    def _save_frozen_dims(self):
        """保存冻结的维度"""
        data = {
            "frozen_dims": self.FROZEN_DIMS,
            "updated_at": datetime.now().isoformat()
        }
        with open(self.FROZEN_LOG, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def freeze_dimension(self, dimension):
        """冻结指定维度"""
        if dimension in self.MAIN_DIMS and dimension not in self.FROZEN_DIMS:
            self.FROZEN_DIMS.append(dimension)
            self._save_frozen_dims()
            print(f"  [OK] Frozen dimension: {dimension}")
    
    def unfreeze_dimension(self, dimension):
        """解冻指定维度"""
        if dimension in self.FROZEN_DIMS:
            self.FROZEN_DIMS.remove(dimension)
            self._save_frozen_dims()
            print(f"  [OK] Unfrozen dimension: {dimension}")
    
    def get_active_dimensions(self):
        """获取活跃的维度（未冻结的）"""
        return [dim for dim in self.MAIN_DIMS if dim not in self.FROZEN_DIMS]
    
    def load_badcase_data(self, file_path):
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def load_all_scoring_data(self, file_path):
        """加载全量评分数据"""
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        seen = set()
        for position, item in enumerate(data):
            if 'index' not in item:
                raise ValueError(f"Scoring row {position} is missing global index")
            index = int(item['index'])
            if index in seen:
                raise ValueError(f"Scoring data contains duplicate global index {index}")
            seen.add(index)
        
        return data

    def resolve_case_index(self, case):
        """Resolve global identity, reading legacy positional badcases if needed."""
        if 'index' in case:
            return int(case['index'])
        if 'data_index' not in case:
            raise ValueError("E badcase is missing both index and legacy data_index")
        if self._legacy_position_to_index is None:
            rows = self.load_all_scoring_data(self.ALL_DATA_FILE)
            self._legacy_position_to_index = {
                position: int(item['index']) for position, item in enumerate(rows)
            }
        position = int(case['data_index'])
        if position not in self._legacy_position_to_index:
            raise IndexError(f"Legacy E badcase data_index out of range: {position}")
        return self._legacy_position_to_index[position]
    
    def compute_statistics_for_normal_selection(self, all_data, dimension):
        """
        计算全量数据的统计量，用于normal sample选择
        基于badcase miner的统计方法
        """
        diffs = []
        for item in all_data:
            diff = item['AI'][dimension] - item['teacher'][dimension]
            diffs.append(diff)
        
        if not diffs:
            return None
        
        sorted_diffs = sorted(diffs)
        n = len(sorted_diffs)
        
        if n % 2 == 1:
            median_diff = sorted_diffs[n // 2]
        else:
            median_diff = (sorted_diffs[n // 2 - 1] + sorted_diffs[n // 2]) / 2
        
        residuals = [d - median_diff for d in diffs]
        abs_residuals = [abs(r) for r in residuals]
        
        sorted_abs_residuals = sorted(abs_residuals)
        if len(sorted_abs_residuals) % 2 == 1:
            mad = sorted_abs_residuals[len(sorted_abs_residuals) // 2]
        else:
            mad = (sorted_abs_residuals[len(sorted_abs_residuals) // 2 - 1] + 
                   sorted_abs_residuals[len(sorted_abs_residuals) // 2]) / 2
        
        mad = max(mad, 1e-6)
        scale_factor = 1.4826 * mad + 1e-6
        
        z_scores = [r / scale_factor for r in residuals]
        
        for idx, item in enumerate(all_data):
            item['z_scores'] = item.get('z_scores', {})
            item['z_scores'][dimension] = z_scores[idx]
            item['residual'] = residuals[idx]
        
        return {
            'median_diff': median_diff,
            'MAD': mad,
            'scale_factor': scale_factor
        }
    
    def load_prompt(self, file_path):
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    
    def save_json(self, data, path):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def _extract_case_info(self, case, dimension):
        """提取样本信息"""
        z_score = 0
        if case.get('z_scores') and dimension in case['z_scores']:
            z_score = case['z_scores'][dimension]
        elif 'z_score' in case:
            z_score = case['z_score']
        
        return {
            'essay': case.get('essay', ''),
            'teacher': {k: v for k, v in case.get('teacher', {}).items() 
                       if k in ['content', 'expression', 'structure']},
            'AI': {k: v for k, v in case.get('AI', {}).items() 
                  if k in ['content', 'expression', 'structure']},
            'diff': case.get('diffs', {}).get(dimension, 0),
            'residual': case.get('residual', 0),
            'z_score': z_score,
            'direction': case.get('direction', 'neutral'),
            'index': self.resolve_case_index(case)
        }
    
    def _get_essay_type(self, essay):
        """
        通用题目类型提取器
        提取《》内的标题作为类型标识
        无标题则取前10字作为fallback
        """
        match = re.search(r'《(.+?)》', essay[:50])
        if match:
            return match.group(1)
        # fallback：无标题格式时取前10字
        return essay[:10].strip()
    
    def select_outlier_samples(self, badcase_data, dimension):
        """
        选择异常样本（outlier samples）
        优先 severe，soft 补足，最多 MAX_OUTLIER_SAMPLES 个
        """
        severe_cases = badcase_data['E_residual'][dimension]['severe']
        soft_cases = badcase_data['E_residual'][dimension]['soft']
        
        selected = []
        
        for case in severe_cases:
            selected.append(self._extract_case_info(case, dimension))
        
        if len(selected) < self.MAX_OUTLIER_SAMPLES:
            random.seed(self.RANDOM_SEED)
            needed = self.MAX_OUTLIER_SAMPLES - len(selected)
            soft_sample_size = min(needed, len(soft_cases))
            
            if soft_sample_size > 0:
                sampled_soft = random.sample(soft_cases, soft_sample_size)
                for case in sampled_soft:
                    selected.append(self._extract_case_info(case, dimension))
        
        return selected[:self.MAX_OUTLIER_SAMPLES]
    
    def select_normal_samples(self, badcase_data, dimension, outlier_samples):
        """
        选择正常对齐样本（normal aligned samples）
        
        标准：
        - abs(z_score) < NORMAL_ZSCORE_THRESHOLD
        - abs(raw_diff) <= NORMAL_DIFF_THRESHOLD
        - 不属于任何E类badcase
        - 尽量与outlier样本同分段、长度接近
        - 优先选择同题目类型的样本
        
        从 ALL_DATA_FILE 加载全量数据，而不是从 badcase_data
        """
        try:
            all_items = self.load_all_scoring_data(self.ALL_DATA_FILE)
            print(f"  Loaded {len(all_items)} items from {self.ALL_DATA_FILE} for normal sample selection")
        except Exception as e:
            print(f"  [WARN] Failed to load all_data: {e}")
            return []
        
        if not all_items:
            print(f"  [WARN] No items in all_data file")
            return []
        
        self.compute_statistics_for_normal_selection(all_items, dimension)
        
        outlier_indices = set(s.get('index', -1) for s in outlier_samples)
        
        # 提取outlier的主要题目类型
        outlier_types = [self._get_essay_type(s['essay']) for s in outlier_samples]
        majority_type = max(set(outlier_types), key=outlier_types.count) if outlier_types else None
        
        if outlier_samples:
            outlier_avg_score = sum(s['teacher'][dimension] for s in outlier_samples) / len(outlier_samples)
            outlier_avg_length = sum(len(s['essay']) for s in outlier_samples) / len(outlier_samples)
        else:
            outlier_avg_score = 5
            outlier_avg_length = 500
        
        candidates = []
        
        for item in all_items:
            index = int(item['index'])
            if index in outlier_indices:
                continue
            
            z_score = item.get('z_scores', {}).get(dimension, 0)
            
            raw_diff = item['AI'][dimension] - item['teacher'][dimension]
            
            if abs(z_score) < self.NORMAL_ZSCORE_THRESHOLD and abs(raw_diff) <= self.NORMAL_DIFF_THRESHOLD:
                teacher_score = item['teacher'][dimension]
                essay_length = len(item.get('essay', ''))
                
                score_dist = abs(teacher_score - outlier_avg_score)
                length_dist = abs(essay_length - outlier_avg_length) / max(outlier_avg_length, 1)
                
                # 检查是否同类型
                item_type = self._get_essay_type(item.get('essay', ''))
                is_same_type = (item_type == majority_type)
                
                candidates.append({
                    'item': item,
                    'z_score': z_score,
                    'raw_diff': raw_diff,
                    'score_dist': score_dist,
                    'length_dist': length_dist,
                    'match_score': score_dist + length_dist + (0 if is_same_type else 2.0)
                })
        
        candidates.sort(key=lambda x: x['match_score'])
        
        selected = []
        for candidate in candidates[:self.MAX_NORMAL_SAMPLES]:
            selected.append(self._extract_case_info(candidate['item'], dimension))
        
        return selected
    
    def build_contrastive_analysis_request(self, scoring_prompt, dimension, outlier_samples, normal_samples):
        """
        构建对比式分析API请求
        
        核心改变：
        - 同时输入 outlier 和 normal 样本
        - 要求对比分析，而非简单总结
        """
        system_prompt = f"""You are a scoring preference analyst for essay evaluation.

## NON-NEGOTIABLE RULE-WRITING BAN — APPLY BEFORE ANY ANALYSIS

Every item in `localized_prompt_rules` MUST obey all of the following in its
`trigger_condition`, `scoring_adjustment`, `counter_examples`,
`anti_overfit_boundary`, and `forbidden_generalization` prose:

- NEVER copy, derive, or invent a percentage, numeric score anchor, score range,
  cutoff, floor, ceiling, fixed increment, or mandatory adjustment.
- NEVER write `%`, a numeric interval such as `70-85`, or a score expression
  such as `5 points` / `5分`, even if the current scoring prompt contains one.
- Describe only qualitative distinctions grounded in observable essay features.
- If the idea cannot be expressed safely without a numeric anchor, keep it as
  an observation and set `should_be_injected_at` to `do_not_inject`.

Before returning JSON, silently preflight every localized rule. If any banned
numeric anchor remains in those prose fields, rewrite it qualitatively or mark
the rule `do_not_inject`. A rule that violates this ban is unusable regardless
of confidence or evidence count.

## CRITICAL CONSTRAINTS

1. **Dimension Focus**: Analyze ONLY the {dimension} dimension. DO NOT discuss content/expression/structure other than {dimension}.
2. **No Global Changes**: DO NOT adjust global scoring scale, rubric, or overall bias calibration.
3. **Contrastive Analysis Required**: Analyze BOTH outliers AND normal samples together.
4. **Evidence Gating**: ONLY extract patterns appearing across MULTIPLE samples.
5. **Confidence Requirement**: Assign confidence scores. LOW confidence = do NOT include in final rules.
6. **Anti-hallucination**: DO NOT infer preferences from a single essay.
7. **Soft Rules Only**: DO NOT output hard constraints (e.g., percentages, fixed thresholds, mandatory requirements). Use soft guidance like "consider", "tend to", "may indicate", "generally".
8. **No Hard Thresholds**: NEVER use hard threshold keywords. The forbidden keyword list includes: "不得低于", "必须上调", "起评点", "固定", "凡是", "只要", "一律", "%". Never transplant numeric anchors from the current prompt into a localized rule. Use qualitative guidance instead. If the idea cannot be expressed without a forbidden threshold, set should_be_injected_at to "do_not_inject".
9. **Injection Location Restrictions**: 
   - MUST be an exact bold subheading from the prompt's "注意事项" section, e.g., **内容分特殊情形**, **表达分特殊情形**, **结构分特殊情形**
   - content dimension: only inject under content-related headings
   - expression dimension: only inject under expression-related headings  
   - structure dimension: only inject under structure-related headings
   - Use "do_not_inject" if the rule should not be injected
10. **Do Not Inject Conditions**: Set should_be_injected_at to "do_not_inject" if:
    - evidence_count < 2
    - no normal contrast evidence
    - rule depends on hard thresholds
    - rule may affect global bias calibration
    - rule is too case-specific
    - cannot provide anti_overfit_boundary
    - cannot provide counter_evidence_spans
    - cannot find a matching bold subheading in the prompt's "注意事项" section

## ANALYSIS TASK

Your goal is to identify WHY:
- AI succeeds on normal samples (aligned with teacher)
- AI fails on outlier samples (deviates from teacher)

Then extract:
- What textual features teachers ACTUALLY reward in this dimension
- What features AI consistently fails to recognize
- What localized rules can safely improve alignment WITHOUT causing B-class regression

## REQUIRED OUTPUT FORMAT

Output ONLY valid JSON with these exact fields:

{{
    "validated_preferences": [
        {{
            "preference": "Text feature teachers reward",
            "evidence": {{
                "outlier_indices": [list of integers],
                "outlier_pattern": "Pattern within 20 chars",
                "normal_indices": [list of integers],
                "normal_pattern": "Pattern within 20 chars or null"
            }},
            "confidence": 0.0-1.0,
            "generalizable": true/false,
            "evidence_count": integer
        }}
    ],
    "ignored_features": [
        {{
            "feature": "Text feature AI ignores",
            "evidence": {{
                "outlier_indices": [list of integers],
                "outlier_pattern": "Pattern within 20 chars",
                "normal_indices": [list of integers],
                "normal_pattern": "Pattern within 20 chars or null"
            }},
            "confidence": 0.0-1.0,
            "evidence_count": integer
        }}
    ],
    "over_penalized_patterns": [
        {{
            "pattern": "Feature AI over-penalizes",
            "reason": "Reason within 20 chars",
            "evidence": {{
                "outlier_indices": [list of integers],
                "outlier_pattern": "Pattern within 20 chars"
            }},
            "confidence": 0.0-1.0,
            "evidence_count": integer
        }}
    ],
    "localized_prompt_rules": [
        {{
            "trigger_condition": "Condition to trigger rule",
            "scoring_adjustment": "Scoring adjustment to apply",
            "counter_examples": "Cases where condition should NOT trigger (e.g., 'short essays', 'narrative style')",
            "evidence": {{
                "outlier_indices": [list of integers],
                "normal_indices": [list of integers]
            }},
            "safe_for_global_bias": true/false,
            "confidence": 0.0-1.0,
            "evidence_count": integer,
            "should_be_injected_at": "Exact bold subheading from the prompt's '注意事项' section, e.g., **内容分特殊情形** or **表达分特殊情形** or **结构分特殊情形**. Use 'do_not_inject' if the rule should not be injected.",
            "injection_reason": "Why this injection location is suggested based on prompt analysis",
            "injection_strength": "soft | medium",
            "expected_scope": ["content | expression | structure"],
            "anti_overfit_boundary": "Boundary conditions where this rule does NOT apply",
            "forbidden_generalization": ["What this rule should NOT be generalized into"],
            "positive_evidence_spans": [
                {{
                    "index": integer,
                    "span": "Exact text from essay (30-120 chars)",
                    "why_supports_rule": "How this span supports the rule"
                }}
            ],
            "counter_evidence_spans": [
                {{
                    "index": integer,
                    "span": "Exact text from essay (30-120 chars)",
                    "why_limits_rule": "How this span limits rule extrapolation"
                }}
            ]
        }}
    ],
    "non_generalizable_observations": [
        "Single-case observations that cannot form rules"
    ],
    "global_bias_risk_analysis": [
        {{
            "risk": "Risk description",
            "affected_dimension": "{dimension}",
            "severity": "low/medium/high"
        }}
    ]
}}

## STRICT FORMAT RULES
- evidence.outlier_indices: list of global index integers ONLY, no text
- evidence.normal_indices: list of global index integers ONLY, [] if none
- evidence_count: integer, must equal len(outlier_indices)
- pattern fields: max 20 characters, no full sentences
- If evidence_count < 2, set confidence <= 0.5 automatically

## EVIDENCE GATING RULES

- If a pattern appears in ONLY 1 sample → mark confidence < 0.5
- If no contrast with normal samples exists → mark generalizable = false
- If risk of B-class regression exists → add to global_bias_risk_analysis
- If not actionable/insertable into prompt → move to non_generalizable_observations

Output ONLY JSON. No explanations outside JSON structure."""

        dimension_context = {
            'content': {
                'description': 'Content dimension - focuses on ideas, argumentation depth, material usage',
                'focus': 'Thesis quality, argumentation logic, material relevance'
            },
            'expression': {
                'description': 'Expression dimension - focuses on language expression, rhetoric, literary merit',
                'focus': 'Word choice, sentence variation, rhetorical techniques'
            },
            'structure': {
                'description': 'Structure dimension - focuses on article organization, paragraph layout',
                'focus': 'Opening/closing, paragraph coherence, logical sequence'
            }
        }
        
        context = dimension_context[dimension]
        
        user_content = f"""# Analysis Target
- Dimension: {dimension}
- Description: {context['description']}
- Focus: {context['focus']}

# Current Scoring Prompt (Relevant Section)
{scoring_prompt[:3000]}

# Outlier Samples (AI deviates from teacher)
These are RESIDUAL anomalies. AI scoring significantly diverges from teacher scoring.
Priority: severe cases first.

{json.dumps(outlier_samples, ensure_ascii=False, indent=2)}

# Normal Samples (AI aligns with teacher)
These are WELL-ALIGNED samples. AI scoring closely matches teacher scoring.
Use these as CONTRAST/CONTROL group to validate patterns.

{json.dumps(normal_samples, ensure_ascii=False, indent=2)}

# Analysis Instructions

1. Compare outlier samples with normal samples
2. Identify WHAT differs between them that explains AI's deviation
3. Validate each inferred preference against normal samples
4. Only include rules with confidence >= 0.6 in localized_prompt_rules
5. Rules with confidence < 0.6 go to non_generalizable_observations

Output ONLY JSON."""

        return {
            "model": self.model,
            "max_tokens": int(os.getenv("AES_E_MAX_TOKENS", "8192")),
            "system": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": user_content
                }
            ]
        }
    
    def send_api_request(self, request_data, max_retries=3, retry_delay=10):
        """发送API请求，带重试机制"""
        if not self.api_key:
            raise RuntimeError("AES_API_KEY is not set. Copy .env.example and export the value before running E-type analysis.")
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
                    timeout=150
                )
                
                print(f"  Status Code: {response.status_code}")
                
                if response.status_code != 200:
                    print(f"  Error Response: {response.text}")
                    if attempt < max_retries - 1:
                        import time
                        print(f"  Retrying in {retry_delay} seconds...")
                        time.sleep(retry_delay)
                        continue
                    return None
                
                return response.json()
                
            except Exception as e:
                print(f"  Request failed: {str(e)}")
                if attempt < max_retries - 1:
                    import time
                    print(f"  Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                    continue
                return None
        
        return None
    
    def parse_contrastive_response(self, response):
        """解析对比式分析响应"""
        try:
            text_content = extract_response_text(response)
            if not text_content:
                return None

            if text_content.startswith('```json'):
                text_content = text_content[7:]
            if text_content.startswith('```'):
                text_content = text_content[3:]
            if text_content.endswith('```'):
                text_content = text_content[:-3]

            result = json.loads(text_content.strip())
            return result
        except json.JSONDecodeError as e:
            print(f"  JSON parse error: {e}")
            return None
        except Exception as e:
            print(f"  Response parse error: {e}")
            return None
    
    def analyze_dimension(self, badcase_data, scoring_prompt, dimension):
        """
        对比式分析单个维度
        
        Args:
            badcase_data: badcase数据
            scoring_prompt: 评分prompt
            dimension: 目标维度
        
        Returns:
            分析结果 + 对比日志
        """
        print(f"\n{'='*60}")
        print(f"CONTRASTIVE ANALYSIS: {dimension.upper()}")
        print(f"{'='*60}")
        
        print(f"\n[Step 1] Select OUTLIER samples for {dimension}...")
        explicit_pool = (getattr(self, "EXPLICIT_SAMPLES", None) or {}).get(dimension)
        if explicit_pool is not None:
            outlier_samples = list(explicit_pool.get("outliers", []))
            print(f"  [EXPLICIT] frozen E sample pool: {len(outlier_samples)} outliers")
        else:
            outlier_samples = self.select_outlier_samples(badcase_data, dimension)
        outlier_indices = [s.get('index', -1) for s in outlier_samples]
        print(f"  Selected {len(outlier_samples)} outlier samples")
        print(f"  Outlier indices: {outlier_indices}")
        
        if len(outlier_samples) < 2:
            print(f"  [WARN] Insufficient outlier samples for {dimension}, need at least 2")
            return None, None
        
        severe_count = sum(1 for s in outlier_samples if abs(s.get('z_score', 0)) > 2.5)
        print(f"  Severe: {severe_count}, Soft: {len(outlier_samples) - severe_count}")
        
        print(f"\n[Step 2] Select MATCHED NORMAL samples for {dimension}...")
        if explicit_pool is not None:
            normal_samples = list(explicit_pool.get("normals", []))
            print(f"  [EXPLICIT] frozen normal controls: {len(normal_samples)}")
        else:
            normal_samples = self.select_normal_samples(badcase_data, dimension, outlier_samples)
        normal_indices = [s.get('index', -1) for s in normal_samples]
        print(f"  Selected {len(normal_samples)} normal samples")
        print(f"  Normal indices: {normal_indices}")
        
        if len(normal_samples) == 0:
            print(f"  [WARN] No normal samples found, analysis may have lower confidence")
        
        print(f"\n[Step 3] Build contrastive analysis request...")
        request_data = self.build_contrastive_analysis_request(
            scoring_prompt, dimension, outlier_samples, normal_samples
        )
        
        debug_path = os.path.join(self.OUTPUT_DIR, f"{dimension}_contrastive_request.json")
        self.save_json(request_data, debug_path)
        print(f"  Request saved to {debug_path}")
        
        print(f"\n[Step 4] Send contrastive API request...")
        response = self.send_api_request(request_data)

        if response and response.get("stop_reason") == "max_tokens":
            retry_budget = min(int(request_data["max_tokens"]) * 2, 16384)
            if retry_budget > int(request_data["max_tokens"]):
                print(
                    "  [WARN] Response was truncated at max_tokens; "
                    f"retrying once with max_tokens={retry_budget}"
                )
                request_data = {**request_data, "max_tokens": retry_budget}
                response = self.send_api_request(request_data)
        
        if not response:
            print(f"  [ERROR] API request failed for {dimension}")
            return None, None
        
        result_path = os.path.join(self.OUTPUT_DIR, f"{dimension}_contrastive_response.json")
        self.save_json(response, result_path)
        print(f"  Response saved to {result_path}")
        
        print(f"\n[Step 5] Parse contrastive analysis result...")
        analysis_result = self.parse_contrastive_response(response)
        
        contrastive_log = {
            'dimension': dimension,
            'outlier_sample_count': len(outlier_samples),
            'outlier_indices': outlier_indices,
            'normal_sample_count': len(normal_samples),
            'normal_indices': normal_indices,
            'severe_count': severe_count,
            'soft_count': len(outlier_samples) - severe_count,
            'normal_zscore_threshold': self.NORMAL_ZSCORE_THRESHOLD,
            'normal_diff_threshold': self.NORMAL_DIFF_THRESHOLD
        }
        
        if analysis_result:
            print(f"  [OK] Successfully parsed {dimension} analysis")
            
            validated_count = len(analysis_result.get('validated_preferences', []))
            ignored_count = len(analysis_result.get('ignored_features', []))
            over_penalized_count = len(analysis_result.get('over_penalized_patterns', []))
            rules_count = len(analysis_result.get('localized_prompt_rules', []))
            non_gen_count = len(analysis_result.get('non_generalizable_observations', []))
            
            avg_confidence = 0
            if 'localized_prompt_rules' in analysis_result and analysis_result['localized_prompt_rules']:
                confidences = [r.get('confidence', 0) for r in analysis_result['localized_prompt_rules']]
                avg_confidence = sum(confidences) / len(confidences)
            
            contrastive_log['analysis_stats'] = {
                'validated_preferences': validated_count,
                'ignored_features': ignored_count,
                'over_penalized_patterns': over_penalized_count,
                'localized_prompt_rules': rules_count,
                'non_generalizable_observations': non_gen_count,
                'avg_rule_confidence': round(avg_confidence, 2)
            }
            
            print(f"  - validated_preferences: {validated_count}")
            print(f"  - ignored_features: {ignored_count}")
            print(f"  - over_penalized_patterns: {over_penalized_count}")
            print(f"  - localized_prompt_rules: {rules_count} (avg confidence: {avg_confidence:.2f})")
            print(f"  - non_generalizable_observations: {non_gen_count}")
        else:
            print(f"  [ERROR] Failed to parse {dimension} analysis")
            contrastive_log['analysis_stats'] = None
        
        return analysis_result, contrastive_log
    
    def get_sample_statistics(self, badcase_data, dimension):
        """获取样本统计信息"""
        severe = badcase_data['E_residual'][dimension]['severe']
        soft = badcase_data['E_residual'][dimension]['soft']
        
        normal_candidates = 0
        try:
            all_items = self.load_all_scoring_data(self.ALL_DATA_FILE)
            self.compute_statistics_for_normal_selection(all_items, dimension)
            for item in all_items:
                z_score = item.get('z_scores', {}).get(dimension, 0)
                raw_diff = item['AI'][dimension] - item['teacher'][dimension]
                if abs(z_score) < self.NORMAL_ZSCORE_THRESHOLD and abs(raw_diff) <= self.NORMAL_DIFF_THRESHOLD:
                    normal_candidates += 1
        except Exception as e:
            print(f"  [WARN] Failed to count normal candidates: {e}")
        
        return {
            'dimension': dimension,
            'severe_count': len(severe),
            'soft_count': len(soft),
            'total_outliers': len(severe) + len(soft),
            'available_normal_candidates': normal_candidates,
            'filtered_count': badcase_data['statistics']['E_residual'][dimension]['filtered_count'],
            'remaining_count': badcase_data['statistics']['E_residual'][dimension]['remaining_count']
        }
    
    def save_contrastive_log(self, iteration_num, all_logs):
        """保存对比分析日志"""
        log_data = {
            "iteration": iteration_num,
            "timestamp": datetime.now().isoformat(),
            "contrastive_analysis": all_logs
        }
        
        if os.path.exists(self.CONTRASTIVE_LOG):
            with open(self.CONTRASTIVE_LOG, 'r', encoding='utf-8') as f:
                history = json.load(f)
        else:
            history = []
        
        history.append(log_data)
        
        with open(self.CONTRASTIVE_LOG, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        
        print(f"\n[INFO] Contrastive log saved to {self.CONTRASTIVE_LOG}")
    
    def save_iteration_log(self, iteration_num, all_results, stats_summary):
        """保存迭代记录"""
        results_for_json = {}
        for dimension, (result, _) in all_results.items():
            results_for_json[dimension] = result
        
        log_entry = {
            "iteration": iteration_num,
            "timestamp": datetime.now().isoformat(),
            "badcase_file": self.BADCASE_FILE,
            "scoring_prompt": self.SCORING_PROMPT_PATH,
            "dimensions_analyzed": list(all_results.keys()),
            "stats_summary": stats_summary,
            "results": results_for_json
        }
        
        if os.path.exists(self.ITERATION_LOG):
            with open(self.ITERATION_LOG, 'r', encoding='utf-8') as f:
                history = json.load(f)
        else:
            history = []
        
        history.append(log_entry)
        
        with open(self.ITERATION_LOG, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        
        print(f"\n[INFO] Iteration {iteration_num} logged to {self.ITERATION_LOG}")
    
    def get_next_iteration_number(self):
        """获取下一次迭代编号"""
        if os.path.exists(self.ITERATION_LOG):
            with open(self.ITERATION_LOG, 'r', encoding='utf-8') as f:
                history = json.load(f)
            return len(history) + 1
        return 1
    
    def show_iteration_history(self):
        """显示迭代历史摘要"""
        if not os.path.exists(self.ITERATION_LOG):
            print("[INFO] No iteration history found")
            return
        
        with open(self.ITERATION_LOG, 'r', encoding='utf-8') as f:
            history = json.load(f)
        
        print("\n" + "="*60)
        print("Contrastive E-Type Analysis - Iteration History")
        print("="*60)
        for entry in history:
            print(f"\n迭代 #{entry['iteration']} ({entry['timestamp'][:19]})")
            print(f"  Badcase文件: {entry.get('badcase_file', 'N/A')}")
            print(f"  分析维度: {', '.join(entry.get('dimensions_analyzed', []))}")
            if entry.get('stats_summary'):
                for dim, stats in entry['stats_summary'].items():
                    print(f"  - {dim}: outliers={stats['total_outliers']}, normal_candidates={stats.get('available_normal_candidates', 'N/A')}")
    
    HARD_THRESHOLD_KEYWORDS = [
        "不得低于", "必须上调", "起评点", "固定", "%",
        "凡是", "只要", "一律"
    ]
    
    def is_hard_threshold_rule(self, rule: dict) -> bool:
        """检测规则是否包含硬阈值关键词"""
        fields_to_check = [
            rule.get('trigger_condition', ''),
            rule.get('scoring_adjustment', ''),
            rule.get('counter_examples', ''),
            rule.get('anti_overfit_boundary', ''),
            rule.get('forbidden_generalization', '')
        ]
        
        for field in fields_to_check:
            if isinstance(field, str):
                for keyword in self.HARD_THRESHOLD_KEYWORDS:
                    if keyword in field:
                        return True
                if re.search(r"\d+(?:\.\d+)?\s*(?:分|points?)", field, re.IGNORECASE):
                    return True
                if re.search(r"\d+\s*[-–—~至到]\s*\d+", field):
                    return True
            elif isinstance(field, list):
                for item in field:
                    if isinstance(item, str):
                        for keyword in self.HARD_THRESHOLD_KEYWORDS:
                            if keyword in item:
                                return True
                        if re.search(r"\d+(?:\.\d+)?\s*(?:分|points?)", item, re.IGNORECASE):
                            return True
                        if re.search(r"\d+\s*[-–—~至到]\s*\d+", item):
                            return True
        return False
    
    def is_valid_injection_target(self, rule: dict, dimension: str) -> bool:
        """验证注入位置是否为有效的加粗子标题格式"""
        should_be_injected_at = rule.get('should_be_injected_at', '')
        
        # do_not_inject 是合法的
        if should_be_injected_at == 'do_not_inject':
            return True
        
        # 检查是否为有效的加粗子标题格式（格式应为 **xxx**）
        import re
        if not re.match(r'\*\*[^*]+\*\*', should_be_injected_at):
            return False
        
        # 验证维度匹配
        dimension_keywords = {
            'content': ['内容'],
            'expression': ['表达', '语言'],
            'structure': ['结构']
        }
        
        keywords = dimension_keywords.get(dimension, [])
        if keywords:
            return any(keyword in should_be_injected_at for keyword in keywords)
        
        return True
    
    def compile_all_rules(self, all_results):
        """汇总所有维度的规则，仅保留原始规则和可行规则"""
        compiled = {}
        
        for dimension, (result, _) in all_results.items():
            if not result:
                continue
            
            # 获取原始规则
            raw_rules = result.get('localized_prompt_rules', [])
            
            # 过滤可行规则
            feasible_rules = []
            for rule in raw_rules:
                # 读取字段（兼容旧数据）
                is_safe = rule.get('safe_for_global_bias', False)
                confidence = rule.get('confidence', 0)
                evidence_count = rule.get('evidence_count', 0)
                has_normal = (
                    rule.get('evidence', {}).get('normal_indices') and 
                    len(rule['evidence']['normal_indices']) > 0
                )
                should_be_injected_at = rule.get('should_be_injected_at', '')
                injection_strength = rule.get('injection_strength', 'soft')
                expected_scope = rule.get('expected_scope', [dimension])
                anti_overfit_boundary = rule.get('anti_overfit_boundary', '')
                forbidden_generalization = rule.get('forbidden_generalization', [])
                positive_evidence_spans = rule.get('positive_evidence_spans', [])
                counter_evidence_spans = rule.get('counter_evidence_spans', [])
                
                # 硬条件检查
                has_hard_threshold = self.is_hard_threshold_rule(rule)
                is_valid_target = self.is_valid_injection_target(rule, dimension)
                is_do_not_inject = should_be_injected_at == 'do_not_inject'
                
                # 必须满足所有硬条件
                if not (is_safe and confidence >= 0.6 and evidence_count >= 2 and 
                        has_normal and not is_do_not_inject and is_valid_target and not has_hard_threshold):
                    continue
                
                # 渐进要求评估
                has_boundary = bool(anti_overfit_boundary)
                has_counter_spans = len(counter_evidence_spans) > 0
                has_injection_location = bool(should_be_injected_at) and should_be_injected_at != 'do_not_inject'
                
                # 确定可信级别
                if has_boundary and has_counter_spans and has_injection_location:
                    rule['confidence_level'] = 'trusted'
                else:
                    rule['confidence_level'] = 'candidate'
                rule['evidence_index_type'] = 'global_index'
                
                feasible_rules.append(rule)
            
            compiled[dimension] = {
                'raw_rules': raw_rules,           # 原始规则（未过滤）
                'feasible_rules': feasible_rules  # 可行规则（安全且有效）
            }
        
        return compiled
    
    def run(self, dimension=None):
        """
        运行对比式E类偏好分析
        
        Args:
            dimension: 可选，指定分析单个维度；默认分析所有维度
        """
        iteration_num = self.get_next_iteration_number()
        
        print("=" * 60)
        print(f"Contrastive Residual Preference Extraction System")
        print(f"Iteration #{iteration_num}")
        print("=" * 60)
        
        print("\n" + "="*40)
        print("Configuration")
        print("="*40)
        print(f"  Badcase File: {self.BADCASE_FILE}")
        print(f"  All Data File: {self.ALL_DATA_FILE}")
        print(f"  Scoring Prompt: {self.SCORING_PROMPT_PATH}")
        print(f"  Output Directory: {self.OUTPUT_DIR}")
        print(f"  Iteration Log: {self.ITERATION_LOG}")
        print(f"  Max Outlier Samples: {self.MAX_OUTLIER_SAMPLES}")
        print(f"  Max Normal Samples: {self.MAX_NORMAL_SAMPLES}")
        print(f"  Normal Z-score Threshold: {self.NORMAL_ZSCORE_THRESHOLD}")
        print(f"  Normal Diff Threshold: {self.NORMAL_DIFF_THRESHOLD}")
        print(f"  Frozen Dimensions: {', '.join(self.FROZEN_DIMS) if self.FROZEN_DIMS else 'None'}")
        print(f"  Active Dimensions: {', '.join(self.get_active_dimensions())}")
        print("="*40)
        
        print("\n[Step 1] Load badcase data...")
        try:
            badcase_data = self.load_badcase_data(self.BADCASE_FILE)
            print(f"  [OK] Loaded badcase data")
        except Exception as e:
            print(f"  [ERROR] Failed to load badcase data: {e}")
            return
        
        print("\n[Step 2] Load scoring prompt...")
        try:
            scoring_prompt = self.load_prompt(self.SCORING_PROMPT_PATH)
            print(f"  [OK] Loaded scoring prompt")
        except Exception as e:
            print(f"  [ERROR] Failed to load scoring prompt: {e}")
            return
        
        print("\n[Step 3] Get sample statistics...")
        stats_summary = {}
        for dim in self.MAIN_DIMS:
            stats_summary[dim] = self.get_sample_statistics(badcase_data, dim)
            s = stats_summary[dim]
            print(f"  {dim}: outliers={s['total_outliers']} (severe={s['severe_count']}, soft={s['soft_count']}), "
                  f"normal_candidates={s['available_normal_candidates']}")
        
        print("\n[Step 4] Contrastive analysis for dimensions...")
        all_results = {}
        all_contrastive_logs = {}
        
        # 如果指定了维度，则只分析该维度（不考虑冻结）
        # 否则分析所有活跃维度（排除冻结的）
        if dimension:
            dims_to_analyze = [dimension]
        else:
            dims_to_analyze = self.get_active_dimensions()
            print(f"  Active dimensions to analyze: {', '.join(dims_to_analyze)}")
            
            if self.FROZEN_DIMS:
                print(f"  Frozen dimensions (skipped): {', '.join(self.FROZEN_DIMS)}")
        
        if not dims_to_analyze:
            print("  [WARN] No active dimensions to analyze")
            return
        
        for dim in dims_to_analyze:
            if dim not in self.MAIN_DIMS:
                print(f"\n[WARN] Unknown dimension: {dim}")
                continue
            
            # 如果不是手动指定维度，检查是否被冻结
            if not dimension and dim in self.FROZEN_DIMS:
                print(f"\n[SKIP] Dimension '{dim}' is frozen, skipping...")
                continue
            
            result, contrastive_log = self.analyze_dimension(badcase_data, scoring_prompt, dim)
            all_results[dim] = (result, contrastive_log)
            if contrastive_log:
                all_contrastive_logs[dim] = contrastive_log
            
            if dim in dims_to_analyze[:-1]:
                import time
                print("\n  Waiting 5 seconds before next dimension...")
                time.sleep(5)
        
        print("\n[Step 5] Save consolidated results...")
        consolidated_path = os.path.join(self.OUTPUT_DIR, f"contrastive_consolidated_{iteration_num}.json")
        
        results_for_json = {k: v[0] for k, v in all_results.items()}
        
        consolidated = {
            "iteration": iteration_num,
            "timestamp": datetime.now().isoformat(),
            "stats_summary": stats_summary,
            "analysis_results": results_for_json,
            "compiled_rules": self.compile_all_rules(all_results)
        }
        
        self.save_json(consolidated, consolidated_path)
        print(f"  [OK] Saved to {consolidated_path}")
        
        self.save_iteration_log(iteration_num, all_results, stats_summary)
        self.save_contrastive_log(iteration_num, all_contrastive_logs)
        
        print("\n" + "="*60)
        print("Contrastive Analysis Complete")
        print("="*60)
        
        print("\n[Localized Prompt Rules Summary]")
        for dim, rule_data in consolidated["compiled_rules"].items():
            raw_rules = rule_data.get('raw_rules', [])
            feasible_rules = rule_data.get('feasible_rules', [])
            
            print(f"\n{dim.upper()}:")
            print(f"  - Raw Rules: {len(raw_rules)}")
            print(f"  - Feasible Rules (safe & valid): {len(feasible_rules)}")
            
            if feasible_rules:
                print("    Feasible Rules:")
                for i, rule in enumerate(feasible_rules, 1):
                    trigger = rule.get('trigger_condition', 'N/A')[:50]
                    confidence = rule.get('confidence', 0)
                    print(f"      {i}. [C={confidence:.2f}] {trigger}...")
        
        print("\n" + "="*60)
        print(f"Results saved to: {self.OUTPUT_DIR}/")
        print("="*60)


if __name__ == "__main__":
    import sys
    
    analyzer = ContrastiveETypeAnalyzer()
    
    if len(sys.argv) > 1:
        if sys.argv[1] == "--history":
            analyzer.show_iteration_history()
        elif sys.argv[1] == "--config":
            print("\nCurrent Configuration:")
            print(f"  BADCASE_FILE: {analyzer.BADCASE_FILE}")
            print(f"  ALL_DATA_FILE: {analyzer.ALL_DATA_FILE}")
            print(f"  SCORING_PROMPT_PATH: {analyzer.SCORING_PROMPT_PATH}")
            print(f"  OUTPUT_DIR: {analyzer.OUTPUT_DIR}")
            print(f"  ITERATION_LOG: {analyzer.ITERATION_LOG}")
            print(f"  CONTRASTIVE_LOG: {analyzer.CONTRASTIVE_LOG}")
            print(f"  MAX_OUTLIER_SAMPLES: {analyzer.MAX_OUTLIER_SAMPLES}")
            print(f"  MAX_NORMAL_SAMPLES: {analyzer.MAX_NORMAL_SAMPLES}")
            print(f"  NORMAL_ZSCORE_THRESHOLD: {analyzer.NORMAL_ZSCORE_THRESHOLD}")
            print(f"  NORMAL_DIFF_THRESHOLD: {analyzer.NORMAL_DIFF_THRESHOLD}")
            print(f"  MAIN_DIMS: {analyzer.MAIN_DIMS}")
        elif sys.argv[1] == "--set":
            if len(sys.argv) > 3:
                if sys.argv[2] == "badcase":
                    analyzer.BADCASE_FILE = sys.argv[3]
                    print(f"BADCASE_FILE set to: {analyzer.BADCASE_FILE}")
                elif sys.argv[2] == "alldata":
                    analyzer.ALL_DATA_FILE = sys.argv[3]
                    print(f"ALL_DATA_FILE set to: {analyzer.ALL_DATA_FILE}")
                elif sys.argv[2] == "prompt":
                    analyzer.SCORING_PROMPT_PATH = sys.argv[3]
                    print(f"SCORING_PROMPT_PATH set to: {analyzer.SCORING_PROMPT_PATH}")
        elif sys.argv[1] == "--dim":
            if len(sys.argv) > 2:
                dim = sys.argv[2]
                if dim in analyzer.MAIN_DIMS:
                    analyzer.run(dimension=dim)
                else:
                    print(f"[ERROR] Unknown dimension: {dim}")
                    print(f"Available dimensions: {analyzer.MAIN_DIMS}")
            else:
                print("[ERROR] Please specify dimension")
                print(f"Usage: python etype_preference_analyzer.py --dim <dimension>")
                print(f"Available dimensions: {analyzer.MAIN_DIMS}")
        elif sys.argv[1] == "--freeze":
            if len(sys.argv) > 2:
                dim = sys.argv[2]
                if dim in analyzer.MAIN_DIMS:
                    analyzer.freeze_dimension(dim)
                else:
                    print(f"[ERROR] Unknown dimension: {dim}")
                    print(f"Available dimensions: {analyzer.MAIN_DIMS}")
            else:
                print("[ERROR] Please specify dimension to freeze")
                print(f"Usage: python etype_preference_analyzer.py --freeze <dimension>")
                print(f"Available dimensions: {analyzer.MAIN_DIMS}")
        elif sys.argv[1] == "--unfreeze":
            if len(sys.argv) > 2:
                dim = sys.argv[2]
                analyzer.unfreeze_dimension(dim)
            else:
                print("[ERROR] Please specify dimension to unfreeze")
                print(f"Usage: python etype_preference_analyzer.py --unfreeze <dimension>")
                print(f"Frozen dimensions: {analyzer.FROZEN_DIMS}")
        elif sys.argv[1] == "--frozen":
            print("\nFrozen Dimensions:")
            if analyzer.FROZEN_DIMS:
                for dim in analyzer.FROZEN_DIMS:
                    print(f"  - {dim}")
            else:
                print("  None (all dimensions active)")
            print(f"\nActive Dimensions:")
            for dim in analyzer.get_active_dimensions():
                print(f"  - {dim}")
        else:
            print("Usage:")
            print("  python etype_preference_analyzer.py                    - Analyze all active dimensions")
            print("  python etype_preference_analyzer.py --dim <dimension>  - Analyze specific dimension")
            print("  python etype_preference_analyzer.py --history         - Show iteration history")
            print("  python etype_preference_analyzer.py --config          - Show configuration")
            print("  python etype_preference_analyzer.py --frozen          - Show frozen dimensions")
            print("  python etype_preference_analyzer.py --freeze <dim>    - Freeze dimension")
            print("  python etype_preference_analyzer.py --unfreeze <dim>  - Unfreeze dimension")
            print("  python etype_preference_analyzer.py --set badcase <f> - Set badcase file")
            print("  python etype_preference_analyzer.py --set alldata <f> - Set all-data file")
            print("  python etype_preference_analyzer.py --set prompt <f>  - Set prompt file")
            print("\nAvailable dimensions:", analyzer.MAIN_DIMS)
    else:
        analyzer.run()
