"""Complete bounded allocation search over PR03 snapshot-bound cashflows.

Every nonzero candidate leg is recalculated at its exact amount. The search
does not interpolate a one-unit quote, trust an LLM score, or call a transaction
adapter. Results are comparison/intent commitments without signing authority.
"""

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from math import comb

from .application import confirmed_mandate, single_asset_budget
from .liquidity import available_at
from .mandate import integer, reserve_amount
from .tron_cashflow import VERSION as CASHFLOW_VERSION, calculate_tron_cashflows
from .values import MachineError, decimal, decstr, digest, ident, require_keys, utc


VERSION = "economic-plan-comparison-1"
REQUEST_VERSION = "economic-plan-request-1"
INTENT_VERSION = "economic-plan-intent-1"
MAX_CANDIDATES = 4096
REQUEST_KEYS = {"schema_version", "snapshot_hash", "grid_step_bps",
                "min_plan_distance_bps", "max_plan_age_seconds", "scenarios",
                "product_templates"}
TEMPLATE_KEYS = {"product_id", "max_bps", "current_bps", "daily_loss_bps",
                 "stress_loss_bps", "quote"}


def _bps(value, label, maximum=10000):
    return integer(value, label, 0, maximum)


def _asset(snapshot, product_id):
    product = snapshot["products"][product_id]
    if product_id in {"tron.native.stake", "justlend.strx"}:
        return "TRX", 6
    if product_id.startswith("usdd.vault."):
        # A Vault quote's principal is collateral, not issued USDD or the base
        # asset. PR03 currently registers only TRX and USDT ilks and both use
        # six decimal token atoms. Transaction routing remains a later PR.
        collateral = product["ilk"].split("-", 1)[0]
        if collateral not in {"TRX", "USDT"}:
            raise MachineError("unsupported Vault collateral asset")
        return collateral, 6
    token = product["capability"]["token"]
    return token["asset"], token["decimals"]


def _principal_for(snapshot, template, capital, weight):
    quote = template["quote"]
    asset, decimals = _asset(snapshot, template["product_id"])
    prices = quote.get("prices_base")
    if not isinstance(prices, dict) or asset not in prices:
        raise MachineError("plan template lacks the product base price")
    price = decimal(prices[asset])
    if price <= 0:
        raise MachineError("plan template price must be positive")
    target = capital * Decimal(weight) / 10000
    atom = Decimal(1).scaleb(-decimals)
    amount = (target / price).quantize(atom, rounding=ROUND_FLOOR)
    if amount <= 0:
        raise MachineError("grid leg is below one underlying token atom")
    return decstr(amount)


def _scaled(value, factor, decimals, *, liability=False):
    atom = Decimal(1).scaleb(-decimals)
    rounding = ROUND_CEILING if liability else ROUND_FLOOR
    return decstr((decimal(value) * factor).quantize(atom, rounding=rounding))


def _rescale_quote(snapshot, template, principal):
    """Scale only product quantities whose PR03 quote is amount-specific.

    Prices, rates, fees, wait times and resource windows stay unchanged. Vault
    debt/deployment and an sTRX exit queue describe quantities relative to the
    quote's original principal and therefore must move with a grid candidate.
    """
    quote = deepcopy(template["quote"])
    basis = decimal(quote["principal"])
    target = decimal(principal)
    if basis <= 0:
        raise MachineError("plan quote basis principal must be positive")
    factor = target / basis
    name = template["product_id"]
    if name.startswith("usdd.vault."):
        vault = quote.get("vault")
        if not isinstance(vault, dict):
            raise MachineError("Vault plan template requires Vault accounting terms")
        decimals = snapshot["products"]["justlend.v1.jUSDD"]["capability"]["token"]["decimals"]
        vault["deployed_usdd"] = _scaled(vault["deployed_usdd"], factor, decimals)
        vault["stored_debt_usdd"] = _scaled(
            vault["stored_debt_usdd"], factor, decimals, liability=True)
    elif name == "justlend.strx" and quote.get("exit_tranches") is not None:
        tranches = quote["exit_tranches"]
        if not isinstance(tranches, list) or not tranches:
            raise MachineError("sTRX exit tranches must be a nonempty list")
        if any(not isinstance(item, dict) or set(item) != {"amount", "after_seconds"}
               for item in tranches):
            raise MachineError("invalid sTRX exit tranche")
        if sum((decimal(item["amount"]) for item in tranches), Decimal(0)) != basis:
            raise MachineError("sTRX quote basis tranches must conserve principal")
        allocated = Decimal(0)
        scaled = []
        for index, item in enumerate(tranches):
            amount = (target - allocated if index == len(tranches) - 1
                      else decimal(_scaled(item["amount"], factor, 6)))
            if amount < 0:
                raise MachineError("scaled sTRX exit tranches exceed principal")
            allocated += amount
            scaled.append({"amount": decstr(amount),
                           "after_seconds": item["after_seconds"]})
        quote["exit_tranches"] = scaled
    quote["principal"] = decstr(target)
    return quote


