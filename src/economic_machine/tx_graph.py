"""Compile a PR04 plan intent into a typed, unsigned TRON transaction DAG.

Expected outputs are dependency claims, not wallet balances.  A later step may
name them as funding, but preflight must wait for the producing transaction to
be confirmed and the account snapshot to be refreshed before that step becomes
eligible.  The graph is explicitly multi-transaction and non-atomic.
"""

import re
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, localcontext

from .capabilities import capability_hash, normalize_capability
from .mandate import normalize_scope, tron_address
from .tron_actions import compile_action, normalize_action_binding
from .values import MachineError, decimal, digest, ident, require_keys, utc


VERSION = "economic-execution-graph-1"
ACCOUNT_VERSION = "economic-account-snapshot-1"
QUOTE_VERSION = "economic-execution-quote-1"
ROUTE_VERSION = "economic-funding-route-1"
LIFECYCLE_VERSION = "economic-lifecycle-request-1"
INTENT_VERSION = "economic-plan-intent-1"
MAX_STEPS = 64
INTENT_KEYS = {"schema_version", "status", "scope", "mandate_policy_hash",
    "mandate_draft_hash", "revision", "snapshot_hash", "comparison_hash", "plan_hash",
    "selected_plan", "as_of", "valid_until", "legs", "cash_amount", "total_cost_base",
    "blockers", "execution_authority", "signature_status", "chain_status", "intent_hash"}
LEG_KEYS = {"product_id", "identity_hash", "capability_hash", "action", "protocol",
    "weight_bps", "principal_asset", "principal_amount", "required_budget_base",
    "cashflow_assumption_hash", "quote_hash", "status"}


def _units(value, decimals, label):
    if type(decimals) is not int or not 0 <= decimals <= 36:
        raise MachineError("invalid " + label + " decimals")
    with localcontext() as ctx:
        ctx.prec = 256
        scaled = decimal(value) * (Decimal(10) ** decimals)
        if scaled != scaled.to_integral_value() or not 0 < scaled < 1 << 256:
            raise MachineError(label + " cannot be represented in base units")
    return int(scaled)


def _base_units(value, label, *, positive=False):
    if (not isinstance(value, str) or re.fullmatch(r"0|[1-9][0-9]*", value) is None
            or len(value) > 78 or int(value) >= 1 << 256
            or (positive and int(value) == 0)):
        raise MachineError("invalid " + label)
    return int(value)


def _asset(raw, label):
    require_keys(raw, {"asset", "token_address", "decimals", "amount_base_units"}, label)
    asset = ident(raw["asset"], label + " asset")
    token = raw["token_address"]
    if token is not None:
        token = tron_address(token)
    elif asset != "TRX":
        raise MachineError("only TRX may omit token address")
    decimals = raw["decimals"]
    if type(decimals) is not int or not 0 <= decimals <= 36:
        raise MachineError("invalid asset decimals")
    amount = _base_units(raw["amount_base_units"], label + " amount")
    return {"asset": asset, "token_address": token, "decimals": decimals,
            "amount_base_units": str(amount)}


def _key(item):
    return item["asset"], item["token_address"], item["decimals"]


def normalize_account_snapshot(raw: dict) -> dict:
    require_keys(raw, {"schema_version", "scope", "snapshot_hash", "observed_at",
        "valid_until", "balances", "allowances", "positions", "vaults",
        "reservations", "market_liquidity"}, "AccountSnapshotV1")
    if raw["schema_version"] != ACCOUNT_VERSION:
        raise MachineError("unsupported account snapshot version")
    if not isinstance(raw["snapshot_hash"], str) or re.fullmatch(
            r"[0-9a-f]{64}", raw["snapshot_hash"]) is None:
        raise MachineError("invalid account snapshot binding")
    start, end = utc(raw["observed_at"]), utc(raw["valid_until"])
    if datetime.fromisoformat(start) >= datetime.fromisoformat(end):
        raise MachineError("account snapshot validity window is empty")
    for field in ("balances", "allowances", "positions", "vaults", "reservations",
                  "market_liquidity"):
        if not isinstance(raw[field], list) or len(raw[field]) > 64:
            raise MachineError(field + " must be a bounded list")
    balances = [_asset(item, "balance") for item in raw["balances"]]
    if not balances or len(balances) > 64 or len({_key(item) for item in balances}) != len(balances):
        raise MachineError("balances must be a bounded unique asset list")
    allowances = []
    seen = set()
    for item in raw["allowances"]:
        require_keys(item, {"token_address", "spender_address", "amount_base_units"},
                     "allowance")
        token, spender = tron_address(item["token_address"]), tron_address(item["spender_address"])
        key = token, spender
        if key in seen:
            raise MachineError("duplicate allowance")
        seen.add(key)
        allowances.append({"token_address": token, "spender_address": spender,
                           "amount_base_units": str(_base_units(
                               item["amount_base_units"], "allowance"))})
    reservations = []
    for item in raw["reservations"]:
        require_keys(item, {"reservation_id", "asset", "token_address", "decimals",
                            "amount_base_units"}, "existing reservation")
        normalized = _asset({key: item[key] for key in (
            "asset", "token_address", "decimals", "amount_base_units")}, "reservation")
        reservations.append({"reservation_id": ident(item["reservation_id"], "reservation id"),
                             **normalized})
    if len({item["reservation_id"] for item in reservations}) != len(reservations):
        raise MachineError("duplicate reservation id")
    positions = []
    position_ids = set()
    for item in raw["positions"]:
        require_keys(item, {"product_id", "shares_base_units", "underlying_base_units"},
                     "position")
        product = ident(item["product_id"], "position product")
        if product in position_ids:
            raise MachineError("duplicate position")
        position_ids.add(product)
        positions.append({"product_id": product,
            "shares_base_units": str(_base_units(item["shares_base_units"], "position shares")),
            "underlying_base_units": str(_base_units(item["underlying_base_units"],
                                                        "position underlying"))})
    vaults = []
    vault_ids = set()
    for item in raw["vaults"]:
        require_keys(item, {"product_id", "vault_id", "collateral_base_units",
                            "debt_base_units"}, "vault position")
        product = ident(item["product_id"], "vault product")
        if product in vault_ids:
            raise MachineError("duplicate vault position")
        vault_ids.add(product)
        vaults.append({"product_id": product,
            "vault_id": str(_base_units(item["vault_id"], "vault id", positive=True)),
            "collateral_base_units": str(_base_units(item["collateral_base_units"],
                                                         "vault collateral")),
            "debt_base_units": str(_base_units(item["debt_base_units"], "vault debt"))})
    liquidity = {}
    for item in raw["market_liquidity"]:
        require_keys(item, {"product_id", "amount_base_units"}, "market liquidity")
        name = ident(item["product_id"], "liquidity product")
        if name in liquidity:
            raise MachineError("duplicate market liquidity")
        liquidity[name] = str(_base_units(item["amount_base_units"], "market liquidity"))
    return {"schema_version": ACCOUNT_VERSION, "scope": normalize_scope(raw["scope"]),
        "snapshot_hash": raw["snapshot_hash"], "observed_at": start,
        "valid_until": end, "balances": sorted(balances, key=_key),
        "allowances": sorted(allowances, key=lambda x: (x["token_address"], x["spender_address"])),
        "positions": sorted(positions, key=lambda x: x["product_id"]),
        "vaults": sorted(vaults, key=lambda x: x["product_id"]),
        "reservations": sorted(reservations, key=lambda x: x["reservation_id"]),
        "market_liquidity": [{"product_id": key, "amount_base_units": liquidity[key]}
                             for key in sorted(liquidity)]}


