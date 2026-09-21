"""归档既有 B 类实验产物（大纲 §9.1）。

重跑前必须把旧产物移入带 run ID 的归档目录，避免：
- 旧 `test_scoring_results*.json` 被 `run_test_iteration` 静默复用；
- 旧 `iteration_history.json` 让下一轮从 V7 起算；
- 新旧划分/模型/prompt 混杂。

本脚本只**移动**文件，不删除；并写出 `ARCHIVE_MANIFEST.json`
（含每个文件的 sha256 与字节数），使归档本身可复核。

用法：
    python archive_run.py                  # 预览将要移动的文件
    python archive_run.py --execute        # 实际执行
    python archive_run.py --run-id X --execute
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import List


DEFAULT_RUN_ID = "v1_seed42_20260920"
ARCHIVE_ROOT = "archive"

# 根目录下属于“旧 B 实验产物”的文件模式。
ROOT_PATTERNS: List[str] = [
    "optimized_prompt*_meta.md",
    "optimized_prompt*.md",
    "train_scoring_results*.json",
    "test_scoring_results*.json",
    "aes_badcases*.json",
    "iteration_history.json",
    "b_gate_report.json",
    "final_prompt_meta.md",
    "final_prompt.md",
    "final_aes_badcases.json",
    "final_train_scoring_results.json",
    "train_essays.json",
    "test_essays.json",
    "train_meta.json",
    "test_meta.json",
    "gate_test_*.json",
    "micro_scoring_*.json",
    "injected_prompt_next*.md",
    "api_request_debug.json",
    "optimized_result.json",
    "debug_final_prompt.txt",
    "raw_response_dump.txt",
    "run_manifest*.json",
    "prompt_candidate_decisions.json",
    "b_version_review.json",
    "b_rerun_log.json",
    "b_protocol_state.json",
    "b_validation_report.json",
    "b_rebuilt_route.json",
    "final_evidence.json",
    "final_train_scoring_results_mean*.json",
]

# 整目录归档（E 路线产物；本阶段不实施 E，但不得删除历史证据）。
DIRECTORY_TARGETS: List[str] = [
    "etype_analysis",
]

# 明确保留在原位的内容（输入与源码）。
KEEP_IN_PLACE = (
    "origin_scoring_results.json",
    "origin_prompt.md",
    "origin_prompt_meta.md",
    "*.py",
    "*.md",
    ".gitignore",
)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect(root: Path, patterns: List[str]) -> List[Path]:
    found: List[Path] = []
    for pattern in patterns:
        for match in sorted(glob.glob(str(root / pattern))):
            path = Path(match)
            if path.is_file() and path.name not in KEEP_IN_PLACE:
                found.append(path)
    return sorted(set(found))


def collect_dirs(root: Path, names: List[str]) -> List[Path]:
    return [root / name for name in names if (root / name).is_dir()]


def main() -> None:
    parser = argparse.ArgumentParser(description="归档既有 B 类实验产物")
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    root = Path(".").resolve()
    target_dir = root / ARCHIVE_ROOT / args.run_id

    files = collect(root, ROOT_PATTERNS)
    directories = collect_dirs(root, DIRECTORY_TARGETS)

    print("=" * 78)
    print(f"归档 run ID: {args.run_id}")
    print(f"归档目录  : {target_dir}")
    print("=" * 78)
    print(f"  待移动文件: {len(files)} 个")
    for path in files:
        print(f"    {path.relative_to(root)}")
    print(f"  待移动目录: {len(directories)} 个")
    for path in directories:
        print(f"    {path.relative_to(root)}/")

    if not files and not directories:
        print("\n  没有需要归档的内容。")
        return

    if not args.execute:
        print("\n  这是预览；加 --execute 才会真正移动。")
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    entries = []

    for path in files:
        relative = path.relative_to(root)
        digest = sha256_of(path)
        size = path.stat().st_size
        destination = target_dir / relative.as_posix().replace("/", "__")
        shutil.move(str(path), str(destination))
        entries.append(
            {
                "original_path": relative.as_posix(),
                "archived_as": destination.name,
                "sha256": digest,
                "bytes": size,
            }
        )

    for path in directories:
        relative = path.relative_to(root)
        destination = target_dir / relative.as_posix().replace("/", "__")
        # 逐文件记录目录内容后再整体移动
        for child in sorted(path.rglob("*")):
            if child.is_file():
                entries.append(
                    {
                        "original_path": child.relative_to(root).as_posix(),
                        "archived_as": f"{destination.name}/{child.relative_to(path).as_posix()}",
                        "sha256": sha256_of(child),
                        "bytes": child.stat().st_size,
                    }
                )
        shutil.move(str(path), str(destination))

    manifest = {
        "run_id": args.run_id,
        "archived_at": datetime.now().isoformat(timespec="seconds"),
        "reason": (
            "重跑前归档旧 B 实验产物，防止 stale scoring/iteration_history 被复用"
            "（大纲 §9.1/§9.2）"
        ),
        "file_count": len(entries),
        "entries": entries,
    }
    (target_dir / "ARCHIVE_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n  已归档 {len(entries)} 个文件 → {target_dir}")
    print(f"  清单: {target_dir / 'ARCHIVE_MANIFEST.json'}")


if __name__ == "__main__":
    main()
