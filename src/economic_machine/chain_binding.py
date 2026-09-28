"""Prepare a typed TRON registry hash from a reviewed, non-executable basket.

No RPC, private key, signing, transaction construction or submission occurs.
Addresses are the 20-byte TVM representation, not external Base58 strings.
"""

import hashlib
import re
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from .authenticated_basket import verify_authenticated_basket
from .basket import verify_basket
from .values import MachineError, decimal, digest, require_keys, utc


VERSION = "economic-basket-registry-binding-1"
AUTHENTICATED_VERSION = "economic-basket-registry-binding-2"
CONTEXT_VERSION = "economic-basket-registry-context-1"
DOMAIN = b"ECONOMIC_BASKET_REGISTRY_V1".ljust(32, b"\x00")
ATTESTATION_DOMAIN = b"ECONOMIC_BASKET_ATTEST_V1".ljust(32, b"\x00")
UINT256 = 1 << 256


def _word(value: int, label: str) -> bytes:
    if type(value) is not int or not 0 <= value < UINT256:
        raise MachineError(label + " is outside uint256")
    return value.to_bytes(32, "big")


def _hash_word(value: object, label: str) -> bytes:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise MachineError(label + " must be bytes32 hex")
    if value == "0" * 64:
        raise MachineError(label + " cannot be zero")
    return bytes.fromhex(value)


def _address_word(value: object, label: str) -> bytes:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise MachineError(label + " must be a 20-byte TVM address")
    if value == "0" * 40:
        raise MachineError(label + " cannot be zero")
    return bytes(12) + bytes.fromhex(value)


def _epoch_seconds(value: str) -> int:
    point = datetime.fromisoformat(utc(value))
    delta = point - datetime(1970, 1, 1, tzinfo=timezone.utc)
    if delta.days < 0 or delta.microseconds:
        raise MachineError("registry expiry must be a whole second after epoch")
    result = delta.days * 86400 + delta.seconds
    if result >= 1 << 64:
        raise MachineError("registry expiry exceeds uint64")
    return result


def prepare_chain_binding(commitment: dict, request: dict, state: dict,
                          policy: dict, context: dict) -> dict:
    """Compute SHA-256(abi.encode(...)) as EconomicPolicyRegistry does."""
    if not verify_basket(commitment, request, state, policy):
        raise MachineError("basket commitment does not replay")
    return _prepare_verified_binding(commitment, context, version=VERSION)


def prepare_authenticated_chain_binding(commitment: dict, template: dict,
                                        signed_bundle: dict, trust_roots: dict,
                                        state: dict, policy: dict, context: dict) -> dict:
    """Bind the signed basket hash without granting chain write authority."""
    if not verify_authenticated_basket(commitment, template, signed_bundle,
                                       trust_roots, state, policy):
        raise MachineError("authenticated basket commitment does not replay")
    return _prepare_verified_binding(commitment, context, version=AUTHENTICATED_VERSION)


def _prepare_verified_binding(commitment: dict, context: dict, *, version: str) -> dict:
    require_keys(context, {"schema_version", "network", "chain_id",
                           "registry_address", "asset_address", "target_address",
                           "asset_decimals"}, "BasketRegistryContext")
    if context["schema_version"] != CONTEXT_VERSION or context["network"] != commitment["network"]:
        raise MachineError("registry context version or network mismatch")
    chain_id = context["chain_id"]
    if type(chain_id) is not int or not 0 < chain_id < UINT256:
        raise MachineError("invalid chain id")
    decimals = context["asset_decimals"]
    if type(decimals) is not int or not 0 <= decimals <= 36:
        raise MachineError("invalid asset decimals")
    with localcontext() as arithmetic:
        arithmetic.prec = 256
        base_units = decimal(commitment["basket"]["capital"]) * (Decimal(10) ** decimals)
        if base_units != base_units.to_integral_value():
            raise MachineError("basket capital is not an exact token-unit amount")
        amount = int(base_units)
    if not 0 < amount < UINT256:
        raise MachineError("basket amount is outside uint256")
    expiry = _epoch_seconds(commitment["valid_until"])
    words = [DOMAIN, _address_word(context["registry_address"], "registry address"),
             _word(chain_id, "chain id"),
             _hash_word(commitment["policy_id"], "policy id"),
             _hash_word(commitment["policy_hash"], "policy hash"),
             _hash_word(commitment["state_root"], "state root"),
             _hash_word(commitment["basket_hash"], "basket hash"),
             _hash_word(commitment["commitment_hash"], "offchain commitment hash"),
             _address_word(context["asset_address"], "asset address"),
             _address_word(context["target_address"], "target address"),
             _word(amount, "basket amount"), _word(expiry, "expiry")]
    binding_hash = hashlib.sha256(b"".join(words)).hexdigest()
    result = {"schema_version": version, "basket_commitment_hash": commitment["commitment_hash"],
            "policy_id": commitment["policy_id"], "policy_hash": commitment["policy_hash"],
            "state_root": commitment["state_root"], "basket_hash": commitment["basket_hash"],
            "registry_context_hash": digest(context), "chain_id": chain_id,
            "registry_address": context["registry_address"],
            "asset_address": context["asset_address"],
            "target_address": context["target_address"],
            "amount_base_units": str(amount), "valid_until_epoch_seconds": expiry,
            "binding_hash": binding_hash, "registry_state": "NOT_QUERIED",
            "transaction_status": "NOT_BUILT", "execution_authority": "NONE"}
    if version == AUTHENTICATED_VERSION:
        source = commitment["authenticated_source"]
        result["authenticated_source_hash"] = digest(source)
        result["signed_assembly_hash"] = source["signed_assembly_hash"]
        result["trust_root_hash"] = source["trust_root_hash"]
        result["source_trust"] = source["status"]
    return result