def normalize_execution_quote(raw: dict) -> dict:
    require_keys(raw, {"schema_version", "product_id", "intent_hash", "snapshot_hash",
        "input", "minimum_output", "fee_limit_sun", "quoted_at", "expires_at",
        "simulation_status", "simulation_evidence_hash"}, "ExecutionQuoteV1")
    if raw["schema_version"] != QUOTE_VERSION:
        raise MachineError("unsupported execution quote version")
    for key in ("intent_hash", "snapshot_hash"):
        if not isinstance(raw[key], str) or re.fullmatch(r"[0-9a-f]{64}", raw[key]) is None:
            raise MachineError("invalid execution quote binding")
    item_in, item_out = _asset(raw["input"], "quote input"), _asset(
        raw["minimum_output"], "quote output")
    if _base_units(item_in["amount_base_units"], "quote input", positive=True) == 0:
        raise MachineError("execution quote input must be positive")
    if _base_units(item_out["amount_base_units"], "quote output", positive=True) == 0:
        raise MachineError("execution quote output must be positive")
    start, end = utc(raw["quoted_at"]), utc(raw["expires_at"])
    if datetime.fromisoformat(start) >= datetime.fromisoformat(end):
        raise MachineError("execution quote expiry must follow quote time")
    status = raw["simulation_status"]
    if status not in {"SIMULATED", "UNSIMULATED"}:
        raise MachineError("invalid execution quote simulation status")
    evidence = raw["simulation_evidence_hash"]
    if status == "SIMULATED":
        if not isinstance(evidence, str) or re.fullmatch(r"[0-9a-f]{64}", evidence) is None:
            raise MachineError("simulated quote needs evidence")
    elif evidence is not None:
        raise MachineError("unsimulated quote cannot carry simulation evidence")
    return {**raw, "product_id": ident(raw["product_id"], "quote product"),
            "input": item_in, "minimum_output": item_out,
            "fee_limit_sun": str(_base_units(raw["fee_limit_sun"], "quote fee", positive=True)),
            "quoted_at": start, "expires_at": end}


def normalize_funding_route(raw: dict) -> dict:
    require_keys(raw, {"schema_version", "route_id", "network", "scope", "intent_hash",
        "snapshot_hash", "provider_id", "source_url", "from_amount",
        "minimum_to_amount", "capacity_from_base_units", "target_address",
        "function_selector", "parameter_hex", "fee_limit_sun", "quoted_at",
        "expires_at", "status", "evidence_hash", "abi_evidence_hash",
        "contract_code_hash"}, "FundingRouteV1")
    if raw["schema_version"] != ROUTE_VERSION or raw["network"] not in {
            "tron-mainnet", "tron-nile", "tron-shasta"}:
        raise MachineError("unsupported funding route")
    scope = normalize_scope(raw["scope"])
    if scope["network"] != raw["network"]:
        raise MachineError("funding route scope/network mismatch")
    for key in ("intent_hash", "snapshot_hash"):
        if not isinstance(raw[key], str) or re.fullmatch(r"[0-9a-f]{64}", raw[key]) is None:
            raise MachineError("invalid funding route context binding")
    provider = ident(raw["provider_id"], "funding route provider")
    if not isinstance(raw["source_url"], str) or not raw["source_url"].startswith("https://"):
        raise MachineError("funding route HTTPS source required")
    source, target = _asset(raw["from_amount"], "route input"), _asset(
        raw["minimum_to_amount"], "route output")
    _base_units(source["amount_base_units"], "route input", positive=True)
    _base_units(target["amount_base_units"], "route output", positive=True)
    capacity = _base_units(raw["capacity_from_base_units"], "route capacity", positive=True)
    if capacity < int(source["amount_base_units"]):
        raise MachineError("route quote exceeds observed capacity")
    if raw["status"] not in {"SIMULATED", "UNSIMULATED"}:
        raise MachineError("invalid funding route status")
    evidence = raw["evidence_hash"]
    if raw["status"] == "SIMULATED":
        for key in ("evidence_hash", "abi_evidence_hash", "contract_code_hash"):
            if (not isinstance(raw[key], str)
                    or re.fullmatch(r"[0-9a-f]{64}", raw[key]) is None):
                raise MachineError("simulated funding route needs quote, ABI and code evidence")
    elif evidence is not None:
        raise MachineError("unsimulated route cannot carry evidence")
    elif raw["abi_evidence_hash"] is not None or raw["contract_code_hash"] is not None:
        raise MachineError("unsimulated route cannot carry verified binding evidence")
    parameter = raw["parameter_hex"]
    if not isinstance(parameter, str) or len(parameter) > 16384 or len(parameter) % 2 or (
            parameter and re.fullmatch(r"[0-9a-f]+", parameter) is None):
        raise MachineError("invalid route parameter encoding")
    if (not isinstance(raw["function_selector"], str)
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*\([^)]*\)", raw["function_selector"]) is None):
        raise MachineError("invalid route function selector")
    start, end = utc(raw["quoted_at"]), utc(raw["expires_at"])
    if datetime.fromisoformat(start) >= datetime.fromisoformat(end):
        raise MachineError("route expiry must follow quote time")
    return {**raw, "route_id": ident(raw["route_id"], "route id"),
        "scope": scope, "provider_id": provider,
        "from_amount": source, "minimum_to_amount": target,
        "capacity_from_base_units": str(capacity),
        "target_address": tron_address(raw["target_address"]),
        "fee_limit_sun": str(_base_units(raw["fee_limit_sun"], "route fee", positive=True)),
        "quoted_at": start, "expires_at": end}


