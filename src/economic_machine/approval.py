"""Exact user-approval cards and wallet signature requests for PR07.

Web-session approval only permits creation of one wallet signature request.  It
does not sign, build through an RPC, broadcast, or grant execution authority.
"""

import re
from copy import deepcopy
from datetime import datetime

from .mandate import normalize_scope, tron_address
from .preflight import VERSION as PREFLIGHT_VERSION, _verify_graph
from .tron_crypto import function_selector
from .values import MachineError, digest, ident, require_keys, utc


SESSION_VERSION = "economic-wallet-session-1"
APPROVAL_VERSION = "economic-approval-1"
GUARD_VERSION = "economic-execution-guard-binding-1"
GUARD_POLICY_VERSION = "economic-execution-guard-policy-1"
ANCHOR_VERSION = "economic-tron-reference-block-1"
REQUEST_VERSION = "economic-wallet-signature-request-1"
GUARD_SIMULATION_VERSION = "economic-guard-simulation-1"
UINT64 = 1 << 64


def _hash(value, label):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise MachineError("invalid " + label)
    return value


def _uint(value, label, *, positive=False, limit=1 << 256):
    if (not isinstance(value, str) or re.fullmatch(r"0|[1-9][0-9]*", value) is None
            or len(value) > 78 or int(value) >= limit or (positive and int(value) == 0)):
        raise MachineError("invalid " + label)
    return int(value)


def _word(number):
    return number.to_bytes(32, "big").hex()


def normalize_wallet_session(raw):
    require_keys(raw, {"schema_version", "session_id", "scope", "auth_context_hash",
                       "issued_at", "expires_at", "status"}, "WalletSessionV1")
    if raw["schema_version"] != SESSION_VERSION or raw["status"] != "AUTHENTICATED":
        raise MachineError("authenticated wallet session required")
    start, end = utc(raw["issued_at"]), utc(raw["expires_at"])
    if datetime.fromisoformat(start) >= datetime.fromisoformat(end):
        raise MachineError("wallet session validity window is empty")
    return {"schema_version": SESSION_VERSION,
        "session_id": ident(raw["session_id"], "wallet session"),
        "scope": normalize_scope(raw["scope"]),
        "auth_context_hash": _hash(raw["auth_context_hash"], "auth context"),
        "issued_at": start, "expires_at": end, "status": "AUTHENTICATED"}


def normalize_guard_binding(raw):
    require_keys(raw, {"schema_version", "network", "owner_address", "guard_address",
        "adapter_address", "underlying_token", "market_address", "share_token",
        "guard_code_hash", "adapter_code_hash", "status", "evidence_hash",
        "registry_entry_hash"},
        "ExecutionGuardBindingV1")
    if raw["schema_version"] != GUARD_VERSION or raw["network"] not in {
            "tron-mainnet", "tron-nile", "tron-shasta"}:
        raise MachineError("unsupported guard binding")
    if raw["status"] != "VERIFIED":
        raise MachineError("verified guard binding required")
    result = {**raw}
    for key in ("owner_address", "guard_address", "adapter_address", "underlying_token",
                "market_address", "share_token"):
        result[key] = tron_address(raw[key])
    for key in ("guard_code_hash", "adapter_code_hash", "evidence_hash"):
        result[key] = _hash(raw[key], key)
    if (result["guard_address"] in {result["adapter_address"], result["underlying_token"],
            result["market_address"], result["share_token"]}
            or result["adapter_address"] in {result["underlying_token"],
                result["market_address"], result["share_token"]}
            or result["underlying_token"] in {result["market_address"],
                                               result["share_token"]}):
        raise MachineError("guard, adapter and underlying addresses must be isolated")
    entry = {key: result[key] for key in ("network", "owner_address", "guard_address",
        "adapter_address", "underlying_token", "market_address", "share_token",
        "guard_code_hash", "adapter_code_hash", "evidence_hash")}
    if result["registry_entry_hash"] != digest(
            {"domain": GUARD_VERSION, "registry_entry": entry}):
        raise MachineError("guard registry entry commitment mismatch")
    return result


