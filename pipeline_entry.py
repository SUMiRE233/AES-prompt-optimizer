import argparse
import json
import os
import shutil
from dataclasses import dataclass

from aes_badcase_miner import AESBadcaseMiner
from batch_scoring import BatchEssayScorer
from preprocess_prompt import PromptPreprocessor
from prompt_optimizer import PromptOptimizer
from sample_extractor import EssayExtractor


ORIGIN_META = "origin_prompt_meta.md"
ORIGIN_PROMPT = "origin_prompt.md"

FINAL_META = "final_prompt_meta.md"
FINAL_PROMPT = "final_prompt.md"
FINAL_BADCASE = "final_aes_badcases.json"
FINAL_TRAIN_SCORING = "final_train_scoring_results.json"
GATE_REPORT = "b_gate_report.json"
MAX_AUTO_VERSION = 6


@dataclass(frozen=True)
class IterationDecision:
    action: str
    reason: str
    stable_iteration: int | None = None


def prompt_meta_path(iteration):
    if iteration == 0:
        return ORIGIN_META
    return f"optimized_prompt{iteration}_meta.md"


def prompt_path(iteration):
    if iteration == 0:
        return ORIGIN_PROMPT
    return f"optimized_prompt{iteration}.md"


def train_scoring_path(iteration):
    return f"train_scoring_results{iteration}.json"


def test_scoring_path(iteration):
    return f"test_scoring_results{iteration}.json"


def badcase_path(iteration):
    return f"aes_badcases{iteration}.json"


def preprocess_prompt(input_path, output_path):
    preprocessor = PromptPreprocessor()
    preprocessor.process(input_path=input_path, output_path=output_path)


def run_scoring(prompt_file, essays_file, output_file, origin_file):
    scorer = BatchEssayScorer()
    scorer.prompt_path = prompt_file
    scorer.essays_path = essays_file
    scorer.output_path = output_file
    scorer.origin_data_path = origin_file
    scorer.run()


def run_badcase_mining(scoring_file, output_file):
    miner = AESBadcaseMiner(scoring_file)
    return miner.run(output_file)


def run_prompt_optimization(current_meta, badcases_file, target_meta):
    optimizer = PromptOptimizer()
    optimizer.CURRENT_PROMPT = current_meta
    optimizer.BADCASE_FILE = badcases_file
    optimizer.TARGET_PROMPT = target_meta
    optimizer.run()


def b_bias_counts_from_badcases(badcase_data):
    stats = badcase_data["statistics"]["B_bias"]
    severe = int(stats["severe_count"])
    soft = int(stats["soft_count"])
    return {
        "severe": severe,
        "soft": soft,
        "total": severe + soft,
    }


