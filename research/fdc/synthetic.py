"""Clearly labeled synthetic learning fixtures. Not an independent gold set."""

import hashlib
import argparse
import json
import random
from decimal import Decimal
from pathlib import Path


RISK_CAP = {"cautious": Decimal("0.50"),
            "balanced": Decimal("0.75"), "growth": Decimal("1.00")}


def _split(family: str) -> str:
    bucket = int(hashlib.sha256(family.encode()).hexdigest()[:8], 16) % 10
    return "train" if bucket < 8 else "development" if bucket == 8 else "synthetic_holdout"


def _text(value: Decimal) -> str:
    return format(value, "f")


def _oracle(need: dict, candidates: list[dict]) -> tuple[str, list[dict], str]:
    amount = Decimal(need["amount"])
    reserve = Decimal(need["liquid_reserve"])
    cap = min(amount - reserve, amount * RISK_CAP[need["risk"]])
    valid = [candidate for candidate in candidates
             if candidate["status"] in {"active", "synthetic"}
             and Decimal(candidate["available_cash"]) > 0
             and candidate["asset"] == need["asset"]]
    if cap <= 0 or not valid:
        return "abstain", [], "No investable amount or eligible market"
    best = max(valid, key=lambda item: (
        Decimal(item["supply_apy"]), Decimal(item["available_cash"])))
    cap = min(cap, Decimal(best["available_cash"]))
    cautious = min(cap, amount * Decimal("0.25"))
    sizes = list(dict.fromkeys((cautious, cap)))
    plans = []
    for size in sizes:
        if size <= 0:
            continue
        plans.append({"allocations": {candidate["id"]: _text(size if candidate is best else Decimal(0))
                                      for candidate in candidates},
                      "reserve": _text(amount - size), "evidence_ids": ["e-rate"]})
    return "propose", plans, "Synthetic known-rate, single-asset allocation oracle"


def generate(path: Path, families: int = 1000, seed: int = 20260924) -> dict:
    if not 1 <= families <= 100_000:
        raise ValueError("families out of supported range")
    rng = random.Random(seed)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    splits = {}
    with path.open("x", encoding="utf-8") as output:
        for index in range(families):
            family = f"fixture-{seed}-{index:06d}"
            split = _split(family)
            amount = Decimal(rng.randrange(200, 5001))
            reserve = Decimal(rng.randrange(0, int(amount) + 1))
            risk = rng.choice(tuple(RISK_CAP))
            horizon = rng.randrange(7, 181)
            asset = rng.choice(("USDT", "USDD"))
            rates = [Decimal(rng.randrange(1, 1801)) / Decimal(10000) for _ in range(2)]
            liquidity = [Decimal(rng.randrange(0, 10001)) for _ in range(2)]
            candidates = []
            for candidate_index in range(2):
                candidate_id = f"{family}-market-{candidate_index}"
                rate = rates[candidate_index]
                cash = liquidity[candidate_index]
                history = [{"age_days": age,
                            "supply_apy": _text(rate if age == 0 else max(
                                Decimal(0), rate + Decimal(rng.randrange(-200, 201)) / Decimal(10000))),
                            "available_cash": _text(cash)} for age in (28, 14, 7, 0)]
                candidates.append({"id": candidate_id, "asset": asset,
                                   "network": "synthetic", "status": "synthetic",
                                   "supply_apy": _text(rate), "available_cash": _text(cash),
                                   "fee_quote": "0", "history": history,
                                   "risk_group": f"synthetic-risk-{candidate_index}",
                                   "source_ref": "fixture:" + family})
            for revision, modified_reserve in enumerate((reserve, min(amount, reserve + amount / Decimal(4)))):
                need = {"asset": asset, "amount": _text(amount),
                        "liquid_reserve": _text(modified_reserve),
                        "horizon_days": horizon, "risk": risk}
                mode, plans, reason = _oracle(need, candidates)
                record = {
                    "schema_version": "2.0.0", "episode_id": f"{family}-r{revision}",
                    "scenario_family": family, "as_of": "2026-09-24T00:00:00Z",
                    "split": split, "source_mode": "synthetic_fixture",
                    "review_status": "synthetic_unreviewed",
                    "rights_status": "synthetic_generated",
                    "utterance_origin": "synthetic_template", "source_release": None,
                    "text": f"{amount} {asset} 중 {modified_reserve}는 바로 쓰고 {horizon}일 운용하고 싶어",
                    "needs": need, "candidates": candidates,
                    "evidence": [{"id": "e-rate", "text": "Synthetic market rates and cash only",
                                  "source_ref": "fixture:" + family, "locator": "/candidates"}],
                    "target": {"mode": mode, "plans": plans, "reason": reason,
                               "oracle_kind": "synthetic_rule"},
                }
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
                splits[split] = splits.get(split, 0) + 1
    return {"episodes": count, "families": families, "splits": splits,
            "source_mode": "synthetic_fixture", "path": str(path)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate labeled synthetic research fixtures")
    parser.add_argument("output", type=Path)
    parser.add_argument("--families", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260924)
    arguments = parser.parse_args()
    print(json.dumps(generate(arguments.output, arguments.families, arguments.seed),
                     ensure_ascii=False))
