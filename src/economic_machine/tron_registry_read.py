"""Read-only TRON registry observation from a customer-selected node.

Solidified constant calls are bracketed by the same solid block ID. Node HTTP
responses are observations, not Merkle proofs or authority to execute trades.
"""

import hashlib
import json
import os
import re
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .chain_binding import verify_chain_binding
from .values import MachineError, digest, require_keys


VERSION = "economic-tron-registry-observation-4"
CONFIG_VERSION = "economic-tron-registry-reader-1"
MAX_RESPONSE = 1048576


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise MachineError("TRON node redirect refused")


def _hex(value: object, length: int, label: str) -> str:
    if not isinstance(value, str):
        raise MachineError(label + " must be hex")
    raw = value.removeprefix("0x").lower()
    if len(raw) != length or re.fullmatch(r"[0-9a-f]+", raw) is None:
        raise MachineError(label + " has wrong hex width")
    return raw


def _address(value: object, label: str) -> str:
    return _hex(value, 40, label)


def _word(value: int) -> str:
    if type(value) is not int or not 0 <= value < 1 << 256:
        raise MachineError("invalid ABI uint256")
    return f"{value:064x}"


def _node_url(value: object) -> str:
    if not isinstance(value, str):
        raise MachineError("TRON node URL required")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise MachineError("invalid TRON node URL") from exc
    if (parsed.scheme not in {"https", "http"} or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not parsed.hostname
            or (port is not None and port < 1)):
        raise MachineError("invalid TRON node URL")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and
        parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise MachineError("TRON node must use HTTPS or loopback HTTP")
    if not re.fullmatch(r"/[A-Za-z0-9/_-]*", parsed.path or "/"):
        raise MachineError("invalid TRON node base path")
    return value.rstrip("/")


