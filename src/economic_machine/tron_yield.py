"""TRON-specific yield opportunity normalization and bounded plan comparison.

No provider fact is authenticated here. Annualized rates, incentives, rental
quotes, capacity, prices, and scenario losses are caller-supplied observations
or assumptions. The output is a planning comparison, never trade authority.
"""

from datetime import datetime
from decimal import Decimal, ROUND_FLOOR, localcontext
from math import ceil

from .grid_search import VERSION_V2 as GRID_VERSION, search_grid
from .values import MachineError, decimal, decstr, digest, ident, require_keys, utc


VERSION = "economic-tron-yield-plan-1"
REQUEST_VERSION = "economic-tron-yield-request-1"
OPPORTUNITY_VERSION = "economic-tron-yield-opportunity-1"
CHAIN_VERSION = "economic-tron-chain-parameters-1"
KINDS = {"TRX_STAKE", "TRX_STAKE_ENERGY", "JUSTLEND_STRX",
         "JUSTLEND_USDT", "JUSTLEND_USDD", "USDD_EARN",
         "USDD_VAULT_STRATEGY"}
REQUIRED_KINDS = {"TRX_STAKE_ENERGY", "JUSTLEND_USDT", "JUSTLEND_USDD",
                  "USDD_VAULT_STRATEGY"}
COMPONENTS = {"lending", "voting", "energy_rental", "protocol_earn"}
COSTS = {"entry", "exit", "swap", "network"}
REQUEST_KEYS = {"schema_version", "owner_id", "network", "asset", "as_of",
                "capital", "horizon_days", "max_age_ms", "max_source_skew_ms",
                "cash_floor_bps", "min_invested_bps", "max_turnover_bps",
                "max_stress_loss", "max_liquidity_days", "group_caps_bps",
                "factor_gross_caps_bps", "factor_net_bounds_bps", "scenarios",
                "grid_step_bps", "min_plan_distance_bps", "chain_parameters",
                "max_vault_debt_to_collateral_bps", "opportunities"}
OPPORTUNITY_KEYS = {"schema_version", "product_id", "kind", "group", "network",
                    "funding_asset", "underlying_asset", "market_status",
                    "source_id", "source_record_hash", "observed_at",
                    "valid_until", "capacity", "max_bps", "current_bps",
                    "withdrawal_days", "redemption_enabled",
                    "annualized_components_bps", "aggregate_reported_bps",
                    "reward_tokens", "round_trip_cost_bps",
                    "scenario_pnl_bps", "factor_loadings_bps", "rental_quote",
                    "vault_terms"}

VAULT_KEYS = {"collateral_asset", "collateral_value_usdt", "debt_value_usdt",
              "minted_usdd", "accrued_fee_usdd", "usdd_price_usdt",
              "destination", "destination_apy_bps",
              "stability_fee_bps", "min_collateral_ratio_bps", "safety_buffer_bps",
              "liquidation_penalty_bps", "destination_withdrawal_days",
              "collateral_shock_bps_by_scenario", "debt_shock_bps_by_scenario",
              "source_id", "source_record_hash", "observed_at", "valid_until"}


