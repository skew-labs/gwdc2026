"""Separate amount, unit, time, withdrawal and shared-risk features for FDC."""

import json
import math
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import torch

from .features import _embed
from .model import Config


FEATURE_VERSION = "fdc-typed-v3-0.1.0"
DEPENDENCY_INDEX = {name: index for index, name in enumerate((
    "budget", "withdrawal_immediate", "withdrawal_deadline",
    "withdrawal_notice", "risk_total", "risk_shared",
    "market_cash", "source_freshness"))}
QUESTION_INDEX = {name: index for index, name in enumerate((
    "withdrawal.latest_day", "withdrawal.max_notice_days",
    "withdrawal.min_immediate", "budget.value", "budget.unit", "as_of"))}
MODE_INDEX = {"propose": 0, "ask": 1, "abstain": 2}
INPUT_KEYS = ("history", "history_valid", "times", "asset_features",
              "asset_valid", "relations", "evidence_embeddings",
              "evidence_valid", "constraints", "constraint_valid",
              "goal_features", "user_embedding")


def load_episodes(path: Path, split: str) -> list[dict]:
    with Path(path).open(encoding="utf-8") as stream:
        return [row for raw in stream if raw.strip()
                if (row := json.loads(raw))["split"] == split]


def dimensions(rows: list[dict]) -> tuple[int, int, int]:
    if not rows:
        raise ValueError("empty split")
    return (max(len(row["candidates"]) for row in rows),
            max(1, max(len(item["history"]) for row in rows for item in row["candidates"])),
            max(1, max(len(row["evidence"]) for row in rows)))