def normalize_guard_policy(raw):
    require_keys(raw, {"schema_version", "network", "entry_hashes", "issued_at",
                       "expires_at", "policy_hash"}, "ExecutionGuardPolicyV1")
    if raw["schema_version"] != GUARD_POLICY_VERSION or raw["network"] not in {
            "tron-mainnet", "tron-nile", "tron-shasta"}:
        raise MachineError("unsupported guard trust policy")
    entries = raw["entry_hashes"]
    if (not isinstance(entries, list) or not 1 <= len(entries) <= 32
            or entries != sorted(set(entries))):
        raise MachineError("guard trust policy entries must be sorted and unique")
    entries = [_hash(item, "guard registry entry") for item in entries]
    start, end = utc(raw["issued_at"]), utc(raw["expires_at"])
    if datetime.fromisoformat(start) >= datetime.fromisoformat(end):
        raise MachineError("guard trust policy validity window is empty")
    result = {"schema_version": GUARD_POLICY_VERSION, "network": raw["network"],
        "entry_hashes": entries, "issued_at": start, "expires_at": end}
    expected = digest({"domain": GUARD_POLICY_VERSION, "policy": result})
    if raw["policy_hash"] != expected:
        raise MachineError("guard trust policy commitment mismatch")
    result["policy_hash"] = expected
    return result


def _manifest(manifest, graph):
    if not isinstance(manifest, dict) or manifest.get("schema_version") != PREFLIGHT_VERSION:
        raise MachineError("PreflightManifestV1 required")
    claimed = manifest.get("manifest_hash")
    payload = deepcopy(manifest)
    payload.pop("manifest_hash", None)
    if claimed != digest({"domain": PREFLIGHT_VERSION, "manifest": payload}):
        raise MachineError("preflight manifest commitment mismatch")
    if (manifest.get("graph_hash") != graph["graph_hash"]
            or manifest.get("execution_authority") != "NONE"
            or manifest.get("signature_status") != "NOT_REQUESTED"
            or manifest.get("chain_status") != "NOT_SUBMITTED"):
        raise MachineError("preflight manifest is not an unsigned graph check")
    return manifest


def _direct_envelope(step):
    action = step["action"]
    if action.get("transport") != "TRIGGER_SMART_CONTRACT" or action.get("status") != (
            "READY_FOR_SIMULATION"):
        raise MachineError("direct approval supports only a verified smart-contract action")
    selector = function_selector(action["function_selector"])
    parameter = action["parameter_hex"]
    if not isinstance(parameter, str) or re.fullmatch(r"[0-9a-f]*", parameter) is None:
        raise MachineError("canonical action parameter required")
    return {"schema_version": "economic-transaction-envelope-1", "path": "DIRECT_WALLET",
        "operation": step["operation"], "target_address": action["target_address"],
        "function_selector": action["function_selector"], "selector_hex": selector,
        "parameter_hex": parameter, "data_hex": selector + parameter,
        "call_value_sun": action["call_value_sun"],
        "fee_limit_sun": action["fee_limit_sun"],
        "source_action_hash": action["action_hash"], "guard_binding": None}