def _binding(bindings, operation, target, capability):
    expected_hash = capability_hash(capability)
    normalized = [normalize_action_binding(item) for item in bindings]
    found = [item for item in normalized if item["operation"] == operation
             and item["target_address"] == target]
    if len(found) > 1:
        raise MachineError("duplicate action binding")
    if found:
        if (found[0]["network"] != capability["network"]
                or found[0]["capability_hash"] != expected_hash):
            raise MachineError("action binding does not belong to product capability")
        return found[0]
    return {"schema_version": "tron-action-binding-1", "operation": operation,
        "network": capability["network"], "target_address": target,
        "capability_hash": expected_hash, "abi_evidence_hash": None,
        "contract_code_hash": None, "status": "UNVERIFIED",
        "source_url": "https://github.com/justlend/mcp-server-justlend"}


def _intent(intent):
    require_keys(intent, INTENT_KEYS, "PlanIntentV1")
    if (intent["schema_version"] != INTENT_VERSION
            or intent["status"] != "INTENT_PREPARED"):
        raise MachineError("prepared PR04 plan intent required")
    payload = deepcopy(intent)
    claimed = payload.pop("intent_hash", None)
    if claimed != digest({"domain": INTENT_VERSION, "payload": payload}):
        raise MachineError("plan intent commitment mismatch")
    if (intent.get("execution_authority") != "NONE"
            or intent.get("signature_status") != "NOT_REQUESTED"
            or intent.get("chain_status") != "NOT_SUBMITTED"):
        raise MachineError("plan intent has unexpected authority or chain state")
    for key in ("mandate_policy_hash", "mandate_draft_hash", "snapshot_hash",
                "comparison_hash", "plan_hash", "intent_hash"):
        if not isinstance(intent[key], str) or re.fullmatch(r"[0-9a-f]{64}", intent[key]) is None:
            raise MachineError("invalid plan intent commitment")
    start, end = datetime.fromisoformat(utc(intent["as_of"])), datetime.fromisoformat(
        utc(intent["valid_until"]))
    if start >= end or type(intent["revision"]) is not int or intent["revision"] < 1:
        raise MachineError("invalid plan intent version or lifetime")
    if (not isinstance(intent["legs"], list) or not 1 <= len(intent["legs"]) <= 16
            or not isinstance(intent["blockers"], list)
            or len(set(intent["blockers"])) != len(intent["blockers"])):
        raise MachineError("bounded plan legs and unique blockers required")
    if (intent["selected_plan"] not in {"CONSERVATIVE", "GROWTH"}
            or "PR06_TRANSACTION_GRAPH_REQUIRED" not in intent["blockers"]):
        raise MachineError("plan intent lacks compiler selection or graph blocker")
    if normalize_scope(intent["scope"]) != intent["scope"]:
        raise MachineError("plan intent scope is not canonical")
    if decimal(intent["cash_amount"]) < 0 or decimal(intent["total_cost_base"]) < 0:
        raise MachineError("invalid plan intent cash or cost")
    seen, total_weight = set(), 0
    for leg in intent["legs"]:
        require_keys(leg, LEG_KEYS, "PlanIntentLegV1")
        name = ident(leg["product_id"], "plan leg product")
        if name in seen or type(leg["weight_bps"]) is not int or not 1 <= leg["weight_bps"] <= 10000:
            raise MachineError("unique positive weighted plan legs required")
        seen.add(name)
        if decimal(leg["principal_amount"]) <= 0 or decimal(leg["required_budget_base"]) <= 0:
            raise MachineError("plan leg amounts must be positive")
        if leg["status"] != "AWAITING_TRANSACTION_GRAPH":
            raise MachineError("plan leg is not awaiting a transaction graph")
        total_weight += leg["weight_bps"]
        for key in ("identity_hash", "capability_hash", "cashflow_assumption_hash", "quote_hash"):
            if not isinstance(leg[key], str) or re.fullmatch(r"[0-9a-f]{64}", leg[key]) is None:
                raise MachineError("invalid plan leg commitment")
    if total_weight > 10000:
        raise MachineError("plan leg weights exceed total capital")
    return intent