def b_bias_counts_from_scoring_results(scoring_file):
    with open(scoring_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    severe = 0
    soft = 0

    for item in data:
        content_diff = item["AI"]["content"] - item["teacher"]["content"]
        expression_diff = item["AI"]["expression"] - item["teacher"]["expression"]
        structure_diff = item["AI"]["structure"] - item["teacher"]["structure"]

        bias_score = (content_diff + expression_diff + structure_diff) / 3
        sign = 1 if bias_score > 0 else -1 if bias_score < 0 else 0

        direction_consistency = 0
        if sign != 0:
            if content_diff * sign > 0:
                direction_consistency += 1
            if expression_diff * sign > 0:
                direction_consistency += 1
            if structure_diff * sign > 0:
                direction_consistency += 1

        if abs(bias_score) > 1.5 and direction_consistency >= 3:
            severe += 1
        elif abs(bias_score) > 1 and direction_consistency >= 2:
            soft += 1

    return {
        "severe": severe,
        "soft": soft,
        "total": severe + soft,
    }


def decide_iteration(count_history, current_iteration, max_version=MAX_AUTO_VERSION):
    """Apply the B-route stopping policy to counts through current_iteration."""
    if len(count_history) != current_iteration + 1:
        raise ValueError("count_history must contain V0 through the current version")

    if current_iteration >= max_version:
        return IterationDecision(
            "compare",
            "safety_cap",
            max(0, current_iteration - 1),
        )

    if current_iteration == 0:
        return IterationDecision("continue", "origin")

    previous_counts = count_history[-2]
    current_counts = count_history[-1]
    if current_counts["severe"] < previous_counts["severe"]:
        return IterationDecision("continue", "severe_decreased")
    if current_counts["severe"] > previous_counts["severe"]:
        return IterationDecision(
            "compare",
            "severe_rebound",
            current_iteration - 1,
        )

    soft_stalled_twice = (
        current_iteration >= 2
        and count_history[-1]["soft"] >= count_history[-2]["soft"]
        and count_history[-2]["soft"] >= count_history[-3]["soft"]
    )
    if soft_stalled_twice:
        return IterationDecision(
            "compare",
            "soft_not_down_twice",
            current_iteration - 2,
        )
    return IterationDecision("continue", "severe_flat_soft_patience")


def scoring_mae(scoring_file):
    with open(scoring_file, "r", encoding="utf-8") as f:
        rows = json.load(f)
    errors = [
        abs(item["AI"][dimension] - item["teacher"][dimension])
        for item in rows
        for dimension in ("content", "expression", "structure")
    ]
    return sum(errors) / len(errors)


def candidate_rank(iteration, counts):
    return (5 * counts["severe"] + counts["soft"], counts["severe"], iteration)


def comparison_iterations(decision, last_iteration):
    if decision.reason == "safety_cap":
        return tuple(range(max(0, last_iteration - 2), last_iteration + 1))
    return (decision.stable_iteration, last_iteration)


def compare_candidates(iterations, origin_file, reason):
    candidates = []
    for iteration in dict.fromkeys(iterations):
        scoring_file = test_scoring_path(iteration)
        if not os.path.exists(scoring_file):
            counts = run_test_iteration(iteration, origin_file)
        else:
            counts = b_bias_counts_from_scoring_results(scoring_file)
            print(f"[复用] {scoring_file}")
        candidates.append(
            {
                "iteration": iteration,
                "counts": counts,
                "score": 5 * counts["severe"] + counts["soft"],
                "mae": scoring_mae(scoring_file),
            }
        )

    selected = min(
        candidates,
        key=lambda item: candidate_rank(item["iteration"], item["counts"]),
    )
    for previous, current in zip(candidates, candidates[1:]):
        if current["mae"] > previous["mae"]:
            print(
                "WARNING: validation MAE rebounded "
                f"from V{previous['iteration']}={previous['mae']:.3f} "
                f"to V{current['iteration']}={current['mae']:.3f}; "
                "MAE is recorded only and did not affect final selection."
            )

    report = {
        "reason": reason,
        "ranking": "5*severe+soft, then severe, then earlier version",
        "mae_policy": "record_only; warn_on_rebound",
        "candidates": candidates,
        "selected_iteration": selected["iteration"],
    }
    with open(GATE_REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[保存] {GATE_REPORT}")
    return selected["iteration"]


def ensure_origin_prompt():
    if os.path.exists(ORIGIN_PROMPT):
        return
    preprocess_prompt(ORIGIN_META, ORIGIN_PROMPT)


def save_final_prompt(meta_path, iteration):
    if os.path.exists(meta_path):
        shutil.copy2(meta_path, FINAL_META)
        print(f"[保存] {FINAL_META}")

    prompt_path = meta_path.replace("_meta.md", ".md")
    if os.path.exists(prompt_path):
        shutil.copy2(prompt_path, FINAL_PROMPT)
        print(f"[保存] {FINAL_PROMPT}")

    badcases_file = badcase_path(iteration)
    if os.path.exists(badcases_file):
        shutil.copy2(badcases_file, FINAL_BADCASE)
        print(f"[保存] {FINAL_BADCASE}")

    train_scoring_file = train_scoring_path(iteration)
    if os.path.exists(train_scoring_file):
        shutil.copy2(train_scoring_file, FINAL_TRAIN_SCORING)
        print(f"[保存] {FINAL_TRAIN_SCORING}")


def extract_samples(origin_file):
    extractor = EssayExtractor()
    extractor.input_path = origin_file
    extractor.run(split=True)


def run_train_iteration(iteration, origin_file):
    meta_file = prompt_meta_path(iteration)
    prompt_file = prompt_path(iteration)
    scoring_file = train_scoring_path(iteration)
    badcases_file = badcase_path(iteration)

    preprocess_prompt(meta_file, prompt_file)
    run_scoring(prompt_file, "train_essays.json", scoring_file, origin_file)
    return run_badcase_mining(scoring_file, badcases_file)


def run_test_iteration(iteration, origin_file):
    prompt_file = prompt_path(iteration)
    scoring_file = test_scoring_path(iteration)

    if not os.path.exists(prompt_file):
        preprocess_prompt(prompt_meta_path(iteration), prompt_file)

    run_scoring(prompt_file, "test_essays.json", scoring_file, origin_file)
    return b_bias_counts_from_scoring_results(scoring_file)


def load_count_history(last_iteration):
    history = []
    for iteration in range(last_iteration + 1):
        path = badcase_path(iteration)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Cannot resume: missing {path}")
        with open(path, "r", encoding="utf-8") as f:
            history.append(b_bias_counts_from_badcases(json.load(f)))
    return history


def run_pipeline(origin_file, resume_from=0, max_version=MAX_AUTO_VERSION):
    print("=" * 60)
    print("AES prompt self-iteration pipeline")
    print("=" * 60)

    if not 0 <= resume_from < max_version:
        raise ValueError("resume_from must be between V0 and one version before the cap")

    if resume_from == 0:
        print("\n[0] Extract train/test essays")
        extract_samples(origin_file)
        print("\n[0] Build origin prompt and score training set")
        ensure_origin_prompt()
        run_scoring(ORIGIN_PROMPT, "train_essays.json", train_scoring_path(0), origin_file)
        previous_badcases = run_badcase_mining(train_scoring_path(0), badcase_path(0))
        count_history = [b_bias_counts_from_badcases(previous_badcases)]
    else:
        print(f"\n[resume] Reusing completed artifacts through V{resume_from}")
        count_history = load_count_history(resume_from)

    decision = decide_iteration(count_history, resume_from, max_version)
    if decision.action == "compare":
        final_iteration = compare_candidates(
            comparison_iterations(decision, resume_from), origin_file, decision.reason
        )
        final_meta = prompt_meta_path(final_iteration)
        save_final_prompt(final_meta, final_iteration)
        print(f"FINAL_PROMPT_META={final_meta}")
        return final_meta

    print(f"\n[{resume_from} -> {resume_from + 1}] Optimize prompt meta")
    run_prompt_optimization(
        prompt_meta_path(resume_from),
        badcase_path(resume_from),
        prompt_meta_path(resume_from + 1),
    )

    iteration = resume_from + 1
    while True:
        print(f"\n[{iteration}] Score training set and mine badcases")
        current_badcases = run_train_iteration(iteration, origin_file)
        current_counts = b_bias_counts_from_badcases(current_badcases)

        print(f"Train B-bias current={current_counts}")
        count_history.append(current_counts)
        decision = decide_iteration(count_history, iteration, max_version)

        if decision.action == "compare":
            print(f"\n[{iteration}] Candidate comparison triggered: {decision.reason}")
            final_iteration = compare_candidates(
                comparison_iterations(decision, iteration),
                origin_file,
                decision.reason,
            )
            final_meta = prompt_meta_path(final_iteration)
            save_final_prompt(final_meta, final_iteration)
            print(f"\nFINAL_PROMPT_META={final_meta}")
            return final_meta

        print(f"\n[{iteration} -> {iteration + 1}] Optimize prompt meta")
        run_prompt_optimization(
            prompt_meta_path(iteration),
            badcase_path(iteration),
            prompt_meta_path(iteration + 1),
        )

        iteration += 1


def main():
    parser = argparse.ArgumentParser(
        description="Run the full AES prompt self-iteration pipeline."
    )
    parser.add_argument(
        "--origin-file",
        default="origin_scoring_results.json",
        help="Input scoring results file used to extract train/test essays.",
    )
    parser.add_argument(
        "--resume-from",
        type=int,
        default=0,
        help="Reuse complete artifacts through this version and continue from there.",
    )
    parser.add_argument(
        "--max-version",
        type=int,
        default=MAX_AUTO_VERSION,
        help="Last automatically generated version (default: V6).",
    )
    args = parser.parse_args()
    run_pipeline(args.origin_file, args.resume_from, args.max_version)


if __name__ == "__main__":
    main()
