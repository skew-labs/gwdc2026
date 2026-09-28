"""Deterministic, bounded portfolio candidate selection; no trade authority.

Scores only the supplied candidate set. Expected returns, stress losses and
cost estimates are assumptions supplied by the caller, not verified facts.
"""

from datetime import datetime
from decimal import Decimal, Inexact, Rounded, localcontext

from .values import MachineError, decstr, decimal, digest, ident, require_keys, utc


VERSION = "economic-portfolio-selection-1"
VERSION_V2 = "economic-portfolio-selection-2"
BPS = Decimal(10000)


def _bps(value: object, label: str, *, maximum: int = 10000) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise MachineError(label + " must be bounded integer basis points")
    return value


def _signed_bps(value: object, label: str, *, maximum: int = 50000) -> int:
    if type(value) is not int or not -maximum <= value <= maximum:
        raise MachineError(label + " must be bounded signed integer basis points")
    return value


def _fresh(observed: str, now: str, max_age_ms: int) -> bool:
    age = datetime.fromisoformat(now) - datetime.fromisoformat(observed)
    age_us = (age.days * 86400 + age.seconds) * 1000000 + age.microseconds
    return 0 <= age_us <= max_age_ms * 1000


def select_portfolio(request: dict) -> dict:
    """Rank valid candidates by net expected return, stress, turnover and id.

    There is no search over the full allocation space and no claim that a
    selected portfolio is a global or realized optimum.
    """
    with localcontext() as context:
        context.prec = 256
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        return _select_portfolio(request)


