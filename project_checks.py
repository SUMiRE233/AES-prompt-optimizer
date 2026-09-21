"""Offline completion checks for AES data contracts and repository hygiene."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{10,}")
SOURCE_SUFFIXES = {".py", ".md", ".txt", ".toml", ".yaml", ".yml"}


class CheckFailure(ValueError):
    """Raised when an offline project invariant is violated."""


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def index_rows(rows: list[dict[str, Any]], label: str) -> dict[int, dict[str, Any]]:
    lookup: dict[int, dict[str, Any]] = {}
    for position, row in enumerate(rows):
        if "index" not in row:
            raise CheckFailure(f"{label}[{position}] is missing global index")
        index = int(row["index"])
        if index in lookup:
            raise CheckFailure(f"{label} contains duplicate index {index}")
        lookup[index] = row
    return lookup


def validate_subset(
    subset: list[dict[str, Any]],
    origin: dict[int, dict[str, Any]],
    label: str,
) -> set[int]:
    subset_lookup = index_rows(subset, label)
    for index, row in subset_lookup.items():
        if index not in origin:
            raise CheckFailure(f"{label} index {index} is absent from origin")
        if row.get("essay") != origin[index].get("essay"):
            raise CheckFailure(f"{label} essay mismatch at global index {index}")
    return set(subset_lookup)


def normalized_essay_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def validate_no_duplicate_essays(rows: list[dict[str, Any]], label: str) -> None:
    seen: dict[str, int] = {}
    duplicates = []
    for position, row in enumerate(rows):
        text = normalized_essay_text(row.get("essay"))
        if not text:
            raise CheckFailure(f"{label}[{position}] has empty essay text")
        index = int(row["index"])
        if text in seen:
            duplicates.append((seen[text], index))
        else:
            seen[text] = index
    if duplicates:
        raise CheckFailure(f"{label} contains normalized duplicate essays: {duplicates}")


def validate_scoring_artifact(
    rows: list[dict[str, Any]],
    origin: dict[int, dict[str, Any]],
    label: str,
) -> None:
    artifact = index_rows(rows, label)
    for index, row in artifact.items():
        if index not in origin:
            raise CheckFailure(f"{label} index {index} is absent from origin")
        source = origin[index]
        if row.get("essay") != source.get("essay"):
            raise CheckFailure(f"{label} essay mismatch at global index {index}")
        if row.get("teacher") != source.get("teacher"):
            raise CheckFailure(f"{label} teacher-label mismatch at global index {index}")
        for dimension in ("content", "expression", "structure"):
            value = row.get("AI", {}).get(dimension)
            if not isinstance(value, (int, float)) or not 0 <= value <= 9:
                raise CheckFailure(f"{label} has invalid AI.{dimension} at global index {index}")


def iter_source_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        if any(part in {".git", ".venv", "venv", "__pycache__"} for part in path.parts):
            continue
        yield path


def check_source_hygiene(root: Path = ROOT) -> None:
    leaks = []
    for path in iter_source_files(root):
        if SECRET_PATTERN.search(path.read_text(encoding="utf-8", errors="replace")):
            leaks.append(str(path.relative_to(root)))
    if leaks:
        raise CheckFailure(f"credential-shaped values found in source files: {', '.join(leaks)}")


def check_local_data(root: Path = ROOT) -> list[str]:
    origin_path = root / "origin_scoring_results.json"
    if not origin_path.exists():
        return ["private origin data absent; local artifact checks skipped"]

    origin_rows = load_json(origin_path)
    origin = index_rows(origin_rows, "origin")
    validate_no_duplicate_essays(origin_rows, "origin")
    notes = [f"origin: {len(origin)} unique rows"]
    notes.append("origin: no normalized exact essay duplicates")

    train_path = root / "train_essays.json"
    validation_path = root / "test_essays.json"
    if train_path.exists() and validation_path.exists():
        train = validate_subset(load_json(train_path), origin, "train")
        validation = validate_subset(load_json(validation_path), origin, "validation")
        overlap = train & validation
        if overlap:
            raise CheckFailure(f"train/validation overlap: {sorted(overlap)}")
        if train | validation != set(origin):
            missing = sorted(set(origin) - (train | validation))
            raise CheckFailure(f"train/validation do not cover origin; missing={missing}")
        notes.append(f"split: train={len(train)}, validation={len(validation)}, overlap=0")

    scoring_patterns = ("train_scoring_results*.json", "test_scoring_results*.json")
    checked = 0
    for pattern in scoring_patterns:
        for path in sorted(root.glob(pattern)):
            validate_scoring_artifact(load_json(path), origin, path.name)
            checked += 1
    notes.append(f"scoring artifacts aligned: {checked}")

    manifest_path = root / "cv_folds.json"
    if manifest_path.exists():
        notes.extend(check_cv_protocol(root, origin, manifest_path))
    return notes


def check_cv_protocol(root: Path, origin: dict, manifest_path: Path) -> list[str]:
    """校验 CV 划分协议：报告留出 + 工作池 + 各折的完整性与互斥性。"""
    manifest = load_json(manifest_path)
    notes = []

    report_path = root / "report_holdout_essays.json"
    work_path = root / "work_pool_essays.json"
    if not report_path.exists() or not work_path.exists():
        return notes

    report = validate_subset(load_json(report_path), origin, "report_holdout")
    work = validate_subset(load_json(work_path), origin, "work_pool")
    if report & work:
        raise CheckFailure(f"报告留出与工作池重叠: {sorted(report & work)}")
    if report | work != set(origin):
        missing = sorted(set(origin) - (report | work))
        raise CheckFailure(f"报告留出与工作池未覆盖 origin; missing={missing}")

    declared_report = {int(value) for value in manifest.get("report_holdout_indices", [])}
    if declared_report != report:
        raise CheckFailure("cv_folds.json 的 report_holdout_indices 与文件不一致")
    declared_work = {int(value) for value in manifest.get("work_pool_indices", [])}
    if declared_work != work:
        raise CheckFailure("cv_folds.json 的 work_pool_indices 与文件不一致")

    covered_eval: set[int] = set()
    for fold in manifest.get("folds", []):
        number = int(fold["fold"])
        train_path = root / "fold_essays" / f"fold{number}_train_essays.json"
        eval_path = root / "fold_essays" / f"fold{number}_eval_essays.json"
        if not train_path.exists() or not eval_path.exists():
            raise CheckFailure(f"fold{number} 的训练/评估作文文件缺失")
        train = validate_subset(load_json(train_path), origin, f"fold{number}_train")
        evaluation = validate_subset(load_json(eval_path), origin, f"fold{number}_eval")
        if train & evaluation:
            raise CheckFailure(f"fold{number} 训练与评估重叠: {sorted(train & evaluation)}")
        if train | evaluation != work:
            raise CheckFailure(f"fold{number} 训练+评估未覆盖工作池")
        if evaluation & covered_eval:
            raise CheckFailure(f"fold{number} 的评估集与其它折重叠")
        covered_eval |= evaluation

    if covered_eval != work:
        missing = sorted(work - covered_eval)
        raise CheckFailure(f"各折评估集未覆盖工作池; missing={missing}")

    notes.append(
        f"cv protocol: report={len(report)}, work={len(work)}, "
        f"folds={len(manifest.get('folds', []))}"
    )
    return notes


def run(root: Path = ROOT) -> list[str]:
    check_source_hygiene(root)
    return ["source credential scan: clean", *check_local_data(root)]


def main() -> int:
    try:
        notes = run()
    except (CheckFailure, json.JSONDecodeError, OSError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    for note in notes:
        print(f"PASS: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