def _guard_envelope(step, graph, binding, nonce, deadline):
    binding = normalize_guard_binding(binding)
    action = step["action"]
    if binding["network"] != graph["scope"]["network"] or binding["owner_address"] != (
            graph["scope"]["wallet"]):
        raise MachineError("guard binding scope mismatch")
    if action.get("status") != "READY_FOR_SIMULATION" or step["operation"] not in {
            "JUSTLEND_SUPPLY", "JUSTLEND_REDEEM_SHARES"}:
        raise MachineError("guard supports only verified JustLend supply/redeem steps")
    if binding["market_address"] != action["target_address"]:
        raise MachineError("guard market differs from source action")
    if len(step["inputs"]) != 1 or len(step["expected_outputs"]) != 1:
        raise MachineError("guard step needs one exact input and output")
    item_in, item_out = step["inputs"][0], step["expected_outputs"][0]
    if step["operation"] == "JUSTLEND_SUPPLY":
        if (binding["underlying_token"] != item_in["token_address"]
                or binding["share_token"] != item_out["token_address"]):
            raise MachineError("guard supply token binding mismatch")
        signature = "executeSupply(bytes32,bytes32,bytes32,uint256,uint256,uint256,uint64,uint64)"
    else:
        if (binding["share_token"] != item_in["token_address"]
                or binding["underlying_token"] != item_out["token_address"]):
            raise MachineError("guard redeem token binding mismatch")
        signature = "executeRedeem(bytes32,bytes32,bytes32,uint256,uint256,uint256,uint64,uint64)"
    amount = _uint(item_in["amount_base_units"], "guard input amount", positive=True)
    minimum = _uint(item_out["amount_base_units"], "guard minimum output", positive=True)
    fee = _uint(action["fee_limit_sun"], "guard fee", positive=True)
    words = [graph["plan_hash"], graph["graph_hash"], step["step_hash"],
             _word(amount), _word(minimum), _word(fee), _word(nonce), _word(deadline)]
    selector = function_selector(signature)
    return {"schema_version": "economic-transaction-envelope-1", "path": "EXECUTION_GUARD",
        "operation": step["operation"], "target_address": binding["guard_address"],
        "function_selector": signature, "selector_hex": selector,
        "parameter_hex": "".join(words), "data_hex": selector + "".join(words),
        "call_value_sun": "0", "fee_limit_sun": str(fee),
        "source_action_hash": action["action_hash"], "guard_binding": binding}


def compile_guard_envelope(graph, step_id, guard_binding, *, nonce, expires_at):
    """Build the exact guard call that must be simulated before approval."""
    steps = _verify_graph(graph)
    step = next((item for item in steps if item["step_id"] == step_id), None)
    if step is None:
        raise MachineError("guard step not found")
    deadline = int(datetime.fromisoformat(utc(expires_at)).timestamp())
    return _guard_envelope(step, graph, guard_binding,
                           _uint(nonce, "approval nonce", limit=UINT64), deadline)


def _guard_simulation(raw, envelope, graph, step, manifest, prepared_at):
    require_keys(raw, {"schema_version", "network", "guard_address", "envelope_hash",
        "status", "recorded_at", "evidence_hash", "projected_output_base_units",
        "owner_output_delta_base_units", "simulated_fee_sun",
        "guard_underlying_residual", "guard_share_residual",
        "adapter_underlying_residual", "adapter_share_residual", "allowances_reset"},
        "GuardSimulationV1")
    recorded = utc(raw["recorded_at"])
    output = _uint(raw["projected_output_base_units"], "guard projected output")
    owner_delta = _uint(raw["owner_output_delta_base_units"], "owner output delta")
    simulated_fee = _uint(raw["simulated_fee_sun"], "guard simulated fee")
    minimum = _uint(step["expected_outputs"][0]["amount_base_units"],
                    "guard minimum output", positive=True)
    residuals = [_uint(raw[key], key) for key in ("guard_underlying_residual",
        "guard_share_residual", "adapter_underlying_residual", "adapter_share_residual")]
    if (raw["schema_version"] != GUARD_SIMULATION_VERSION
            or raw["network"] != graph["scope"]["network"]
            or tron_address(raw["guard_address"]) != envelope["target_address"]
            or raw["envelope_hash"] != digest(envelope)
            or raw["status"] != "SUCCESS"
            or raw["allowances_reset"] is not True
            or any(residuals)
            or output < minimum or owner_delta < minimum
            or simulated_fee > _uint(envelope["fee_limit_sun"], "guard fee limit")
            or not datetime.fromisoformat(utc(manifest["checked_at"]))
                <= datetime.fromisoformat(recorded)
                <= datetime.fromisoformat(prepared_at)):
        raise MachineError("guard execution simulation did not prove exact postconditions")
    result = deepcopy(raw)
    result["guard_address"] = tron_address(raw["guard_address"])
    result["evidence_hash"] = _hash(raw["evidence_hash"], "guard simulation evidence")
    result["recorded_at"] = recorded
    for key in ("projected_output_base_units", "owner_output_delta_base_units",
                "simulated_fee_sun", "guard_underlying_residual", "guard_share_residual",
                "adapter_underlying_residual", "adapter_share_residual"):
        result[key] = str(_uint(raw[key], key))
    return result


