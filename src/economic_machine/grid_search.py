"""Complete enumeration of a bounded discrete portfolio allocation grid.

The certificate is only about the caller's grid and supplied assumptions.
Search refuses a grid it cannot enumerate fully within its fixed budget.
"""

from math import comb

from .portfolio import VERSION as SELECTION_VERSION, VERSION_V2 as SELECTION_VERSION_V2, select_portfolio
from .values import MachineError, digest, ident, require_keys


VERSION = "economic-portfolio-grid-search-1"
VERSION_V2 = "economic-portfolio-grid-search-2"
MAX_CANDIDATES = 4096


def search_grid(request: dict) -> dict:
    if not isinstance(request, dict) or request.get("schema_version") not in {VERSION, VERSION_V2}:
        raise MachineError("unsupported grid search version")
    v2 = request["schema_version"] == VERSION_V2
    keys = {"schema_version", "owner_id", "network", "asset", "as_of",
            "capital", "max_age_ms", "cash_floor_bps", "max_turnover_bps",
            "max_stress_loss", "max_liquidity_days", "group_caps_bps",
            "scenarios", "products", "grid_step_bps"}
    if v2:
        keys |= {"factor_gross_caps_bps", "factor_net_bounds_bps", "max_source_skew_ms"}
    require_keys(request, keys, "GridSearchRequest")
    step = request["grid_step_bps"]
    if type(step) is not int or not 1 <= step <= 10000 or 10000 % step != 0:
        raise MachineError("grid step must divide 10000 basis points")
    products = request["products"]
    if not isinstance(products, list) or not 1 <= len(products) <= 16:
        raise MachineError("bounded product list required")
    names = []
    for product in products:
        if not isinstance(product, dict) or "product_id" not in product:
            raise MachineError("grid product requires an id")
        names.append(ident(product["product_id"], "product id"))
    if len(set(names)) != len(names):
        raise MachineError("duplicate product id")
    names.sort()
    slots = 10000 // step
    count = comb(slots + len(names), len(names))
    if count > MAX_CANDIDATES:
        raise MachineError("grid exceeds complete enumeration budget")

    candidates = []

    def enumerate_weights(index: int, remaining: int, chosen: dict) -> None:
        if index == len(names):
            candidates.append({"candidate_id": f"grid_{len(candidates):04d}",
                               "weights_bps": dict(chosen)})
            return
        name = names[index]
        for units in range(remaining + 1):
            chosen[name] = units * step
            enumerate_weights(index + 1, remaining - units, chosen)
        del chosen[name]

    enumerate_weights(0, slots, {})
    if len(candidates) != count:
        raise MachineError("grid enumeration count mismatch")
    selection_request = {key: value for key, value in request.items()
                         if key not in {"schema_version", "grid_step_bps"}}
    selection_request.update({"schema_version": SELECTION_VERSION_V2 if v2 else SELECTION_VERSION,
                              "candidates": candidates})
    verdict = select_portfolio(selection_request)
    result = {"schema_version": "economic-portfolio-grid-verdict-2" if v2 else "economic-portfolio-grid-verdict-1",
              "grid_request_hash": digest(request), "grid_step_bps": step,
              "enumerated_candidates": count, "complete_enumeration": True,
              "optimum_scope": "DEFINED_GRID_AND_SUPPLIED_ASSUMPTIONS_ONLY",
              "selection_request": selection_request, "portfolio_verdict": verdict,
              "execution_authority": "NONE", "chain_status": "NOT_SUBMITTED"}
    result["grid_verdict_hash"] = digest(result)
    return result


def verify_grid(result: dict, request: dict) -> bool:
    if not isinstance(result, dict):
        return False
    try:
        return result == search_grid(request)
    except (MachineError, KeyError, TypeError, ValueError):
        return False
