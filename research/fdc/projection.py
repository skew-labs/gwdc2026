"""Exact synthetic-only allocation projection and violation diagnostics.

This knows no real fees, wallet balance, route, or token approvals. Never use it
to authorize a transaction.
"""

from decimal import Decimal


RISK_CAP = {"cautious": Decimal("0.50"), "balanced": Decimal("0.75"),
            "growth": Decimal("1.00")}
RELATIVE_TOLERANCE = Decimal("0.000001")


def inspect_and_project(row: dict, probabilities: list[float]) -> dict:
    if row["source_mode"] != "synthetic_fixture":
        raise ValueError("projection diagnostics accept synthetic fixtures only")
    candidates = row["candidates"]
    if len(probabilities) != len(candidates) + 1:
        raise ValueError("probability/candidate length mismatch")
    if any(not 0 <= p <= 1 for p in probabilities):
        raise ValueError("invalid model probability")
    amount = Decimal(row["needs"]["amount"])
    tolerance = amount * RELATIVE_TOLERANCE
    reserve_required = Decimal(row["needs"]["liquid_reserve"])
    supply_cap = min(amount - reserve_required,
                     amount * RISK_CAP[row["needs"]["risk"]])
    proposed = [amount * Decimal(str(p)) for p in probabilities[:-1]]
    proposed_hold = amount * Decimal(str(probabilities[-1]))
    reasons = []
    if proposed_hold + tolerance < reserve_required:
        reasons.append("reserve")
    if sum(proposed, Decimal(0)) > supply_cap + tolerance:
        reasons.append("risk_or_liquidity_cap")
    if abs(sum(proposed, proposed_hold) - amount) > tolerance:
        reasons.append("budget")
    for i, candidate in enumerate(candidates):
        if proposed[i] <= tolerance:
            continue
        if candidate["asset"] != row["needs"]["asset"] or candidate["status"] not in {
            "active", "synthetic"
        }:
            reasons.append("ineligible_candidate")
        if proposed[i] > Decimal(candidate["available_cash"]) + tolerance:
            reasons.append("market_cash")
    remaining = supply_cap
    corrected = [Decimal(0)] * len(candidates)
    for i in sorted(range(len(candidates)), key=lambda j: proposed[j], reverse=True):
        candidate = candidates[i]
        if candidate["asset"] != row["needs"]["asset"] or candidate["status"] not in {
            "active", "synthetic"
        }:
            continue
        accepted = min(proposed[i], Decimal(candidate["available_cash"]), remaining)
        corrected[i] = max(Decimal(0), accepted)
        remaining -= corrected[i]
    corrected_hold = amount - sum(corrected, Decimal(0))
    if corrected_hold < reserve_required or sum(corrected, corrected_hold) != amount:
        raise AssertionError("projection failed exact budget/reserve conservation")
    correction = (sum((abs(a - b) for a, b in zip(proposed, corrected)), Decimal(0))
                  + abs(proposed_hold - corrected_hold)) / amount
    return {"raw_violation": bool(reasons), "reasons": sorted(set(reasons)),
            "corrected_allocations": [format(value, "f") for value in corrected],
            "corrected_hold": format(corrected_hold, "f"),
            "correction_l1_fraction": float(correction)}
