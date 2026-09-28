"""Fixed TRON action catalogue and precision-safe unsigned call encoding.

The catalogue distinguishes an ABI shape from a deployed-contract binding.
Knowing that JustLend uses ``mint(uint256)`` does not prove that a supplied
address is that contract.  Callers must separately bind the product capability,
code/ABI evidence, network, amount, fee and expiry in an ExecutionGraphV1.
"""

import re

from .mandate import hash32, integer, tron_address
from .values import MachineError, digest, ident, require_keys, utc


VERSION = "tron-action-1"
BINDING_VERSION = "tron-action-binding-1"
SMART = "TRIGGER_SMART_CONTRACT"
NATIVE = "TRON_SYSTEM_CONTRACT"
BLOCKED = "UNVERIFIED_COMPOSITE"

# JustLend signatures are taken from the official justlend/mcp-server-justlend
# ABI catalogue. Native operation names follow the official TRON Stake 2.0
# APIs. USDD manager primitives are known, but a safe customer flow also needs
# token join/exit and proxy composition; those entries therefore stay blocked.
CATALOG = {
    "TRC20_APPROVE": (SMART, "approve(address,uint256)", ("address", "uint256"),
                      False, "BOOL_TRUE", True),
    "JUSTLEND_SUPPLY": (SMART, "mint(uint256)", ("uint256",),
                        False, "UINT_ZERO", True),
    "JUSTLEND_REDEEM_SHARES": (SMART, "redeem(uint256)", ("uint256",),
                               False, "UINT_ZERO", True),
    "JUSTLEND_REDEEM_UNDERLYING": (SMART, "redeemUnderlying(uint256)", ("uint256",),
                                   False, "UINT_ZERO", True),
    "STRX_STAKE": (SMART, "deposit()", (), True, "RECEIPT_SUCCESS", True),
    "STRX_UNSTAKE": (SMART, "withdraw(uint256)", ("uint256",),
                     False, "RECEIPT_SUCCESS", True),
    "STRX_CLAIM": (SMART, "claimAll()", (), False, "RECEIPT_SUCCESS", True),
    "TRON_STAKE": (NATIVE, "FreezeBalanceV2Contract", ("uint256", "resource"),
                   False, "RECEIPT_SUCCESS", True),
    "TRON_UNSTAKE": (NATIVE, "UnfreezeBalanceV2Contract", ("uint256", "resource"),
                     False, "RECEIPT_SUCCESS", True),
    "TRON_DELEGATE": (NATIVE, "DelegateResourceContract",
                      ("uint256", "resource", "address", "bool"),
                      False, "RECEIPT_SUCCESS", True),
    "TRON_UNDELEGATE": (NATIVE, "UnDelegateResourceContract",
                        ("uint256", "resource", "address"),
                        False, "RECEIPT_SUCCESS", True),
    "TRON_CLAIM_UNSTAKED": (NATIVE, "WithdrawExpireUnfreezeContract", (),
                            False, "RECEIPT_SUCCESS", True),
    "USDD_VAULT_OPEN": (BLOCKED, "open(bytes32,address)", ("bytes32", "address"),
                        False, "UINT_ID", False),
    "USDD_VAULT_ADD_COLLATERAL": (BLOCKED, "frob(uint256,int256,int256)",
                                  ("uint256", "int256", "int256"),
                                  False, "RECEIPT_SUCCESS", False),
    "USDD_VAULT_MINT": (BLOCKED, "frob(uint256,int256,int256)",
                        ("uint256", "int256", "int256"),
                        False, "RECEIPT_SUCCESS", False),
    "USDD_VAULT_REPAY": (BLOCKED, "frob(uint256,int256,int256)",
                         ("uint256", "int256", "int256"),
                         False, "RECEIPT_SUCCESS", False),
    "USDD_VAULT_WITHDRAW_COLLATERAL": (BLOCKED, "frob(uint256,int256,int256)",
                                       ("uint256", "int256", "int256"),
                                       False, "RECEIPT_SUCCESS", False),
}


def _uint(value, label):
    if (not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]*", value)
            or len(value) > 78 or int(value) >= 1 << 256):
        raise MachineError("invalid " + label)
    return int(value)


def _word(number):
    return number.to_bytes(32, "big").hex()


def _encode(kind, value):
    if kind == "uint256":
        return _word(_uint(value, "uint256 argument"))
    if kind == "int256":
        if (not isinstance(value, str) or re.fullmatch(r"-?(0|[1-9][0-9]*)", value) is None
                or len(value) > 79):
            raise MachineError("invalid int256 argument")
        number = int(value)
        if not -(1 << 255) <= number < 1 << 255:
            raise MachineError("int256 argument outside range")
        return _word(number % (1 << 256))
    if kind == "address":
        return "0" * 24 + tron_address(value)[2:]
    if kind == "bytes32":
        hash32(value, "bytes32 argument")
        return value
    if kind == "resource":
        if value not in {"BANDWIDTH", "ENERGY"}:
            raise MachineError("unsupported TRON resource")
        return value
    if kind == "bool":
        if type(value) is not bool:
            raise MachineError("boolean argument required")
        return value
    raise MachineError("unsupported action argument type")


