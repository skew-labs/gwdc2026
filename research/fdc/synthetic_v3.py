"""Paired, typed synthetic finance fixtures with exact constrained labels.

Generated rates and plans are research fixtures, never market observations.
"""

import argparse
import hashlib
import json
import random
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


AT = datetime(2026, 9, 24, tzinfo=timezone.utc)
CHANGES = ("need.budget.value", "need.withdrawal.min_immediate.value",
           "need.withdrawal.latest_day", "need.withdrawal.max_notice_days",
           "need.risk.shared_group_fraction", "need.horizon_days",
           "need.withdrawal.latest_day")


def fmt(value: Decimal) -> str:
    return format(value, "f")


def split(family: str) -> str:
    bucket = int(hashlib.sha256(family.encode()).hexdigest()[:8], 16) % 10
    return "train" if bucket < 8 else "development" if bucket == 8 else "synthetic_holdout"


def money(value: Decimal, unit: str) -> dict:
    return {"value": fmt(value), "unit": unit}


def label(need: dict, candidates: list[dict]) -> dict:
    withdrawal = need["withdrawal"]
    missing = [name for name in ("latest_day", "max_notice_days")
               if withdrawal[name] is None]
    if missing:
        return {"mode": "ask", "question_fields": ["withdrawal." + name for name in missing],
                "abstain_reasons": [], "plans": [], "oracle_kind": "synthetic_rule"}
    amount = Decimal(need["budget"]["value"])
    immediate = Decimal(withdrawal["min_immediate"]["value"])
    total_limit = min(amount - immediate, amount * Decimal(need["risk"]["total_fraction"]))
    shared_limit = amount * Decimal(need["risk"]["shared_group_fraction"])
    available = [item for item in candidates
                 if item["asset"] == need["budget"]["unit"]
                 and item["status"] == "synthetic"
                 and item["lock_days"] <= withdrawal["latest_day"]
                 and item["notice_days"] <= withdrawal["max_notice_days"]
                 and Decimal(item["available_cash"]["value"]) > 0]
    if total_limit <= 0 or not available:
        return {"mode": "abstain", "question_fields": [],
                "abstain_reasons": ["NO_ELIGIBLE_ALLOCATABLE_CANDIDATE"],
                "plans": [], "oracle_kind": "synthetic_rule"}
    ordered = sorted(available, key=lambda item: (-Decimal(item["supply_apy"]), item["id"]))
    plans = []
    signatures = set()
    for plan_id, desired in (("liquidity_priority", min(total_limit, amount / Decimal(4))),
                             ("yield_priority", total_limit)):
        remaining = desired
        exposure: dict[str, Decimal] = {}
        allocations = {}
        dependencies = {"budget", "source_freshness", "withdrawal_deadline", "withdrawal_notice"}
        for item in ordered:
            group_room = min((shared_limit - exposure.get(group, Decimal(0))
                              for group in item["risk_groups"]), default=remaining)
            size = max(Decimal(0), min(remaining,
                                       Decimal(item["available_cash"]["value"]), group_room))
            if size <= 0:
                continue
            allocations[item["id"]] = fmt(size)
            remaining -= size
            for group in item["risk_groups"]:
                exposure[group] = exposure.get(group, Decimal(0)) + size
            if size == Decimal(item["available_cash"]["value"]):
                dependencies.add("market_cash")
            if any(exposure[group] == shared_limit for group in item["risk_groups"]):
                dependencies.add("risk_shared")
            if remaining <= 0:
                break
        invested = sum((Decimal(value) for value in allocations.values()), Decimal(0))
        if invested <= 0:
            continue
        hold = amount - invested
        if invested == amount * Decimal(need["risk"]["total_fraction"]):
            dependencies.add("risk_total")
        if hold == immediate:
            dependencies.add("withdrawal_immediate")
        signature = (tuple(sorted(allocations.items())), fmt(hold))
        if signature in signatures:
            continue
        signatures.add(signature)
        evidence = sorted({evidence_id for item in candidates if item["id"] in allocations
                           for evidence_id in item["evidence_ids"]})
        plans.append({"plan_id": plan_id, "allocations": allocations,
                      "hold": fmt(hold), "evidence_ids": evidence,
                      "dependency_ids": sorted(dependencies)})
    if not plans:
        return {"mode": "abstain", "question_fields": [],
                "abstain_reasons": ["SHARED_RISK_OR_CASH_CAP_ZERO"],
                "plans": [], "oracle_kind": "synthetic_rule"}
    return {"mode": "propose", "question_fields": [], "abstain_reasons": [],
            "plans": plans, "oracle_kind": "synthetic_rule"}


def decision_signature(target: dict) -> tuple:
    return (target["mode"], tuple(target["question_fields"]),
            tuple(target["abstain_reasons"]),
            tuple((plan["plan_id"], tuple(sorted(plan["allocations"].items())), plan["hold"])
                  for plan in target["plans"]))


