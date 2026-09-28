"""Versioned product capability declarations; never inferred from a protocol name."""

from .mandate import ACTIONS, NETWORKS, hash32, integer, tron_address
from .values import MachineError, digest, ident, require_keys


VERSION = "economic-product-capability-1"
STAGES = {"read", "quote", "simulate", "execute", "reconcile"}
STATUSES = {"UNKNOWN", "UNSUPPORTED", "SUPPORTED"}


def normalize_capability(raw: dict) -> dict:
    require_keys(raw, {"schema_version", "chain", "network", "contract", "protocol",
                       "protocol_version", "action", "token", "stages"}, "ProductCapabilityV1")
    if (raw["schema_version"] != VERSION or raw["chain"] != "TRON"
            or not isinstance(raw["network"], str) or raw["network"] not in NETWORKS):
        raise MachineError("unsupported product chain/network/version")
    if not isinstance(raw["action"], str) or raw["action"] not in ACTIONS:
        raise MachineError("unsupported product action")
    token = require_keys(raw["token"], {"asset", "address", "decimals"}, "product token")
    normalized_token = {"asset": ident(token["asset"], "token asset"),
                        "address": tron_address(token["address"]) if token["address"] is not None else None,
                        "decimals": integer(token["decimals"], "token decimals", 0, 36)}
    if token["address"] is None and token["asset"] != "TRX":
        raise MachineError("only native TRX may omit token address")
    contract = tron_address(raw["contract"]) if raw["contract"] is not None else None
    if contract is None and raw["protocol"] != "tron-native":
        raise MachineError("only native TRON may omit contract")
    stages = require_keys(raw["stages"], STAGES, "capability stages")
    normalized_stages = {}
    for stage, item in stages.items():
        require_keys(item, {"status", "evidence_hash"}, "capability stage")
        if not isinstance(item["status"], str) or item["status"] not in STATUSES:
            raise MachineError("invalid capability status")
        if item["status"] == "SUPPORTED":
            hash32(item["evidence_hash"], "capability evidence")
        elif item["evidence_hash"] is not None:
            hash32(item["evidence_hash"], "capability evidence")
        normalized_stages[stage] = dict(item)
    return {**raw, "contract": contract, "protocol": ident(raw["protocol"], "protocol"),
            "protocol_version": ident(raw["protocol_version"], "protocol version"),
            "token": normalized_token, "stages": normalized_stages}


def product_id(raw: dict) -> str:
    item = normalize_capability(raw)
    return digest({"domain": VERSION + "/identity",
                   **{key: item[key] for key in ("chain", "network", "contract", "protocol",
                                                  "protocol_version", "action")}})


def capability_hash(raw: dict) -> str:
    return digest({"domain": VERSION, "capability": normalize_capability(raw)})