def compile_execution_graph(intent: dict, snapshot: dict, account: dict,
                            execution_quotes: list[dict], funding_routes: list[dict],
                            action_bindings: list[dict], *, at: str,
                            fee_budget_sun: str) -> dict:
    intent = _intent(intent)
    account = normalize_account_snapshot(account)
    at = utc(at)
    if intent["scope"] != account["scope"] or snapshot.get("scope") != intent["scope"]:
        raise MachineError("execution scope mismatch")
    if snapshot.get("snapshot_hash") != intent["snapshot_hash"] or (
            account["snapshot_hash"] != intent["snapshot_hash"]):
        raise MachineError("execution snapshot binding mismatch")
    moment = datetime.fromisoformat(at)
    if not (datetime.fromisoformat(account["observed_at"]) <= moment
            < datetime.fromisoformat(account["valid_until"]) and
            moment < datetime.fromisoformat(utc(intent["valid_until"]))):
        raise MachineError("account or plan intent expired")
    quotes = {item["product_id"]: item for item in map(normalize_execution_quote,
                                                        execution_quotes)}
    if len(quotes) != len(execution_quotes):
        raise MachineError("duplicate execution quote")
    leg_names = {item["product_id"] for item in intent["legs"]}
    if set(quotes) != leg_names:
        raise MachineError("execution quotes must exactly cover plan legs")
    routes = list(map(normalize_funding_route, funding_routes))
    if len({item["route_id"] for item in routes}) != len(routes):
        raise MachineError("duplicate funding route")
    if any(item["scope"] != intent["scope"] or item["intent_hash"] != intent["intent_hash"]
           or item["snapshot_hash"] != intent["snapshot_hash"] for item in routes):
        raise MachineError("funding route belongs to another scope, intent or snapshot")
    fee_budget = _base_units(fee_budget_sun, "graph fee budget", positive=True)
    balances = {_key(item): int(item["amount_base_units"]) for item in account["balances"]}
    existing = {}
    for item in account["reservations"]:
        key = _key(item)
        existing[key] = existing.get(key, 0) + int(item["amount_base_units"])
    available = {key: value - existing.get(key, 0) for key, value in balances.items()}
    if any(value < 0 for value in available.values()):
        raise MachineError("existing reservations exceed observed balance")
    allowances = {(item["token_address"], item["spender_address"]):
                  int(item["amount_base_units"]) for item in account["allowances"]}
    steps, blockers, graph_reserved, expected_inflow, used_routes = [], set(), {}, {}, set()
    fee_total = 0

    def add(kind, operation, depends, product, inputs, outputs, action, postconditions,
            *, status="PLANNED", local_blockers=()):
        if len(steps) >= MAX_STEPS:
            raise MachineError("execution graph step budget exceeded")
        step_id = "step-" + str(len(steps) + 1).zfill(3)
        entry = {"step_id": step_id, "kind": kind, "operation": operation,
            "depends_on": list(dict.fromkeys(depends)), "product_id": product,
            "inputs": inputs, "expected_outputs": outputs, "action": action,
            "postconditions": postconditions, "status": status,
            "blockers": sorted(set(local_blockers)),
            "output_availability": "UNCONFIRMED" if outputs else "NONE"}
        entry["step_hash"] = digest({"domain": "economic-execution-step-1",
                                     "step": entry})
        steps.append(entry)
        blockers.update(entry["blockers"])
        return step_id

    def reserve(asset, amount, product):
        key = _key(asset)
        if available.get(key, 0) < amount:
            return None
        available[key] -= amount
        graph_reserved[key] = graph_reserved.get(key, 0) + amount
        value = {**asset, "amount_base_units": str(amount),
                 "availability": "OBSERVED_ACCOUNT_BALANCE"}
        return add("OFFCHAIN_RESERVATION", "RESERVE_BALANCE", [], product, [value], [],
                   None, [{"kind": "RESERVATION_HELD", "asset": asset["asset"],
                           "amount_base_units": str(amount)}])

    for leg in intent["legs"]:
        name = leg["product_id"]
        product = snapshot.get("products", {}).get(name)
        if product is None or product.get("identity_hash") != leg["identity_hash"] or (
                product.get("capability_hash") != leg["capability_hash"]):
            raise MachineError("plan leg product binding mismatch")
        capability = normalize_capability(product["capability"])
        if capability_hash(capability) != leg["capability_hash"]:
            raise MachineError("product capability commitment mismatch")
        if (leg["action"] != capability["action"]
                or leg["protocol"] != capability["protocol"]):
            raise MachineError("plan leg action or protocol differs from capability")
        quote = quotes.get(name)
        if quote is None:
            blockers.add("EXECUTION_QUOTE_MISSING:" + name)
            continue
        if (quote["intent_hash"] != intent["intent_hash"] or
                quote["snapshot_hash"] != intent["snapshot_hash"] or
                not datetime.fromisoformat(quote["quoted_at"]) <= moment
                < datetime.fromisoformat(quote["expires_at"])):
            raise MachineError("execution quote context or time mismatch")
        token = capability["token"]
        required = _units(leg["principal_amount"], quote["input"]["decimals"],
                          "leg principal")
        if (quote["input"]["asset"] != leg["principal_asset"]
                or int(quote["input"]["amount_base_units"]) != required):
            raise MachineError("execution quote input differs from plan leg")
        if (not name.startswith("usdd.vault.") and name != "justlend.strx" and (
                quote["input"]["token_address"] != token["address"]
                or quote["input"]["decimals"] != token["decimals"])):
            raise MachineError("execution quote token differs from product capability")
        if name == "justlend.strx" and (quote["input"]["asset"] != "TRX"
                or quote["input"]["token_address"] is not None
                or quote["input"]["decimals"] != 6
                or quote["minimum_output"]["asset"] != token["asset"]
                or quote["minimum_output"]["token_address"] != token["address"]
                or quote["minimum_output"]["decimals"] != token["decimals"]):
            raise MachineError("sTRX quote must map native TRX to sTRX")
        if name.startswith("usdd.vault.") and (quote["minimum_output"]["asset"] != "USDD"
                or quote["minimum_output"]["token_address"] != token["address"]
                or quote["minimum_output"]["decimals"] != token["decimals"]):
            raise MachineError("vault quote must state minimum issued USDD")
        local = []
        if quote["simulation_status"] != "SIMULATED":
            local.append("EXECUTION_QUOTE_NOT_SIMULATED:" + name)
        if capability["stages"]["simulate"]["status"] != "SUPPORTED":
            local.append("CAPABILITY_SIMULATION_UNSUPPORTED:" + name)
        if capability["stages"]["execute"]["status"] != "SUPPORTED":
            local.append("CAPABILITY_EXECUTION_UNSUPPORTED:" + name)
        if name.startswith("justlend.v1.") and not product.get("new_supply_allowed", False):
            local.append("NEW_SUPPLY_DISABLED:" + name)
        asset = {"asset": quote["input"]["asset"],
                 "token_address": quote["input"]["token_address"],
                 "decimals": quote["input"]["decimals"]}
        observed = min(required, available.get(_key(asset), 0))
        dependencies, sources = [], []
        if observed:
            dependency = reserve(asset, observed, name)
            dependencies.append(dependency)
            sources.append({**asset, "amount_base_units": str(observed),
                            "availability": "OBSERVED_ACCOUNT_BALANCE"})
        shortage = required - observed
        if shortage:
            candidates = [route for route in routes
                if route["route_id"] not in used_routes
                if route["network"] == intent["scope"]["network"]
                and route["scope"] == intent["scope"]
                and route["intent_hash"] == intent["intent_hash"]
                and route["snapshot_hash"] == intent["snapshot_hash"]
                and route["minimum_to_amount"]["asset"] == asset["asset"]
                and route["minimum_to_amount"]["token_address"] == asset["token_address"]
                and int(route["minimum_to_amount"]["amount_base_units"]) >= shortage
                and datetime.fromisoformat(route["quoted_at"]) <= moment
                < datetime.fromisoformat(route["expires_at"])]
            candidates.sort(key=lambda row: (int(row["from_amount"]["amount_base_units"]),
                                              row["route_id"]))
            route = candidates[0] if candidates else None
            if route is None:
                local.append("FUNDING_SHORTFALL:" + asset["asset"])
            else:
                fundable = [row for row in candidates if available.get(
                    _key(row["from_amount"]), 0) >= int(row["from_amount"]["amount_base_units"])]
                if not fundable:
                    local.append("ROUTE_INPUT_SHORTFALL:" + route["from_amount"]["asset"])
                    route = None
            if route is not None:
                route = fundable[0]
                from_asset = {key: route["from_amount"][key] for key in (
                    "asset", "token_address", "decimals")}
                route_input = int(route["from_amount"]["amount_base_units"])
                reserve_id = reserve(from_asset, route_input, name)
                if reserve_id is None:
                    local.append("ROUTE_INPUT_SHORTFALL:" + from_asset["asset"])
                else:
                    used_routes.add(route["route_id"])
                    route_blockers = [] if route["status"] == "SIMULATED" else [
                        "FUNDING_ROUTE_NOT_SIMULATED:" + route["route_id"]]
                    action = {"schema_version": "tron-route-action-1",
                        "route_id": route["route_id"], "transport": "TRIGGER_SMART_CONTRACT",
                        "target_address": route["target_address"],
                        "function_selector": route["function_selector"],
                        "parameter_hex": route["parameter_hex"], "call_value_sun": "0",
                        "fee_limit_sun": route["fee_limit_sun"],
                        "expires_at": route["expires_at"],
                        "result_semantics": "RECEIPT_SUCCESS",
                        "simulation_evidence_hash": route["evidence_hash"],
                        "abi_evidence_hash": route["abi_evidence_hash"],
                        "contract_code_hash": route["contract_code_hash"],
                        "status": "READY_FOR_SIMULATION" if not route_blockers else "BLOCKED",
                        "signature_status": "NOT_REQUESTED", "transaction_status": "NOT_BUILT",
                        "execution_authority": "NONE"}
                    action["action_hash"] = digest(action)
                    output = {**asset, "amount_base_units":
                              route["minimum_to_amount"]["amount_base_units"],
                              "availability": "DEPENDENCY_OUTPUT_UNCONFIRMED"}
                    route_id = add("ONCHAIN_CALL", "CONVERT", [reserve_id], name,
                        [{**from_asset, "amount_base_units": str(route_input),
                          "availability": "OBSERVED_ACCOUNT_BALANCE"}], [output], action,
                        [{"kind": "MIN_OUTPUT", "asset": asset["asset"],
                          "amount_base_units": str(shortage)}],
                        status="BLOCKED" if route_blockers else "PLANNED",
                        local_blockers=route_blockers)
                    dependencies.append(route_id)
                    fee_total += int(route["fee_limit_sun"])
                    expected_inflow[_key(asset)] = expected_inflow.get(_key(asset), 0) + int(
                        route["minimum_to_amount"]["amount_base_units"])
                    sources.append({**asset, "amount_base_units": str(shortage),
                                    "availability": "DEPENDENCY_OUTPUT_UNCONFIRMED",
                                    "source_step_id": route_id})
        operation = None
        arguments = []
        call_value = "0"
        if name == "justlend.v1.jTRX" and token["asset"] == "TRX" and token["address"] is None:
            operation, call_value = "JUSTLEND_SUPPLY_TRX", str(required)
        elif name.startswith("justlend.v1."):
            operation, arguments = "JUSTLEND_SUPPLY", [str(required)]
        elif name == "justlend.strx":
            operation, call_value = "STRX_STAKE", str(required)
        elif name == "tron.native.stake":
            operation, arguments = "TRON_STAKE", [str(required), "ENERGY"]
        elif name.startswith("usdd.vault."):
            # A PR04 vault quote proves economics, not an exact user-vault id,
            # collateral/debt delta, proxy, join or exit calldata.  Represent
            # the lifecycle without inventing executable arguments.
            for vault_op in ("USDD_VAULT_OPEN", "USDD_VAULT_ADD_COLLATERAL",
                             "USDD_VAULT_MINT"):
                action = {"schema_version": "tron-composite-action-placeholder-1",
                    "operation": vault_op, "transport": "UNVERIFIED_COMPOSITE",
                    "target_address": capability["contract"], "arguments": None,
                    "parameter_hex": None, "call_value_sun": None,
                    "fee_limit_sun": quote["fee_limit_sun"],
                    "expires_at": quote["expires_at"], "status": "BLOCKED",
                    "blockers": ["VAULT_EXACT_PROXY_JOIN_EXIT_CALLDATA_REQUIRED"],
                    "signature_status": "NOT_REQUESTED",
                    "transaction_status": "NOT_BUILT", "execution_authority": "NONE"}
                action["action_hash"] = digest(action)
                dependencies = [add("ONCHAIN_CALL", vault_op, dependencies, name, sources, [],
                    action, [{"kind": "VAULT_POSITION_REQUERY_REQUIRED"}], status="BLOCKED",
                    local_blockers=[*local, *action["blockers"]])]
            fee_total += int(quote["fee_limit_sun"]) * 3
            continue
        else:
            local.append("UNSUPPORTED_PRODUCT_GRAPH:" + name)
        if operation is None:
            blockers.update(local)
            continue
        approval_id = None
        if operation == "JUSTLEND_SUPPLY" and token["address"] is not None:
            allowance = allowances.get((token["address"], capability["contract"]), 0)
            if allowance < required:
                approve_binding = _binding(action_bindings, "TRC20_APPROVE",
                                           token["address"], capability)
                approve = compile_action(approve_binding,
                    [capability["contract"], str(required)], call_value_sun="0",
                    fee_limit_sun=quote["fee_limit_sun"], expires_at=quote["expires_at"])
                approval_id = add("ONCHAIN_CALL", "TRC20_APPROVE", dependencies, name, [], [],
                    approve, [{"kind": "ALLOWANCE_EQUALS", "token_address": token["address"],
                               "spender_address": capability["contract"],
                               "amount_base_units": str(required)}],
                    status="BLOCKED" if approve["status"] == "BLOCKED" else "PLANNED",
                    local_blockers=approve["blockers"])
                dependencies = [approval_id]
                fee_total += int(quote["fee_limit_sun"])
        binding = _binding(action_bindings, operation, capability["contract"], capability)
        action = compile_action(binding, arguments, call_value_sun=call_value,
            fee_limit_sun=quote["fee_limit_sun"], expires_at=quote["expires_at"])
        local.extend(action["blockers"])
        output = {**quote["minimum_output"], "availability": "UNCONFIRMED"}
        post = [{"kind": "MIN_OUTPUT", "asset": output["asset"],
                 "amount_base_units": output["amount_base_units"]},
                {"kind": "INPUT_DECREASE", "asset": asset["asset"],
                 "amount_base_units": str(required)}]
        if operation == "JUSTLEND_SUPPLY":
            post.append({"kind": "PROTOCOL_RESULT", "expected": "0"})
        add("NATIVE_SYSTEM" if action["transport"] == "TRON_SYSTEM_CONTRACT" else "ONCHAIN_CALL",
            operation, dependencies, name, sources, [output], action, post,
            status="BLOCKED" if local else "PLANNED", local_blockers=local)
        fee_total += int(quote["fee_limit_sun"])
    if fee_total > fee_budget:
        blockers.add("GRAPH_FEE_BUDGET_EXCEEDED")
    if not steps:
        blockers.add("NO_EXECUTION_STEPS")
    conservation = []
    keys = sorted(set(balances) | set(existing) | set(graph_reserved) | set(expected_inflow),
                  key=lambda value: (value[0], value[1] or "", value[2]))
    for key in keys:
        conservation.append({"asset": key[0], "token_address": key[1], "decimals": key[2],
            "observed_base_units": str(balances.get(key, 0)),
            "existing_reserved_base_units": str(existing.get(key, 0)),
            "graph_reserved_base_units": str(graph_reserved.get(key, 0)),
            "remaining_observed_base_units": str(available.get(key, 0)),
            "expected_inflow_unconfirmed_base_units": str(expected_inflow.get(key, 0))})
    result = {"schema_version": VERSION,
        "status": "BLOCKED" if blockers else "READY_FOR_SIMULATION",
        "execution_semantics": "ORDERED_MULTI_TRANSACTION_NON_ATOMIC",
        "scope": intent["scope"], "intent_hash": intent["intent_hash"],
        "plan_hash": intent["plan_hash"], "snapshot_hash": intent["snapshot_hash"],
        "account_snapshot_hash": digest(account), "created_at": at,
        "valid_until": intent["valid_until"], "fee_budget_sun": str(fee_budget),
        "maximum_fee_limits_sun": str(fee_total), "steps": steps,
        "execution_quote_hashes": [{"product_id": name, "quote_hash": digest(quotes[name])}
                                    for name in sorted(quotes)],
        "funding_route_hashes": [{"route_id": row["route_id"], "route_hash": digest(row)}
                                 for row in sorted(routes, key=lambda item: item["route_id"])],
        "asset_conservation": conservation, "blockers": sorted(blockers),
        "simulation_status": "NOT_RUN", "approval_status": "NOT_REQUESTED",
        "signature_status": "NOT_REQUESTED", "chain_status": "NOT_SUBMITTED",
        "execution_authority": "NONE"}
    result["graph_hash"] = digest({"domain": VERSION, "graph": result})
    return result