def generate(path: Path, families: int = 1000, seed: int = 20260924) -> dict:
    if not 1 <= families <= 100_000:
        raise ValueError("families outside supported range")
    rng = random.Random(seed)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    effects = {}
    modes = {}
    splits = {}
    with path.open("x", encoding="utf-8") as output:
        for index in range(families):
            family = f"typed-fixture-{seed}-{index:06d}"
            unit = rng.choice(("USDT", "USDD"))
            amount = Decimal(rng.randrange(400, 5001))
            immediate = Decimal(rng.randrange(0, int(amount * Decimal("0.35")) + 1))
            need = {
                "budget": money(amount, unit), "horizon_days": 30,
                "withdrawal": {"min_immediate": money(immediate, unit),
                               "latest_day": None if index % len(CHANGES) == 6 else 90,
                               "max_notice_days": 14},
                "risk": {"total_fraction": "0.75", "shared_group_fraction": "0.45"},
            }
            candidates = []
            evidence = []
            for number, (lock, notice, group) in enumerate(((0, 0, "shared-A"),
                                                             (14, 7, "shared-A"),
                                                             (45, 2, "shared-B"))):
                candidate_id = f"{family}-market-{number}"
                rate = Decimal(rng.randrange(100, 1801)) / Decimal(10000)
                cash = Decimal(rng.randrange(int(amount * Decimal("0.25")),
                                             int(amount * Decimal("1.5")) + 1))
                evidence_id = f"e-{number}"
                candidates.append({"id": candidate_id, "asset": unit,
                                   "status": ("legacy" if index % 11 == 0 or
                                              (index % 13 == 0 and number == 1)
                                              else "synthetic"),
                                   "supply_apy": fmt(rate), "available_cash": money(cash, unit),
                                   "lock_days": lock, "notice_days": notice,
                                   "observed_at": (AT - timedelta(hours=2)).isoformat(),
                                   "valid_until": (AT + timedelta(days=1)).isoformat(),
                                   "risk_groups": [group], "history": [],
                                   "source_ref": "fixture:" + family,
                                   "evidence_ids": [evidence_id]})
                evidence.append({"id": evidence_id,
                                 "text": (f"Synthetic market {number}: APY {fmt(rate)}, cash {fmt(cash)} "
                                          f"{unit}, lock {lock} days, notice {notice} days"),
                                 "source_ref": "fixture:" + family,
                                 "locator": f"/candidates/{number}"})
            base_target = label(need, candidates)
            altered = deepcopy(need)
            change = CHANGES[index % len(CHANGES)]
            if index % len(CHANGES) == 0:
                altered["budget"]["value"] = fmt(amount * Decimal("1.5"))
            elif index % len(CHANGES) == 1:
                altered["withdrawal"]["min_immediate"]["value"] = fmt(
                    min(amount, immediate + amount * Decimal("0.25")))
            elif index % len(CHANGES) == 2:
                altered["withdrawal"]["latest_day"] = 7
            elif index % len(CHANGES) == 3:
                altered["withdrawal"]["max_notice_days"] = 0
            elif index % len(CHANGES) == 4:
                altered["risk"]["shared_group_fraction"] = "0.15"
            elif index % len(CHANGES) == 5:
                altered["horizon_days"] = 60
            else:
                altered["withdrawal"]["latest_day"] = 30
            other_target = label(altered, candidates)
            effect = ("changed" if decision_signature(base_target) != decision_signature(other_target)
                      else "unchanged")
            effects[effect] = effects.get(effect, 0) + 1
            family_split = split(family)
            for revision, selected_need, target in ((0, need, base_target),
                                                    (1, altered, other_target)):
                row = {"schema_version": "3.0.0", "episode_id": f"{family}-r{revision}",
                       "scenario_family": family,
                       "parent_episode_id": f"{family}-r0" if revision else None,
                       "changed_field": change if revision else None,
                       "expected_pair_effect": effect if revision else None,
                       "as_of": AT.isoformat(), "split": family_split,
                       "source_mode": "synthetic_fixture",
                       "review_status": "synthetic_unreviewed",
                       "rights_status": "synthetic_generated",
                       "text": (f"{selected_need['budget']['value']} {unit}, 즉시 보유 "
                                f"{selected_need['withdrawal']['min_immediate']['value']} {unit}, "
                                f"{selected_need['horizon_days']}일, 인출 기한 "
                                f"{selected_need['withdrawal']['latest_day']}일"),
                       "need": selected_need, "candidates": candidates,
                       "evidence": evidence, "target": target}
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                modes[target["mode"]] = modes.get(target["mode"], 0) + 1
                splits[family_split] = splits.get(family_split, 0) + 1
    return {"path": str(path), "families": families, "episodes": 2 * families,
            "splits": splits, "modes": modes, "counterfactual_pairs": effects,
            "status": "SYNTHETIC_RESEARCH_ONLY"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--families", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260924)
    arguments = parser.parse_args()
    print(json.dumps(generate(arguments.output, arguments.families, arguments.seed),
                     ensure_ascii=False))
