"""Evaluate exact post-model synthetic feasibility without touching live routes."""

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import torch

from .baseline import FlatMLP
from .encoder import encode_episodes
from .features_v3 import INPUT_KEYS, load_episodes, vectorize
from .model import Config, FinancialDecisionCore
from .projection_v3 import inspect_and_project
from .train_v3 import _ordered_rows
from .validate_v3 import validate


def evaluate(dataset: Path, run_dir: Path, schema: Path) -> dict:
    audit = validate(dataset, schema)
    report = json.loads((run_dir / "report.json").read_text())
    checkpoint = torch.load(run_dir / "checkpoint.pt", map_location="cpu",
                            weights_only=True)
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    if digest != report["dataset_sha256"] or checkpoint["status"] != "SYNTHETIC_RESEARCH_ONLY":
        raise ValueError("research checkpoint/dataset identity mismatch")
    rows = load_episodes(dataset, "synthetic_holdout")
    pairs = _ordered_rows(rows)
    cfg = Config(**checkpoint["config"])
    dims = tuple(checkpoint["dimensions"])
    kind = report["text_encoder"]["kind"]
    lookup = encode_episodes(rows) if kind == "e5-small" else None
    model = (FinancialDecisionCore(cfg) if checkpoint["architecture"] == "fdc"
             else FlatMLP(cfg, dims, constraints=8))
    model.load_state_dict(checkpoint["state_dict"])
    model.eval()
    proposed = 0
    raw_violations = 0
    corrections = 0.0
    post_errors = 0
    abstain_required = 0
    corrected_mae = 0.0
    pair_effects = {key: {"count": 0, "delta_mae": 0.0}
                    for key in ("changed", "unchanged")}
    with torch.no_grad():
        for base, other in pairs:
            corrected_pairs = []
            target_pairs = []
            for row in (base, other):
                if row["target"]["mode"] != "propose":
                    corrected_pairs.append(None)
                    target_pairs.append(None)
                    continue
                item = vectorize(row, cfg, dims, lookup)
                output = model(**{name: item[name].unsqueeze(0) for name in INPUT_KEYS})
                probabilities = output["allocation_logits"].softmax(-1)[0, 0]
                p = probabilities[:len(row["candidates"])].tolist() + [probabilities[-1].item()]
                result = inspect_and_project(row, p)
                proposed += 1
                raw_violations += int(result["raw_violation"])
                post_errors += int(result["post_projection_violation"])
                abstain_required += int(result["abstain_required"])
                corrections += result["correction_l1_fraction"]
                amount = Decimal(row["need"]["budget"]["value"])
                corrected = [float(Decimal(value) / amount)
                             for value in result["corrected_allocations"]]
                corrected.append(float(Decimal(result["corrected_hold"]) / amount))
                plan = row["target"]["plans"][0]
                target = [float(Decimal(plan["allocations"].get(candidate["id"], "0")) / amount)
                          for candidate in row["candidates"]]
                target.append(float(Decimal(plan["hold"]) / amount))
                corrected_mae += sum(abs(a - b) for a, b in zip(corrected, target)) / len(target)
                corrected_pairs.append(corrected)
                target_pairs.append(target)
            if all(item is not None for item in corrected_pairs):
                effect = other["expected_pair_effect"]
                delta = [b - a for a, b in zip(*corrected_pairs)]
                target_delta = [b - a for a, b in zip(*target_pairs)]
                pair_effects[effect]["count"] += 1
                pair_effects[effect]["delta_mae"] += sum(
                    abs(a - b) for a, b in zip(delta, target_delta)) / len(delta)
    return {"status": "SYNTHETIC_RESEARCH_ONLY", "dataset_sha256": digest,
            "audit": audit, "checkpoint": str(run_dir / "checkpoint.pt"),
            "propose_episodes": proposed,
            "raw_violation_rate": raw_violations / proposed if proposed else None,
            "post_projection_violation_rate": post_errors / proposed if proposed else None,
            "abstain_after_projection": abstain_required,
            "mean_correction_l1_fraction": corrections / proposed if proposed else None,
            "corrected_allocation_mae": corrected_mae / proposed if proposed else None,
            "counterfactual": {key: {"comparable_pairs": value["count"],
                                     "corrected_delta_mae": value["delta_mae"] / value["count"]
                                     if value["count"] else None}
                               for key, value in pair_effects.items()},
            "limitations": ["Synthetic known constraints only", "No fee, wallet or live route validation"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--schema", type=Path, required=True)
    arguments = parser.parse_args()
    output = evaluate(arguments.dataset, arguments.run_dir, arguments.schema)
    path = arguments.run_dir / "projection_report.json"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"projection_report": str(path), **{
        key: output[key] for key in ("raw_violation_rate", "post_projection_violation_rate",
                                "mean_correction_l1_fraction", "corrected_allocation_mae",
                                "counterfactual")}}, ensure_ascii=False))