def _validate_request(request, snapshot):
    require_keys(request, REQUEST_KEYS, "PlanRequestV1")
    if request["schema_version"] != REQUEST_VERSION:
        raise MachineError("unsupported plan request version")
    if request["snapshot_hash"] != snapshot["snapshot_hash"]:
        raise MachineError("plan request snapshot binding mismatch")
    step = _bps(request["grid_step_bps"], "grid step")
    if step == 0 or 10000 % step:
        raise MachineError("grid step must divide 10000 basis points")
    distance = _bps(request["min_plan_distance_bps"], "plan distance")
    if distance == 0:
        raise MachineError("two plans require positive allocation distance")
    integer(request["max_plan_age_seconds"], "plan lifetime", 1, 86400)
    scenarios = request["scenarios"]
    if (not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 16
            or len(set(scenarios)) != len(scenarios)):
        raise MachineError("bounded distinct stress scenarios required")
    for scenario in scenarios:
        ident(scenario, "stress scenario")
    templates = request["product_templates"]
    if not isinstance(templates, list) or not 1 <= len(templates) <= 16:
        raise MachineError("bounded product templates required")
    book = {}
    current = 0
    for template in templates:
        require_keys(template, TEMPLATE_KEYS, "PlanProductTemplate")
        name = ident(template["product_id"], "plan product")
        if name in book or name not in snapshot["products"]:
            raise MachineError("duplicate or unknown plan product")
        if not isinstance(template["quote"], dict):
            raise MachineError("plan product quote must be an object")
        if template["quote"].get("product_id") != name:
            raise MachineError("cashflow quote/product mismatch")
        maximum = _bps(template["max_bps"], "product maximum")
        present = _bps(template["current_bps"], "current product weight")
        current += present
        stress = template["stress_loss_bps"]
        if not isinstance(stress, dict) or set(stress) != set(scenarios):
            raise MachineError("product stress assumptions must cover all scenarios")
        for value in stress.values():
            _bps(value, "stress loss", 10000)
        daily = _bps(template["daily_loss_bps"], "daily loss", 10000)
        book[name] = {**template, "max_bps": maximum, "current_bps": present,
                      "daily_loss_bps": daily}
    if current > 10000:
        raise MachineError("current product weights exceed capital")
    names = sorted(book)
    slots = 10000 // step
    count = comb(slots + len(names), len(names))
    if count > MAX_CANDIDATES:
        raise MachineError("grid exceeds complete enumeration budget")
    return step, distance, scenarios, names, book, count


def _enumerate(names, step):
    slots = 10000 // step
    rows = []

    def walk(index, remaining, chosen):
        if index == len(names):
            rows.append(dict(chosen))
            return
        name = names[index]
        for units in range(remaining + 1):
            chosen[name] = units * step
            walk(index + 1, remaining - units, chosen)
        del chosen[name]

    walk(0, slots, {})
    return rows


def _whole_bps(amount, capital):
    return int((amount * 10000 / capital).to_integral_value(rounding=ROUND_FLOOR))


