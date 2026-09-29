"""Compare a solidified TRON call with independently observed account state."""

import re
from copy import deepcopy
from datetime import datetime

from .approval import APPROVAL_VERSION
from .preflight import _verify_graph
from .tron_execution import verify_execution_result
from .tx_graph import normalize_account_snapshot
from .values import MachineError, digest, utc


VERSION = "economic-position-reconciliation-1"
POST_STATE_VERSION = "economic-post-state-evidence-1"


def _approval(raw):
    if not isinstance(raw, dict) or raw.get("schema_version") != APPROVAL_VERSION:
        raise MachineError("ApprovalV1 required")
    claimed = raw.get("approval_hash")
    body = deepcopy(raw)
    body.pop("approval_hash", None)
    if claimed != digest({"domain": APPROVAL_VERSION, "approval": body}):
        raise MachineError("approval card commitment mismatch")
    if raw.get("status") != "APPROVED_UNSIGNED" or raw.get("approval_status") != "APPROVED":
        raise MachineError("approved unsigned card required")
    return raw


def _asset_key(item):
    return item["asset"], item["token_address"], item["decimals"]


def _balance_map(account):
    return {_asset_key(item): int(item["amount_base_units"]) for item in account["balances"]}


def _position_map(account):
    return {item["product_id"]: item for item in account["positions"]}


def _vault_map(account):
    return {item["product_id"]: item for item in account["vaults"]}


def _allowance_map(account):
    return {(item["token_address"], item["spender_address"]): int(
        item["amount_base_units"]) for item in account["allowances"]}


def _signed(value):
    return str(int(value))


def _delta(value):
    return None if value is None else _signed(value)


def _check(checks, kind, passed, *, expected=None, actual=None):
    checks.append({"kind": kind, "passed": passed, "expected": expected,
                   "actual": actual})


def _post_state(raw, execution):
    if not isinstance(raw, dict):
        raise MachineError("PostStateEvidenceV1 required")
    from .values import require_keys
    require_keys(raw, {"schema_version", "network", "txid", "block_number",
        "block_id", "observed_at", "source_hash", "completeness", "account"},
        "PostStateEvidenceV1")
    if raw["schema_version"] != POST_STATE_VERSION:
        raise MachineError("unsupported post-state evidence")
    if (raw["network"] != execution["scope"]["network"]
            or raw["txid"] != execution["txid"]
            or raw["completeness"] != "REQUIRED_FIELDS_COMPLETE"):
        raise MachineError("post-state evidence scope or completeness mismatch")
    height = raw["block_number"]
    if type(height) is not int or not execution["block"]["number"] <= height <= (
            execution["block"]["number"] + 64):
        raise MachineError("post-state block is outside bounded reconciliation window")
    block_id = raw["block_id"]
    if (not isinstance(block_id, str) or re.fullmatch(r"[0-9a-f]{64}", block_id) is None
            or int(block_id[:16], 16) != height):
        raise MachineError("post-state block commitment mismatch")
    if not isinstance(raw["source_hash"], str) or re.fullmatch(
            r"[0-9a-f]{64}", raw["source_hash"]) is None:
        raise MachineError("post-state source hash required")
    account = normalize_account_snapshot(raw["account"])
    observed = utc(raw["observed_at"])
    if observed != account["observed_at"]:
        raise MachineError("post-state evidence and account time differ")
    return {**raw, "observed_at": observed, "account": account}