def prepare_approval_card(graph, manifest, session, step_id, *, execution_path,
                          nonce, prepared_at, expires_at, guard_binding=None,
                          guard_simulation=None, guard_policy=None):
    steps = _verify_graph(graph)
    manifest = _manifest(manifest, graph)
    session = normalize_wallet_session(session)
    prepared_at, expires_at = utc(prepared_at), utc(expires_at)
    at, end = datetime.fromisoformat(prepared_at), datetime.fromisoformat(expires_at)
    if (session["scope"] != graph["scope"] or manifest["scope"] != graph["scope"]
            or not datetime.fromisoformat(session["issued_at"]) <= at
            < end <= datetime.fromisoformat(session["expires_at"])
            or end > datetime.fromisoformat(utc(graph["valid_until"]))):
        raise MachineError("approval session, graph or lifetime mismatch")
    nonce = _uint(nonce, "approval nonce", limit=UINT64)
    step = next((item for item in steps if item["step_id"] == step_id), None)
    report = next((item for item in manifest["step_reports"] if item["step_id"] == step_id), None)
    if step is None or report is None or report["status"] != "READY" or report[
            "simulation_status"] != "SUCCESS" or report["reasons"]:
        raise MachineError("only a successfully simulated ready step can be approved")
    deadline = int(end.timestamp())
    if execution_path == "DIRECT_WALLET":
        if guard_binding is not None or guard_simulation is not None or guard_policy is not None:
            raise MachineError("direct wallet approval cannot include guard evidence")
        envelope = _direct_envelope(step)
        guard_simulation_hash = None
        guard_policy_hash = None
    elif execution_path == "EXECUTION_GUARD":
        if guard_binding is None or guard_simulation is None or guard_policy is None:
            raise MachineError("guard execution requires binding, trust policy and simulation")
        guard_policy = normalize_guard_policy(guard_policy)
        normalized_binding = normalize_guard_binding(guard_binding)
        if (guard_policy["network"] != graph["scope"]["network"]
                or normalized_binding["registry_entry_hash"] not in guard_policy["entry_hashes"]
                or not datetime.fromisoformat(guard_policy["issued_at"]) <= at
                < end <= datetime.fromisoformat(guard_policy["expires_at"])):
            raise MachineError("guard binding is absent from the active trust policy")
        envelope = _guard_envelope(step, graph, guard_binding, nonce, deadline)
        guard_simulation = _guard_simulation(
            guard_simulation, envelope, graph, step, manifest, prepared_at)
        guard_simulation_hash = digest(guard_simulation)
        guard_policy_hash = guard_policy["policy_hash"]
    else:
        raise MachineError("unsupported execution path")
    if datetime.fromisoformat(utc(step["action"]["expires_at"])) < end:
        raise MachineError("approval outlives source action")
    payload = {"schema_version": APPROVAL_VERSION, "status": "AWAITING_EXPLICIT_APPROVAL",
        "scope": graph["scope"], "session_id": session["session_id"],
        "auth_context_hash": session["auth_context_hash"], "plan_hash": graph["plan_hash"],
        "graph_hash": graph["graph_hash"], "step_id": step["step_id"],
        "step_hash": step["step_hash"], "action_hash": step["action"]["action_hash"],
        "snapshot_hash": graph["snapshot_hash"],
        "fresh_account_hash": manifest["fresh_account_hash"],
        "preflight_manifest_hash": manifest["manifest_hash"], "execution_path": execution_path,
        "nonce": str(nonce), "prepared_at": prepared_at, "expires_at": expires_at,
        "recipient_address": graph["scope"]["wallet"],
        "approved_inputs": deepcopy(step["inputs"]),
        "minimum_outputs": deepcopy(step["expected_outputs"]),
        "fee_limit_sun": envelope["fee_limit_sun"], "envelope": envelope,
        "guard_simulation_hash": guard_simulation_hash,
        "guard_policy_hash": guard_policy_hash,
        "approval_status": "PENDING", "signature_status": "NOT_REQUESTED",
        "chain_status": "NOT_SUBMITTED", "execution_authority": "NONE"}
    payload["approval_hash"] = digest({"domain": APPROVAL_VERSION, "approval": payload})
    return payload


