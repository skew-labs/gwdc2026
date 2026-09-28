"""Integer accounting for expected-versus-actual portfolio performance.

All values are base-asset atoms. External cash flows are separated from return,
and debt principal changes are balance-sheet movements rather than profit.
"""

import re
from copy import deepcopy

from .mandate import normalize_scope
from .position_reconciliation import verify_reconciliation
from .tron_execution import verify_execution_result
from .values import MachineError, digest, ident, require_keys, utc


VERSION = "economic-performance-ledger-1"
COMPARISON_VERSION = "economic-forecast-actual-comparison-1"
FLOW_KINDS = {"DEPOSIT", "WITHDRAWAL"}
RETURN_KINDS = {"INTEREST_INCOME", "REWARD_INCOME", "PRICE_PNL", "REALIZED_PNL"}
COST_KINDS = {"NETWORK_FEE", "PROTOCOL_FEE", "DEBT_COST"}


def _integer(value, label, *, signed=False):
    pattern = r"0|-?[1-9][0-9]*" if signed else r"0|[1-9][0-9]*"
    if (not isinstance(value, str) or re.fullmatch(pattern, value) is None
            or len(value.lstrip("-")) > 78 or abs(int(value)) >= 1 << 255):
        raise MachineError("invalid " + label)
    return int(value)


def _hash(value, label):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise MachineError("invalid " + label)
    return value


def _entries(rows, allowed, label, *, signed_amount=False):
    if not isinstance(rows, list) or len(rows) > 256:
        raise MachineError(label + " must be a bounded list")
    result, seen = [], set()
    for row in rows:
        require_keys(row, {"entry_id", "kind", "amount_base_units", "observed_at",
                           "evidence_hash"}, label + " entry")
        entry_id = ident(row["entry_id"], label + " entry id")
        if entry_id in seen or row["kind"] not in allowed:
            raise MachineError("duplicate or unsupported " + label + " entry")
        seen.add(entry_id)
        amount = _integer(row["amount_base_units"], label + " amount",
                          signed=signed_amount)
        if not signed_amount and amount == 0:
            raise MachineError(label + " amount must be positive")
        result.append({"entry_id": entry_id, "kind": row["kind"],
            "amount_base_units": str(amount), "observed_at": utc(row["observed_at"]),
            "evidence_hash": _hash(row["evidence_hash"], label + " evidence")})
    return sorted(result, key=lambda item: item["entry_id"])


def tron_fee_cost_entry(execution, *, entry_id, observed_at, base_asset,
                        base_decimals, trx_price_base_units_per_trx,
                        price_evidence_hash):
    if (not verify_execution_result(execution)
            or execution.get("status") != "SOLID_EXECUTED_PENDING_POST_STATE"):
        raise MachineError("solidified successful execution required for TRON fee")
    receipt = execution.get("resource_receipt")
    if not isinstance(receipt, dict):
        raise MachineError("TRON fee receipt is unavailable")
    total = receipt.get("total_fee_sun")
    if total is not None:
        fee_sun, basis = _integer(total, "TRON total fee"), "RECEIPT_TOTAL_FEE"
    else:
        parts = [receipt.get("energy_fee_sun"), receipt.get("bandwidth_fee_sun")]
        if all(item is None for item in parts):
            raise MachineError("TRON fee fields are missing, not zero")
        fee_sun = sum(_integer(item, "TRON fee component") for item in parts
                      if item is not None)
        basis = "RECEIPT_ENERGY_PLUS_BANDWIDTH_FEE"
    price = _integer(trx_price_base_units_per_trx, "TRX price", signed=False)
    if fee_sun < 0 or price <= 0 or type(base_decimals) is not int or not 0 <= base_decimals <= 18:
        raise MachineError("invalid TRON fee conversion")
    if not isinstance(base_asset, str) or not base_asset:
        raise MachineError("TRON fee base asset required")
    # 1 TRX = 1,000,000 sun. Liability conversion rounds upward.
    amount = (fee_sun * price + 999_999) // 1_000_000
    evidence = {"txid": execution["txid"],
        "execution_result_hash": execution["execution_result_hash"],
        "receipt_record_hash": execution["receipt_record_hash"],
        "fee_sun": str(fee_sun), "fee_basis": basis,
        "base_asset": base_asset, "base_decimals": base_decimals,
        "trx_price_base_units_per_trx": str(price),
        "price_evidence_hash": _hash(price_evidence_hash, "TRX price evidence")}
    entry = {"entry_id": ident(entry_id, "TRON fee entry"), "kind": "NETWORK_FEE",
        "amount_base_units": str(amount), "observed_at": utc(observed_at),
        "evidence_hash": digest({"domain": "economic-tron-fee-evidence-1",
                                 "evidence": evidence})}
    return {"entry": entry, "evidence": evidence}