def _adjustments(histogram):
    mapping = {
        "CASH_OR_TOTAL_BUDGET": "LOWER_INVESTED_AMOUNT_OR_CASH_REQUIREMENT",
        "FEE_LIMIT": "RAISE_FEE_LIMIT_OR_USE_LOWER_COST_PRODUCTS",
        "CUMULATIVE_AMOUNT_LIMIT": "RAISE_CUMULATIVE_LIMIT_OR_LOWER_INVESTMENT",
        "DAILY_LOSS_LIMIT": "RAISE_DAILY_LOSS_LIMIT_OR_USE_LOWER_RISK_PRODUCTS",
        "STRESS_LOSS_LIMIT": "RAISE_STRESS_BUDGET_OR_USE_LOWER_RISK_PRODUCTS",
        "LIQUIDITY_REQUIREMENT": "RELAX_WITHDRAWAL_SCHEDULE_OR_USE_FASTER_EXITS",
        "PROTOCOL_LIMIT": "RAISE_PROTOCOL_CAP_OR_DIVERSIFY_PROTOCOLS",
        "PRICE_EXPOSURE_LIMIT": "RAISE_PRICE_EXPOSURE_CAP_OR_REMOVE_EXPOSED_PRODUCTS",
        "TURNOVER_LIMIT": "RAISE_TURNOVER_CAP_OR_STAY_CLOSER_TO_CURRENT_WEIGHTS",
        "PRODUCT": "RAISE_PRODUCT_CAP_OR_ADD_ANOTHER_PRODUCT",
        "NO_INVESTED_CAPITAL": "LOWER_MINIMUM_INVESTMENT_EXPECTATION",
    }
    hints = {mapping[reason] for reason in histogram if reason in mapping}
    return sorted(hints)


