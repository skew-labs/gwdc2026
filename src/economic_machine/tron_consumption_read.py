"""Read solidified TRON transaction evidence without granting settlement authority.

The node response is not a Merkle proof. BasketConsumed says the registry was
called; it does not prove a fill, token delivery, post-state or owner approval.
"""

import hashlib
import re
import time

from .tron_registry_read import _block, _hex, _http_transport, _node_url
from .values import MachineError, digest, require_keys


VERSION = "economic-tron-transaction-observation-2"
LEGACY_VERSION = "economic-tron-transaction-observation-1"
CONFIG_VERSION = "economic-tron-transaction-reader-1"
ASSESSMENT_VERSION = "economic-tron-basket-consumption-assessment-1"
BASKET_CONSUMED_TOPIC = "b754d98b17d9d2e535bc3f0fcdb40c0949f5b1a251b024cb0ec22b057f71afd7"
MAX_TRANSACTION_RESPONSE = 8388608


def _config(raw: dict) -> dict:
    require_keys(raw, {"schema_version", "solidity_url", "api_key_env",
                       "max_block_age_ms", "network_anchor_height",
                       "network_anchor_block_id"}, "TronTransactionReader")
    if raw["schema_version"] != CONFIG_VERSION:
        raise MachineError("unsupported TRON transaction reader version")
    age = raw["max_block_age_ms"]
    if type(age) is not int or not 1000 <= age <= 3600000:
        raise MachineError("invalid solid block age")
    height = raw["network_anchor_height"]
    if type(height) is not int or not 0 <= height < 1 << 31:
        raise MachineError("invalid TRON network anchor height")
    anchor = _hex(raw["network_anchor_block_id"], 64, "network anchor block ID")
    if int(anchor[:16], 16) != height:
        raise MachineError("TRON network anchor height mismatch")
    api_env = raw["api_key_env"]
    if api_env is not None and (not isinstance(api_env, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", api_env) is None):
        raise MachineError("invalid API key environment variable")
    return {"schema_version": CONFIG_VERSION,
            "solidity_url": _node_url(raw["solidity_url"]),
            "fullnode_url": _node_url(raw["solidity_url"]),
            "api_key_env": api_env, "max_block_age_ms": age,
            "network_anchor_height": height, "network_anchor_block_id": anchor}


def _address(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise MachineError(label + " must be a TRON hex address")
    raw = value.removeprefix("0x").lower()
    if raw.startswith("41") and len(raw) == 42:
        raw = raw[2:]
    return _hex(raw, 40, label)


def _log(value: object) -> dict:
    if not isinstance(value, dict):
        raise MachineError("TRON event log malformed")
    topics = value.get("topics")
    if not isinstance(topics, list) or len(topics) > 4:
        raise MachineError("TRON event topics malformed")
    data = value.get("data", "")
    if not isinstance(data, str) or len(data) > 4096:
        raise MachineError("TRON event data oversized")
    data = data.removeprefix("0x").lower()
    if len(data) % 2 or (data and re.fullmatch(r"[0-9a-f]+", data) is None):
        raise MachineError("TRON event data malformed")
    return {"address": _address(value.get("address"), "log address"),
            "topics": [_hex(topic, 64, "log topic") for topic in topics],
            "data": data}


def _tip(node) -> dict:
    return _block(node("solidity", "/walletsolidity/getnowblock", None))


def _raw_data_hex(body: dict, txid: str) -> str:
    raw = body.get("raw_data_hex")
    if not isinstance(raw, str) or not raw or len(raw) % 2 or len(raw) > 1048576:
        raise MachineError("TRON raw transaction bytes missing or oversized")
    raw = raw.removeprefix("0x").lower()
    if len(raw) % 2 or re.fullmatch(r"[0-9a-f]+", raw) is None:
        raise MachineError("TRON raw transaction bytes malformed")
    if hashlib.sha256(bytes.fromhex(raw)).hexdigest() != txid:
        raise MachineError("TRON transaction ID does not hash raw bytes")
    return raw


def _optional_nonnegative(raw, key):
    value = raw.get(key)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise MachineError("TRON receipt " + key + " is invalid")
    return str(value)


def read_tron_transaction(txid: str, config: dict, *, now_ms: int | None = None,
                          transport=None) -> dict:
    """Observe one transaction from a customer-selected solidified node."""
    txid = _hex(txid, 64, "TRON transaction ID")
    settings = _config(config)
    if now_ms is None:
        now_ms = time.time_ns() // 1000000
    if type(now_ms) is not int or now_ms < 0:
        raise MachineError("invalid observation time")
    node = transport or _http_transport(settings, max_response=MAX_TRANSACTION_RESPONSE)
    before = _tip(node)
    anchor = _block(node("solidity", "/walletsolidity/getblockbynum",
                         {"num": settings["network_anchor_height"]}))
    if (anchor["number"] != settings["network_anchor_height"]
            or anchor["block_id"] != settings["network_anchor_block_id"]
            or anchor["number"] > before["number"]):
        raise MachineError("TRON network anchor mismatch")
    body = node("solidity", "/walletsolidity/gettransactionbyid", {"value": txid})
    info = node("solidity", "/walletsolidity/gettransactioninfobyid", {"value": txid})
    if not isinstance(body, dict) or not isinstance(info, dict):
        raise MachineError("TRON transaction response malformed")
    if info and not body:
        raise MachineError("TRON receipt without transaction body")

    status = "NOT_OBSERVED"
    details = None
    if body and not info:
        if _hex(body.get("txID"), 64, "solid transaction ID") != txid:
            raise MachineError("TRON transaction body ID mismatch")
        _raw_data_hex(body, txid)
        status = "SOLID_BODY_RECEIPT_MISSING"
    elif body:
        if (_hex(body.get("txID"), 64, "solid transaction ID") != txid
                or _hex(info.get("id"), 64, "solid receipt ID") != txid):
            raise MachineError("TRON body and receipt ID mismatch")
        body_bytes = _raw_data_hex(body, txid)
        height, timestamp = info.get("blockNumber"), info.get("blockTimeStamp")
        if (type(height) is not int or height < 0 or height > before["number"]
                or type(timestamp) is not int or timestamp < 0):
            raise MachineError("TRON receipt block reference invalid")
        block_response = node("solidity", "/walletsolidity/getblockbynum", {"num": height})
        block = _block(block_response)
        if block["number"] != height or block["timestamp_ms"] != timestamp:
            raise MachineError("TRON receipt block mismatch")
        transactions = block_response.get("transactions")
        if not isinstance(transactions, list) or len(transactions) > 4096:
            raise MachineError("TRON transaction list malformed")
        matches = [item for item in transactions
                   if isinstance(item, dict) and item.get("txID") == txid]
        if len(matches) != 1:
            raise MachineError("TRON transaction absent from solidified block")
        if _raw_data_hex(matches[0], txid) != body_bytes:
            raise MachineError("TRON block and body raw bytes mismatch")
        raw = body.get("raw_data")
        contracts = raw.get("contract") if isinstance(raw, dict) else None
        if not isinstance(contracts, list) or len(contracts) != 1:
            raise MachineError("TRON transaction contract structure invalid")
        contract = contracts[0]
        if not isinstance(contract, dict):
            raise MachineError("TRON transaction contract structure invalid")
        parameter = contract.get("parameter")
        value = parameter.get("value") if isinstance(parameter, dict) else None
        if (contract.get("type") != "TriggerSmartContract"
                or not isinstance(value, dict)):
            raise MachineError("TRON transaction is not a direct smart contract call")
        target = _address(value.get("contract_address"), "call target")
        ret = body.get("ret")
        receipt = info.get("receipt")
        if (not isinstance(ret, list) or len(ret) != 1 or not isinstance(ret[0], dict)
                or not isinstance(receipt, dict)):
            raise MachineError("TRON execution result malformed")
        execution_success = (ret[0].get("contractRet") == "SUCCESS"
                             and info.get("result") in (None, "SUCESS", "SUCCESS")
                             and receipt.get("result") == "SUCCESS")
        logs = info.get("log", [])
        if not isinstance(logs, list) or len(logs) > 256:
            raise MachineError("TRON receipt logs malformed")
        status = "SOLID_EXECUTED" if execution_success else "SOLID_EXECUTION_FAILED"
        details = {"block": block, "call_target_address": target,
                   "execution_success": execution_success,
                   "body_record_hash": digest(body), "receipt_record_hash": digest(info),
                   "resource_receipt": {
                       "total_fee_sun": _optional_nonnegative(info, "fee"),
                       "energy_fee_sun": _optional_nonnegative(receipt, "energy_fee"),
                       "bandwidth_fee_sun": _optional_nonnegative(receipt, "net_fee"),
                       "energy_usage_total": _optional_nonnegative(
                           receipt, "energy_usage_total"),
                       "bandwidth_usage": _optional_nonnegative(receipt, "net_usage")},
                   "logs": [_log(item) for item in logs]}

    after = _tip(node)
    if (after["number"] < before["number"]
            or (after["number"] == before["number"]
                and after["block_id"] != before["block_id"])):
        raise MachineError("TRON solidified tip regressed or changed")
    age = now_ms - after["timestamp_ms"]
    if not -5000 <= age <= settings["max_block_age_ms"]:
        raise MachineError("TRON solidified tip stale or from the future")
    result = {"schema_version": VERSION, "txid": txid, "status": status,
              "network_anchor": anchor, "solid_tip_before": before,
              "solid_tip_after": after, "observed_at_ms": now_ms,
              "endpoint_fingerprint": digest(settings["solidity_url"]),
              "details": details, "source_trust": "NODE_RESPONSE_ONLY",
              "settlement_status": "NOT_VERIFIED", "execution_authority": "NONE"}
    result["observation_hash"] = digest(result)
    return result


def assess_basket_consumption(binding: dict, observation: dict) -> dict:
    """Match an observed registry event; never convert it to a fill receipt."""
    if (not isinstance(binding, dict)
            or binding.get("schema_version") not in {"economic-basket-registry-binding-1",
                                                     "economic-basket-registry-binding-2"}
            or not isinstance(observation, dict)
            or observation.get("schema_version") not in {VERSION, LEGACY_VERSION}):
        raise MachineError("typed binding and transaction observation required")
    recorded = dict(observation)
    claimed_hash = recorded.pop("observation_hash", None)
    if claimed_hash != digest(recorded):
        raise MachineError("TRON transaction observation hash mismatch")
    reasons = []
    if observation["status"] == "SOLID_EXECUTION_FAILED":
        reasons.append("EXECUTION_FAILED")
    elif observation["status"] != "SOLID_EXECUTED" or observation["details"] is None:
        reasons.append("SOLID_SUCCESSFUL_RECEIPT_MISSING")
    else:
        details = observation["details"]
        if not details["execution_success"]:
            reasons.append("EXECUTION_FAILED")
        if details["call_target_address"] != binding["target_address"]:
            reasons.append("CALL_TARGET_MISMATCH")
        expiry = binding.get("valid_until_epoch_seconds")
        if type(expiry) is not int or details["block"]["timestamp_ms"] // 1000 >= expiry:
            reasons.append("BASKET_EXPIRED_AT_BLOCK")
        registry = _hex(binding["registry_address"], 40, "registry address")
        matches = [item for item in details["logs"]
                   if item["address"] == registry and item["topics"]
                   and item["topics"][0] == BASKET_CONSUMED_TOPIC]
        if len(matches) != 1:
            reasons.append("BASKET_EVENT_NOT_UNIQUE")
        else:
            event = matches[0]
            target = _hex(binding["target_address"], 40, "target address")
            asset = _hex(binding["asset_address"], 40, "asset address")
            expected_topics = [BASKET_CONSUMED_TOPIC,
                               _hex(binding["binding_hash"], 64, "binding hash"),
                               _hex(binding["policy_id"], 64, "policy id"),
                               target.rjust(64, "0")]
            amount = binding.get("amount_base_units")
            if (not isinstance(amount, str) or len(amount) > 78
                    or re.fullmatch(r"[1-9][0-9]*", amount) is None
                    or int(amount) >= 1 << 256):
                raise MachineError("invalid basket amount")
            expected_data = asset.rjust(64, "0") + f"{int(amount):064x}"
            if event["topics"] != expected_topics:
                reasons.append("BASKET_EVENT_TOPICS_MISMATCH")
            if event["data"] != expected_data:
                reasons.append("BASKET_EVENT_DATA_MISMATCH")
    if observation["source_trust"] != "NODE_RESPONSE_ONLY":
        reasons.append("SOURCE_TRUST_MISMATCH")
    if (observation["settlement_status"] != "NOT_VERIFIED"
            or observation["execution_authority"] != "NONE"):
        reasons.append("AUTHORITY_CLAIM_MISMATCH")
    result = {"schema_version": ASSESSMENT_VERSION,
              "status": "CONSUMPTION_OBSERVED" if not reasons else "WITHHELD",
              "reason_codes": reasons, "txid": observation["txid"],
              "binding_hash": binding["binding_hash"],
              "observation_hash": claimed_hash,
              "source_trust": "NODE_RESPONSE_ONLY",
              "settlement_status": "NOT_VERIFIED", "execution_authority": "NONE"}
    if binding["schema_version"] == "economic-basket-registry-binding-2":
        result["authenticated_source_hash"] = _hex(
            binding.get("authenticated_source_hash"), 64, "authenticated source hash")
        result["signed_assembly_hash"] = _hex(
            binding.get("signed_assembly_hash"), 64, "signed assembly hash")
    result["assessment_hash"] = digest(result)
    return result
