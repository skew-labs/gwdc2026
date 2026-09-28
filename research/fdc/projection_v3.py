"""Exact synthetic research projection for typed withdrawal/shared-risk limits.

This produces no transaction or real executable recommendation. Unknown real
terms remain outside this research projector.
"""

import math
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from .validate_v3 import eligible


TOLERANCE = Decimal("0.00001")


def inspect_and_project(row: dict, probabilities: list[float]) -> dict:
    if row["source_mode"] != "synthetic_fixture":
        raise ValueError("V3 projector accepts synthetic fixtures only")
    candidates = row["candidates"]
    if len(probabilities) != len(candidates) + 1 or any(
        not math.isfinite(p) or p < 0 or p > 1 for p in probabilities
    ):
        raise ValueError("invalid probability vector")
    need = row["need"]
    at = datetime.fromisoformat(row["as_of"])
    amount = Decimal(need["budget"]["value"])
    immediate = Decimal(need["withdrawal"]["min_immediate"]["value"])
    supply_cap = min(amount - immediate,
                     amount * Decimal(need["risk"]["total_fraction"]))
    shared_cap = amount * Decimal(need["risk"]["shared_group_fraction"])
    proposed = [amount * Decimal(str(value)) for value in probabilities[:-1]]
    proposed_hold = amount * Decimal(str(probabilities[-1]))
    tolerance = amount * TOLERANCE

    def violations(allocations: list[Decimal], hold: Decimal) -> list[str]:
        findings = []
        if abs(sum(allocations, hold) - amount) > tolerance:
            findings.append("budget")
        if hold + tolerance < immediate:
            findings.append("withdrawal_immediate")
        if sum(allocations, Decimal(0)) > supply_cap + tolerance:
            findings.append("risk_total_or_reserve")
        exposure = defaultdict(Decimal)
        for allocation, candidate in zip(allocations, candidates):
            if allocation <= tolerance:
                continue
            if not eligible(candidate, need, at):
                findings.append("ineligible_or_withdrawal_terms")
            if allocation > Decimal(candidate["available_cash"]["value"]) + tolerance:
                findings.append("market_cash")
            for group in candidate["risk_groups"]:
                exposure[group] += allocation
        if any(value > shared_cap + tolerance for value in exposure.values()):
            findings.append("risk_shared")
        return sorted(set(findings))

    raw_reasons = violations(proposed, proposed_hold)
    remaining = supply_cap
    exposure = defaultdict(Decimal)
    accepted = [Decimal(0)] * len(candidates)
    for index in sorted(range(len(candidates)), key=lambda i: proposed[i], reverse=True):
        candidate = candidates[index]
        if not eligible(candidate, need, at):
            continue
        group_room = min((shared_cap - exposure[group] for group in candidate["risk_groups"]),
                         default=remaining)
        accepted[index] = max(Decimal(0), min(proposed[index], remaining,
                                             Decimal(candidate["available_cash"]["value"]),
                                             group_room))
        remaining -= accepted[index]
        for group in candidate["risk_groups"]:
            exposure[group] += accepted[index]
    hold = amount - sum(accepted, Decimal(0))
    post_reasons = violations(accepted, hold)
    if post_reasons:
        raise AssertionError("exact synthetic projection failed: " + ",".join(post_reasons))
    correction = (sum((abs(before - after) for before, after in zip(proposed, accepted)),
                      Decimal(0)) + abs(proposed_hold - hold)) / amount
    return {"raw_violation": bool(raw_reasons), "raw_reasons": raw_reasons,
            "corrected_allocations": [format(value, "f") for value in accepted],
            "corrected_hold": format(hold, "f"),
            "post_projection_violation": False,
            "abstain_required": not any(value > 0 for value in accepted),
            "correction_l1_fraction": float(correction),
            "status": "SYNTHETIC_RESEARCH_ONLY"}