def _evaluate_candidate(record, snapshot, assembler, request, book, weights, scenarios, at, index):
    mandate = confirmed_mandate(record, at)
    terms = mandate["terms"]
    _, capital_text, _ = single_asset_budget(mandate)
    capital = decimal(capital_text)
    active = []
    reasons = []
    for name, weight in sorted(weights.items()):
        if not weight:
            continue
        if weight > book[name]["max_bps"]:
            reasons.append("PRODUCT:" + name + ":MAXIMUM_EXCEEDED")
            continue
        principal = _principal_for(snapshot, book[name], capital, weight)
        quote = _rescale_quote(snapshot, book[name], principal)
        active.append(quote)
    if not active:
        return {"candidate_id": f"grid_{index:04d}", "weights_bps": weights,
                "eligible": False,
                "reason_codes": sorted(set(reasons + ["NO_INVESTED_CAPITAL"]))}
    assumptions = {"schema_version": CASHFLOW_VERSION,
                   "snapshot_hash": snapshot["snapshot_hash"], "quotes": active}
    cashflows = calculate_tron_cashflows(record, snapshot, assumptions,
                                         assembler=assembler, at=at)
    rows = cashflows["quotes"]
    for row in rows:
        if row["status"] != "CALCULATED_ASSUMPTIONS":
            reasons.extend("PRODUCT:" + row["product_id"] + ":" + reason
                           for reason in row["reason_codes"])
    required = sum((decimal(row["accounting"]["required_budget_base"])
                    + decimal(row["repayment_reserve_base"])
                    for row in rows if "accounting" in row), Decimal(0))
    fees = sum((decimal(row["accounting"]["total_cost_base"])
                for row in rows if "accounting" in row), Decimal(0))
    principal = sum((decimal(row["accounting"]["principal_base"])
                     for row in rows if "accounting" in row), Decimal(0))
    net_income = sum((decimal(row["accounting"]["net_income_base"], signed=True)
                      for row in rows if "accounting" in row), Decimal(0))
    cash = capital - required
    immediate = reserve_amount(terms["immediate_cash"], capital_text)
    if cash < immediate or cash < 0:
        reasons.append("CASH_OR_TOTAL_BUDGET")
    if fees > decimal(terms["limits"]["fee_amount"]["amount"]):
        reasons.append("FEE_LIMIT")
    if principal > decimal(terms["limits"]["cumulative_amount"]["amount"]):
        reasons.append("CUMULATIVE_AMOUNT_LIMIT")
    protocol = {}
    exposure = {}
    for row in rows:
        if "accounting" not in row:
            continue
        product = snapshot["products"][row["product_id"]]
        proto = product["capability"]["protocol"]
        base = decimal(row["accounting"]["principal_base"])
        protocol[proto] = protocol.get(proto, Decimal(0)) + base
        for asset, value in row["price_exposure_base"].items():
            exposure[asset] = exposure.get(asset, Decimal(0)) + decimal(value)
        binding = row["detail"].get("destination_binding") if isinstance(row.get("detail"), dict) else None
        if binding:
            value = decimal(row["detail"]["deployed_usdd"]) * decimal(book[row["product_id"]]["quote"]["prices_base"]["USDD"])
            protocol["justlend"] = protocol.get("justlend", Decimal(0)) + value
    for proto, value in protocol.items():
        if value * 10000 > capital * terms["protocol_caps_bps"].get(proto, 0):
            reasons.append("PROTOCOL_LIMIT")
    for asset, value in exposure.items():
        if asset != "USDT" and value * 10000 > capital * terms["price_exposure_caps_bps"].get(asset, 0):
            reasons.append("PRICE_EXPOSURE_LIMIT")
    requirements = [(0, immediate)] + [
        (item["after_seconds"], reserve_amount(item["minimum"], capital_text))
        for item in terms["withdrawals"]]
    liquidity = []
    for when, minimum in requirements:
        available = max(Decimal(0), cash) + sum(
            (available_at(row["liquidity"], when) for row in rows if "liquidity" in row), Decimal(0))
        liquidity.append({"after_seconds": when, "required": decstr(minimum),
                          "available": decstr(available), "satisfied": available >= minimum})
    if not all(item["satisfied"] for item in liquidity):
        reasons.append("LIQUIDITY_REQUIREMENT")
    daily_loss = Decimal(0)
    stress = {}
    for row in rows:
        if "accounting" not in row:
            continue
        template = book[row["product_id"]]
        base = decimal(row["accounting"]["principal_base"])
        daily_loss += base * template["daily_loss_bps"] / 10000
    if daily_loss > decimal(terms["limits"]["daily_loss"]["amount"]):
        reasons.append("DAILY_LOSS_LIMIT")
    for scenario in scenarios:
        loss = Decimal(0)
        for row in rows:
            if "accounting" not in row:
                continue
            template = book[row["product_id"]]
            base = decimal(row["accounting"]["principal_base"])
            assumed = base * template["stress_loss_bps"][scenario] / 10000
            detail = row.get("detail", {})
            actual = next((decimal(item["loss_base"]) for item in detail.get("scenarios", [])
                           if item["name"] == scenario), Decimal(0))
            principal_loss = decimal(row["principal_loss_reserve_base"])
            loss += max(assumed, actual, principal_loss)
        stress[scenario] = decstr(loss)
    worst = max((decimal(value) for value in stress.values()), default=Decimal(0))
    if worst > decimal(terms["limits"]["stress_loss"]["amount"]):
        reasons.append("STRESS_LOSS_LIMIT")
    turnover = sum(abs(weights[name] - book[name]["current_bps"]) for name in weights)
    turnover += abs(_whole_bps(max(Decimal(0), cash), capital)
                    - (10000 - sum(book[name]["current_bps"] for name in book)))
    turnover //= 2
    # No separate turnover term exists in MandateV1. Preserve measurement but
    # do not invent a hard limit from the old research request.
    row = {"candidate_id": f"grid_{index:04d}", "weights_bps": weights,
           "cash_bps": _whole_bps(max(Decimal(0), cash), capital),
           "cash_amount": decstr(max(Decimal(0), cash)), "principal_base": decstr(principal),
           "total_cost_base": decstr(fees), "required_budget_base": decstr(required),
           "net_income_base": decstr(net_income), "stress_loss_base": stress,
           "daily_loss_base": decstr(daily_loss),
           "worst_stress_loss_base": decstr(worst), "turnover_bps": turnover,
           "liquidity_checks": liquidity, "cashflow_assumptions": assumptions,
           "cashflows": rows, "eligible": not reasons,
           "reason_codes": sorted(set(reasons))}
    row["candidate_hash"] = digest(row)
    return row


def _validate_snapshot_time(snapshot, assembler, at):
    age = (datetime.fromisoformat(at)
           - datetime.fromisoformat(snapshot["as_of"])).total_seconds()
    if not 0 <= age <= assembler.config["max_age_seconds"]:
        raise MachineError("snapshot is stale or from the future")
    directory = next((item for item in snapshot["captures"]
                      if item["source_id"] == "justlend_contracts"), None)
    if directory is None:
        raise MachineError("product registry capture missing")
    registry_age = (datetime.fromisoformat(at)
                    - datetime.fromisoformat(directory["received_at"])).total_seconds()
    if (directory["error"] is not None or not 0 <= registry_age
            <= assembler.config["max_registry_age_seconds"]):
        raise MachineError("product registry is stale or unavailable")


