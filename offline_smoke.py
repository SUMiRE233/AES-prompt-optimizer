"""Public, synthetic, no-network smoke path for AES-prompt-optimizer."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from aes_badcase_miner import AESBadcaseMiner
from project_checks import index_rows, validate_scoring_artifact


ROOT = Path(__file__).resolve().parent
DEFAULT_FIXTURE = ROOT / "fixtures" / "synthetic_scoring_results.json"


def run(fixture_path: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    rows = json.loads(fixture_path.read_text(encoding="utf-8"))
    origin = index_rows(rows, "synthetic fixture")
    validate_scoring_artifact(rows, origin, "synthetic fixture")

    with tempfile.TemporaryDirectory() as temp_dir:
        output = Path(temp_dir) / "badcases.json"
        result = AESBadcaseMiner(str(fixture_path)).run(str(output))
        if not output.exists():
            raise RuntimeError("Offline smoke did not produce a badcase artifact")

    dimensions = set(result["statistics"]["E_residual"])
    expected = {"content", "expression", "structure"}
    if dimensions != expected:
        raise RuntimeError(f"Unexpected E dimensions: {sorted(dimensions)}")

    return {
        "status": "pass",
        "network_used": False,
        "fixture": str(fixture_path.relative_to(ROOT)),
        "rows": len(rows),
        "dimensions": sorted(dimensions),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    args = parser.parse_args()
    print(json.dumps(run(args.fixture.resolve()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