def reconcile_position(graph, approved, execution, before_account,
                       post_state_evidence, *, at):
    steps = _verify_graph(graph)
    approved = _approval(approved)
    if not verify_execution_result(execution):
        raise MachineError("committed TRON execution result required")
    if execution["status"] != "SOLID_EXECUTED_PENDING_POST_STATE":
        raise MachineError("solidified successful execution required before reconciliation")
    before = normalize_account_snapshot(before_account)
    post_state = _post_state(post_state_evidence, execution)
    after = post_state["account"]
    at = utc(at)
    moment = datetime.fromisoformat(at)
    step = next((item for item in steps if item["step_id"] == approved["step_id"]), None)
    if step is None:
        raise MachineError("approved step absent from execution graph")
    if (approved["graph_hash"] != graph["graph_hash"]
            or approved["plan_hash"] != graph["plan_hash"]
            or approved["step_hash"] != step["step_hash"]
            or approved["action_hash"] != step["action"]["action_hash"]
            or execution["graph_hash"] != graph["graph_hash"]
            or execution["step_hash"] != step["step_hash"]
            or execution["approval_hash"] != approved["approval_hash"]):
        raise MachineError("graph, approval and execution commitments differ")
    if (before["scope"] != graph["scope"] or after["scope"] != graph["scope"]
            or digest(before) != approved["fresh_account_hash"]):
        raise MachineError("account state does not bind the approved fresh snapshot")
    if not datetime.fromisoformat(before["observed_at"]) <= datetime.fromisoformat(
            approved["prepared_at"]):
        raise MachineError("approved account observation is from the future")
    block = execution["block"]
    block_time = datetime.fromtimestamp(block["timestamp_ms"] / 1000,
                                        tz=datetime.fromisoformat(at).tzinfo)
    if (datetime.fromisoformat(after["observed_at"]) < block_time
            or datetime.fromisoformat(after["observed_at"]) <= datetime.fromisoformat(
                before["observed_at"])
            or not datetime.fromisoformat(after["observed_at"]) <= moment
            < datetime.fromisoformat(after["valid_until"])):
        raise MachineError("post-state snapshot is stale, early or future")
    if digest(after) == digest(before):
        raise MachineError("post-state snapshot did not change")

    balances_before, balances_after = _balance_map(before), _balance_map(after)
    positions_before, positions_after = _position_map(before), _position_map(after)
    vaults_before, vaults_after = _vault_map(before), _vault_map(after)
    allowances_after = _allowance_map(after)
    operation = step["operation"]
    checks, deltas, reasons = [], {}, []

    if operation == "TRC20_APPROVE":
        condition = next((item for item in step["postconditions"]
                          if item["kind"] == "ALLOWANCE_EQUALS"), None)
        if condition is None:
            raise MachineError("approve step omits allowance postcondition")
        key = (condition["token_address"], condition["spender_address"])
        actual = allowances_after.get(key)
        expected = int(condition["amount_base_units"])
        _check(checks, "ALLOWANCE_EQUALS", actual == expected,
               expected=str(expected), actual=None if actual is None else str(actual))
    elif operation in {"JUSTLEND_SUPPLY", "JUSTLEND_SUPPLY_TRX", "JUSTLEND_REDEEM_SHARES",
                       "JUSTLEND_REDEEM_UNDERLYING", "STRX_STAKE"}:
        if len(step["inputs"]) != 1 or len(step["expected_outputs"]) != 1:
            raise MachineError("position action requires one input and output")
        item_in, item_out = step["inputs"][0], step["expected_outputs"][0]
        input_key, output_key = _asset_key(item_in), _asset_key(item_out)
        product = step["product_id"]
        before_position, after_position = positions_before.get(product), positions_after.get(product)
        if before_position is None or after_position is None:
            reasons.append("COMPLETE_BEFORE_AND_AFTER_POSITION_REQUIRED")
        input_before, input_after = balances_before.get(input_key), balances_after.get(input_key)
        amount, minimum = int(item_in["amount_base_units"]), int(
            item_out["amount_base_units"])
        if operation in {"JUSTLEND_SUPPLY", "JUSTLEND_SUPPLY_TRX", "STRX_STAKE"}:
            if input_before is None or input_after is None:
                reasons.append("COMPLETE_INPUT_BALANCE_REQUIRED")
            spent = None if input_before is None or input_after is None else input_before-input_after
            shares = None if before_position is None or after_position is None else int(
                after_position["shares_base_units"])-int(before_position["shares_base_units"])
            deltas.update(input_balance_delta=_delta(spent), share_delta=_delta(shares))
            exact_input = operation in {"JUSTLEND_SUPPLY", "JUSTLEND_SUPPLY_TRX"}
            if operation == "JUSTLEND_SUPPLY_TRX":
                amount += int(execution["resource_receipt"]["total_fee_sun"])
            _check(checks, "INPUT_DECREASE", spent is not None and (
                spent == amount if exact_input else spent >= amount),
                expected=(str(amount) if exact_input else ">=" + str(amount)),
                actual=None if spent is None else str(spent))
            _check(checks, "POSITION_SHARE_INCREASE", shares is not None and shares >= minimum,
                   expected=">=" + str(minimum), actual=None if shares is None else str(shares))
        else:
            if output_key not in balances_before or output_key not in balances_after:
                reasons.append("COMPLETE_OUTPUT_BALANCE_REQUIRED")
            received = (None if output_key not in balances_before or output_key not in balances_after
                        else balances_after[output_key]-balances_before[output_key])
            # A native redemption credits TRX and pays its network fee in TRX.
            # Compare gross protocol proceeds, while retaining the actual fee in
            # the receipt. Token redemptions must not receive this adjustment.
            if received is not None and item_out['asset'] == 'TRX' and item_out.get('token_address') is None:
                received += int(execution['resource_receipt']['total_fee_sun'])
            share_change = None if before_position is None or after_position is None else int(
                before_position["shares_base_units"])-int(after_position["shares_base_units"])
            underlying_change = None if before_position is None or after_position is None else int(
                before_position["underlying_base_units"])-int(
                    after_position["underlying_base_units"])
            deltas.update(output_balance_delta=_delta(received),
                          share_decrease=_delta(share_change),
                          underlying_decrease=_delta(underlying_change))
            _check(checks, "MIN_OUTPUT", received is not None and received >= minimum,
                   expected=">=" + str(minimum), actual=None if received is None else str(received))
            if operation == "JUSTLEND_REDEEM_SHARES":
                _check(checks, "POSITION_SHARE_DECREASE",
                       share_change is not None and share_change == amount,
                       expected=str(amount), actual=None if share_change is None else str(share_change))
            else:
                _check(checks, "POSITION_UNDERLYING_DECREASE",
                       underlying_change is not None and underlying_change >= amount,
                       expected=">=" + str(amount),
                       actual=None if underlying_change is None else str(underlying_change))
                _check(checks, "POSITION_SHARE_DECREASE_POSITIVE",
                       share_change is not None and share_change > 0,
                       expected=">0", actual=None if share_change is None else str(
                           share_change))
    elif operation in {"USDD_VAULT_REPAY", "USDD_VAULT_WITHDRAW_COLLATERAL"}:
        before_vault, after_vault = (vaults_before.get(step["product_id"]),
                                     vaults_after.get(step["product_id"]))
        if before_vault is None or after_vault is None or before_vault["vault_id"] != after_vault[
                "vault_id"]:
            reasons.append("COMPLETE_SAME_VAULT_STATE_REQUIRED")
        else:
            amount = int(step["inputs"][0]["amount_base_units"])
            if operation == "USDD_VAULT_REPAY":
                actual = int(before_vault["debt_base_units"])-int(after_vault[
                    "debt_base_units"])
                _check(checks, "VAULT_DEBT_DECREASE", actual >= amount,
                       expected=">=" + str(amount), actual=str(actual))
                deltas["vault_debt_decrease"] = str(actual)
                item_in = step["inputs"][0]
                key = _asset_key(item_in)
                before_value, after_value = balances_before.get(key), balances_after.get(key)
                spent = (None if before_value is None or after_value is None else
                         before_value-after_value)
                _check(checks, "REPAY_BALANCE_DECREASE", spent == amount,
                       expected=str(amount), actual=None if spent is None else str(spent))
                deltas["repay_balance_decrease"] = _delta(spent)
            else:
                actual = int(before_vault["collateral_base_units"])-int(after_vault[
                    "collateral_base_units"])
                _check(checks, "VAULT_COLLATERAL_DECREASE", actual == amount,
                       expected=str(amount), actual=str(actual))
                deltas["vault_collateral_decrease"] = str(actual)
                item_out = step["expected_outputs"][0]
                key = _asset_key(item_out)
                before_value, after_value = balances_before.get(key), balances_after.get(key)
                received = (None if before_value is None or after_value is None else
                            after_value-before_value)
                minimum = int(item_out["amount_base_units"])
                _check(checks, "COLLATERAL_OUTPUT_INCREASE",
                       received is not None and received >= minimum,
                       expected=">=" + str(minimum),
                       actual=None if received is None else str(received))
                deltas["collateral_output_increase"] = _delta(received)
    else:
        reasons.append("OPERATION_POST_STATE_ADAPTER_NOT_IMPLEMENTED:" + operation)

    reasons.extend(item["kind"] for item in checks if not item["passed"])
    reasons = sorted(set(reasons))
    reconciled = not reasons
    onchain_steps = [item for item in steps if item["kind"] != "OFFCHAIN_RESERVATION"]
    terminal = bool(onchain_steps and onchain_steps[-1]["step_id"] == step["step_id"])
    result = {"schema_version": VERSION,
        "status": "RECONCILED" if reconciled else "DISPUTED",
        "scope": deepcopy(graph["scope"]), "plan_hash": graph["plan_hash"],
        "graph_hash": graph["graph_hash"], "step_id": step["step_id"],
        "step_hash": step["step_hash"], "operation": operation,
        "approval_hash": approved["approval_hash"], "execution_result_hash": execution[
            "execution_result_hash"], "txid": execution["txid"],
        "receipt_record_hash": execution["receipt_record_hash"],
        "before_account_hash": digest(before), "after_account_hash": digest(after),
        "post_state_evidence_hash": digest(post_state),
        "after_snapshot_hash": after["snapshot_hash"], "reconciled_at": at,
        "checks": checks, "actual_deltas": deltas, "reason_codes": reasons,
        "graph_terminal_step": terminal,
        "capital_status": ("RELEASE_ELIGIBLE" if reconciled and terminal else
                           "GRAPH_LOCKED" if reconciled else "LOCKED_DISPUTED"),
        "position_status": "VERIFIED_POST_STATE" if reconciled else "MISMATCH",
        "execution_authority": "NONE"}
    result["reconciliation_hash"] = digest({"domain": VERSION,
                                             "reconciliation": result})
    return result


def verify_reconciliation(raw):
    if not isinstance(raw, dict) or raw.get("schema_version") != VERSION:
        return False
    claimed = raw.get("reconciliation_hash")
    body = deepcopy(raw)
    body.pop("reconciliation_hash", None)
    return claimed == digest({"domain": VERSION, "reconciliation": body})
