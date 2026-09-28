"""Fail-closed preflight for an unsigned ExecutionGraphV1.

Simulation output is evidence about a possible transaction, never a confirmed
balance or position.  Every on-chain dependency requires a new observed account
snapshot after confirmation; a successful constant call cannot unlock the next
transaction in the graph.
"""

import re
from copy import deepcopy
from datetime import datetime

from .tx_graph import ACCOUNT_VERSION, VERSION as GRAPH_VERSION, normalize_account_snapshot
from .values import MachineError, digest, ident, require_keys, utc


VERSION = "economic-preflight-manifest-1"
SIMULATION_VERSION = "economic-step-simulation-1"
STATUSES = {"SUCCESS", "REVERTED", "RPC_ERROR"}


def _uint(value, label):
    if (not isinstance(value, str) or re.fullmatch(r"0|[1-9][0-9]*", value) is None
            or len(value) > 78 or int(value) >= 1 << 256):
        raise MachineError("invalid " + label)
    return int(value)


def _hash(value, label, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise MachineError("invalid " + label)
    return value


def _asset(raw, label):
    require_keys(raw, {"asset", "token_address", "decimals", "amount_base_units"}, label)
    asset = ident(raw["asset"], label + " asset")
    token = raw["token_address"]
    if token is not None and (not isinstance(token, str)
            or re.fullmatch(r"41[0-9a-f]{40}", token) is None):
        raise MachineError("invalid " + label + " token")
    if token is None and asset != "TRX":
        raise MachineError("only TRX may omit token address")
    decimals = raw["decimals"]
    if type(decimals) is not int or not 0 <= decimals <= 36:
        raise MachineError("invalid " + label + " decimals")
    return {"asset": asset, "token_address": token, "decimals": decimals,
            "amount_base_units": str(_uint(raw["amount_base_units"], label + " amount"))}


def normalize_simulation(raw):
    require_keys(raw, {"schema_version", "step_id", "action_hash", "status",
        "decoded_result", "fee_estimate_sun", "projected_outputs",
        "postcondition_results", "evidence_hash", "recorded_at"}, "StepSimulationV1")
    if raw["schema_version"] != SIMULATION_VERSION or raw["status"] not in STATUSES:
        raise MachineError("unsupported step simulation")
    step_id = ident(raw["step_id"], "simulation step")
    action_hash = _hash(raw["action_hash"], "simulation action hash")
    evidence = _hash(raw["evidence_hash"], "simulation evidence", nullable=True)
    if evidence is None:
        raise MachineError("all simulation outcomes require durable evidence")
    result = raw["decoded_result"]
    if result is not None and type(result) not in {str, bool}:
        raise MachineError("decoded result must be string, boolean or null")
    if not isinstance(raw["projected_outputs"], list) or len(raw["projected_outputs"]) > 32:
        raise MachineError("bounded projected outputs required")
    outputs = [_asset(item, "projected output") for item in raw["projected_outputs"]]
    if len({_asset_key(item) for item in outputs}) != len(outputs):
        raise MachineError("duplicate projected output asset")
    if not isinstance(raw["postcondition_results"], list) or len(
            raw["postcondition_results"]) > 32:
        raise MachineError("bounded postcondition results required")
    checks, seen = [], set()
    for item in raw["postcondition_results"]:
        require_keys(item, {"kind", "passed", "observed"}, "postcondition result")
        kind = ident(item["kind"], "postcondition kind")
        if kind in seen or type(item["passed"]) is not bool:
            raise MachineError("unique boolean postcondition result required")
        if item["observed"] is not None and not isinstance(item["observed"], str):
            raise MachineError("postcondition observation must be text or null")
        seen.add(kind)
        checks.append({"kind": kind, "passed": item["passed"],
                       "observed": item["observed"]})
    return {"schema_version": SIMULATION_VERSION, "step_id": step_id,
        "action_hash": action_hash, "status": raw["status"],
        "decoded_result": result,
        "fee_estimate_sun": str(_uint(raw["fee_estimate_sun"], "simulation fee")),
        "projected_outputs": sorted(outputs, key=lambda x: (
            x["asset"], x["token_address"] or "", x["decimals"])),
        "postcondition_results": sorted(checks, key=lambda x: x["kind"]),
        "evidence_hash": evidence, "recorded_at": utc(raw["recorded_at"])}


def _verify_graph(graph):
    if not isinstance(graph, dict) or graph.get("schema_version") != GRAPH_VERSION:
        raise MachineError("ExecutionGraphV1 required")
    claimed = graph.get("graph_hash")
    payload = deepcopy(graph)
    payload.pop("graph_hash", None)
    if claimed != digest({"domain": GRAPH_VERSION, "graph": payload}):
        raise MachineError("execution graph commitment mismatch")
    if (graph.get("execution_semantics") != "ORDERED_MULTI_TRANSACTION_NON_ATOMIC"
            or graph.get("execution_authority") != "NONE"
            or graph.get("signature_status") != "NOT_REQUESTED"
            or graph.get("chain_status") != "NOT_SUBMITTED"):
        raise MachineError("execution graph authority or atomicity changed")
    steps = graph.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 64:
        raise MachineError("bounded execution graph steps required")
    ids = []
    for step in steps:
        if not isinstance(step, dict):
            raise MachineError("invalid execution step")
        claimed_step = step.get("step_hash")
        payload = deepcopy(step)
        payload.pop("step_hash", None)
        if claimed_step != digest({"domain": "economic-execution-step-1", "step": payload}):
            raise MachineError("execution step commitment mismatch")
        step_id = ident(step.get("step_id"), "step id")
        dependencies = step.get("depends_on")
        if not isinstance(dependencies, list) or any(not isinstance(item, str)
                                                     for item in dependencies):
            raise MachineError("execution step dependencies must be a list")
        if step_id in ids or any(item not in ids for item in dependencies):
            raise MachineError("execution graph has duplicate or forward dependency")
        ids.append(step_id)
        action = step.get("action")
        if action is not None:
            claimed_action = action.get("action_hash")
            action_payload = deepcopy(action)
            action_payload.pop("action_hash", None)
            if claimed_action != digest(action_payload):
                raise MachineError("action commitment mismatch")
            if not isinstance(action.get("expires_at"), str):
                raise MachineError("action expiry missing")
    return steps


def _asset_key(item):
    return item["asset"], item.get("token_address"), item["decimals"]


def compile_preflight(graph: dict, fresh_account: dict, simulations: list[dict], *, at: str) -> dict:
    steps = _verify_graph(graph)
    account = normalize_account_snapshot(fresh_account)
    at = utc(at)
    moment = datetime.fromisoformat(at)
    if graph["scope"] != account["scope"] or graph["snapshot_hash"] != account["snapshot_hash"]:
        raise MachineError("preflight account scope or snapshot binding mismatch")
    if not datetime.fromisoformat(account["observed_at"]) <= moment < datetime.fromisoformat(
            account["valid_until"]):
        raise MachineError("preflight account snapshot expired")
    if datetime.fromisoformat(account["observed_at"]) < datetime.fromisoformat(
            utc(graph["created_at"])):
        raise MachineError("preflight requires an account observation at or after graph creation")
    if not moment < datetime.fromisoformat(utc(graph["valid_until"])):
        raise MachineError("execution graph expired")
    if not isinstance(simulations, list) or len(simulations) > 64:
        raise MachineError("bounded simulation list required")
    sims = {item["step_id"]: item for item in map(normalize_simulation, simulations)}
    if len(sims) != len(simulations):
        raise MachineError("duplicate step simulation")
    onchain_ids = {step["step_id"] for step in steps if step["kind"] != "OFFCHAIN_RESERVATION"}
    if set(sims) - onchain_ids:
        raise MachineError("simulation references a non-onchain or unknown step")

    balances = {_asset_key(item): int(item["amount_base_units"])
                for item in account["balances"]}
    reserved = {}
    for item in account["reservations"]:
        key = _asset_key(item)
        reserved[key] = reserved.get(key, 0) + int(item["amount_base_units"])
    required = {}
    for step in steps:
        if step["kind"] == "OFFCHAIN_RESERVATION":
            for item in step["inputs"]:
                key = _asset_key(item)
                required[key] = required.get(key, 0) + int(item["amount_base_units"])
    short = {key for key, amount in required.items()
             if amount + reserved.get(key, 0) > balances.get(key, 0)}
    positions = {item["product_id"]: item for item in account["positions"]}
    vaults = {item["product_id"]: item for item in account["vaults"]}
    liquidity = {item["product_id"]: int(item["amount_base_units"])
                 for item in account["market_liquidity"]}

    reports, blockers = [], set(graph["blockers"])
    total_simulated_fee = 0
    for step in steps:
        reasons = list(step["blockers"])
        status = "READY"
        if step["kind"] == "OFFCHAIN_RESERVATION":
            if any(_asset_key(item) in short for item in step["inputs"]):
                reasons.append("FRESH_BALANCE_SHORTFALL")
                status = "BLOCKED"
            reports.append({"step_id": step["step_id"], "status": status,
                "simulation_status": "NOT_APPLICABLE", "reasons": sorted(set(reasons)),
                "evidence_hash": None, "fee_estimate_sun": "0",
                "post_state_status": "OBSERVED_RESERVATION_ONLY"})
            blockers.update(reasons)
            continue

        action = step["action"]
        if action is None or action.get("status") == "BLOCKED":
            reasons.append("ACTION_NOT_EXECUTABLE")
        if action is not None and datetime.fromisoformat(utc(action["expires_at"])) <= moment:
            reasons.append("ACTION_EXPIRED")
        position = positions.get(step["product_id"])
        vault = vaults.get(step["product_id"])
        for condition in step["postconditions"]:
            kind = condition["kind"]
            threshold = int(condition.get("amount_base_units", "0"))
            if (kind == "MARKET_LIQUIDITY_AT_LEAST"
                    and liquidity.get(step["product_id"], 0) < threshold):
                reasons.append("FRESH_MARKET_LIQUIDITY_SHORTFALL")
            elif (kind == "POSITION_SHARES_AT_LEAST"
                    and (position is None or int(position["shares_base_units"]) < threshold)):
                reasons.append("FRESH_POSITION_SHARE_SHORTFALL")
            elif (kind == "POSITION_UNDERLYING_AT_LEAST"
                    and (position is None or int(position["underlying_base_units"]) < threshold)):
                reasons.append("FRESH_POSITION_UNDERLYING_SHORTFALL")
            elif (kind == "VAULT_DEBT_AT_LEAST"
                    and (vault is None or int(vault["debt_base_units"]) < threshold)):
                reasons.append("FRESH_VAULT_DEBT_SHORTFALL")
            elif (kind == "VAULT_COLLATERAL_AT_LEAST"
                    and (vault is None or int(vault["collateral_base_units"]) < threshold)):
                reasons.append("FRESH_VAULT_COLLATERAL_SHORTFALL")
            elif kind == "BALANCE_AT_LEAST":
                key = (condition["asset"], condition["token_address"], condition["decimals"])
                if balances.get(key, 0) - reserved.get(key, 0) < threshold:
                    reasons.append("FRESH_BALANCE_SHORTFALL")
            elif kind == "NOT_BEFORE" and moment < datetime.fromisoformat(
                    utc(condition["timestamp"])):
                reasons.append("CLAIM_NOT_YET_ELIGIBLE")
        sim = sims.get(step["step_id"])
        sim_status = "NOT_PROVIDED"
        evidence = None
        fee = 0
        if sim is None:
            reasons.append("STEP_SIMULATION_REQUIRED")
        else:
            if action is None:
                raise MachineError("simulation cannot bind a missing action")
            if sim["action_hash"] != action.get("action_hash"):
                raise MachineError("simulation action binding mismatch")
            if not datetime.fromisoformat(utc(graph["created_at"])) <= datetime.fromisoformat(
                    sim["recorded_at"]) <= moment:
                raise MachineError("simulation is outside graph preflight window")
            sim_status, evidence = sim["status"], sim["evidence_hash"]
            fee = int(sim["fee_estimate_sun"])
            total_simulated_fee += fee
            if sim["status"] == "REVERTED":
                reasons.append("SIMULATION_REVERTED")
            elif sim["status"] == "RPC_ERROR":
                reasons.append("SIMULATION_RPC_ERROR")
            else:
                semantics = action.get("result_semantics")
                if semantics == "UINT_ZERO" and sim["decoded_result"] != "0":
                    reasons.append("PROTOCOL_ERROR_CODE")
                if semantics == "BOOL_TRUE" and sim["decoded_result"] is not True:
                    reasons.append("PROTOCOL_FALSE_RETURN")
                if fee > int(action["fee_limit_sun"]):
                    reasons.append("STEP_FEE_LIMIT_EXCEEDED")
                projected = {_asset_key(item): int(item["amount_base_units"])
                             for item in sim["projected_outputs"]}
                for expected in step["expected_outputs"]:
                    if projected.get(_asset_key(expected), 0) < int(expected["amount_base_units"]):
                        reasons.append("PROJECTED_OUTPUT_BELOW_MINIMUM")
                checks = {item["kind"]: item["passed"]
                          for item in sim["postcondition_results"]}
                for condition in step["postconditions"]:
                    if checks.get(condition["kind"]) is not True:
                        reasons.append("POSTCONDITION_NOT_PROVEN:" + condition["kind"])

        if any(dep in onchain_ids for dep in step["depends_on"]):
            reasons.append("CONFIRMED_DEPENDENCY_AND_FRESH_SNAPSHOT_REQUIRED")
        if any(item.get("availability") == "DEPENDENCY_OUTPUT_UNCONFIRMED"
               for item in step["inputs"]):
            reasons.append("EXPECTED_OUTPUT_IS_NOT_OBSERVED_BALANCE")
        if reasons:
            status = "BLOCKED"
        reports.append({"step_id": step["step_id"], "status": status,
            "simulation_status": sim_status, "reasons": sorted(set(reasons)),
            "evidence_hash": evidence, "fee_estimate_sun": str(fee),
            "post_state_status": "EXPECTED_ONLY_UNCONFIRMED"})
        blockers.update(reasons)

    if total_simulated_fee > int(graph["fee_budget_sun"]):
        blockers.add("GRAPH_FEE_BUDGET_EXCEEDED_AT_PREFLIGHT")
    if total_simulated_fee > int(graph["maximum_fee_limits_sun"]):
        blockers.add("SIMULATED_FEE_EXCEEDS_COMPILED_MAXIMUM")
    result = {"schema_version": VERSION, "status": "BLOCKED" if blockers else
              "PREFLIGHT_PASSED_UNSIGNED", "graph_hash": graph["graph_hash"],
        "scope": graph["scope"], "snapshot_hash": graph["snapshot_hash"],
        "fresh_account_hash": digest(account), "checked_at": at,
        "execution_semantics": "ORDERED_MULTI_TRANSACTION_NON_ATOMIC",
        "step_reports": reports, "total_simulated_fee_sun": str(total_simulated_fee),
        "blockers": sorted(blockers), "post_state_status": "EXPECTED_ONLY_UNCONFIRMED",
        "approval_status": "NOT_REQUESTED", "signature_status": "NOT_REQUESTED",
        "chain_status": "NOT_SUBMITTED", "execution_authority": "NONE"}
    result["manifest_hash"] = digest({"domain": VERSION, "manifest": result})
    return result


def verify_preflight(manifest, graph, fresh_account, simulations, *, at):
    try:
        return manifest == compile_preflight(graph, fresh_account, simulations, at=at)
    except (MachineError, KeyError, TypeError, ValueError, ArithmeticError):
        return False
