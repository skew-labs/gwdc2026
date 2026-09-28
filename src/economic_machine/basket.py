"""Bind a selected portfolio to a policy and state snapshot without execution.

The commitment is a reproducible review artifact. A source hash does not
authenticate a feed, and neither this module nor the registry moves assets.
"""

import re
from datetime import datetime
from decimal import Decimal, Inexact, Rounded, localcontext

from .portfolio import select_portfolio
from .state import normalize_state, state_root
from .values import MachineError, decstr, decimal, digest, ident, require_keys, utc


VERSION = "economic-basket-commitment-1"
POLICY_VERSION = "economic-basket-policy-1"


def _elapsed_us(later: datetime, earlier: datetime) -> int:
    delta = later - earlier
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def _policy(raw: dict, request: dict, state: dict, valid_until: str) -> tuple[dict, str]:
    require_keys(raw, {"schema_version", "policy_id", "owner_id", "network", "asset",
                       "allowed_products", "capital_fact_path", "max_capital",
                       "effective_at", "expires_at", "max_plan_age_ms"}, "BasketPolicy")
    if raw["schema_version"] != POLICY_VERSION:
        raise MachineError("unsupported basket policy version")
    if not isinstance(raw["policy_id"], str) or re.fullmatch(r"[0-9a-f]{64}", raw["policy_id"]) is None:
        raise MachineError("policy id must be a bytes32 hex value")
    for key in ("owner_id", "network", "asset"):
        ident(raw[key], "policy " + key)
        if raw[key] != request[key] or (key != "asset" and raw[key] != state[key]):
            raise MachineError("policy/request/state identity mismatch")
    products = raw["allowed_products"]
    if (not isinstance(products, list) or not 1 <= len(products) <= 16
            or any(not isinstance(name, str) for name in products)
            or products != sorted(set(products))):
        raise MachineError("allowed products must be a sorted unique list")
    for name in products:
        ident(name, "allowed product")
    ident(raw["capital_fact_path"], "capital fact path")
    maximum = decimal(raw["max_capital"])
    if maximum <= 0 or decimal(request["capital"]) > maximum:
        raise MachineError("portfolio capital exceeds policy")
    if type(raw["max_plan_age_ms"]) is not int or not 1 <= raw["max_plan_age_ms"] <= 86400000:
        raise MachineError("invalid plan lifetime")
    start, end = utc(raw["effective_at"]), utc(raw["expires_at"])
    at = datetime.fromisoformat(request["as_of"])
    expiry = datetime.fromisoformat(valid_until)
    if not datetime.fromisoformat(start) <= at < expiry <= datetime.fromisoformat(end):
        raise MachineError("basket policy or commitment is outside its validity window")
    if _elapsed_us(expiry, at) > raw["max_plan_age_ms"] * 1000:
        raise MachineError("basket commitment exceeds maximum lifetime")
    fact = state["facts"].get(raw["capital_fact_path"])
    if (fact is None or fact["quality"] != "VALID" or fact["unit"] != raw["asset"]
            or decimal(fact["value"]) < decimal(request["capital"])):
        raise MachineError("valid capital fact in the portfolio asset is required")
    observed = datetime.fromisoformat(fact["observed_at"])
    if not 0 <= _elapsed_us(at, observed) <= request["max_age_ms"] * 1000:
        raise MachineError("capital fact is stale or from the future")
    if _elapsed_us(expiry, observed) > request["max_age_ms"] * 1000:
        raise MachineError("capital fact expires before the basket commitment")
    normalized = {**raw, "max_capital": decstr(maximum),
                  "effective_at": start, "expires_at": end}
    return normalized, digest(normalized)


def commit_basket(request: dict, verdict: dict, state: dict, policy: dict,
                  *, valid_until: str) -> dict:
    """Replay the selector, then make a non-executable multi-leg commitment."""
    with localcontext() as context:
        context.prec = 256
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        return _commit_basket(request, verdict, state, policy, valid_until=valid_until)


def _commit_basket(request: dict, verdict: dict, state: dict, policy: dict,
                   *, valid_until: str) -> dict:
    expected = select_portfolio(request)
    if verdict != expected:
        raise MachineError("portfolio verdict does not replay from the request")
    if verdict["status"] != "SELECTED":
        raise MachineError("abstained portfolio has no basket to commit")
    snapshot = normalize_state(state)
    if (snapshot["owner_id"] != request["owner_id"]
            or snapshot["network"] != request["network"]
            or snapshot["as_of"] != utc(request["as_of"])):
        raise MachineError("portfolio request and state snapshot mismatch")
    until = utc(valid_until)
    normalized_policy, policy_hash = _policy(policy, request, snapshot, until)
    expiry = datetime.fromisoformat(until)
    for product in request["products"]:
        if _elapsed_us(expiry, datetime.fromisoformat(product["observed_at"])) > request["max_age_ms"] * 1000:
            raise MachineError("portfolio input expires before the basket commitment")
    selected = next(item for item in verdict["evaluated"]
                    if item["candidate_id"] == verdict["selected_candidate_id"])
    if not selected["eligible"]:
        raise MachineError("selected basket is ineligible")
    product_book = {item["product_id"]: item for item in request["products"]}
    if any(name not in normalized_policy["allowed_products"]
           for name, weight in selected["weights_bps"].items() if weight):
        raise MachineError("basket contains product outside policy")
    capital = decimal(request["capital"])
    legs = [{"product_id": name, "group": product_book[name]["group"],
             "weight_bps": weight, "amount": decstr(capital * Decimal(weight) / 10000),
             "source_hash": product_book[name]["source_hash"]}
            for name, weight in sorted(selected["weights_bps"].items()) if weight]
    cash_amount = decstr(capital * Decimal(selected["cash_bps"]) / 10000)
    if sum((decimal(item["amount"]) for item in legs), decimal(cash_amount)) != capital:
        raise MachineError("basket amounts do not conserve capital")
    basket = {"capital": decstr(capital), "asset": request["asset"],
              "candidate_id": selected["candidate_id"], "legs": legs,
              "cash_bps": selected["cash_bps"], "cash_amount": cash_amount}
    result = {"schema_version": VERSION, "owner_id": request["owner_id"],
              "network": request["network"], "asset": request["asset"],
              "as_of": utc(request["as_of"]), "valid_until": until,
              "policy_id": normalized_policy["policy_id"], "policy_hash": policy_hash,
              "state_root": state_root(snapshot), "request_hash": verdict["request_hash"],
              "verdict_hash": verdict["verdict_hash"], "basket": basket,
              "basket_hash": digest(basket), "execution_authority": "NONE",
              "execution_status": "NO_BASKET_EXECUTOR", "chain_status": "NOT_SUBMITTED"}
    result["commitment_hash"] = digest({"domain": VERSION, "payload": result})
    return result


def verify_basket(commitment: dict, request: dict, state: dict, policy: dict) -> bool:
    """Reject any changed commitment, policy, state or selection input."""
    if not isinstance(commitment, dict) or "valid_until" not in commitment:
        return False
    try:
        return commitment == commit_basket(request, select_portfolio(request), state, policy,
                                           valid_until=commitment["valid_until"])
    except (MachineError, KeyError, TypeError, ValueError):
        return False
