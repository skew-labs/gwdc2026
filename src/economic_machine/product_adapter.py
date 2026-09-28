"""Typed product inputs for portfolio construction, without feed authority.

SPOT, PERP and LENDING observations supply capacity, costs and withdrawal
timing. A separate model supplies return, scenario and factor assumptions.
Hashes bind records for replay; they do not authenticate their origin.
"""

from datetime import datetime
from fractions import Fraction

from .portfolio import VERSION_V2, select_portfolio
from .values import MachineError, decimal, digest, ident, require_keys, utc


VERSION = "economic-product-input-assembly-1"
TEMPLATE_VERSION = "economic-portfolio-template-1"
BUNDLE_VERSION = "economic-product-bundle-1"
MANIFEST_VERSION = "economic-product-adapter-1"
OBSERVATION_VERSION = "economic-product-observation-1"
ASSUMPTION_VERSION = "economic-product-assumptions-1"
POSITION_VERSION = "economic-position-snapshot-1"

TEMPLATE_KEYS = {"schema_version", "owner_id", "network", "asset", "as_of",
                 "capital", "max_age_ms", "max_source_skew_ms", "cash_floor_bps",
                 "max_turnover_bps", "max_stress_loss", "max_liquidity_days",
                 "group_caps_bps", "factor_gross_caps_bps", "factor_net_bounds_bps",
                 "scenarios", "candidates"}
OBSERVATION_KEYS = {"schema_version", "product_id", "network", "asset", "kind",
                    "base_asset", "quote_asset", "notional_unit", "market_status",
                    "source_id", "source_record_hash", "observed_at", "valid_until",
                    "tradable_notional", "market"}
ASSUMPTION_KEYS = {"schema_version", "product_id", "model_id", "model_hash",
                   "observation_hash", "generated_at", "valid_until",
                   "expected_return_bps", "scenario_pnl_bps", "factor_loadings_bps"}


def _hex32(value: object, label: str) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        raise MachineError(label + " must be lowercase SHA-256 hex")
    return value