def normalize_action_binding(raw: dict) -> dict:
    require_keys(raw, {"schema_version", "operation", "network", "target_address",
                       "capability_hash", "abi_evidence_hash", "contract_code_hash",
                       "status", "source_url"}, "TronActionBindingV1")
    if raw["schema_version"] != BINDING_VERSION:
        raise MachineError("unsupported TRON action binding version")
    operation = ident(raw["operation"], "operation")
    if operation not in CATALOG:
        raise MachineError("unknown TRON operation")
    if raw["network"] not in {"tron-mainnet", "tron-nile", "tron-shasta"}:
        raise MachineError("unsupported action network")
    transport = CATALOG[operation][0]
    target = raw["target_address"]
    if transport == NATIVE:
        if target is not None:
            raise MachineError("native action cannot have a smart-contract target")
    elif target is not None:
        target = tron_address(target)
    if raw["status"] not in {"VERIFIED", "UNVERIFIED"}:
        raise MachineError("invalid action binding status")
    for key in ("capability_hash", "abi_evidence_hash", "contract_code_hash"):
        if raw[key] is not None:
            hash32(raw[key], key)
    if raw["status"] == "VERIFIED" and any(raw[key] is None for key in (
            "capability_hash", "abi_evidence_hash", "contract_code_hash")):
        raise MachineError("verified binding requires capability, ABI and code evidence")
    if not isinstance(raw["source_url"], str) or not raw["source_url"].startswith("https://"):
        raise MachineError("HTTPS action source required")
    return {**raw, "target_address": target}


def compile_action(binding: dict, arguments: list, *, call_value_sun: str,
                   fee_limit_sun: str, expires_at: str) -> dict:
    binding = normalize_action_binding(binding)
    operation = binding["operation"]
    transport, selector, types, payable, semantics, complete = CATALOG[operation]
    if not isinstance(arguments, list) or len(arguments) != len(types):
        raise MachineError("action arguments do not match fixed ABI")
    encoded = [_encode(kind, value) for kind, value in zip(types, arguments)]
    call_value, fee_limit = _uint(call_value_sun, "call value"), _uint(
        fee_limit_sun, "fee limit")
    if fee_limit == 0:
        raise MachineError("positive fee limit required")
    if call_value and not payable:
        raise MachineError("nonpayable action cannot carry TRX")
    if payable and operation == "STRX_STAKE" and call_value == 0:
        raise MachineError("sTRX deposit needs positive call value")
    if transport == SMART and binding["target_address"] is None:
        raise MachineError("smart-contract action target missing")
    status = "READY_FOR_SIMULATION"
    blockers = []
    if binding["status"] != "VERIFIED":
        status, blockers = "BLOCKED", ["UNVERIFIED_ACTION_BINDING"]
    if not complete:
        status = "BLOCKED"
        blockers.append("INCOMPLETE_VAULT_JOIN_EXIT_SEQUENCE")
    parameter_hex = None
    if transport == SMART:
        parameter_hex = "".join(encoded)
    native_parameters = None
    if transport == NATIVE:
        native_parameters = [{"type": kind, "value": value}
                             for kind, value in zip(types, encoded)]
    expires_at = utc(expires_at)
    result = {"schema_version": VERSION, "operation": operation,
        "transport": transport, "target_address": binding["target_address"],
        "function_selector": selector, "parameter_types": list(types),
        "arguments": list(arguments), "parameter_hex": parameter_hex,
        "native_parameters": native_parameters, "call_value_sun": str(call_value),
        "fee_limit_sun": str(fee_limit), "result_semantics": semantics,
        "expires_at": expires_at, "binding": binding, "status": status,
        "blockers": sorted(set(blockers)), "signature_status": "NOT_REQUESTED",
        "transaction_status": "NOT_BUILT", "execution_authority": "NONE"}
    result["action_hash"] = digest(result)
    return result


def verify_action(action: dict, binding: dict, arguments: list, *,
                  call_value_sun: str, fee_limit_sun: str, expires_at: str) -> bool:
    try:
        return action == compile_action(binding, arguments,
            call_value_sun=call_value_sun, fee_limit_sun=fee_limit_sun,
            expires_at=expires_at)
    except (MachineError, KeyError, TypeError, ValueError):
        return False
