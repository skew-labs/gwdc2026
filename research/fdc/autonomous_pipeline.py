"""Run fact mining and formal-optimum mining from one durable hourly service."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .auto_cases import run_once as mine_facts
from .formal_optima import run_once as mine_optima


def _save(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def run_once(data_dir: Path, output_dir: Path, release_root: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    stage = "fact_cases"
    try:
        facts = mine_facts(data_dir, output_dir, release_root)
        stage = "formal_optima"
        optima = mine_optima(data_dir, output_dir / "formal")
    except Exception as exc:
        _save(output_dir / "pipeline_latest_attempt.json", {
            "status": "RUN_FAILED", "stage": stage,
            "at": datetime.now(timezone.utc).isoformat(),
            "error_type": type(exc).__name__, "error": str(exc)[:300],
            "real_customer_optimum_count": 0, "training_enabled": False})
        raise
    result = {"status": "RESEARCH_DATA_PIPELINE_COMPLETE", "facts": facts,
              "formal_optima": optima, "real_customer_optimum_count": 0,
              "training_enabled": False, "product_actionable": False}
    _save(output_dir / "pipeline_latest.json", result)
    _save(output_dir / "pipeline_latest_attempt.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--release-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run_once(args.data_dir, args.output_dir, args.release_root),
                     ensure_ascii=False, indent=2))