def _select_portfolio(request: dict) -> dict:
    if not isinstance(request, dict) or request.get("schema_version") not in {VERSION, VERSION_V2}:
        raise MachineError("unsupported portfolio selection version")
    v2 = request["schema_version"] == VERSION_V2
    keys = {"schema_version", "owner_id", "network", "asset", "as_of",
            "capital", "max_age_ms", "cash_floor_bps", "max_turnover_bps",
            "max_stress_loss", "max_liquidity_days", "group_caps_bps",
            "scenarios", "products", "candidates"}
    if v2:
        keys |= {"factor_gross_caps_bps", "factor_net_bounds_bps", "max_source_skew_ms"}
    require_keys(request, keys, "PortfolioRequest")
    for key in ("owner_id", "network", "asset"):
        ident(request[key], key)
    at = utc(request["as_of"])
    capital = decimal(request["capital"])
    if capital <= 0:
        raise MachineError("portfolio capital must be positive")
    max_loss = decimal(request["max_stress_loss"])
    if type(request["max_age_ms"]) is not int or not 1 <= request["max_age_ms"] <= 86400000:
        raise MachineError("invalid portfolio observation age")
    if (v2 and (type(request["max_source_skew_ms"]) is not int
                or not 0 <= request["max_source_skew_ms"] <= request["max_age_ms"])):
        raise MachineError("invalid cross-product observation skew")
    cash_floor = _bps(request["cash_floor_bps"], "cash floor")
    max_turnover = _bps(request["max_turnover_bps"], "turnover cap")
    if (type(request["max_liquidity_days"]) is not int
            or not 0 <= request["max_liquidity_days"] <= 365):
        raise MachineError("invalid liquidity horizon")
    scenarios = request["scenarios"]
    if (not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 16
            or any(not isinstance(scenario, str) for scenario in scenarios)
            or len(set(scenarios)) != len(scenarios)):
        raise MachineError("scenarios must be distinct and bounded")
    for scenario in scenarios:
        ident(scenario, "scenario")
    groups = request["group_caps_bps"]
    if not isinstance(groups, dict) or not 1 <= len(groups) <= 32:
        raise MachineError("group caps required")
    for group, cap in groups.items():
        ident(group, "group")
        _bps(cap, "group cap")
    factors = {}
    if v2:
        gross_caps, net_bounds = (request["factor_gross_caps_bps"],
                                   request["factor_net_bounds_bps"])
        if (not isinstance(gross_caps, dict) or not 1 <= len(gross_caps) <= 32
                or not isinstance(net_bounds, dict) or set(net_bounds) != set(gross_caps)):
            raise MachineError("factor gross caps and net bounds must cover the same factors")
        for name, cap in gross_caps.items():
            ident(name, "risk factor")
            _bps(cap, "factor gross cap", maximum=50000)
            bounds = net_bounds[name]
            require_keys(bounds, {"min", "max"}, "FactorNetBounds")
            low = _signed_bps(bounds["min"], "factor net minimum")
            high = _signed_bps(bounds["max"], "factor net maximum")
            if low > high:
                raise MachineError("factor net bounds are reversed")
            factors[name] = {"gross_cap": cap, "min_net": low, "max_net": high}
    products = request["products"]
    if not isinstance(products, list) or not 1 <= len(products) <= 16:
        raise MachineError("products must be bounded")
    book = {}
    observed_times = []
    for product in products:
        product_keys = {"product_id", "group", "expected_return_bps", "trade_cost_bps",
                        "current_bps", "max_bps", "liquidity_days", "observed_at",
                        "source_hash"}
        product_keys |= ({"scenario_pnl_bps", "factor_loadings_bps"} if v2
                         else {"stress_loss_bps"})
        require_keys(product, product_keys, "PortfolioProduct")
        name = ident(product["product_id"], "product id")
        if name in book:
            raise MachineError("duplicate product id")
        if ident(product["group"], "product group") not in groups:
            raise MachineError("product group has no cap")
        if (type(product["expected_return_bps"]) is not int
                or not -10000 <= product["expected_return_bps"] <= 100000):
            raise MachineError("invalid expected return")
        _bps(product["trade_cost_bps"], "trade cost")
        _bps(product["current_bps"], "current weight")
        _bps(product["max_bps"], "product cap")
        if type(product["liquidity_days"]) is not int or not 0 <= product["liquidity_days"] <= 365:
            raise MachineError("invalid product liquidity")
        if (not isinstance(product["source_hash"], str) or len(product["source_hash"]) != 64
                or any(char not in "0123456789abcdef" for char in product["source_hash"])):
            raise MachineError("product source hash required")
        if not _fresh(utc(product["observed_at"]), at, request["max_age_ms"]):
            raise MachineError("stale or future product input")
        if v2:
            observed_times.append(datetime.fromisoformat(product["observed_at"]))
        stress = product["scenario_pnl_bps" if v2 else "stress_loss_bps"]
        if not isinstance(stress, dict) or set(stress) != set(scenarios):
            raise MachineError("every product needs every stress scenario")
        for shock in stress.values():
            if v2:
                _signed_bps(shock, "scenario PnL", maximum=100000)
            else:
                _bps(shock, "stress loss")
        if v2:
            loadings = product["factor_loadings_bps"]
            if not isinstance(loadings, dict) or set(loadings) != set(factors):
                raise MachineError("every product needs every factor loading")
            for loading in loadings.values():
                _signed_bps(loading, "factor loading")
        book[name] = product
    if v2:
        skew = max(observed_times) - min(observed_times)
        skew_us = (skew.days * 86400 + skew.seconds) * 1000000 + skew.microseconds
        if skew_us > request["max_source_skew_ms"] * 1000:
            raise MachineError("cross-product observations exceed allowed skew")
    current_cash = 10000 - sum(item["current_bps"] for item in book.values())
    if current_cash < 0:
        raise MachineError("current portfolio weights exceed total capital")
    candidates = request["candidates"]
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 4096:
        raise MachineError("candidate set must be bounded")
    seen_candidates = set()
    evaluated = []
    for candidate in candidates:
        require_keys(candidate, {"candidate_id", "weights_bps"}, "PortfolioCandidate")
        name = ident(candidate["candidate_id"], "candidate id")
        if name in seen_candidates:
            raise MachineError("duplicate candidate id")
        seen_candidates.add(name)
        weights = candidate["weights_bps"]
        if not isinstance(weights, dict) or set(weights) != set(book):
            raise MachineError("candidate must specify every product weight")
        for weight in weights.values():
            _bps(weight, "candidate weight")
        invested = sum(weights.values())
        cash = 10000 - invested
        reasons = []
        if cash < cash_floor:
            reasons.append("CASH_FLOOR")
        turnover = (sum(abs(weights[key] - book[key]["current_bps"]) for key in book)
                    + abs(cash - current_cash)) // 2
        if turnover > max_turnover:
            reasons.append("TURNOVER_CAP")
        if any(weights[key] > book[key]["max_bps"] for key in book):
            reasons.append("PRODUCT_CAP")
        if any(sum(weights[key] for key in book if book[key]["group"] == group) > cap
               for group, cap in groups.items()):
            reasons.append("GROUP_CAP")
        if any(weights[key] and book[key]["liquidity_days"] > request["max_liquidity_days"]
               for key in book):
            reasons.append("LIQUIDITY_HORIZON")
        cost_bps = sum(abs(weights[key] - book[key]["current_bps"])
                       * book[key]["trade_cost_bps"] for key in book) / BPS
        scenario_pnl = {}
        factor_net = {}
        factor_gross = {}
        if v2:
            scenario_pnl = {scenario: sum(weights[key] * book[key]["scenario_pnl_bps"][scenario]
                                           for key in book) / BPS for scenario in scenarios}
            worst_loss_bps = max(Decimal(0), cost_bps - min(scenario_pnl.values()))
            factor_net = {factor: sum(weights[key] * book[key]["factor_loadings_bps"][factor]
                                      for key in book) / BPS for factor in factors}
            factor_gross = {factor: sum(weights[key] * abs(book[key]["factor_loadings_bps"][factor])
                                        for key in book) / BPS for factor in factors}
            if any(factor_gross[factor] > factors[factor]["gross_cap"] for factor in factors):
                reasons.append("FACTOR_GROSS_CAP")
            if any(not factors[factor]["min_net"] <= factor_net[factor] <= factors[factor]["max_net"]
                   for factor in factors):
                reasons.append("FACTOR_NET_BOUNDS")
        else:
            worst_loss_bps = max(sum(weights[key] * book[key]["stress_loss_bps"][scenario]
                                     for key in book) / BPS for scenario in scenarios)
        stress_amount = capital * Decimal(worst_loss_bps) / BPS
        if stress_amount > max_loss:
            reasons.append("STRESS_LOSS_CAP")
        gross_bps = sum(weights[key] * book[key]["expected_return_bps"]
                        for key in book) / BPS
        row = {"candidate_id": name, "weights_bps": dict(sorted(weights.items())),
               "cash_bps": cash, "turnover_bps": turnover,
               "gross_expected_bps": decstr(gross_bps),
               "estimated_cost_bps": decstr(cost_bps),
               "net_expected_bps": decstr(gross_bps - cost_bps),
               "worst_stress_loss": decstr(stress_amount),
               "eligible": not reasons, "reason_codes": reasons}
        if v2:
            row.update({"scenario_pnl_bps": {key: decstr(value) for key, value in sorted(scenario_pnl.items())},
                        "factor_net_exposure_bps": {key: decstr(value) for key, value in sorted(factor_net.items())},
                        "factor_gross_exposure_bps": {key: decstr(value) for key, value in sorted(factor_gross.items())}})
        evaluated.append(row)
    eligible = [item for item in evaluated if item["eligible"]]
    eligible.sort(key=lambda item: (-decimal(item["net_expected_bps"], signed=True),
                                    decimal(item["worst_stress_loss"]),
                                    item["turnover_bps"], item["candidate_id"]))
    result = {"schema_version": "economic-portfolio-verdict-2" if v2 else "economic-portfolio-verdict-1",
              "request_hash": digest(request), "owner_id": request["owner_id"],
              "network": request["network"], "as_of": at,
              "status": "SELECTED" if eligible else "ABSTAIN",
              "selected_candidate_id": eligible[0]["candidate_id"] if eligible else None,
              "evaluated": sorted(evaluated, key=lambda item: item["candidate_id"]),
              "selection_scope": "SUPPLIED_CANDIDATES_ONLY",
              "execution_authority": "NONE", "chain_status": "NOT_SUBMITTED"}
    result["verdict_hash"] = digest(result)
    return result
