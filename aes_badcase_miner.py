"""
AES (Automatic Essay Scoring) Badcase Mining System

This module implements a comprehensive badcase detection system for essay scoring,
including multiple badcase categories with robust statistical methods.
"""

import json
import statistics
import sys
from typing import Dict, List, Any, Optional, Tuple


# ============================================================
# ⭐ 配置参数（显眼位置）
# ============================================================
class Config:
    """Configuration parameters for badcase mining"""
    
    # ----------------------
    # 输入输出文件配置
    # ----------------------
    INPUT_FILE = "train_scoring_results0.json"          # 输入：评分结果（教师评分+AI评分）
    OUTPUT_FILE = "aes_badcases0.json"   # 输出：Badcase挖掘结果
    
    # ----------------------
    # 检测参数配置
    # ----------------------
    MAIN_DIMS = ['content', 'expression', 'structure']  # 主要分析维度
    
    # E类最小业务差异阈值
    MIN_DIFF = {
        "content": 1,
        "expression": 1,
        "structure": 1
    }
    
    # A类自适应阈值下限（暂时禁用A类检测）
    A_THRESHOLD_FLOOR = 1.8


# ============================================================
# 主类
# ============================================================
class AESBadcaseMiner:
    """
    Prompt-related badcase mining for content, expression, and structure.

    Active output covers B bias and E residual anomalies only. Technique and
    length belong to upstream deterministic evaluation and are out of scope.
    """

    def __init__(self, input_path: str):
        """
        Initialize the miner with input data.
        
        Args:
            input_path: Path to the input JSON file
        """
        self.input_path = input_path
        self.data: List[Dict] = []
        self.statistics: Dict[str, Any] = {}
        self._load_data()

    def _load_data(self) -> None:
        """Load and validate input data from JSON file."""
        with open(self.input_path, 'r', encoding='utf-8') as f:
            self.data = json.load(f)
            
        # Security fix: Check for empty input
        if not self.data:
            raise ValueError("Input data is empty.")

    @staticmethod
    def _safe_mad(abs_deviations: List[float]) -> float:
        """
        Calculate MAD safely, returning a small value if all deviations are zero.
        
        Args:
            abs_deviations: List of absolute deviations from median
            
        Returns:
            MAD value, at least 1e-6
        """
        mad = statistics.median(abs_deviations) if abs_deviations else 0.0
        return max(mad, 1e-6)

    def _compute_basic_statistics(self) -> None:
        """
        Compute all basic statistics for the three main dimensions:
        - raw diff: AI_k - Teacher_k
        - median_diff
        - residual
        - MAD
        - robust z-score
        """
        stats = {}
        total_count = len(self.data)
        stats['total_count'] = total_count

        # Process each main dimension
        for dim in Config.MAIN_DIMS:
            # 1. Compute raw diffs: diff_k = AI_k - Teacher_k
            diffs = []
            for item in self.data:
                diff = item['AI'][dim] - item['teacher'][dim]
                diffs.append(diff)

            # 2. Compute median_diff: m_k = median(diff_k)
            median_diff = statistics.median(diffs)

            # 3. Compute residuals: r_k = diff_k - m_k
            residuals = [d - median_diff for d in diffs]

            # 4. Compute MAD: MAD_k = median(abs(r_k))
            abs_residuals = [abs(r) for r in residuals]
            mad = self._safe_mad(abs_residuals)
            scale_factor = 1.4826 * mad + 1e-6  # Prevent division by zero

            # 5. Compute robust z-score: z_k = r_k / (1.4826 * MAD_k + 1e-6)
            z_scores = [r / scale_factor for r in residuals]
            
            # Count positive/negative bias
            positive_bias_count = sum(1 for d in diffs if d > 0)
            negative_bias_count = sum(1 for d in diffs if d < 0)

            # Store dimension statistics
            stats[dim] = {
                'raw_diffs': diffs,
                'median_diff': median_diff,
                'residuals': residuals,
                'MAD': mad,
                'scale_factor': scale_factor,
                'z_scores': z_scores,
                'positive_bias_count': positive_bias_count,
                'negative_bias_count': negative_bias_count
            }

        self.statistics = stats

    def _detect_class_a_instability(self) -> Tuple[Dict[str, List], float, float]:
        """
        Detect A类：High-Dimension Instability
        
        I = 0.4*|content_diff| + 0.4*|expression_diff| + 0.2*|structure_diff|
        affected_dims = count(|diff_k| >= 1)
        T_A = adaptive threshold based on median and MAD, with business floor
        
        Requires direction diversity (both positive and negative diffs present).
        
        Returns:
            Tuple of (result dict, raw threshold, final threshold)
        """
        severe = []
        soft = []

        # Get dimension statistics for threshold calculation
        content_stats = self.statistics['content']
        expr_stats = self.statistics['expression']
        struct_stats = self.statistics['structure']

        # Compute raw adaptive threshold T_A
        computed_threshold = (
            0.4 * (abs(content_stats['median_diff']) + 0.5 * content_stats['MAD'])
            + 0.4 * (abs(expr_stats['median_diff']) + 0.5 * expr_stats['MAD'])
            + 0.2 * (abs(struct_stats['median_diff']) + 0.5 * struct_stats['MAD'])
        )
        
        # Apply business floor
        T_A = max(computed_threshold, Config.A_THRESHOLD_FLOOR)

        for idx, item in enumerate(self.data):
            # Get diffs for this item
            content_diff = item['AI']['content'] - item['teacher']['content']
            expr_diff = item['AI']['expression'] - item['teacher']['expression']
            struct_diff = item['AI']['structure'] - item['teacher']['structure']
            
            diffs = [content_diff, expr_diff, struct_diff]

            # Direction diversity check: must have both positive and negative diffs
            has_positive = any(d > 0 for d in diffs)
            has_negative = any(d < 0 for d in diffs)
            
            if not (has_positive and has_negative):
                continue

            # Compute instability score I
            I = (
                0.4 * abs(content_diff)
                + 0.4 * abs(expr_diff)
                + 0.2 * abs(struct_diff)
            )

            # Compute affected_dims
            affected_dims = 0
            if abs(content_diff) >= 1:
                affected_dims += 1
            if abs(expr_diff) >= 1:
                affected_dims += 1
            if abs(struct_diff) >= 1:
                affected_dims += 1

            # Only consider if affected_dims >= 2
            if affected_dims >= 2:
                # Determine severity first, then create badcase object
                is_severe = I > 1.5 * T_A
                is_soft = T_A < I <= 1.5 * T_A
                
                if is_severe or is_soft:
                    badcase = self._create_badcase_item(
                        item=item,
                        idx=idx,
                        extra_fields={
                            'instability_score': I,
                            'affected_dims': affected_dims,
                            'threshold': T_A
                        }
                    )
                    badcase['severity'] = 'severe' if is_severe else 'soft'
                    if is_severe:
                        severe.append(badcase)
                    else:
                        soft.append(badcase)

        return (
            {
                'severe': severe,
                'soft': soft
            },
            computed_threshold,
            T_A
        )

    def _detect_class_b_bias(self) -> Dict[str, List]:
        """
        Detect B类：High-Dimension Consistent Bias
        
        B = (content_diff + expression_diff + structure_diff) / 3
        direction_consistency: number of dims with same sign as B
        
        Uses raw diffs, NOT residuals/z-scores.
        
        Returns:
            Dict with 'severe' and 'soft' badcase lists
        """
        severe = []
        soft = []

        for idx, item in enumerate(self.data):
            # Get raw diffs
            content_diff = item['AI']['content'] - item['teacher']['content']
            expr_diff = item['AI']['expression'] - item['teacher']['expression']
            struct_diff = item['AI']['structure'] - item['teacher']['structure']

            # Compute bias score B
            B = (content_diff + expr_diff + struct_diff) / 3

            # Compute direction consistency
            sign_B = 1 if B > 0 else -1 if B < 0 else 0
            direction_consistency = 0

            if sign_B != 0:
                if (content_diff * sign_B) > 0:
                    direction_consistency += 1
                if (expr_diff * sign_B) > 0:
                    direction_consistency += 1
                if (struct_diff * sign_B) > 0:
                    direction_consistency += 1

            # Determine direction (fix: handle B == 0)
            if B > 0:
                direction = "lenient"
            elif B < 0:
                direction = "strict"
            else:
                direction = "neutral"

            # Apply rules: determine severity first
            is_severe = abs(B) > 1.5 and direction_consistency >= 3
            is_soft = abs(B) > 1 and direction_consistency >= 2
            
            if is_severe or is_soft:
                badcase = self._create_badcase_item(
                    item=item,
                    idx=idx,
                    extra_fields={
                        'bias_score': B,
                        'direction_consistency': direction_consistency,
                        'direction': direction
                    }
                )
                badcase['severity'] = 'severe' if is_severe else 'soft'
                if is_severe:
                    severe.append(badcase)
                else:
                    soft.append(badcase)

        return {
            'severe': severe,
            'soft': soft
        }

    def _detect_class_c_technique(self) -> Dict[str, Dict[str, List]]:
        """
        Detect C类：Technique Drift
        
        Distinguishes between hallucination (AI高估) and miss (AI漏判).
        
        Returns:
            Dict with 'hallucination' and 'miss' sub-dicts, each with 'severe' and 'soft'
        """
        hallucination_severe = []
        hallucination_soft = []
        miss_severe = []
        miss_soft = []

        for idx, item in enumerate(self.data):
            if item.get('AI', {}).get('technique') is None:
                continue
            teacher_tech = item['teacher']['technique']
            ai_tech = item['AI']['technique']
            technique_diff = ai_tech - teacher_tech

            # Hallucination: AI高估 - determine first
            if teacher_tech == 0:
                is_severe = ai_tech >= 2
                is_soft = ai_tech >= 1 and ai_tech < 2
                
                if is_severe or is_soft:
                    badcase = self._create_badcase_item(
                        item=item,
                        idx=idx,
                        extra_fields={
                            'type': 'hallucination',
                            'technique_diff': technique_diff
                        }
                    )
                    badcase['severity'] = 'severe' if is_severe else 'soft'
                    if is_severe:
                        hallucination_severe.append(badcase)
                    else:
                        hallucination_soft.append(badcase)

            # Miss: AI漏判 - determine first
            if ai_tech == 0:
                is_severe = teacher_tech >= 2
                is_soft = teacher_tech >= 1 and teacher_tech < 2
                
                if is_severe or is_soft:
                    badcase = self._create_badcase_item(
                        item=item,
                        idx=idx,
                        extra_fields={
                            'type': 'miss',
                            'technique_diff': technique_diff
                        }
                    )
                    badcase['severity'] = 'severe' if is_severe else 'soft'
                    if is_severe:
                        miss_severe.append(badcase)
                    else:
                        miss_soft.append(badcase)

        return {
            'hallucination': {
                'severe': hallucination_severe,
                'soft': hallucination_soft
            },
            'miss': {
                'severe': miss_severe,
                'soft': miss_soft
            }
        }

    def _detect_class_d_length(self) -> Dict[str, List]:
        """
        Detect D类：Length Mismatch
        
        Length is objective, no z-score used.
        Any mismatch counts as badcase, no severe/soft distinction.
        
        Returns:
            Dict with 'badcases' list
        """
        badcases = []

        for idx, item in enumerate(self.data):
            if item.get('AI', {}).get('length') is None:
                continue
            teacher_len = item['teacher']['length']
            ai_len = item['AI']['length']
            length_diff = ai_len - teacher_len

            # Any mismatch counts as badcase
            if teacher_len != ai_len:
                badcase = self._create_badcase_item(
                    item=item,
                    idx=idx,
                    extra_fields={
                        'length_diff': length_diff
                    }
                )
                badcases.append(badcase)

        return {
            'badcases': badcases
        }

    def _detect_class_e_residual(self) -> Tuple[Dict[str, Dict[str, List]], Dict[str, Dict[str, int]]]:
        """
        Detect E类：Residual Subjective Anomaly
        
        Uses residual z-scores for content, expression, structure.
        Applies minimum business diff filter before z-score check.
        
        Returns:
            Tuple of (result dict, filter statistics)
        """
        result = {}
        filter_stats = {}

        for dim in Config.MAIN_DIMS:
            severe = []
            soft = []
            dim_stats = self.statistics[dim]
            min_diff = Config.MIN_DIFF[dim]
            
            filtered_count = 0
            remaining_count = 0

            for idx, item in enumerate(self.data):
                # Get values
                raw_diff = dim_stats['raw_diffs'][idx]
                residual = dim_stats['residuals'][idx]
                z_score = dim_stats['z_scores'][idx]
                
                # Apply business filtering first
                if abs(raw_diff) < min_diff:
                    filtered_count += 1
                    continue
                
                remaining_count += 1
                
                # Determine direction (fix: handle z_score == 0)
                if z_score > 0:
                    direction = "lenient"
                elif z_score < 0:
                    direction = "strict"
                else:
                    direction = "neutral"

                # Apply rules: determine severity first
                is_severe = abs(z_score) > 2.5
                is_soft = 1.5 < abs(z_score) <= 2.5
                
                if is_severe or is_soft:
                    badcase = self._create_badcase_item(
                        item=item,
                        idx=idx,
                        extra_fields={
                            'dimension': dim,
                            'raw_diff': raw_diff,
                            'residual': residual,
                            'z_score': z_score,
                            'direction': direction
                        }
                    )
                    badcase['severity'] = 'severe' if is_severe else 'soft'
                    if is_severe:
                        severe.append(badcase)
                    else:
                        soft.append(badcase)

            result[dim] = {
                'severe': severe,
                'soft': soft
            }
            
            filter_stats[dim] = {
                'filtered_count': filtered_count,
                'remaining_count': remaining_count
            }

        return result, filter_stats

    def _create_badcase_item(self, item: Dict, idx: int, extra_fields: Dict) -> Dict:
        """
        Create a standardized badcase item with all required fields.
        
        Args:
            item: Original data item
            idx: Index in data list
            extra_fields: Additional fields for the specific badcase type
            
        Returns:
            Complete badcase item
        """
        badcase = {
            'index': item.get('index'),
            'name': item['name'],
            'essay': item['essay'],
            'teacher': item['teacher'].copy(),
            'AI': item['AI'].copy(),
            'data_index': idx
        }

        # Add page if present
        if 'page' in item:
            badcase['page'] = item['page']

        # Compute and add all diffs
        badcase['diffs'] = {
            'content': item['AI']['content'] - item['teacher']['content'],
            'expression': item['AI']['expression'] - item['teacher']['expression'],
            'structure': item['AI']['structure'] - item['teacher']['structure']
        }

        # Add z-scores if available
        if self.statistics:
            badcase['z_scores'] = {}
            for dim in Config.MAIN_DIMS:
                badcase['z_scores'][dim] = self.statistics[dim]['z_scores'][idx]

        # Add extra fields
        badcase.update(extra_fields)

        return badcase

    def _build_statistics_summary(self) -> Dict:
        """
        Build a clean statistics summary for output (exclude long lists).
        
        Returns:
            Statistics dict with only summary values
        """
        summary = {
            'total_count': self.statistics['total_count']
        }

        for dim in Config.MAIN_DIMS:
            dim_stats = self.statistics[dim]
            summary[dim] = {
                'median_diff': dim_stats['median_diff'],
                'MAD': dim_stats['MAD'],
                'scale_factor': dim_stats['scale_factor'],
                'positive_bias_count': dim_stats['positive_bias_count'],
                'negative_bias_count': dim_stats['negative_bias_count']
            }

        return summary

    def run(self, output_path: str = None) -> Dict:
        """
        Run the complete badcase mining pipeline.
        
        Args:
            output_path: Path to save the output JSON file. If None, uses Config.OUTPUT_FILE
            
        Returns:
            Complete results dict
        """
        if output_path is None:
            output_path = Config.OUTPUT_FILE
            
        # Step 1: Compute basic statistics
        self._compute_basic_statistics()

        # Step 2: Detect prompt-related badcase categories only.
        b_result = self._detect_class_b_bias()
        e_result, e_filter_stats = self._detect_class_e_residual()

        # Build statistics summary
        stats = self._build_statistics_summary()
        
        # Add E class business threshold
        stats['E_business_threshold'] = Config.MIN_DIFF.copy()
        
        # Add counts for each category
        stats['B_bias'] = {
            'severe_count': len(b_result['severe']),
            'soft_count': len(b_result['soft'])
        }
        
        stats['E_residual'] = {}
        for dim in Config.MAIN_DIMS:
            stats['E_residual'][dim] = {
                'severe_count': len(e_result[dim]['severe']),
                'soft_count': len(e_result[dim]['soft']),
                'filtered_count': e_filter_stats[dim]['filtered_count'],
                'remaining_count': e_filter_stats[dim]['remaining_count']
            }

        # Build output for the three prompt-scored dimensions.
        results = {
            'statistics': stats,
            'B_bias': b_result,
            'E_residual': e_result
        }

        # Step 4: Save to file
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        # Print summary
        self._print_summary(results, output_path)

        return results

    def _print_summary(self, results: Dict, output_path: str) -> None:
        """Print a readable summary of badcase mining results."""
        print("=" * 60)
        print("AES BADCASE MINING - RESULTS SUMMARY")
        print("=" * 60)
        print(f"\nInput file:  {self.input_path}")
        print(f"Output file: {output_path}")
        print(f"\nTotal essays: {results['statistics']['total_count']}")

        print("\n--- BASIC STATISTICS ---")
        for dim in Config.MAIN_DIMS:
            s = results['statistics'][dim]
            print(f"{dim:12}: median_diff={s['median_diff']:+.3f}, MAD={s['MAD']:.3f}, "
                  f"+bias={s['positive_bias_count']}, -bias={s['negative_bias_count']}")
        
        print(f"\nE_business_threshold: {results['statistics']['E_business_threshold']}")

        print("\n--- B: High-Dimension Consistent Bias ---")
        b = results['B_bias']
        print(f"  Severe: {len(b['severe']):2d}")
        print(f"  Soft:   {len(b['soft']):2d}")

        print("\n--- E: Residual Subjective Anomaly ---")
        e = results['E_residual']
        e_stats = results['statistics']['E_residual']
        for dim in Config.MAIN_DIMS:
            dim_stats = e_stats[dim]
            print(f"  {dim:12} - Severe: {len(e[dim]['severe']):2d}, Soft: {len(e[dim]['soft']):2d}, "
                  f"filtered={dim_stats['filtered_count']}, remaining={dim_stats['remaining_count']}")

        print("\n" + "=" * 60)
        print(f"Results saved to {output_path}")
        print("=" * 60)


# ============================================================
# 命令行接口
# ============================================================
def main():
    """Main entry point for the AES badcase mining system."""
    print("=" * 60)
    print("AES Badcase Miner")
    print("=" * 60)
    
    # 显示当前配置
    print("\n默认配置:")
    print(f"  INPUT_FILE:  {Config.INPUT_FILE}")
    print(f"  OUTPUT_FILE: {Config.OUTPUT_FILE}")
    
    # 解析命令行参数
    input_file = Config.INPUT_FILE
    output_file = Config.OUTPUT_FILE
    
    if len(sys.argv) > 1:
        # 支持: python aes_badcase_miner.py <input.json> <output.json>
        input_file = sys.argv[1]
        if len(sys.argv) > 2:
            output_file = sys.argv[2]
    
    print(f"\n运行参数:")
    print(f"  输入: {input_file}")
    print(f"  输出: {output_file}")
    print("-" * 60)
    
    try:
        miner = AESBadcaseMiner(input_file)
        miner.run(output_file)
    except Exception as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