def compare_plans(record, snapshot, request, *, assembler, at):
    """Return two policy-compliant plans or a replayable abstention."""
    with localcontext() as context:
        context.prec = 256
        return _compare_plans(record, snapshot, request, assembler=assembler, at=at)


def _compare_plans(record, snapshot, request, *, assembler, at):
    at = utc(at)
    mandate = confirmed_mandate(record, at)
    snapshot = assembler.verify(snapshot)
    _validate_snapshot_time(snapshot, assembler, at)
    if snapshot["network"] != mandate["scope"]["network"] or (
            snapshot["scope"] is not None and snapshot["scope"] != mandate["scope"]):
        raise MachineError("plan scope differs from mandate/snapshot")
    step, distance, scenarios, names, book, expected_count = _validate_request(request, snapshot)
    weights = _enumerate(names, step)
    if len(weights) != expected_count:
        raise MachineError("complete grid enumeration count mismatch")
    evaluated = [_evaluate_candidate(record, snapshot, assembler, request, book, row,
                                     scenarios, at, index)
                 for index, row in enumerate(weights)]
    eligible = [row for row in evaluated if row["eligible"]]
    conservative = sorted(eligible, key=lambda row: (
        decimal(row["worst_stress_loss_base"]),
        -decimal(row["net_income_base"], signed=True), row["candidate_id"]))
    growth = sorted(eligible, key=lambda row: (
        -decimal(row["net_income_base"], signed=True),
        decimal(row["worst_stress_loss_base"]), row["candidate_id"]))
    first = conservative[0] if conservative else None
    second = next((row for row in growth if first and (
        sum(abs(row["weights_bps"][name] - first["weights_bps"][name]) for name in names)
        + abs(row["cash_bps"] - first["cash_bps"])) // 2 >= distance), None)
    histogram = {}
    product_exclusions = {name: {} for name in names}
    for row in evaluated:
        if row["eligible"]:
            continue
        for reason in row["reason_codes"]:
            if reason.startswith("PRODUCT:"):
                _, product_id, product_reason = reason.split(":", 2)
                counts = product_exclusions[product_id]
                counts[product_reason] = counts.get(product_reason, 0) + 1
                key = "PRODUCT"
            else:
                key = reason
            histogram[key] = histogram.get(key, 0) + 1
    vaults = [name for name in names if name.startswith("usdd.vault.")]
    vault_eligible = sum(any(row["weights_bps"].get(name, 0) for name in vaults)
                         for row in eligible)
    result = {"schema_version": VERSION, "scope": mandate["scope"],
              "mandate_policy_hash": record["policy_hash"],
              "mandate_draft_hash": record["draft_hash"], "revision": mandate["revision"],
              "snapshot_hash": snapshot["snapshot_hash"], "request_hash": digest(request),
              "at": at, "grid_step_bps": step, "enumerated_candidates": len(evaluated),
              "complete_enumeration": True,
              "optimum_scope": "DEFINED_GRID_AND_EXPLICIT_ASSUMPTIONS_ONLY",
              "assumption_status": "EXPLICIT_UNVERIFIED_MARKET_AND_RISK_ASSUMPTIONS",
              "eligible_candidates": len(eligible), "exclusion_histogram": histogram,
              "product_exclusions": product_exclusions,
              "adjustable_condition_hints": _adjustments(histogram),
              "vault_comparison": {"universe": vaults,
                                   "eligible_candidate_count": vault_eligible},
              "plans": [], "execution_authority": "NONE", "chain_status": "NOT_SUBMITTED"}
    if first is None or second is None:
        result.update(status="NO_TWO_VIABLE_PLANS",
                      reason_codes=["NO_TWO_VIABLE_PLANS"])
    else:
        for label, row in (("CONSERVATIVE", first), ("GROWTH", second)):
            plan = {"name": label, **row}
            plan["plan_hash"] = digest({"domain": VERSION + "/plan", "plan": plan,
                                        "policy_hash": record["policy_hash"],
                                        "snapshot_hash": snapshot["snapshot_hash"],
                                        "request_hash": digest(request)})
            result["plans"].append(plan)
        result.update(status="COMPARISON_READY", reason_codes=[])
    result["comparison_hash"] = digest(result)
    return result


def verify_comparison(result, record, snapshot, request, *, assembler, at):
    try:
        return result == compare_plans(record, snapshot, request, assembler=assembler, at=at)
    except (MachineError, KeyError, TypeError, ValueError, ZeroDivisionError):
        return False


def compile_plan_intent(comparison, record, snapshot, request, *, selected_plan,
                        assembler, at, valid_until):
    expected = compare_plans(record, snapshot, request, assembler=assembler, at=at)
    if comparison != expected or comparison["status"] != "COMPARISON_READY":
        raise MachineError("plan comparison does not replay or is not ready")
    plan = next((item for item in comparison["plans"] if item["name"] == selected_plan), None)
    if plan is None:
        raise MachineError("selected plan is not one of the compared plans")
    mandate = confirmed_mandate(record, utc(at))
    start, end = datetime.fromisoformat(utc(at)), datetime.fromisoformat(utc(valid_until))
    maximum = start + timedelta(seconds=request["max_plan_age_seconds"])
    if not start < end <= min(maximum, datetime.fromisoformat(mandate["terms"]["expires_at"])):
        raise MachineError("plan intent expiry exceeds policy or plan lifetime")
    cashflow_book = {row["product_id"]: row for row in plan["cashflows"]}
    quote_book = {row["product_id"]: row for row in plan["cashflow_assumptions"]["quotes"]}
    legs = []
    blockers = {"LIVE_QUOTE_AND_RISK_ASSUMPTIONS_REQUIRED",
                "PR06_TRANSACTION_GRAPH_REQUIRED", "PR07_WALLET_SIGNATURE_REQUIRED"}
    for name, weight in sorted(plan["weights_bps"].items()):
        if not weight:
            continue
        product = snapshot["products"][name]
        cap = product["capability"]
        if cap["stages"]["execute"]["status"] != "SUPPORTED":
            blockers.add("EXECUTION_CAPABILITY_NOT_VERIFIED:" + name)
        row, quote = cashflow_book[name], quote_book[name]
        legs.append({"product_id": name, "identity_hash": product["identity_hash"],
                     "capability_hash": product["capability_hash"], "action": cap["action"],
                     "protocol": cap["protocol"], "weight_bps": weight,
                     "principal_asset": row["principal_asset"],
                     "principal_amount": row["principal_amount"],
                     "required_budget_base": row["accounting"]["required_budget_base"],
                     "cashflow_assumption_hash": row["assumption_hash"],
                     "quote_hash": digest(quote), "status": "AWAITING_TRANSACTION_GRAPH"})
    payload = {"schema_version": INTENT_VERSION, "status": "INTENT_PREPARED",
               "scope": comparison["scope"], "mandate_policy_hash": record["policy_hash"],
               "mandate_draft_hash": record["draft_hash"], "revision": mandate["revision"],
               "snapshot_hash": snapshot["snapshot_hash"],
               "comparison_hash": comparison["comparison_hash"],
               "plan_hash": plan["plan_hash"], "selected_plan": selected_plan,
               "as_of": utc(at), "valid_until": utc(valid_until), "legs": legs,
               "cash_amount": plan["cash_amount"], "total_cost_base": plan["total_cost_base"],
               "blockers": sorted(blockers), "execution_authority": "NONE",
               "signature_status": "NOT_REQUESTED", "chain_status": "NOT_SUBMITTED"}
    payload["intent_hash"] = digest({"domain": INTENT_VERSION, "payload": payload})
    return payload


def verify_plan_intent(intent, comparison, record, snapshot, request, *, assembler, at):
    if not isinstance(intent, dict) or intent.get("schema_version") != INTENT_VERSION:
        return False
    try:
        return intent == compile_plan_intent(
            comparison, record, snapshot, request, selected_plan=intent["selected_plan"],
            assembler=assembler, at=at, valid_until=intent["valid_until"])
    except (MachineError, KeyError, TypeError, ValueError, ZeroDivisionError):
        return False