def verify_chain_binding(binding: dict, commitment: dict, request: dict,
                         state: dict, policy: dict, context: dict) -> bool:
    if not isinstance(binding, dict):
        return False
    try:
        return binding == prepare_chain_binding(commitment, request, state, policy, context)
    except (MachineError, KeyError, TypeError, ValueError):
        return False


def verify_authenticated_chain_binding(binding: dict, commitment: dict, template: dict,
                                       signed_bundle: dict, trust_roots: dict, state: dict,
                                       policy: dict, context: dict) -> bool:
    if not isinstance(binding, dict) or binding.get("schema_version") != AUTHENTICATED_VERSION:
        return False
    try:
        return binding == prepare_authenticated_chain_binding(
            commitment, template, signed_bundle, trust_roots, state, policy, context)
    except (MachineError, KeyError, TypeError, ValueError, ZeroDivisionError):
        return False


def compute_attestation_digest(binding: dict, attestor_epoch: int) -> str:
    """Reproduce the contract's SHA-256 message; this does not verify sources."""
    if (not isinstance(binding, dict) or binding.get("schema_version") not in
            {AUTHENTICATED_VERSION, "economic-vault-leg-registry-binding-1"}):
        raise MachineError("authenticated registry binding required")
    if type(attestor_epoch) is not int or not 1 <= attestor_epoch < 1 << 64:
        raise MachineError("attestor epoch must be a positive uint64")
    chain_id = binding.get("chain_id")
    if type(chain_id) is not int or not 0 < chain_id < UINT256:
        raise MachineError("invalid chain id")
    words = [ATTESTATION_DOMAIN,
             _address_word(binding.get("registry_address"), "registry address"),
             _word(chain_id, "chain id"),
             _hash_word(binding.get("binding_hash"), "binding hash"),
             _hash_word(binding.get("authenticated_source_hash"), "authenticated source hash"),
             _word(attestor_epoch, "attestor epoch")]
    return hashlib.sha256(b"".join(words)).hexdigest()


def prepare_attestation_message(binding: dict, commitment: dict, template: dict,
                                signed_bundle: dict, trust_roots: dict, state: dict,
                                policy: dict, context: dict, *, attestor_epoch: int) -> dict:
    """Replay all signed inputs before preparing a message for external signers."""
    if not verify_authenticated_chain_binding(binding, commitment, template,
                                              signed_bundle, trust_roots, state,
                                              policy, context):
        raise MachineError("authenticated chain binding does not replay")
    result = {"schema_version": "economic-basket-attestation-message-1",
              "binding_hash": binding["binding_hash"],
              "authenticated_source_hash": binding["authenticated_source_hash"],
              "registry_address": binding["registry_address"],
              "chain_id": binding["chain_id"], "attestor_epoch": attestor_epoch,
              "message_hash": compute_attestation_digest(binding, attestor_epoch),
              "status": "PREPARED_NOT_SIGNED", "execution_authority": "NONE"}
    result["preparation_hash"] = digest(result)
    return result