def _integer(value: object, label: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise MachineError(label + " is outside its typed range")
    return value


def _time_in_window(observed: object, expires: object, now: str, age_ms: int,
                    label: str) -> str:
    start, end, at = (datetime.fromisoformat(utc(item))
                      for item in (observed, expires, now))
    interval = at - start
    age_us = (interval.days * 86400 + interval.seconds) * 1000000 + interval.microseconds
    if not start <= at < end or not 0 <= age_us <= age_ms * 1000:
        raise MachineError(label + " is stale, future, or expired")
    return start.isoformat()


def _indexed(items: object, label: str) -> dict[str, dict]:
    if not isinstance(items, list) or not 1 <= len(items) <= 16:
        raise MachineError(label + " must be a bounded product list")
    result = {}
    for item in items:
        if not isinstance(item, dict):
            raise MachineError(label + " item must be an object")
        name = ident(item.get("product_id"), "product id")
        if name in result:
            raise MachineError(label + " has duplicate product id")
        result[name] = item
    return result


def _ceiling_bps(numerator, denominator) -> int:
    if denominator <= 0 or numerator < 0:
        raise MachineError("invalid quote ratio")
    value = Fraction(numerator) * 10000 / Fraction(denominator)
    return -(-value.numerator // value.denominator)


def _market(manifest: dict, observation: dict) -> tuple[int, int]:
    kind = manifest["kind"]
    market = observation["market"]
    if kind == "SPOT":
        require_keys(market, {"bid_price", "ask_price", "price_unit", "fee_bps", "impact_bps"},
                     "SpotMarket")
        if market["price_unit"] != manifest["quote_asset"] + "/" + manifest["base_asset"]:
            raise MachineError("spot price unit mismatch")
        bid, ask = decimal(market["bid_price"]), decimal(market["ask_price"])
        if bid <= 0 or ask < bid:
            raise MachineError("spot bid/ask are crossed or nonpositive")
        spread = _ceiling_bps(ask - bid, bid)
        if spread > manifest["max_spread_bps"]:
            raise MachineError("spot spread exceeds adapter limit")
        cost = spread + _integer(market["fee_bps"], "spot fee", 0, 10000)
        cost += _integer(market["impact_bps"], "spot impact", 0, 10000)
        liquidity = manifest["min_liquidity_days"]
    elif kind == "PERP":
        require_keys(market, {"mark_price", "index_price", "price_unit", "spread_bps", "fee_bps",
                              "impact_bps", "funding_bps_per_8h"}, "PerpMarket")
        if market["price_unit"] != manifest["quote_asset"] + "/" + manifest["base_asset"]:
            raise MachineError("perp price unit mismatch")
        mark, index = decimal(market["mark_price"]), decimal(market["index_price"])
        if mark <= 0 or index <= 0:
            raise MachineError("perp mark/index must be positive")
        basis = _ceiling_bps(abs(mark - index), index)
        if basis > manifest["max_basis_bps"]:
            raise MachineError("perp mark/index basis exceeds adapter limit")
        cost = sum(_integer(market[key], key, 0, 10000)
                   for key in ("spread_bps", "fee_bps", "impact_bps"))
        _integer(market["funding_bps_per_8h"], "perp funding", -10000, 10000)
        liquidity = manifest["min_liquidity_days"]
    else:
        require_keys(market, {"deposit_asset", "supply_apy_bps", "entry_fee_bps",
                              "exit_fee_bps", "redemption_days", "redemption_enabled",
                              "utilization_bps"}, "LendingMarket")
        if market["deposit_asset"] != manifest["base_asset"]:
            raise MachineError("lending deposit unit mismatch")
        if market["redemption_enabled"] is not True:
            raise MachineError("lending redemption is not available")
        _integer(market["supply_apy_bps"], "supply APY", 0, 100000)
        _integer(market["utilization_bps"], "utilization", 0, 10000)
        liquidity = max(manifest["min_liquidity_days"],
                        _integer(market["redemption_days"], "redemption days", 0, 365))
        cost = _integer(market["entry_fee_bps"], "entry fee", 0, 10000)
        cost += _integer(market["exit_fee_bps"], "exit fee", 0, 10000)
    if cost > 10000:
        raise MachineError("conservative round-trip cost exceeds 10000 bps")
    return cost, liquidity


def assemble_portfolio_inputs(template: dict, bundle: dict) -> dict:
    """Turn typed observations and bound assumptions into a v2 verdict."""
    require_keys(template, TEMPLATE_KEYS, "PortfolioTemplate")
    if template["schema_version"] != TEMPLATE_VERSION:
        raise MachineError("unsupported portfolio template version")
    require_keys(bundle, {"schema_version", "manifests", "observations",
                          "assumptions", "positions"}, "ProductBundle")
    if bundle["schema_version"] != BUNDLE_VERSION:
        raise MachineError("unsupported product bundle version")
    _integer(template["max_age_ms"], "maximum input age", 1, 86400000)
    manifests = _indexed(bundle["manifests"], "manifests")
    observations = _indexed(bundle["observations"], "observations")
    assumptions = _indexed(bundle["assumptions"], "assumptions")
    if set(manifests) != set(observations) or set(manifests) != set(assumptions):
        raise MachineError("product adapter, observation and assumption sets differ")
    positions = bundle["positions"]
    require_keys(positions, {"schema_version", "owner_id", "network", "asset",
                             "observed_at", "source_id", "source_record_hash",
                             "current_weights_bps"}, "PositionSnapshot")
    if (positions["schema_version"] != POSITION_VERSION
            or any(positions[key] != template[key] for key in ("owner_id", "network", "asset"))):
        raise MachineError("position snapshot identity mismatch")
    ident(positions["source_id"], "position source")
    _hex32(positions["source_record_hash"], "position source hash")
    position_at = datetime.fromisoformat(utc(positions["observed_at"]))
    portfolio_at = datetime.fromisoformat(utc(template["as_of"]))
    position_age = portfolio_at - position_at
    position_age_us = ((position_age.days * 86400 + position_age.seconds) * 1000000
                       + position_age.microseconds)
    if not 0 <= position_age_us <= template["max_age_ms"] * 1000:
        raise MachineError("position snapshot is stale or future")
    current = positions["current_weights_bps"]
    if not isinstance(current, dict) or set(current) != set(manifests):
        raise MachineError("position snapshot must cover every product")
    for weight in current.values():
        _integer(weight, "current weight", 0, 10000)
    if sum(current.values()) > 10000:
        raise MachineError("current weights exceed 10000 bps")

    capital = decimal(template["capital"])
    if capital <= 0:
        raise MachineError("portfolio capital must be positive")
    products, provenance = [], []
    for name in sorted(manifests):
        manifest, observed, model = manifests[name], observations[name], assumptions[name]
        kind = manifest.get("kind")
        manifest_keys = {"schema_version", "product_id", "network", "asset", "group",
                         "base_asset", "quote_asset",
                         "kind", "max_bps", "min_liquidity_days"}
        if kind == "SPOT":
            manifest_keys.add("max_spread_bps")
        elif kind == "PERP":
            manifest_keys.add("max_basis_bps")
        elif kind != "LENDING":
            raise MachineError("unsupported product kind")
        require_keys(manifest, manifest_keys, "ProductAdapter")
        require_keys(observed, OBSERVATION_KEYS, "ProductObservation")
        require_keys(model, ASSUMPTION_KEYS, "ProductAssumptions")
        if (manifest["schema_version"] != MANIFEST_VERSION
                or observed["schema_version"] != OBSERVATION_VERSION
                or model["schema_version"] != ASSUMPTION_VERSION):
            raise MachineError("unsupported product input version")
        if (any(item["product_id"] != name for item in (manifest, observed, model))
                or any(item["network"] != template["network"] or item["asset"] != template["asset"]
                       for item in (manifest, observed))
                or observed["kind"] != kind):
            raise MachineError("product identity, network, asset or kind mismatch")
        for asset_name in ("base_asset", "quote_asset"):
            ident(manifest[asset_name], asset_name)
            if observed[asset_name] != manifest[asset_name]:
                raise MachineError("product observation asset unit mismatch")
        if (manifest["quote_asset"] != template["asset"]
                or observed["notional_unit"] != template["asset"]):
            raise MachineError("product capital/notional unit mismatch")
        if kind == "LENDING" and manifest["base_asset"] != template["asset"]:
            raise MachineError("lending deposit asset differs from capital")
        if observed["market_status"] != "ACTIVE":
            raise MachineError("product market is not active")
        ident(manifest["group"], "product group")
        ident(observed["source_id"], "observation source")
        ident(model["model_id"], "risk model")
        _hex32(observed["source_record_hash"], "observation source hash")
        _hex32(model["model_hash"], "risk model hash")
        if model["observation_hash"] != digest(observed):
            raise MachineError("risk assumptions do not bind the observation")
        _time_in_window(observed["observed_at"], observed["valid_until"], template["as_of"],
                        template["max_age_ms"], "product observation")
        generated = _time_in_window(model["generated_at"], model["valid_until"],
                                    template["as_of"], template["max_age_ms"], "risk assumptions")
        if datetime.fromisoformat(generated) < datetime.fromisoformat(utc(observed["observed_at"])):
            raise MachineError("risk assumptions predate their bound observation")
        max_bps = _integer(manifest["max_bps"], "adapter product cap", 0, 10000)
        minimum = _integer(manifest["min_liquidity_days"], "minimum liquidity days", 0, 365)
        if kind == "SPOT":
            _integer(manifest["max_spread_bps"], "maximum spread", 0, 10000)
        elif kind == "PERP":
            _integer(manifest["max_basis_bps"], "maximum basis", 0, 10000)
        capacity = decimal(observed["tradable_notional"])
        capacity_bps = int(Fraction(capacity) * 10000 / Fraction(capital))
        cost, liquidity = _market(manifest, observed)
        product = {"product_id": name, "group": manifest["group"],
                   "expected_return_bps": model["expected_return_bps"],
                   "trade_cost_bps": cost, "scenario_pnl_bps": model["scenario_pnl_bps"],
                   "factor_loadings_bps": model["factor_loadings_bps"],
                   "current_bps": current[name], "max_bps": min(max_bps, capacity_bps),
                   "liquidity_days": max(minimum, liquidity),
                   "observed_at": observed["observed_at"],
                   "source_hash": digest({"adapter": manifest, "observation": observed,
                                          "assumptions": model})}
        products.append(product)
        provenance.append({"product_id": name, "kind": kind,
                           "adapter_hash": digest(manifest),
                           "observation_hash": digest(observed),
                           "assumption_hash": digest(model),
                           "source_id_claim": observed["source_id"],
                           "source_record_hash_claim": observed["source_record_hash"],
                           "risk_model_id_claim": model["model_id"],
                           "risk_model_hash_claim": model["model_hash"],
                           "capacity_cap_bps": min(max_bps, capacity_bps)})
    request = {**template, "schema_version": VERSION_V2, "products": products}
    verdict = select_portfolio(request)
    result = {"schema_version": VERSION, "status": "ASSEMBLED_UNVERIFIED_SOURCES",
              "source_trust": "CLAIMED_NOT_VERIFIED", "execution_authority": "NONE",
              "chain_status": "NOT_SUBMITTED", "input_hash": digest({"template": template,
                                                            "bundle": bundle}),
              "selection_request": request, "portfolio_verdict": verdict,
              "provenance": provenance,
              "position_snapshot_hash": digest(positions)}
    result["assembly_hash"] = digest(result)
    return result


def verify_portfolio_inputs(result: dict, template: dict, bundle: dict) -> bool:
    """Replay every typed input, computed cost/cap and portfolio verdict."""
    if not isinstance(result, dict):
        return False
    try:
        return result == assemble_portfolio_inputs(template, bundle)
    except (MachineError, KeyError, TypeError, ValueError, ZeroDivisionError):
        return False
