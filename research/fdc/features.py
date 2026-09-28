"""Versioned, deterministic Episode V2 to FDC tensors.

Approximate floats are learning features only. Exact Decimal inputs remain in
the episode and must be used again by a separate allocation verifier.
"""

import hashlib
import json
import math
from decimal import Decimal
from pathlib import Path

import torch

from .model import Config


FEATURE_VERSION = "fdc-features-0.1.0"
INPUT_KEYS = ("history", "history_valid", "times", "asset_features",
              "asset_valid", "relations", "evidence_embeddings",
              "evidence_valid", "constraints", "constraint_valid",
              "goal_features", "user_embedding")
MODE_INDEX = {"propose": 0, "ask": 1, "abstain": 2}
RISK_INDEX = {"cautious": 0, "balanced": 1, "growth": 2}


def _ratio(numerator: str | Decimal, denominator: Decimal) -> float:
    return float(Decimal(numerator) / max(denominator, Decimal("0.000001")))


def _hash_text(value: str, width: int) -> torch.Tensor:
    """Untrained lexical baseline; NEVER label as a pretrained language encoder."""
    result = torch.zeros(width, dtype=torch.float32)
    compact = " ".join(value.lower().split())[:4096]
    pieces = [compact[i:i + 3] for i in range(max(0, len(compact) - 2))]
    for piece in pieces:
        digest = hashlib.blake2b(piece.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % width
        sign = 1 if digest[4] % 2 else -1
        result[index] += sign
    norm = torch.linalg.vector_norm(result)
    return result / norm if norm > 0 else result


def _embed(role: str, value: str, width: int, lookup: dict | None) -> torch.Tensor:
    result = _hash_text(value, width) if lookup is None else lookup[(role, value)]
    if result.shape != (width,) or not torch.isfinite(result).all():
        raise ValueError("text encoder shape or values do not match feature contract")
    return result


def load_episodes(path: Path, split: str) -> list[dict]:
    rows = []
    with Path(path).open(encoding="utf-8") as stream:
        for raw in stream:
            if raw.strip():
                row = json.loads(raw)
                if row["split"] == split:
                    rows.append(row)
    return rows


def dimensions(rows: list[dict]) -> tuple[int, int, int]:
    if not rows:
        raise ValueError("empty episode split")
    return (max(len(row["candidates"]) for row in rows),
            max(1, max(len(c["history"]) for row in rows for c in row["candidates"])),
            max(1, max(len(row["evidence"]) for row in rows)))


def vectorize(row: dict, cfg: Config, dims: tuple[int, int, int],
              text_lookup: dict | None = None) -> dict:
    n, t, e = dims
    if len(row["candidates"]) > n or len(row["evidence"]) > e:
        raise ValueError("episode exceeds feature dimensions")
    amount = Decimal(row["needs"]["amount"])
    reserve = Decimal(row["needs"]["liquid_reserve"])
    if amount <= 0:
        raise ValueError("positive amount required")
    data = {
        "history": torch.zeros(n, t, cfg.numeric_features),
        "history_valid": torch.zeros(n, t, dtype=torch.bool),
        "times": torch.zeros(n, t, cfg.time_features),
        "asset_features": torch.zeros(n, cfg.asset_features),
        "asset_valid": torch.zeros(n, dtype=torch.bool),
        "relations": torch.zeros(n, n, cfg.relation_types),
        "evidence_embeddings": torch.zeros(e, cfg.text_width),
        "evidence_valid": torch.zeros(e, dtype=torch.bool),
        "constraints": torch.zeros(3, cfg.constraint_features),
        "constraint_valid": torch.ones(3, dtype=torch.bool),
        "goal_features": torch.zeros(cfg.goal_features),
        "user_embedding": _embed("query", row["text"], cfg.text_width, text_lookup),
        "target_allocations": torch.zeros(cfg.plans, n + 1),
        "target_plan_mask": torch.zeros(cfg.plans, dtype=torch.bool),
        "target_evidence": torch.zeros(cfg.plans, e + 1),
        "target_mode": torch.tensor(MODE_INDEX[row["target"]["mode"]], dtype=torch.long),
    }
    need = row["needs"]
    data["goal_features"][0] = math.log1p(float(amount)) / 12
    data["goal_features"][1] = float(reserve / amount)
    data["goal_features"][2] = need["horizon_days"] / 365
    data["goal_features"][3 + RISK_INDEX[need["risk"]]] = 1
    data["goal_features"][6] = 1 if need["asset"] == "USDT" else 0
    data["goal_features"][7] = 1 if need["asset"] == "USDD" else 0
    # Explicit typed constraint tokens. Remaining axes are reserved/versioned.
    data["constraints"][0, :3] = torch.tensor([1, float(reserve / amount), 0])
    data["constraints"][1, :3] = torch.tensor([0, 0, need["horizon_days"] / 365])
    data["constraints"][2, 3 + RISK_INDEX[need["risk"]]] = 1

    candidate_index = {}
    for i, candidate in enumerate(row["candidates"]):
        candidate_index[candidate["id"]] = i
        data["asset_valid"][i] = True
        features = data["asset_features"][i]
        features[0] = min(2, float(Decimal(candidate["supply_apy"])))
        features[1] = min(10, _ratio(candidate["available_cash"], amount))
        features[2] = math.log1p(float(Decimal(candidate["available_cash"]))) / 12
        features[3] = 1 if candidate["asset"] == need["asset"] else 0
        features[4] = 1 if candidate["status"] in {"active", "synthetic"} else 0
        features[5] = 1 if candidate["fee_quote"] is None else 0
        if candidate["fee_quote"] is not None:
            features[6] = min(10, _ratio(candidate["fee_quote"], amount))
        features[7] = 1 if candidate["network"] == "tron_mainnet" else 0
        features[8] = 1 if candidate["network"] == "tron_testnet" else 0
        features[9] = 1 if candidate["network"] == "synthetic" else 0
        history = candidate["history"]
        if len(history) > t:
            raise ValueError("history exceeds feature dimensions")
        for j, point in enumerate(history):
            age = point["age_days"]
            data["history_valid"][i, j] = True
            data["history"][i, j, 0] = min(2, float(Decimal(point["supply_apy"])))
            data["history"][i, j, 1] = min(10, _ratio(point["available_cash"], amount))
            data["times"][i, j, 0] = age / 365
            data["times"][i, j, 1] = math.log1p(age) / 6
            data["times"][i, j, 2] = 1 if age == 0 else 0
    for i, first in enumerate(row["candidates"]):
        for j, second in enumerate(row["candidates"]):
            if i == j:
                data["relations"][i, j, 0] = 1
            if first["asset"] == second["asset"]:
                data["relations"][i, j, 1] = 1
            if first["risk_group"] == second["risk_group"]:
                data["relations"][i, j, 2] = 1
            if first["network"] == second["network"]:
                data["relations"][i, j, 3] = 1
    evidence_index = {}
    for i, item in enumerate(row["evidence"]):
        evidence_index[item["id"]] = i
        data["evidence_valid"][i] = True
        data["evidence_embeddings"][i] = _embed(
            "passage", item["text"], cfg.text_width, text_lookup)
    for k, plan in enumerate(row["target"]["plans"][:cfg.plans]):
        data["target_plan_mask"][k] = True
        for candidate_id, value in plan["allocations"].items():
            data["target_allocations"][k, candidate_index[candidate_id]] = float(Decimal(value) / amount)
        data["target_allocations"][k, n] = float(Decimal(plan["reserve"]) / amount)
        for evidence_id in plan["evidence_ids"]:
            data["target_evidence"][k, evidence_index[evidence_id]] = 1
    if any(not torch.isfinite(value).all() for key, value in data.items()
           if key in INPUT_KEYS and torch.is_floating_point(value)):
        raise ValueError("nonfinite model feature")
    return data


def collate(rows: list[dict]) -> dict:
    return {key: torch.stack([row[key] for row in rows]) for key in rows[0]}