def _integer(value: object, label: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise MachineError(label + " is outside its typed range")
    return value


def _hash32(value: object, label: str) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        raise MachineError(label + " must be lowercase bytes32 hex")
    return value


def _fresh(start: object, end: object, at: str, max_age_ms: int) -> bool:
    before = datetime.fromisoformat(utc(start))
    after = datetime.fromisoformat(utc(end))
    now = datetime.fromisoformat(at)
    age = now - before
    age_us = (age.days * 86400 + age.seconds) * 1000000 + age.microseconds
    return before <= now < after and 0 <= age_us <= max_age_ms * 1000


def _weighted_bps(weights: dict[str, int], values: dict[str, int]) -> str:
    return decstr(sum(Decimal(weights[name]) * values[name]
                      for name in weights) / Decimal(10000))


def _vault_rate(opportunity: dict, request: dict) -> tuple[int | None, dict]:
    """Net a USDD mint-and-deploy strategy against its debt and liquidation risk."""
    vault = require_keys(opportunity["vault_terms"], VAULT_KEYS, "USDDVaultStrategy")
    name = opportunity["product_id"]
    if vault["collateral_asset"] != opportunity["underlying_asset"]:
        raise MachineError("vault collateral asset mismatch")
    if vault["destination"] not in {"JUSTLEND_USDD", "USDD_EARN"}:
        raise MachineError("vault minted USDD needs a supported yield destination")
    minted = decimal(vault["minted_usdd"])
    accrued_fee = decimal(vault["accrued_fee_usdd"])
    usdd_price = decimal(vault["usdd_price_usdt"])
    if minted <= 0:
        raise MachineError("vault minted USDD must be positive")
    if accrued_fee < 0 or usdd_price <= 0:
        raise MachineError("vault fee or USDD price invalid")
    collateral = decimal(vault["collateral_value_usdt"])
    debt = decimal(vault["debt_value_usdt"])
    if collateral <= 0 or debt <= 0:
        raise MachineError("vault collateral and debt values must be positive")
    if debt != (minted + accrued_fee) * usdd_price:
        raise MachineError("vault debt must equal outstanding USDD times observed price")
    ident(vault["source_id"], "vault source")
    _hash32(vault["source_record_hash"], "vault source hash")
    if not _fresh(vault["observed_at"], vault["valid_until"],
                  utc(request["as_of"]), request["max_age_ms"]):
        return None, {"product_id": name, "reason": "STALE_VAULT_TERMS"}
    destination_rate = _integer(vault["destination_apy_bps"],
                                "vault destination APY", 0, 100000)
    stability_fee = _integer(vault["stability_fee_bps"],
                             "vault stability fee", 0, 100000)
    minimum_ratio = _integer(vault["min_collateral_ratio_bps"],
                             "vault liquidation ratio", 10000, 100000)
    buffer = _integer(vault["safety_buffer_bps"],
                      "vault safety buffer", 0, 50000)
    penalty = _integer(vault["liquidation_penalty_bps"],
                       "vault liquidation penalty", 0, 10000)
    destination_exit = _integer(vault["destination_withdrawal_days"],
                                "vault destination exit", 0, 365)
    if opportunity["withdrawal_days"] < destination_exit:
        raise MachineError("vault exit understates destination redemption")
    shocks_collateral = vault["collateral_shock_bps_by_scenario"]
    shocks_debt = vault["debt_shock_bps_by_scenario"]
    if (not isinstance(shocks_collateral, dict)
            or set(shocks_collateral) != set(request["scenarios"])
            or not isinstance(shocks_debt, dict)
            or set(shocks_debt) != set(request["scenarios"])):
        raise MachineError("vault shock scenarios incomplete")
    debt_ratio = debt * Decimal(10000) / collateral
    current_ratio = collateral * Decimal(10000) / debt
    if debt_ratio > request["max_vault_debt_to_collateral_bps"]:
        return None, {"product_id": name, "reason": "VAULT_DEBT_CAP"}
    if current_ratio <= minimum_ratio + buffer:
        return None, {"product_id": name, "reason": "VAULT_SAFETY_BUFFER"}
    stressed_ratios = {}
    for scenario in request["scenarios"]:
        collateral_shock = _integer(shocks_collateral[scenario],
                                    "vault collateral shock", -9999, 100000)
        debt_shock = _integer(shocks_debt[scenario],
                              "vault debt shock", -9999, 100000)
        ratio = (collateral * (10000 + collateral_shock) * Decimal(10000)
                 / (debt * (10000 + debt_shock)))
        stressed_ratios[scenario] = decstr(ratio)
        if ratio <= minimum_ratio + buffer:
            return None, {"product_id": name,
                          "reason": "VAULT_LIQUIDATION_SCENARIO",
                          "scenario": scenario}
    deployed_value = minted * usdd_price
    net_rate = int((((Decimal(destination_rate) * deployed_value
                      - Decimal(stability_fee) * debt) / collateral)
                    .to_integral_value(rounding=ROUND_FLOOR)))
    if not -10000 <= net_rate <= 100000:
        raise MachineError("vault net annualized rate is outside planner range")
    return net_rate, {"collateral_value_usdt": decstr(collateral),
                      "debt_value_usdt": decstr(debt),
                      "minted_usdd": decstr(minted),
                      "accrued_fee_usdd": decstr(accrued_fee),
                      "usdd_price_usdt": decstr(usdd_price),
                      "deployed_value_usdt": decstr(deployed_value),
                      "debt_to_collateral_bps": decstr(debt_ratio),
                      "current_collateral_ratio_bps": decstr(current_ratio),
                      "stressed_collateral_ratios_bps": stressed_ratios,
                      "min_collateral_ratio_bps": minimum_ratio,
                      "safety_buffer_bps": buffer,
                      "liquidation_penalty_bps": penalty,
                      "destination": vault["destination"],
                      "destination_apy_bps": destination_rate,
                      "stability_fee_bps": stability_fee,
                      "net_rate_on_collateral_bps": net_rate}


def _normalize(opportunity: dict, request: dict, chain: dict) -> tuple[dict | None, dict]:
    require_keys(opportunity, OPPORTUNITY_KEYS, "TronYieldOpportunity")
    if opportunity["schema_version"] != OPPORTUNITY_VERSION:
        raise MachineError("unsupported TRON opportunity version")
    name = ident(opportunity["product_id"], "product id")
    kind = opportunity["kind"]
    if kind not in KINDS:
        raise MachineError("unsupported TRON yield kind")
    if (opportunity["network"] != request["network"]
            or opportunity["funding_asset"] != request["asset"]):
        raise MachineError("opportunity network or funding asset mismatch")
    ident(opportunity["group"], "product group")
    ident(opportunity["underlying_asset"], "underlying asset")
    ident(opportunity["source_id"], "source id")
    _hash32(opportunity["source_record_hash"], "source record hash")
    cap = decimal(opportunity["capacity"])
    if cap < 0:
        raise MachineError("negative capacity")
    maximum = _integer(opportunity["max_bps"], "maximum weight", 0, 10000)
    current = _integer(opportunity["current_bps"], "current weight", 0, 10000)
    exit_days = _integer(opportunity["withdrawal_days"], "withdrawal days", 0, 365)
    if type(opportunity["redemption_enabled"]) is not bool:
        raise MachineError("redemption enabled must be boolean")
    components = opportunity["annualized_components_bps"]
    require_keys(components, COMPONENTS, "TronYieldComponents")
    for value in components.values():
        _integer(value, "annualized yield component", 0, 100000)
    aggregate = _integer(opportunity["aggregate_reported_bps"],
                         "aggregate reported APY", 0, 100000)
    if kind == "JUSTLEND_STRX":
        if aggregate == 0 or any(components.values()) or opportunity["reward_tokens"]:
            raise MachineError("sTRX reported total cannot be double-counted")
    elif kind == "USDD_VAULT_STRATEGY":
        if aggregate or any(components.values()) or opportunity["reward_tokens"]:
            raise MachineError("vault yield must be netted from collateral and debt")
    elif aggregate:
        raise MachineError("reported total is reserved for sTRX")
    if kind == "TRX_STAKE" and (components["energy_rental"] or components["lending"]
                                or components["protocol_earn"]):
        raise MachineError("native stake can only count voting yield")
    if kind == "TRX_STAKE_ENERGY" and (components["lending"] or components["protocol_earn"]):
        raise MachineError("native Energy strategy yield composition mismatch")
    if kind in {"JUSTLEND_USDT", "JUSTLEND_USDD"} and any(
            components[key] for key in ("voting", "energy_rental", "protocol_earn")):
        raise MachineError("JustLend lending yield composition mismatch")
    if kind == "USDD_EARN" and any(
            components[key] for key in ("lending", "voting", "energy_rental")):
        raise MachineError("USDD Earn yield composition mismatch")
    if (kind in {"TRX_STAKE", "TRX_STAKE_ENERGY", "JUSTLEND_STRX"}
            and opportunity["underlying_asset"] != "TRX"):
        raise MachineError("TRX strategy must retain TRX price exposure")
    if kind in {"JUSTLEND_USDT"} and opportunity["underlying_asset"] != "USDT":
        raise MachineError("USDT supply asset mismatch")
    if kind in {"JUSTLEND_USDD", "USDD_EARN"} and opportunity["underlying_asset"] != "USDD":
        raise MachineError("USDD strategy must retain USDD price exposure")
    if kind != "USDD_VAULT_STRATEGY" and opportunity["vault_terms"] is not None:
        raise MachineError("non-vault product must not carry vault debt")
    reward_tokens = opportunity["reward_tokens"]
    if not isinstance(reward_tokens, list) or len(reward_tokens) > 4:
        raise MachineError("bounded reward token list required")
    if kind != "JUSTLEND_USDD" and reward_tokens:
        raise MachineError("only JustLend USDD reward tokens are modeled")
    reward_bps = 0
    seen_tokens = set()
    for reward in reward_tokens:
        require_keys(reward, {"token", "gross_bps", "haircut_bps",
                              "source_id", "source_record_hash",
                              "observed_at", "valid_until"}, "RewardToken")
        token = ident(reward["token"], "reward token")
        if token in seen_tokens:
            raise MachineError("duplicate reward token")
        seen_tokens.add(token)
        gross = _integer(reward["gross_bps"], "reward APY", 0, 100000)
        haircut = _integer(reward["haircut_bps"], "reward haircut", 0, 10000)
        ident(reward["source_id"], "reward source")
        _hash32(reward["source_record_hash"], "reward source hash")
        if not _fresh(reward["observed_at"], reward["valid_until"],
                      utc(request["as_of"]), request["max_age_ms"]):
            return None, {"product_id": name, "reason": "STALE_REWARD_QUOTE"}
        reward_bps += gross * (10000 - haircut) // 10000
    if reward_bps > 100000:
        raise MachineError("combined incentive rate is out of range")
    if kind in {"TRX_STAKE", "TRX_STAKE_ENERGY"}:
        minimum_exit = chain["unstake_delay_days"]
        rental = opportunity["rental_quote"]
        if kind == "TRX_STAKE_ENERGY":
            require_keys(rental, {"status", "lock_blocks", "source_id",
                                  "source_record_hash", "observed_at",
                                  "valid_until"}, "EnergyRentalQuote")
            lock = _integer(rental["lock_blocks"], "Energy delegation lock", 0,
                            chain["max_delegate_lock_blocks"])
            ident(rental["source_id"], "rental source")
            _hash32(rental["source_record_hash"], "rental source hash")
            minimum_exit += ceil(lock * chain["block_time_seconds"] / 86400)
            if (rental["status"] != "ACTIVE_QUOTE" or
                    not _fresh(rental["observed_at"], rental["valid_until"],
                               utc(request["as_of"]), request["max_age_ms"])):
                return None, {"product_id": name, "reason": "ENERGY_QUOTE_UNAVAILABLE"}
        elif rental is not None:
            raise MachineError("non-rental stake must not include a rental quote")
        if exit_days < minimum_exit:
            raise MachineError("native stake withdrawal understates chain delay")
    elif opportunity["rental_quote"] is not None:
        raise MachineError("non-Energy product must not include rental quote")
    costs = opportunity["round_trip_cost_bps"]
    require_keys(costs, COSTS, "TronRoundTripCost")
    cost_bps = sum(_integer(value, "round trip cost", 0, 10000)
                   for value in costs.values())
    if cost_bps > 10000:
        raise MachineError("round trip cost exceeds capital")
    stress = opportunity["scenario_pnl_bps"]
    factors = opportunity["factor_loadings_bps"]
    if (not isinstance(stress, dict) or set(stress) != set(request["scenarios"])
            or not isinstance(factors, dict)
            or set(factors) != set(request["factor_gross_caps_bps"])):
        raise MachineError("TRON risk scenarios or factors incomplete")
    for value in stress.values():
        _integer(value, "scenario PnL", -100000, 100000)
    for value in factors.values():
        _integer(value, "risk factor loading", -50000, 50000)
    if opportunity["market_status"] != "ACTIVE":
        return None, {"product_id": name, "reason": "MARKET_INACTIVE"}
    if not opportunity["redemption_enabled"]:
        return None, {"product_id": name, "reason": "REDEMPTION_DISABLED"}
    if not _fresh(opportunity["observed_at"], opportunity["valid_until"],
                  utc(request["as_of"]), request["max_age_ms"]):
        return None, {"product_id": name, "reason": "STALE_MARKET_OBSERVATION"}
    vault_detail = None
    if kind == "USDD_VAULT_STRATEGY":
        annual_bps, vault_detail = _vault_rate(opportunity, request)
        if annual_bps is None:
            return None, vault_detail
        debt_ratio = decimal(vault_detail["debt_to_collateral_bps"])
        vault_terms = opportunity["vault_terms"]
        for scenario in request["scenarios"]:
            collateral_shock = vault_terms["collateral_shock_bps_by_scenario"][scenario]
            debt_shock = vault_terms["debt_shock_bps_by_scenario"][scenario]
            minimum_loss_bps = (Decimal(collateral_shock)
                                - debt_ratio * max(0, debt_shock) / Decimal(10000))
            if Decimal(stress[scenario]) > minimum_loss_bps:
                raise MachineError("vault scenario PnL understates collateral or debt loss")
    else:
        annual_bps = aggregate or sum(components.values()) + reward_bps
    capacity_bps = int(cap * Decimal(10000) / decimal(request["capital"]))
    maximum = min(maximum, capacity_bps)
    if maximum <= 0:
        return None, {"product_id": name, "reason": "NO_CAPACITY"}
    if annual_bps > 100000:
        raise MachineError("combined annualized rate is out of range")
    horizon_rate = int((Decimal(annual_bps) * request["horizon_days"]
                        / Decimal(365)).to_integral_value(rounding=ROUND_FLOOR))
    product = {"product_id": name, "group": opportunity["group"],
               "expected_return_bps": horizon_rate,
               "trade_cost_bps": cost_bps, "scenario_pnl_bps": stress,
               "factor_loadings_bps": factors, "current_bps": current,
               "max_bps": maximum, "liquidity_days": exit_days,
               "observed_at": utc(opportunity["observed_at"]),
               "source_hash": digest({"opportunity": opportunity,
                                      "chain_parameters": chain})}
    detail = {"product_id": name, "kind": kind, "underlying_asset": opportunity["underlying_asset"],
              "annualized_rate_bps": annual_bps,
              "annualized_components_bps": components, "reward_net_bps": reward_bps,
              "round_trip_cost_bps": cost_bps, "withdrawal_days": exit_days,
              "capacity_cap_bps": maximum, "source_id_claim": opportunity["source_id"],
              "source_record_hash_claim": opportunity["source_record_hash"],
              "observed_at": utc(opportunity["observed_at"]),
              "source_hash": product["source_hash"],
              "vault_risk": vault_detail}
    return product, detail


def plan_tron_yield(request: dict) -> dict:
    """Enumerate a finite TRON yield grid and compare eligible plans."""
    with localcontext() as arithmetic:
        arithmetic.prec = 256
        return _plan_tron_yield(request)


def _plan_tron_yield(request: dict) -> dict:
    require_keys(request, REQUEST_KEYS, "TronYieldRequest")
    if request["schema_version"] != REQUEST_VERSION:
        raise MachineError("unsupported TRON yield request version")
    if request["network"] not in {"TRON_MAINNET", "TRON_NILE", "TRON_SHASTA", "TRON_SIM"}:
        raise MachineError("unsupported TRON network")
    if request["asset"] != "USDT":
        raise MachineError("v1 TRON yield planner requires USDT capital")
    ident(request["owner_id"], "owner id")
    at = utc(request["as_of"])
    if decimal(request["capital"]) <= 0 or decimal(request["max_stress_loss"]) < 0:
        raise MachineError("capital or stress budget invalid")
    horizon = _integer(request["horizon_days"], "horizon days", 1, 365)
    max_age = _integer(request["max_age_ms"], "max age", 1, 86400000)
    _integer(request["max_source_skew_ms"], "source skew", 0, max_age)
    cash_floor = _integer(request["cash_floor_bps"], "cash floor", 0, 10000)
    minimum_invested = _integer(request["min_invested_bps"], "minimum invested", 0, 10000)
    if minimum_invested + cash_floor > 10000:
        raise MachineError("cash floor and minimum invested conflict")
    distance = _integer(request["min_plan_distance_bps"], "plan distance", 1, 10000)
    _integer(request["max_vault_debt_to_collateral_bps"],
             "vault debt-to-collateral cap", 0, 10000)
    chain = request["chain_parameters"]
    require_keys(chain, {"schema_version", "network", "observed_at", "valid_until",
                         "source_id", "source_record_hash", "unstake_delay_days",
                         "block_time_seconds", "max_delegate_lock_blocks"},
                 "TronChainParameters")
    if chain["schema_version"] != CHAIN_VERSION or chain["network"] != request["network"]:
        raise MachineError("chain parameters do not match requested network")
    ident(chain["source_id"], "chain parameter source")
    _hash32(chain["source_record_hash"], "chain parameter source hash")
    _integer(chain["unstake_delay_days"], "unstake delay", 1, 365)
    _integer(chain["block_time_seconds"], "block time", 1, 60)
    _integer(chain["max_delegate_lock_blocks"], "maximum delegation lock", 1, 10512000)
    if not _fresh(chain["observed_at"], chain["valid_until"], at, max_age):
        raise MachineError("chain parameters are stale")
    opportunities = request["opportunities"]
    if not isinstance(opportunities, list) or not 2 <= len(opportunities) <= 8:
        raise MachineError("TRON yield universe must have 2 to 8 opportunities")
    names = [item.get("product_id") if isinstance(item, dict) else None
             for item in opportunities]
    if len(set(names)) != len(names):
        raise MachineError("duplicate opportunity id")
    products, details, exclusions = [], [], []
    times = []
    for opportunity in opportunities:
        product, detail = _normalize(opportunity, request, chain)
        if product is None:
            exclusions.append(detail)
        else:
            products.append(product)
            details.append(detail)
            times.append(datetime.fromisoformat(product["observed_at"]))
    coverage = sorted(REQUIRED_KINDS - {item["kind"] for item in opportunities})
    if times:
        skew = max(times) - min(times)
        skew_us = (skew.days * 86400 + skew.seconds) * 1000000 + skew.microseconds
        if skew_us > request["max_source_skew_ms"] * 1000:
            raise MachineError("TRON opportunity observations exceed source skew")
    result = {"schema_version": VERSION, "request_hash": digest(request),
              "owner_id": request["owner_id"], "network": request["network"],
              "asset": request["asset"], "as_of": at, "horizon_days": horizon,
              "rate_method": "SIMPLE_ANNUALIZED_LINEAR_HORIZON_PROXY",
              "required_kind_gaps": coverage, "excluded_opportunities": exclusions,
              "opportunity_details": details, "plans": [],
              "vault_comparison_status": (
                  "MISSING_REQUIRED_OPPORTUNITY"
                  if "USDD_VAULT_STRATEGY" in coverage else
                  "VAULT_OPPORTUNITY_EXCLUDED"
                  if not any(item["kind"] == "USDD_VAULT_STRATEGY"
                             for item in details) else "NOT_EVALUATED"),
              "source_trust": "CALLER_CLAIMS_NOT_VERIFIED",
              "execution_authority": "NONE", "chain_status": "NOT_SUBMITTED"}
    if coverage or len(products) < 2:
        result["status"] = "ABSTAIN"
        result["reason_codes"] = (["CHALLENGE_COVERAGE_MISSING"] if coverage else
                                  ["INSUFFICIENT_ELIGIBLE_PRODUCTS"])
        result["plan_hash"] = digest(result)
        return result
    grid_request = {key: request[key] for key in (
        "owner_id", "network", "asset", "as_of", "capital", "max_age_ms",
        "max_source_skew_ms", "cash_floor_bps", "max_turnover_bps",
        "max_stress_loss", "max_liquidity_days", "group_caps_bps",
        "factor_gross_caps_bps", "factor_net_bounds_bps", "scenarios",
        "grid_step_bps")}
    grid_request.update({"schema_version": GRID_VERSION, "products": products})
    grid = search_grid(grid_request)
    verdict = grid["portfolio_verdict"]
    eligible = [row for row in verdict["evaluated"]
                if row["eligible"] and 10000 - row["cash_bps"] >= minimum_invested]
    eligible.sort(key=lambda row: (decimal(row["worst_stress_loss"]),
                                   -decimal(row["net_expected_bps"], signed=True),
                                   row["candidate_id"]))
    result["grid_hash"] = grid["grid_verdict_hash"]
    result["enumerated_candidates"] = grid["enumerated_candidates"]
    result["eligible_after_investment_floor"] = len(eligible)
    if not eligible:
        result["status"] = "ABSTAIN"
        result["reason_codes"] = ["NO_PLAN_WITHIN_USER_CONSTRAINTS"]
        result["plan_hash"] = digest(result)
        return result
    conservative = eligible[0]
    growth_ranked = sorted(eligible, key=lambda row: (
        -decimal(row["net_expected_bps"], signed=True),
        decimal(row["worst_stress_loss"]), row["candidate_id"]))
    growth = next((row for row in growth_ranked
                   if (sum(abs(row["weights_bps"][name] -
                               conservative["weights_bps"][name])
                           for name in conservative["weights_bps"])
                       + abs(row["cash_bps"] - conservative["cash_bps"])) // 2
                   >= distance), None)
    if growth is None:
        result["status"] = "ABSTAIN"
        result["reason_codes"] = ["TWO_DISTINCT_PLANS_UNAVAILABLE"]
        result["plan_hash"] = digest(result)
        return result
    annual_by_name = {item["product_id"]: item["annualized_rate_bps"] for item in details}
    comparison = [("CONSERVATIVE", conservative), ("GROWTH", growth)]
    vault_ids = {item["product_id"] for item in details
                 if item["kind"] == "USDD_VAULT_STRATEGY"}
    vault_ranked = [row for row in growth_ranked
                    if any(row["weights_bps"][name] > 0 for name in vault_ids)]
    vault_candidate = next((row for row in vault_ranked
                            if row["candidate_id"] not in {
                                conservative["candidate_id"], growth["candidate_id"]}), None)
    if vault_candidate is not None:
        comparison.append(("USDD_VAULT_COMPARISON", vault_candidate))
    result["vault_comparison_status"] = (
        "INCLUDED_IN_PLANS" if any(any(row["weights_bps"][name] > 0
                                       for name in vault_ids)
                                   for _, row in comparison)
        else "NO_ELIGIBLE_VAULT_PLAN" if vault_ids else "VAULT_OPPORTUNITY_EXCLUDED")
    for label, row in comparison:
        weights = row["weights_bps"]
        annual_proxy = _weighted_bps(weights, annual_by_name)
        net_horizon_bps = decimal(row["net_expected_bps"], signed=True)
        result["plans"].append({
            "name": label, "candidate_id": row["candidate_id"],
            "weights_bps": weights, "cash_bps": row["cash_bps"],
            "gross_annualized_rate_proxy_bps": annual_proxy,
            "horizon_net_return_bps": decstr(net_horizon_bps),
            "horizon_net_return_amount": decstr(
                decimal(request["capital"]) * net_horizon_bps / Decimal(10000)),
            "estimated_round_trip_cost_bps": row["estimated_cost_bps"],
            "worst_stress_loss": row["worst_stress_loss"],
            "scenario_pnl_bps": row["scenario_pnl_bps"],
            "factor_net_exposure_bps": row["factor_net_exposure_bps"],
            "factor_gross_exposure_bps": row["factor_gross_exposure_bps"],
            "max_withdrawal_days": max(
                [item["withdrawal_days"] for item in details
                 if weights[item["product_id"]]] or [0]),
            "status": "COMPARISON_ONLY"})
    result["status"] = "PLANS_COMPARED"
    result["reason_codes"] = []
    result["plan_hash"] = digest(result)
    return result


def verify_tron_yield_plan(result: dict, request: dict) -> bool:
    if not isinstance(result, dict):
        return False
    try:
        return result == plan_tron_yield(request)
    except (MachineError, KeyError, TypeError, ValueError, ZeroDivisionError):
        return False
