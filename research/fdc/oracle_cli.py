"""Audit one proposed allocation against a saved V3 episode, read only."""

import argparse
import json
from pathlib import Path

from finagent.store import Store

from .allocation_oracle import judge, validate_verdict


def _object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = judge(_object(args.episode), _object(args.proposal),
                   store=Store(args.data_dir) if args.data_dir else None)
    validate_verdict(result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, indent=2,
                                sort_keys=True) + "\n")
    print(json.dumps({"output": str(args.output), "episode_id": result["episode_id"],
                      "verdict": result["verdict"],
                      "failure_codes": [x["code"] for x in result["hard_failures"]],
                      "unknown_codes": [x["code"] for x in result["unknowns"]],
                      "training_scope": result["training_scope"],
                      "product_actionable": result["product_actionable"]},
                     ensure_ascii=False))