def verify_execution_graph(graph: dict, intent: dict, snapshot: dict, account: dict,
                           execution_quotes: list[dict], funding_routes: list[dict],
                           action_bindings: list[dict], *, at: str,
                           fee_budget_sun: str) -> bool:
    try:
        return graph == compile_execution_graph(intent, snapshot, account, execution_quotes,
            funding_routes, action_bindings, at=at, fee_budget_sun=fee_budget_sun)
    except (MachineError, KeyError, TypeError, ValueError, ArithmeticError):
        return False


def compile_lifecycle_graph(product: dict, account: dict, request: dict,
                            action_bindings: list[dict], *, at: str,
                            fee_limit_sun: str) -> dict:
    """Compile withdrawals, claims, delegation and vault repayment fail-closed."""
    require_keys(request, {"schema_version", "scope", "snapshot_hash", "product_id",
        "identity_hash", "capability_hash", "operation", "input", "minimum_output",
        "recipient", "resource", "vault_id", "claim_after", "requested_at",
        "valid_until"}, "LifecycleRequestV1")
    if request["schema_version"] != LIFECYCLE_VERSION:
        raise MachineError("unsupported lifecycle request version")
    account = normalize_account_snapshot(account)
    at = utc(at)
    start = datetime.fromisoformat(utc(request["requested_at"]))
    end = datetime.fromisoformat(utc(request["valid_until"]))
    moment = datetime.fromisoformat(at)
    if not start <= moment < end:
        raise MachineError("lifecycle request is not currently valid")
    scope = normalize_scope(request["scope"])
    if scope != account["scope"] or request["snapshot_hash"] != account["snapshot_hash"]:
        raise MachineError("lifecycle account binding mismatch")
    name = ident(request["product_id"], "lifecycle product")
    if (product.get("product_id") != name or product.get("identity_hash") != request["identity_hash"]
            or product.get("capability_hash") != request["capability_hash"]):
        raise MachineError("lifecycle product commitment mismatch")
    capability = normalize_capability(product["capability"])
    if capability_hash(capability) != request["capability_hash"]:
        raise MachineError("lifecycle capability commitment mismatch")
    operation = ident(request["operation"], "lifecycle operation")
    allowed = ({"JUSTLEND_REDEEM_SHARES", "JUSTLEND_REDEEM_UNDERLYING"}
               if name.startswith("justlend.v1.") else
               {"STRX_UNSTAKE", "STRX_CLAIM"} if name == "justlend.strx" else
               {"TRON_UNSTAKE", "TRON_DELEGATE", "TRON_UNDELEGATE",
                "TRON_CLAIM_UNSTAKED"} if name == "tron.native.stake" else
               {"USDD_VAULT_REPAY", "USDD_VAULT_WITHDRAW_COLLATERAL"}
               if name.startswith("usdd.vault.") else set())
    if operation not in allowed:
        raise MachineError("operation is incompatible with product")
    item_in, item_out = _asset(request["input"], "lifecycle input"), _asset(
        request["minimum_output"], "lifecycle minimum output")
    amount = _base_units(item_in["amount_base_units"], "lifecycle amount", positive=True)
    _base_units(item_out["amount_base_units"], "lifecycle minimum output", positive=True)
    recipient = tron_address(request["recipient"])
    if recipient != scope["wallet"] and operation not in {"TRON_DELEGATE", "TRON_UNDELEGATE"}:
        raise MachineError("lifecycle output recipient must be the scoped wallet")
    resource = request["resource"]
    if resource is not None and resource not in {"ENERGY", "BANDWIDTH"}:
        raise MachineError("unsupported lifecycle resource")
    vault_id = request["vault_id"]
    if vault_id is not None:
        vault_id = str(_base_units(vault_id, "vault id", positive=True))
    claim_after = utc(request["claim_after"]) if request["claim_after"] is not None else None
    if operation in {"STRX_UNSTAKE", "TRON_UNSTAKE"} and claim_after is not None and not (
            moment < datetime.fromisoformat(claim_after) < end):
        raise MachineError("claim time must be after compile time and before request expiry")
    if (operation not in {"STRX_UNSTAKE", "TRON_UNSTAKE", "STRX_CLAIM",
                          "TRON_CLAIM_UNSTAKED"} and claim_after is not None):
        raise MachineError("claim time is incompatible with lifecycle operation")
    fee = str(_base_units(fee_limit_sun, "lifecycle fee", positive=True))
    blockers = []
    for stage in ("simulate", "execute"):
        if capability["stages"][stage]["status"] != "SUPPORTED":
            blockers.append("CAPABILITY_" + stage.upper() + "_UNSUPPORTED:" + name)
    position = next((row for row in account["positions"] if row["product_id"] == name), None)
    vault = next((row for row in account["vaults"] if row["product_id"] == name), None)
    liquidity = {row["product_id"]: int(row["amount_base_units"])
                 for row in account["market_liquidity"]}.get(name, 0)
    available = {(_key(row)): int(row["amount_base_units"]) for row in account["balances"]}
    for row in account["reservations"]:
        key = _key(row)
        available[key] = available.get(key, 0) - int(row["amount_base_units"])
    inputs = [{**item_in, "availability": "OBSERVED_ACCOUNT_STATE"}]
    post = [{"kind": "MIN_OUTPUT", "asset": item_out["asset"],
             "amount_base_units": item_out["amount_base_units"]}]
    args = []
    target = capability["contract"]
    if operation == "JUSTLEND_REDEEM_SHARES":
        if position is None or int(position["shares_base_units"]) < amount:
            blockers.append("POSITION_SHARE_SHORTFALL")
        if liquidity < int(item_out["amount_base_units"]):
            blockers.append("MARKET_LIQUIDITY_SHORTFALL")
        args = [str(amount)]
        post.append({"kind": "POSITION_SHARES_AT_LEAST", "amount_base_units": str(amount)})
        post.append({"kind": "MARKET_LIQUIDITY_AT_LEAST",
                     "amount_base_units": item_out["amount_base_units"]})
    elif operation == "JUSTLEND_REDEEM_UNDERLYING":
        if position is None or int(position["underlying_base_units"]) < amount:
            blockers.append("POSITION_UNDERLYING_SHORTFALL")
        if liquidity < amount:
            blockers.append("MARKET_LIQUIDITY_SHORTFALL")
        args = [str(amount)]
        post.append({"kind": "POSITION_UNDERLYING_AT_LEAST", "amount_base_units": str(amount)})
        post.append({"kind": "MARKET_LIQUIDITY_AT_LEAST",
                     "amount_base_units": str(amount)})
    elif operation == "STRX_UNSTAKE":
        if position is None or int(position["shares_base_units"]) < amount:
            blockers.append("POSITION_SHARE_SHORTFALL")
        if claim_after is None:
            blockers.append("DELAYED_CLAIM_TIME_REQUIRED")
        args = [str(amount)]
        post.append({"kind": "POSITION_SHARES_AT_LEAST", "amount_base_units": str(amount)})
        post.append({"kind": "WITHDRAWAL_QUEUE_CREATED"})
    elif operation == "STRX_CLAIM":
        # Time alone does not prove that claimAll has a claimable queue.  The
        # current account schema has no authenticated pending-withdrawal row.
        blockers.append("CLAIMABLE_WITHDRAWAL_OBSERVATION_REQUIRED")
        if claim_after is None or moment < datetime.fromisoformat(claim_after):
            blockers.append("WITHDRAWAL_NOT_YET_CLAIMABLE")
    elif operation in {"TRON_UNSTAKE", "TRON_DELEGATE", "TRON_UNDELEGATE"}:
        if resource is None:
            raise MachineError("native resource operation needs an explicit resource")
        if position is None or int(position["underlying_base_units"]) < amount:
            blockers.append("STAKED_TRX_SHORTFALL")
        args = ([str(amount), resource] if operation == "TRON_UNSTAKE" else
                [str(amount), resource, recipient, False] if operation == "TRON_DELEGATE" else
                [str(amount), resource, recipient])
        target = None
        post.append({"kind": "POSITION_UNDERLYING_AT_LEAST", "amount_base_units": str(amount)})
        if operation == "TRON_UNSTAKE":
            if claim_after is None:
                blockers.append("DELAYED_CLAIM_TIME_REQUIRED")
            post.append({"kind": "UNFREEZE_QUEUE_CREATED"})
    elif operation == "TRON_CLAIM_UNSTAKED":
        target = None
        blockers.append("CLAIMABLE_UNFREEZE_OBSERVATION_REQUIRED")
        if claim_after is None or moment < datetime.fromisoformat(claim_after):
            blockers.append("UNFREEZE_NOT_YET_CLAIMABLE")
    else:
        if vault is None or vault_id != vault["vault_id"]:
            blockers.append("VAULT_POSITION_NOT_OBSERVED")
        if operation == "USDD_VAULT_REPAY":
            if available.get(_key(item_in), 0) < amount:
                blockers.append("USDD_REPAY_BALANCE_SHORTFALL")
            if vault is not None and int(vault["debt_base_units"]) < amount:
                blockers.append("REPAY_EXCEEDS_OBSERVED_DEBT")
            post.extend([{"kind": "BALANCE_AT_LEAST", "asset": item_in["asset"],
                          "token_address": item_in["token_address"],
                          "decimals": item_in["decimals"], "amount_base_units": str(amount)},
                         {"kind": "VAULT_DEBT_AT_LEAST", "amount_base_units": str(amount)}])
        elif vault is not None and int(vault["collateral_base_units"]) < amount:
            blockers.append("COLLATERAL_WITHDRAWAL_EXCEEDS_POSITION")
        if operation == "USDD_VAULT_WITHDRAW_COLLATERAL":
            post.append({"kind": "VAULT_COLLATERAL_AT_LEAST",
                         "amount_base_units": str(amount)})

    if operation.startswith("USDD_VAULT_"):
        action = {"schema_version": "tron-composite-action-placeholder-1",
            "operation": operation, "transport": "UNVERIFIED_COMPOSITE",
            "target_address": target, "arguments": None, "parameter_hex": None,
            "call_value_sun": None, "fee_limit_sun": fee, "expires_at": utc(
                request["valid_until"]), "status": "BLOCKED",
            "blockers": ["VAULT_EXACT_PROXY_JOIN_EXIT_CALLDATA_REQUIRED"],
            "signature_status": "NOT_REQUESTED", "transaction_status": "NOT_BUILT",
            "execution_authority": "NONE"}
        action["action_hash"] = digest(action)
        blockers.extend(action["blockers"])
    else:
        binding = _binding(action_bindings, operation, target, capability)
        action = compile_action(binding, args, call_value_sun="0", fee_limit_sun=fee,
                                expires_at=request["valid_until"])
        blockers.extend(action["blockers"])
    delayed_operation = operation in {"STRX_UNSTAKE", "TRON_UNSTAKE"}
    step_outputs = [] if delayed_operation and claim_after is not None else [
        {**item_out, "availability": "UNCONFIRMED"}]
    step = {"step_id": "step-001", "kind": "NATIVE_SYSTEM" if action["transport"] == "TRON_SYSTEM_CONTRACT"
            else "ONCHAIN_CALL", "operation": operation, "depends_on": [],
        "product_id": name, "inputs": inputs,
        "expected_outputs": step_outputs,
        "action": action, "postconditions": post,
        "status": "BLOCKED" if blockers else "PLANNED", "blockers": sorted(set(blockers)),
        "output_availability": "UNCONFIRMED" if step_outputs else "NONE"}
    step["step_hash"] = digest({"domain": "economic-execution-step-1", "step": step})
    steps = [step]
    maximum_fee = int(fee)
    if delayed_operation and claim_after is not None:
        claim_operation = "STRX_CLAIM" if operation == "STRX_UNSTAKE" else "TRON_CLAIM_UNSTAKED"
        claim_target = capability["contract"] if claim_operation == "STRX_CLAIM" else None
        claim_binding = _binding(action_bindings, claim_operation, claim_target, capability)
        claim_action = compile_action(claim_binding, [], call_value_sun="0", fee_limit_sun=fee,
                                      expires_at=request["valid_until"])
        claim_blockers = [*claim_action["blockers"],
                          "CONFIRMED_WITHDRAWAL_AND_FRESH_SNAPSHOT_REQUIRED"]
        blockers.extend(claim_blockers)
        claim_step = {"step_id": "step-002",
            "kind": "NATIVE_SYSTEM" if claim_action["transport"] == "TRON_SYSTEM_CONTRACT"
                    else "ONCHAIN_CALL", "operation": claim_operation,
            "depends_on": ["step-001"], "product_id": name, "inputs": [],
            "expected_outputs": [{**item_out, "availability": "UNCONFIRMED"}],
            "action": claim_action,
            "postconditions": [{"kind": "NOT_BEFORE", "timestamp": claim_after},
                               {"kind": "MIN_OUTPUT", "asset": item_out["asset"],
                                "amount_base_units": item_out["amount_base_units"]}],
            "status": "BLOCKED", "blockers": sorted(set(claim_blockers)),
            "output_availability": "UNCONFIRMED"}
        claim_step["step_hash"] = digest({"domain": "economic-execution-step-1",
                                          "step": claim_step})
        steps.append(claim_step)
        maximum_fee += int(fee)
    request_hash = digest({"domain": LIFECYCLE_VERSION, "request": request})
    result = {"schema_version": VERSION, "status": "BLOCKED" if blockers else
        "READY_FOR_SIMULATION", "execution_semantics": "ORDERED_MULTI_TRANSACTION_NON_ATOMIC",
        "scope": scope, "intent_hash": request_hash, "plan_hash": request_hash,
        "snapshot_hash": request["snapshot_hash"], "account_snapshot_hash": digest(account),
        "created_at": at, "valid_until": utc(request["valid_until"]),
        "fee_budget_sun": str(maximum_fee), "maximum_fee_limits_sun": str(maximum_fee),
        "steps": steps,
        "execution_quote_hashes": [], "funding_route_hashes": [], "asset_conservation": [],
        "blockers": sorted(set(blockers)), "simulation_status": "NOT_RUN",
        "approval_status": "NOT_REQUESTED", "signature_status": "NOT_REQUESTED",
        "chain_status": "NOT_SUBMITTED", "execution_authority": "NONE"}
    result["graph_hash"] = digest({"domain": VERSION, "graph": result})
    return result