def _verify_card(card):
    if not isinstance(card, dict) or card.get("schema_version") != APPROVAL_VERSION:
        raise MachineError("ApprovalV1 required")
    claimed = card.get("approval_hash")
    payload = deepcopy(card)
    payload.pop("approval_hash", None)
    if claimed != digest({"domain": APPROVAL_VERSION, "approval": payload}):
        raise MachineError("approval card commitment mismatch")
    return card


def confirm_approval(card, session, confirmation):
    card = _verify_card(card)
    session = normalize_wallet_session(session)
    require_keys(confirmation, {"approval_hash", "session_id", "auth_context_hash",
        "wallet", "network", "decision", "confirmed_at"}, "ApprovalConfirmationV1")
    confirmed = utc(confirmation["confirmed_at"])
    if (card["status"] != "AWAITING_EXPLICIT_APPROVAL" or card["approval_status"] != "PENDING"
            or confirmation["decision"] != "APPROVE"
            or confirmation["approval_hash"] != card["approval_hash"]
            or confirmation["session_id"] != card["session_id"]
            or confirmation["auth_context_hash"] != card["auth_context_hash"]
            or tron_address(confirmation["wallet"]) != card["scope"]["wallet"]
            or confirmation["network"] != card["scope"]["network"]
            or session["session_id"] != card["session_id"]
            or session["auth_context_hash"] != card["auth_context_hash"]
            or session["scope"] != card["scope"]):
        raise MachineError("approval confirmation context changed")
    moment = datetime.fromisoformat(confirmed)
    if not datetime.fromisoformat(card["prepared_at"]) <= moment < datetime.fromisoformat(
            card["expires_at"]) or moment >= datetime.fromisoformat(session["expires_at"]):
        raise MachineError("approval confirmation expired")
    result = deepcopy(card)
    result.update(status="APPROVED_UNSIGNED", approval_status="APPROVED",
                  confirmed_at=confirmed, confirmation_hash=digest(confirmation),
                  approval_effect="WALLET_SIGNATURE_REQUEST_ALLOWED")
    result.pop("approval_hash")
    result["approval_hash"] = digest({"domain": APPROVAL_VERSION, "approval": result})
    return result


