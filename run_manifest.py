"""Create a secret-free manifest for a reproducible local experiment run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    files: Iterable[Path],
    models: dict[str, str],
    thresholds: dict[str, Any],
    repeat: int,
) -> dict[str, Any]:
    resolved = [path.resolve() for path in files]
    missing = [str(path) for path in resolved if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Manifest inputs are missing: {missing}")
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repeat": int(repeat),
        "models": dict(sorted(models.items())),
        "thresholds": thresholds,
        "files": [
            {
                "name": path.name,
                "sha256": sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in resolved
        ],
    }


def key_value(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"Expected NAME=VALUE, got {value!r}")
        key, item = value.split("=", 1)
        if "key" in key.lower() or "token" in key.lower() or "secret" in key.lower():
            raise ValueError(f"Secret-like manifest field is forbidden: {key}")
        result[key] = item
    return result


def index_set(path: Path) -> list[int]:
    """读出一个作文/评分文件的全局 index 集合。"""
    rows = json.loads(path.read_text(encoding="utf-8"))
    return sorted(int(row["index"]) for row in rows)


def build_experiment_manifest(
    run_id: str,
    inputs: Iterable[Path],
    split_files: dict[str, Path],
    models: dict[str, str],
    thresholds: dict[str, Any],
    stop_policy: dict[str, Any],
    structure_budget: dict[str, Any],
    repeat: int = 1,
) -> dict[str, Any]:
    """大纲 §9.2 的运行 manifest：数据、划分、模型、阈值、停止策略、结构预算。

    只记录哈希与标识，不记录任何 key/token（`key_value` 已拒绝 secret-like 字段；
    这里对 models 的取值同样做一次扫描）。
    """
    manifest = build_manifest(
        files=inputs,
        models=models,
        thresholds=thresholds,
        repeat=repeat,
    )
    for role, value in models.items():
        lowered = role.lower()
        if any(word in lowered for word in ("key", "token", "secret")):
            raise ValueError(f"Secret-like model role is forbidden: {role}")
        if value and value.startswith("sk-"):
            raise ValueError(f"Model value for {role!r} looks like a credential")

    manifest["run_id"] = run_id
    manifest["stop_policy"] = stop_policy
    manifest["structure_budget"] = structure_budget
    manifest["split"] = {
        role: {
            "file": path.name,
            "sha256": sha256(path),
            "count": len(indices),
            "indices": indices,
        }
        for role, path in sorted(split_files.items())
        for indices in [index_set(path)]
    }
    return manifest


def write_experiment_manifest(manifest: dict[str, Any], output: Path) -> Path:
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return output


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> Path:
    """原子写入：先写同目录临时文件，再 `os.replace` 覆盖目标。"""
    path = Path(path)
    directory = path.parent if str(path.parent) else Path(".")
    directory.mkdir(parents=True, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(
        dir=str(directory), prefix=".manifest_", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except BaseException:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise
    return path


def file_hashes(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """`[{'name','sha256','bytes'}]`；文件缺失时 `sha256=None` 并标 `missing`。"""
    result: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            result.append(
                {
                    "name": path.name,
                    "sha256": sha256(path),
                    "bytes": path.stat().st_size,
                }
            )
        else:
            result.append(
                {"name": path.name, "sha256": None, "bytes": None, "missing": True}
            )
    return result


def build_version_record(
    iteration: int,
    prompt_meta: Path,
    runtime_prompt: Path,
    contract_violations: Sequence[Any],
    inputs: Iterable[Path] = (),
    outputs: Iterable[Path] = (),
) -> dict[str, Any]:
    """大纲 §9.2 的“每版”记录：输入/输出文件 hash + 每版校验结果。"""
    return {
        "iteration": int(iteration),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "contract_ok": not list(contract_violations),
        "contract_violations": [str(item) for item in contract_violations],
        "inputs": file_hashes([Path(prompt_meta), *inputs]),
        "outputs": file_hashes([Path(runtime_prompt), *outputs]),
    }


def attach_version_record(manifest_path: Path, record: dict[str, Any]) -> Path:
    """把一个版本的记录按 iteration **覆盖式**写入 `manifest['versions']`。

    幂等：同一版本重跑（如 rerun）不会在 manifest 里堆出重复行。
    """
    path = Path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    versions = [
        item
        for item in manifest.get("versions", [])
        if item.get("iteration") != record.get("iteration")
    ]
    versions.append(record)
    versions.sort(key=lambda item: item.get("iteration", 0))
    manifest["versions"] = versions
    _atomic_write_json(path, manifest)
    return path


def manifest_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    """判断“能否复用旧产物”所需的稳定身份字段（大纲 §9.2 末句 / §11）。"""
    return {
        "models": manifest.get("models"),
        "thresholds": manifest.get("thresholds"),
        "stop_policy": manifest.get("stop_policy"),
        "structure_budget": manifest.get("structure_budget"),
        "protocol": manifest.get("protocol"),
        "files": {
            item["name"]: item["sha256"] for item in manifest.get("files", [])
        },
        "split": {
            role: {"sha256": value.get("sha256"), "indices": value.get("indices")}
            for role, value in sorted(manifest.get("split", {}).items())
        },
    }


def assert_manifest_compatible(
    manifest_path: Path, current_manifest: dict[str, Any]
) -> dict[str, Any]:
    """把旧 manifest 的身份与当前身份比对；不一致则**拒绝复用**旧产物。

    大纲 §9.2：“模型、数据、prompt 或阈值不一致时，禁止自动复用旧产物。”
    """
    stored = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    stored_identity = manifest_identity(stored)
    current_identity = manifest_identity(current_manifest)
    mismatched = [
        key for key in stored_identity if stored_identity[key] != current_identity[key]
    ]
    if mismatched:
        raise RuntimeError(
            "旧 run manifest 与当前身份不一致，拒绝复用旧产物（大纲 §9.2）："
            f"{mismatched}\n"
            f"  manifest: {manifest_path}\n"
            f"  run_id  : {stored.get('run_id')}\n"
            "模型 / 数据 / prompt / 阈值 / 划分任一不一致时不得自动复用。"
        )
    return stored_identity


# 受控迁移只允许更新的**策略声明**字段（数据身份字段永远不可迁移）。
POLICY_FIELDS = ("stop_policy", "protocol")


def manifest_mismatches(manifest_path: Path, current_manifest: dict[str, Any]) -> list[str]:
    """旧 manifest 与当前身份的差异字段列表（不抛错，供受控迁移决策）。"""
    stored = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    stored_identity = manifest_identity(stored)
    current_identity = manifest_identity(current_manifest)
    return [key for key in stored_identity if stored_identity[key] != current_identity[key]]


def apply_policy_migration(manifest_path: Path, current_manifest: dict[str, Any],
                           fields: Sequence[str] = POLICY_FIELDS,
                           note: str = "") -> dict[str, Any]:
    """受控迁移：只允许更新**策略声明**字段（stop_policy / protocol）。

    数据身份（模型、阈值、文件、划分）任一经差异即拒绝；迁移前后的值与说明
    写入 `policy_migrations`，保证“何时、为何改了口径”可复核。
    """
    path = Path(manifest_path)
    stored = json.loads(path.read_text(encoding="utf-8"))
    mismatched = manifest_mismatches(path, current_manifest)
    prohibited = [key for key in mismatched if key not in fields]
    if prohibited:
        raise RuntimeError(
            f"迁移仅允许策略字段 {tuple(fields)}；发现不可迁移的差异：{prohibited}"
        )
    record = {
        "migrated_at": datetime.now(timezone.utc).isoformat(),
        "fields": [key for key in fields if key in mismatched],
        "note": note,
        "from": {key: stored.get(key) for key in fields if key in mismatched},
        "to": {key: current_manifest.get(key) for key in fields if key in mismatched},
    }
    for key in fields:
        stored[key] = current_manifest.get(key)
    stored.setdefault("policy_migrations", []).append(record)
    _atomic_write_json(path, stored)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, type=Path)
    parser.add_argument("--model", action="append", default=[], metavar="ROLE=MODEL")
    parser.add_argument("--threshold", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("run_manifest.json"))
    args = parser.parse_args()
    manifest = build_manifest(
        files=args.input,
        models=key_value(args.model),
        thresholds=key_value(args.threshold),
        repeat=args.repeat,
    )
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