def build_performance_ledger(*, scope, base_asset, base_decimals, period_start,
                             period_end, opening_nav_base_units, closing_nav_base_units,
                             external_flows, return_components, cost_components,
                             opening_state_hash, closing_state_hash, valuation_evidence_hash,
                             reconciliation_records):
    scope = normalize_scope(scope)
    if not isinstance(base_asset, str) or re.fullmatch(r"[A-Z][A-Z0-9]{1,15}", base_asset) is None:
        raise MachineError("invalid performance base asset")
    if type(base_decimals) is not int or not 0 <= base_decimals <= 18:
        raise MachineError("invalid performance base decimals")
    start, end = utc(period_start), utc(period_end)
    if start >= end:
        raise MachineError("performance period is empty")
    opening = _integer(opening_nav_base_units, "opening NAV")
    closing = _integer(closing_nav_base_units, "closing NAV")
    flows = _entries(external_flows, FLOW_KINDS, "external flow")
    returns = _entries(return_components, RETURN_KINDS, "return component",
                       signed_amount=True)
    costs = _entries(cost_components, COST_KINDS, "cost component")
    for entry in flows + returns + costs:
        if not start <= entry["observed_at"] <= end:
            raise MachineError("performance entry is outside the accounting period")
    if (not isinstance(reconciliation_records, list)
            or len(reconciliation_records) > 128
            or any(not verify_reconciliation(item) or item["scope"] != scope
                   or item["status"] != "RECONCILED" for item in reconciliation_records)):
        raise MachineError("only reconciled scoped executions may enter performance")
    if any(not start <= utc(item["reconciled_at"]) <= end
           for item in reconciliation_records):
        raise MachineError("reconciliation is outside the accounting period")
    reconciliation_hashes = sorted({item["reconciliation_hash"]
                                    for item in reconciliation_records})
    if len(reconciliation_hashes) != len(reconciliation_records):
        raise MachineError("duplicate reconciliation in performance period")
    deposits = sum(int(item["amount_base_units"]) for item in flows
                   if item["kind"] == "DEPOSIT")
    withdrawals = sum(int(item["amount_base_units"]) for item in flows
                      if item["kind"] == "WITHDRAWAL")
    external_net = deposits - withdrawals
    investment_pnl = closing - opening - external_net
    return_total = sum(int(item["amount_base_units"]) for item in returns)
    cost_total = sum(int(item["amount_base_units"]) for item in costs)
    attributed = return_total - cost_total
    if investment_pnl != attributed:
        raise MachineError("performance attribution does not reconcile opening, flows and closing")
    result = {"schema_version": VERSION, "status": "EVIDENCE_BOUND_ACCOUNTING",
        "scope": scope, "base_asset": base_asset, "base_decimals": base_decimals,
        "period_start": start, "period_end": end,
        "opening_nav_base_units": str(opening), "closing_nav_base_units": str(closing),
        "external_flows": flows, "return_components": returns, "cost_components": costs,
        "external_net_flow_base_units": str(external_net),
        "investment_pnl_base_units": str(investment_pnl),
        "attributed_pnl_base_units": str(attributed),
        "opening_state_hash": _hash(opening_state_hash, "opening state"),
        "closing_state_hash": _hash(closing_state_hash, "closing state"),
        "valuation_evidence_hash": _hash(valuation_evidence_hash, "valuation evidence"),
        "reconciliation_hashes": reconciliation_hashes,
        "debt_principal_treatment": "BALANCE_SHEET_NOT_RETURN",
        "external_flow_treatment": "EXCLUDED_FROM_INVESTMENT_PNL",
        "truth_scope": "ACCOUNTING_IDENTITY_WITH_CALLER_SUPPLIED_EVIDENCE_COMMITMENTS",
        "execution_authority": "NONE"}
    result["performance_hash"] = digest({"domain": VERSION, "performance": result})
    return result


def _ledger(raw):
    if not isinstance(raw, dict) or raw.get("schema_version") != VERSION:
        raise MachineError("PerformanceLedgerV1 required")
    claimed = raw.get("performance_hash")
    body = deepcopy(raw)
    body.pop("performance_hash", None)
    if claimed != digest({"domain": VERSION, "performance": body}):
        raise MachineError("performance ledger commitment mismatch")
    return raw


def compare_forecast_actual(forecast, ledger):
    ledger = _ledger(ledger)
    require_keys(forecast, {"schema_version", "scope", "base_asset", "base_decimals",
        "period_start", "period_end", "expected_net_income_base_units",
        "assumption_hash", "mode"}, "PerformanceForecastV1")
    if forecast["schema_version"] != "economic-performance-forecast-1":
        raise MachineError("unsupported performance forecast")
    scope = normalize_scope(forecast["scope"])
    if (scope != ledger["scope"] or forecast["base_asset"] != ledger["base_asset"]
            or forecast["base_decimals"] != ledger["base_decimals"]
            or utc(forecast["period_start"]) != ledger["period_start"]
            or utc(forecast["period_end"]) != ledger["period_end"]):
        raise MachineError("forecast and actual performance scopes differ")
    if forecast["mode"] not in {"REPLAY", "SIMULATION", "LIVE"}:
        raise MachineError("invalid forecast mode")
    expected = _integer(forecast["expected_net_income_base_units"],
                        "expected net income", signed=True)
    assumption_hash = _hash(forecast["assumption_hash"], "forecast assumption")
    actual = int(ledger["investment_pnl_base_units"])
    result = {"schema_version": COMPARISON_VERSION, "scope": scope,
        "base_asset": ledger["base_asset"], "base_decimals": ledger["base_decimals"],
        "period_start": ledger["period_start"], "period_end": ledger["period_end"],
        "forecast_mode": forecast["mode"], "assumption_hash": assumption_hash,
        "performance_hash": ledger["performance_hash"],
        "expected_net_income_base_units": str(expected),
        "actual_investment_pnl_base_units": str(actual),
        "variance_base_units": str(actual-expected),
        "classification": ("ABOVE_EXPECTATION" if actual > expected else
                           "BELOW_EXPECTATION" if actual < expected else "MATCHED"),
        "forecast_truth_status": "ASSUMPTION_NOT_GROUND_TRUTH",
        "actual_truth_status": ledger["truth_scope"]}
    result["comparison_hash"] = digest({"domain": COMPARISON_VERSION,
                                        "comparison": result})
    return result