def normalize_anchor(raw):
    require_keys(raw, {"schema_version", "network", "block_number", "block_id",
                       "ref_block_bytes", "ref_block_hash", "observed_at", "valid_until"},
                 "TronReferenceBlockV1")
    if raw["schema_version"] != ANCHOR_VERSION or raw["network"] not in {
            "tron-mainnet", "tron-nile", "tron-shasta"}:
        raise MachineError("unsupported TRON reference block")
    number = _uint(raw["block_number"], "reference block number", limit=1 << 63)
    block_id = _hash(raw["block_id"], "reference block id")
    expected_bytes = number.to_bytes(8, "big")[6:8].hex()
    expected_hash = bytes.fromhex(block_id)[8:16].hex()
    if raw["ref_block_bytes"] != expected_bytes or raw["ref_block_hash"] != expected_hash:
        raise MachineError("TAPOS reference differs from block id/number")
    start, end = utc(raw["observed_at"]), utc(raw["valid_until"])
    if datetime.fromisoformat(start) >= datetime.fromisoformat(end):
        raise MachineError("reference block validity window is empty")
    return {**raw, "block_number": str(number), "observed_at": start, "valid_until": end}


def build_wallet_signature_request(approved, session, anchor, *, issued_at):
    approved = _verify_card(approved)
    session = normalize_wallet_session(session)
    anchor = normalize_anchor(anchor)
    issued_at = utc(issued_at)
    moment = datetime.fromisoformat(issued_at)
    if (approved["status"] != "APPROVED_UNSIGNED" or approved["approval_status"] != "APPROVED"
            or approved["signature_status"] != "NOT_REQUESTED"
            or session["session_id"] != approved["session_id"]
            or session["auth_context_hash"] != approved["auth_context_hash"]
            or session["scope"] != approved["scope"]
            or anchor["network"] != approved["scope"]["network"]):
        raise MachineError("wallet request context changed")
    if (not datetime.fromisoformat(approved["confirmed_at"]) <= moment
            < datetime.fromisoformat(approved["expires_at"])
            or not datetime.fromisoformat(anchor["observed_at"]) <= moment
            < datetime.fromisoformat(anchor["valid_until"])):
        raise MachineError("wallet request or TAPOS anchor expired")
    envelope = approved["envelope"]
    request = {"schema_version": REQUEST_VERSION, "status": "AWAITING_WALLET_SIGNATURE",
        "approval_hash": approved["approval_hash"], "scope": approved["scope"],
        "session_id": approved["session_id"], "auth_context_hash": approved["auth_context_hash"],
        "execution_path": approved["execution_path"], "plan_hash": approved["plan_hash"],
        "graph_hash": approved["graph_hash"], "step_hash": approved["step_hash"],
        "action_hash": approved["action_hash"], "nonce": approved["nonce"],
        "owner_address": approved["scope"]["wallet"],
        "contract_address": envelope["target_address"],
        "function_selector": envelope["function_selector"],
        "parameter_hex": envelope["parameter_hex"], "data_hex": envelope["data_hex"],
        "call_value_sun": envelope["call_value_sun"],
        "fee_limit_sun": envelope["fee_limit_sun"],
        "permission_id": 0, "timestamp_ms": int(moment.timestamp() * 1000),
        "expiration_ms": int(datetime.fromisoformat(approved["expires_at"]).timestamp() * 1000),
        "ref_block_bytes": anchor["ref_block_bytes"],
        "ref_block_hash": anchor["ref_block_hash"], "anchor_hash": digest(anchor),
        "issued_at": issued_at, "expires_at": approved["expires_at"],
        "signature_status": "REQUESTED", "chain_status": "NOT_SUBMITTED",
        "execution_authority": "NONE"}
    request["request_hash"] = digest({"domain": REQUEST_VERSION, "request": request})
    return request


def verify_approval_session(approved, session, *, at):
    try:
        approved = _verify_card(approved)
        session = normalize_wallet_session(session)
        moment = datetime.fromisoformat(utc(at))
        return (session["scope"] == approved["scope"]
                and session["session_id"] == approved["session_id"]
                and session["auth_context_hash"] == approved["auth_context_hash"]
                and datetime.fromisoformat(session["issued_at"]) <= moment
                < min(datetime.fromisoformat(session["expires_at"]),
                      datetime.fromisoformat(approved["expires_at"])))
    except (MachineError, KeyError, TypeError, ValueError):
        return False