def vectorize(row: dict, cfg: Config, dims: tuple[int, int, int],
              text_lookup: dict | None = None) -> dict:
    n, t, e = dims
    if cfg.numeric_features < 2 or cfg.asset_features < 12 or cfg.goal_features < 11:
        raise ValueError("FDC config too narrow for typed finance input")
    if cfg.constraint_features < 12 or cfg.relation_types < 6 or cfg.question_fields < len(QUESTION_INDEX):
        raise ValueError("FDC config too narrow for constraint/output contract")
    if len(row["candidates"]) > n or len(row["evidence"]) > e:
        raise ValueError("episode exceeds feature dimensions")
    need = row["need"]
    unit = need["budget"]["unit"]
    amount = Decimal(need["budget"]["value"])
    withdrawal = need["withdrawal"]
    immediate = Decimal(withdrawal["min_immediate"]["value"])
    at = datetime.fromisoformat(row["as_of"])
    data = {
        "history": torch.zeros(n, t, cfg.numeric_features),
        "history_valid": torch.zeros(n, t, dtype=torch.bool),
        "times": torch.zeros(n, t, cfg.time_features),
        "asset_features": torch.zeros(n, cfg.asset_features),
        "asset_valid": torch.zeros(n, dtype=torch.bool),
        "relations": torch.zeros(n, n, cfg.relation_types),
        "evidence_embeddings": torch.zeros(e, cfg.text_width),
        "evidence_valid": torch.zeros(e, dtype=torch.bool),
        "constraints": torch.zeros(len(DEPENDENCY_INDEX), cfg.constraint_features),
        "constraint_valid": torch.ones(len(DEPENDENCY_INDEX), dtype=torch.bool),
        "goal_features": torch.zeros(cfg.goal_features),
        "user_embedding": _embed("query", row["text"], cfg.text_width, text_lookup),
        "target_allocations": torch.zeros(cfg.plans, n + 1),
        "target_plan_mask": torch.zeros(cfg.plans, dtype=torch.bool),
        "target_evidence": torch.zeros(cfg.plans, e + 1),
        "target_dependency": torch.zeros(cfg.plans, len(DEPENDENCY_INDEX) + 1),
        "target_question": torch.zeros(cfg.question_fields),
        "target_mode": torch.tensor(MODE_INDEX[row["target"]["mode"]], dtype=torch.long),
    }
    goal = data["goal_features"]
    goal[0] = math.log1p(float(amount)) / 12
    goal[1] = float(immediate / amount)
    goal[2] = need["horizon_days"] / 365
    goal[3] = 0 if withdrawal["latest_day"] is None else withdrawal["latest_day"] / 365
    goal[4] = 1 if withdrawal["latest_day"] is None else 0
    goal[5] = 0 if withdrawal["max_notice_days"] is None else withdrawal["max_notice_days"] / 365
    goal[6] = 1 if withdrawal["max_notice_days"] is None else 0
    goal[7] = float(Decimal(need["risk"]["total_fraction"]))
    goal[8] = float(Decimal(need["risk"]["shared_group_fraction"]))
    goal[9] = 1 if unit == "USDT" else 0
    goal[10] = 1 if unit == "USDD" else 0
    day_of_year = at.timetuple().tm_yday
    goal[11] = math.sin(2 * math.pi * day_of_year / 366)
    goal[12] = math.cos(2 * math.pi * day_of_year / 366)
    goal[13] = (at.year - 2020) / 20
    for index in DEPENDENCY_INDEX.values():
        data["constraints"][index, index] = 1
    data["constraints"][0, 8] = goal[0]
    data["constraints"][1, 8] = goal[1]
    data["constraints"][2, 8] = goal[3]
    data["constraints"][2, 9] = goal[4]
    data["constraints"][3, 8] = goal[5]
    data["constraints"][3, 9] = goal[6]
    data["constraints"][4, 8] = goal[7]
    data["constraints"][5, 8] = goal[8]
    data["constraints"][6, 8] = sum(float(Decimal(item["available_cash"]["value"]) / amount)
                                       for item in row["candidates"]) / max(1, n)
    data["constraints"][7, 8] = sum(item["observed_at"] is not None
                                       for item in row["candidates"]) / max(1, n)
    evidence_index = {}
    for index, item in enumerate(row["evidence"]):
        evidence_index[item["id"]] = index
        data["evidence_valid"][index] = True
        data["evidence_embeddings"][index] = _embed(
            "passage", item["text"], cfg.text_width, text_lookup)
    candidate_index = {}
    for index, item in enumerate(row["candidates"]):
        candidate_index[item["id"]] = index
        data["asset_valid"][index] = True
        feature = data["asset_features"][index]
        cash = Decimal(item["available_cash"]["value"])
        feature[0] = min(2, float(Decimal(item["supply_apy"])))
        feature[1] = min(10, float(cash / amount))
        feature[2] = math.log1p(float(cash)) / 12
        feature[3] = 1 if item["asset"] == unit else 0
        feature[4] = 1 if item["status"] in {"active", "synthetic"} else 0
        feature[5] = 0 if item["lock_days"] is None else item["lock_days"] / 365
        feature[6] = 0 if item["notice_days"] is None else item["notice_days"] / 365
        feature[7] = 1 if item["lock_days"] is None else 0
        feature[8] = 1 if item["notice_days"] is None else 0
        feature[9] = 1 if item["observed_at"] is None else 0
        feature[10] = (0 if item["observed_at"] is None else
                       min(1, max(0, (at - datetime.fromisoformat(item["observed_at"])).total_seconds())
                           / (365 * 86400)))
        feature[11] = len(item["risk_groups"]) / 8
        feature[12] = 1 if item["valid_until"] is None else 0
        feature[13] = (0 if item["valid_until"] is None else
                       max(-1, min(1, (datetime.fromisoformat(item["valid_until"]) - at).total_seconds()
                                       / (365 * 86400))))
        for step, point in enumerate(item["history"]):
            if step >= t:
                raise ValueError("history exceeds dimension")
            data["history_valid"][index, step] = True
            data["history"][index, step, 0] = float(Decimal(point["supply_apy"]))
            data["history"][index, step, 1] = min(
                10, float(Decimal(point["available_cash"]["value"]) / amount))
            data["times"][index, step, 0] = point["age_days"] / 365
            data["times"][index, step, 1] = math.log1p(point["age_days"]) / 6
            data["times"][index, step, 2] = 1 if point["age_days"] == 0 else 0
    for first, a in enumerate(row["candidates"]):
        for second, b in enumerate(row["candidates"]):
            relation = data["relations"][first, second]
            relation[0] = 1 if first == second else 0
            relation[1] = 1 if a["asset"] == b["asset"] else 0
            relation[2] = 1 if set(a["risk_groups"]) & set(b["risk_groups"]) else 0
            relation[3] = 1 if a["source_ref"] == b["source_ref"] else 0
            relation[4] = 1 if a["status"] == b["status"] else 0
            relation[5] = 1 if a["lock_days"] == b["lock_days"] else 0
    for field in row["target"]["question_fields"]:
        data["target_question"][QUESTION_INDEX[field]] = 1
    for slot, plan in enumerate(row["target"]["plans"][:cfg.plans]):
        data["target_plan_mask"][slot] = True
        for candidate_id, value in plan["allocations"].items():
            data["target_allocations"][slot, candidate_index[candidate_id]] = float(Decimal(value) / amount)
        data["target_allocations"][slot, n] = float(Decimal(plan["hold"]) / amount)
        for evidence_id in plan["evidence_ids"]:
            data["target_evidence"][slot, evidence_index[evidence_id]] = 1
        for dependency_id in plan["dependency_ids"]:
            data["target_dependency"][slot, DEPENDENCY_INDEX[dependency_id]] = 1
    return data


def collate_pairs(pairs: list[tuple[dict, dict]]) -> dict:
    rows = [row for pair in pairs for row in pair]
    return {key: torch.stack([row[key] for row in rows]) for key in rows[0]}