def _config(raw: dict) -> dict:
    require_keys(raw, {"schema_version", "solidity_url", "fullnode_url",
                       "owner_address", "expected_runtime_sha256",
                       "max_block_age_ms", "api_key_env"}, "TronRegistryReader")
    if raw["schema_version"] != CONFIG_VERSION:
        raise MachineError("unsupported TRON reader version")
    if type(raw["max_block_age_ms"]) is not int or not 1000 <= raw["max_block_age_ms"] <= 3600000:
        raise MachineError("invalid solid block age")
    api_env = raw["api_key_env"]
    if api_env is not None and (not isinstance(api_env, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", api_env) is None):
        raise MachineError("invalid API key environment variable")
    return {**raw, "solidity_url": _node_url(raw["solidity_url"]),
            "fullnode_url": _node_url(raw["fullnode_url"]),
            "owner_address": _address(raw["owner_address"], "owner address"),
            "expected_runtime_sha256": _hex(raw["expected_runtime_sha256"], 64,
                                             "expected runtime hash")}


def _http_transport(config: dict, *, max_response: int = MAX_RESPONSE):
    if type(max_response) is not int or not 1024 <= max_response <= 8388608:
        raise MachineError("invalid TRON response limit")
    key = os.environ.get(config["api_key_env"]) if config["api_key_env"] else None
    if config["api_key_env"] and not key:
        raise MachineError("TRON API key environment variable is empty")
    opener = build_opener(_NoRedirect())

    def request(view: str, path: str, payload: dict | None) -> dict:
        base = config["solidity_url"] if view == "solidity" else config["fullnode_url"]
        url = base + path
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if key:
            headers["TRON-PRO-API-KEY"] = key
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        req = Request(url, data=data, headers=headers, method="GET" if data is None else "POST")
        try:
            with opener.open(req, timeout=8) as response:
                if response.status != 200:
                    raise MachineError("TRON node HTTP status is not 200")
                body = response.read(max_response + 1)
        except MachineError:
            raise
        except Exception as exc:
            raise MachineError("TRON node read failed") from exc
        if len(body) > max_response:
            raise MachineError("TRON node response too large")
        try:
            value = json.loads(body, parse_float=lambda _: (_ for _ in ()).throw(
                MachineError("TRON node floating point response refused")))
        except (ValueError, UnicodeDecodeError) as exc:
            raise MachineError("TRON node response is not valid JSON") from exc
        if not isinstance(value, dict) or "Error" in value:
            raise MachineError("TRON node returned an error")
        return value

    return request


def _block(value: dict) -> dict:
    if not isinstance(value, dict):
        raise MachineError("solid block response required")
    block_id = _hex(value.get("blockID"), 64, "solid block ID")
    header = value.get("block_header")
    if not isinstance(header, dict) or not isinstance(header.get("raw_data"), dict):
        raise MachineError("solid block header is invalid")
    raw = header["raw_data"]
    number, timestamp = raw.get("number"), raw.get("timestamp")
    if (type(number) is not int or number < 0 or type(timestamp) is not int or timestamp < 0
            or int(block_id[:16], 16) != number):
        raise MachineError("solid block header is invalid")
    return {"block_id": block_id, "number": number, "timestamp_ms": timestamp}


def _call(transport, contract: str, owner: str, name: str,
          parameter: str, words: int) -> list[int]:
    payload = {"owner_address": "41" + owner, "contract_address": "41" + contract,
               "function_selector": name, "parameter": parameter, "visible": False}
    response = transport("solidity", "/walletsolidity/triggerconstantcontract", payload)
    if (not isinstance(response, dict) or not isinstance(response.get("result"), dict)
            or response["result"].get("result") is not True):
        raise MachineError("TRON constant call failed")
    transaction = response.get("transaction")
    if not isinstance(transaction, dict) or not isinstance(transaction.get("ret"), list):
        raise MachineError("TRON constant call transaction result missing")
    if (len(transaction["ret"]) != 1
            or not isinstance(transaction["ret"][0], dict)
            or transaction["ret"][0].get("ret") not in {"SUCCESS", "SUCESS"}):
        raise MachineError("TRON constant call VM failed")
    result = response.get("constant_result")
    if not isinstance(result, list) or len(result) != 1:
        raise MachineError("TRON constant call has no single result")
    raw = _hex(result[0], words * 64, "TRON ABI result")
    return [int(raw[index:index + 64], 16) for index in range(0, len(raw), 64)]


def _boolean(word: int) -> bool:
    if word not in (0, 1):
        raise MachineError("invalid ABI boolean")
    return bool(word)


def _abi_address(word: int) -> str:
    if word >= 1 << 160:
        raise MachineError("noncanonical ABI address")
    return f"{word:040x}"


def read_registry_observation(binding: dict, config: dict, *, now_ms: int | None = None,
                              transport=None) -> dict:
    """Read a bounded, read-only snapshot; return no execution authority."""
    if (not isinstance(binding, dict)
            or binding.get("schema_version") not in {"economic-basket-registry-binding-1",
                                                     "economic-basket-registry-binding-2",
                                                     "economic-vault-leg-registry-binding-1"}):
        raise MachineError("typed basket binding required")
    settings = _config(config)
    if now_ms is None:
        now_ms = time.time_ns() // 1000000
    if type(now_ms) is not int or now_ms < 0:
        raise MachineError("invalid observation time")
    node = transport or _http_transport(settings)
    block_before = _block(node("solidity", "/walletsolidity/getnowblock", None))
    code_response = node("fullnode", "/wallet/getcontractinfo",
                         {"value": "41" + _address(binding["registry_address"], "registry address"),
                          "visible": False})
    if not isinstance(code_response, dict):
        raise MachineError("TRON contract information missing")
    runtime = code_response.get("runtimecode")
    if not isinstance(runtime, str) or not runtime:
        raise MachineError("TRON runtime bytecode missing")
    runtime_hex = runtime.removeprefix("0x").lower()
    if len(runtime_hex) % 2 or re.fullmatch(r"[0-9a-f]+", runtime_hex) is None:
        raise MachineError("invalid TRON runtime bytecode")
    code_hash = hashlib.sha256(bytes.fromhex(runtime_hex)).hexdigest()
    contract = _address(binding["registry_address"], "registry address")
    owner = settings["owner_address"]
    policy_id = _hex(binding["policy_id"], 64, "policy id")
    asset_id = _address(binding["asset_address"], "asset address")
    basket_id = _hex(binding["binding_hash"], 64, "binding hash")
    offchain_id = _hex(binding["basket_commitment_hash"], 64, "commitment hash")
    amount_text = binding.get("amount_base_units")
    if (not isinstance(amount_text, str) or len(amount_text) > 78
            or re.fullmatch(r"[1-9][0-9]*", amount_text) is None):
        raise MachineError("invalid basket base-unit amount")
    expiry = binding.get("valid_until_epoch_seconds")
    if type(expiry) is not int or not 0 < expiry < 1 << 64:
        raise MachineError("invalid basket registry expiry")
    parameter = (policy_id + _hex(binding["state_root"], 64, "state root")
                 + _hex(binding["basket_hash"], 64, "basket hash") + offchain_id
                 + _word(int(amount_text))
                 + _word(expiry))
    chain_hash = _call(node, contract, owner, "computeBasketBinding(bytes32,bytes32,bytes32,bytes32,uint256,uint64)",
                       parameter, 1)[0]
    policy = _call(node, contract, owner, "policies(bytes32)", policy_id, 6)
    basket = _call(node, contract, owner, "baskets(bytes32)", basket_id, 9)
    linked = _call(node, contract, owner, "bindingByCommitment(bytes32)", offchain_id, 1)[0]
    active = _call(node, contract, owner, "activeBasket(bytes32)", basket_id, 1)[0]
    paused = _call(node, contract, owner, "paused()", "", 1)[0]
    reserved = _call(node, contract, owner, "reservedBasketAmount(bytes32)", policy_id, 1)[0]
    consumed = _call(node, contract, owner, "consumedBasketAmount(bytes32)", policy_id, 1)[0]
    remaining = _call(node, contract, owner, "basketBudgetRemaining(bytes32)", policy_id, 1)[0]
    asset_parameter = asset_id.rjust(64, "0")
    asset_budget = _call(node, contract, owner, "assetBudgets(address)",
                         asset_parameter, 4)
    asset_remaining = _call(node, contract, owner, "assetBudgetRemaining(address)",
                            asset_parameter, 1)[0]
    attestation_required = _call(node, contract, owner, "attestationRequired(bytes32)",
                                 policy_id, 1)[0]
    authenticated_source = _call(node, contract, owner,
                                 "authenticatedSourceByBinding(bytes32)", basket_id, 1)[0]
    attested_epoch = _call(node, contract, owner, "attestedEpochByBinding(bytes32)",
                           basket_id, 1)[0]
    current_attestor_epoch = _call(node, contract, owner, "attestorEpoch()", "", 1)[0]
    block_after = _block(node("solidity", "/walletsolidity/getnowblock", None))
    if block_before != block_after:
        raise MachineError("TRON solidified view changed during registry read")
    age_ms = now_ms - block_after["timestamp_ms"]
    if not -5000 <= age_ms <= settings["max_block_age_ms"]:
        raise MachineError("TRON solidified block is stale or from the future")
    observation = {"schema_version": VERSION, **block_after,
                   "observed_at_ms": now_ms, "runtime_sha256": code_hash,
                   "expected_runtime_sha256": settings["expected_runtime_sha256"],
                   "onchain_binding_hash": f"{chain_hash:064x}",
                   "policy": {"policy_hash": f"{policy[0]:064x}",
                              "asset_address": _abi_address(policy[1]),
                              "target_address": _abi_address(policy[2]),
                              "max_amount": str(policy[3]), "expires_at": policy[4],
                              "active": _boolean(policy[5]),
                              "attestation_required": _boolean(attestation_required),
                              "reserved_amount": str(reserved),
                              "consumed_amount": str(consumed),
                              "budget_remaining": str(remaining)},
                   "asset_budget": {"asset_address": asset_id,
                                    "limit": str(asset_budget[0]),
                                    "reserved": str(asset_budget[1]),
                                    "consumed": str(asset_budget[2]),
                                    "configured": _boolean(asset_budget[3]),
                                    "remaining": str(asset_remaining)},
                   "basket": {"policy_id": f"{basket[0]:064x}",
                              "state_root": f"{basket[1]:064x}",
                              "basket_hash": f"{basket[2]:064x}",
                              "offchain_commitment_hash": f"{basket[3]:064x}",
                              "amount": str(basket[4]), "valid_until": basket[5],
                              "committed": _boolean(basket[6]),
                              "revoked": _boolean(basket[7]),
                              "consumed": _boolean(basket[8])},
                   "binding_by_commitment": f"{linked:064x}",
                   "authenticated_source_hash": f"{authenticated_source:064x}",
                   "attested_epoch": attested_epoch,
                   "current_attestor_epoch": current_attestor_epoch,
                   "active_basket": _boolean(active), "paused": _boolean(paused),
                   "endpoint_fingerprint": digest({"solid": settings["solidity_url"],
                                                   "full": settings["fullnode_url"]}),
                   "source_trust": "NODE_RESPONSE_ONLY",
                   "execution_authority": "NONE"}
    observation["observation_hash"] = digest(observation)
    return observation


def assess_registry_observation(binding: dict, observation: dict) -> dict:
    """Compare a node report to the basket binding, retaining uncertainty."""
    if not isinstance(binding, dict) or not isinstance(observation, dict):
        raise MachineError("binding and observation required")
    if binding.get("schema_version") not in {"economic-basket-registry-binding-1",
                                             "economic-basket-registry-binding-2",
                                             "economic-vault-leg-registry-binding-1"}:
        raise MachineError("unsupported basket binding")
    if observation.get("schema_version") != VERSION:
        raise MachineError("unsupported registry observation")
    recorded = dict(observation)
    claimed_hash = recorded.pop("observation_hash", None)
    if claimed_hash != digest(recorded):
        raise MachineError("registry observation hash mismatch")
    policy, basket = observation["policy"], observation["basket"]
    asset = observation["asset_budget"]
    reasons = []
    maximum = int(policy["max_amount"])
    reserved = int(policy["reserved_amount"])
    consumed = int(policy["consumed_amount"])
    remaining = int(policy["budget_remaining"])
    asset_limit = int(asset["limit"])
    asset_reserved = int(asset["reserved"])
    asset_consumed = int(asset["consumed"])
    asset_remaining = int(asset["remaining"])
    comparisons = (
        (observation["source_trust"] == "NODE_RESPONSE_ONLY", "SOURCE_TRUST"),
        (observation["execution_authority"] == "NONE", "AUTHORITY_CLAIM"),
        (type(observation["current_attestor_epoch"]) is int
         and 1 <= observation["current_attestor_epoch"] < 1 << 64,
         "ATTESTOR_EPOCH_INVALID"),
        (observation["runtime_sha256"] == observation["expected_runtime_sha256"], "CODE_HASH"),
        (observation["onchain_binding_hash"] == binding["binding_hash"], "BINDING_HASH"),
        (observation["binding_by_commitment"] == binding["binding_hash"], "COMMITMENT_LINK"),
        (policy["policy_hash"] == binding["policy_hash"], "POLICY_HASH"),
        (policy["asset_address"] == binding["asset_address"], "POLICY_ASSET"),
        (policy["target_address"] == binding["target_address"], "POLICY_TARGET"),
        (maximum > 0 and reserved >= 0 and consumed >= 0 and remaining >= 0,
         "POLICY_BUDGET_FORMAT"),
        (maximum >= int(binding["amount_base_units"]), "POLICY_AMOUNT"),
        (reserved >= int(binding["amount_base_units"]), "BASKET_NOT_RESERVED"),
        (reserved + consumed <= maximum, "POLICY_BUDGET_EXCEEDED"),
        (remaining == maximum - reserved - consumed, "POLICY_BUDGET_MISMATCH"),
        (asset["asset_address"] == binding["asset_address"], "ASSET_BUDGET_ASSET"),
        (asset["configured"], "ASSET_BUDGET_UNCONFIGURED"),
        (asset_limit > 0 and asset_reserved >= 0 and asset_consumed >= 0
         and asset_remaining >= 0, "ASSET_BUDGET_FORMAT"),
        (asset_reserved >= int(binding["amount_base_units"]), "ASSET_BASKET_NOT_RESERVED"),
        (asset_reserved >= reserved, "ASSET_POLICY_RESERVATION_MISMATCH"),
        (asset_consumed >= consumed, "ASSET_POLICY_CONSUMPTION_MISMATCH"),
        (asset_reserved + asset_consumed <= asset_limit, "ASSET_BUDGET_EXCEEDED"),
        (asset_remaining == asset_limit - asset_reserved - asset_consumed,
         "ASSET_BUDGET_MISMATCH"),
        (policy["expires_at"] >= binding["valid_until_epoch_seconds"], "POLICY_EXPIRY"),
        (policy["active"], "POLICY_INACTIVE"),
        (basket["policy_id"] == binding["policy_id"], "BASKET_POLICY"),
        (basket["state_root"] == binding["state_root"], "BASKET_STATE"),
        (basket["basket_hash"] == binding["basket_hash"], "BASKET_HASH"),
        (basket["offchain_commitment_hash"] == binding["basket_commitment_hash"],
         "BASKET_COMMITMENT"),
        (basket["amount"] == binding["amount_base_units"], "BASKET_AMOUNT"),
        (basket["valid_until"] == binding["valid_until_epoch_seconds"], "BASKET_EXPIRY"),
        (basket["committed"], "BASKET_ABSENT"),
        (not basket["revoked"], "BASKET_REVOKED"),
        (not basket["consumed"], "BASKET_CONSUMED"),
        (observation["active_basket"], "BASKET_INACTIVE"),
        (not observation["paused"], "REGISTRY_PAUSED"),
        (observation["observed_at_ms"] // 1000 < basket["valid_until"], "LOCALLY_EXPIRED"),
        (observation["observed_at_ms"] // 1000 < policy["expires_at"], "POLICY_LOCALLY_EXPIRED"),
    )
    reasons.extend(code for passed, code in comparisons if not passed)
    if binding.get("schema_version") in {"economic-basket-registry-binding-2",
                                          "economic-vault-leg-registry-binding-1"}:
        reasons.extend(code for passed, code in (
            (policy["attestation_required"], "ATTESTATION_NOT_REQUIRED"),
            (observation["authenticated_source_hash"] ==
             _hex(binding.get("authenticated_source_hash"), 64, "authenticated source hash"),
             "AUTHENTICATED_SOURCE_MISMATCH"),
            (type(observation["attested_epoch"]) is int
             and 0 < observation["attested_epoch"] < 1 << 64,
             "ATTESTED_EPOCH_MISSING"),
            (type(observation["current_attestor_epoch"]) is int
             and observation["current_attestor_epoch"] == observation["attested_epoch"],
             "ATTESTED_EPOCH_INVALID"),
        ) if not passed)
    else:
        reasons.extend(code for passed, code in (
            (not policy["attestation_required"], "LEGACY_POLICY_REQUIRES_ATTESTATION"),
            (observation["authenticated_source_hash"] == "0" * 64,
             "UNEXPECTED_AUTHENTICATED_SOURCE"),
            (observation["attested_epoch"] == 0, "UNEXPECTED_ATTESTED_EPOCH"),
        ) if not passed)
    result = {"schema_version": "economic-tron-registry-assessment-4",
              "status": "OBSERVED_MATCH" if not reasons else "WITHHELD",
              "reason_codes": reasons, "binding_hash": binding["binding_hash"],
              "observation_hash": claimed_hash,
              "source_trust": "NODE_RESPONSE_ONLY", "execution_authority": "NONE"}
    if binding.get("schema_version") in {"economic-basket-registry-binding-2",
                                          "economic-vault-leg-registry-binding-1"}:
        result["authenticated_source_hash"] = _hex(
            binding.get("authenticated_source_hash"), 64, "authenticated source hash")
        result["signed_assembly_hash"] = _hex(
            binding.get("signed_assembly_hash"), 64, "signed assembly hash")
    result["assessment_hash"] = digest(result)
    return result
