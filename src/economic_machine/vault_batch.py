"""Compile a signed multi-product basket into unsigned atomic vault orders.

This prepares hashes and typed arguments only. It does not prove registry or
vault state, sign a digest, construct a transaction, or grant execution rights.
"""

import hashlib
import re
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from .authenticated_basket import verify_authenticated_basket
from .chain_binding import (_address_word, _epoch_seconds, _hash_word, _word,
                            DOMAIN, UINT256)
from .values import MachineError, decimal, digest, ident, require_keys, utc


VERSION = "economic-vault-batch-plan-1"
CONTEXT_VERSION = "economic-vault-batch-context-1"
LEG_DOMAIN = "economic-vault-leg-1"
EXECUTION_DOMAIN = b"ECONOMIC_VAULT_EXECUTE_V1".ljust(32, b"\x00")
BATCH_DOMAIN = b"ECONOMIC_VAULT_BATCH_V1".ljust(32, b"\x00")
LEG_KEYS = {"product_id", "policy_id", "policy_hash", "target_address",
            "output_asset_address", "min_output_base_units", "route_data_hex",
            "deadline_epoch_seconds"}


def _positive_uint(value: object, label: str) -> int:
    if (not isinstance(value, str) or len(value) > 78
            or re.fullmatch(r"[1-9][0-9]*", value) is None):
        raise MachineError(label + " must be a positive decimal integer string")
    number = int(value)
    if not 0 < number < UINT256:
        raise MachineError(label + " exceeds uint256")
    return number


def _amount_base_units(value: str, decimals: int) -> int:
    with localcontext() as arithmetic:
        arithmetic.prec = 256
        amount = decimal(value) * (Decimal(10) ** decimals)
        if amount != amount.to_integral_value():
            raise MachineError("leg amount cannot be represented in token units")
        result = int(amount)
    if not 0 < result < UINT256:
        raise MachineError("leg amount is outside uint256")
    return result


def compute_vault_order_digest(*, vault_address: str, registry_address: str,
                               chain_id: int, binding_hash: str,
                               input_asset_address: str, amount_base_units: int,
                               target_address: str, output_asset_address: str,
                               min_output_base_units: int, route_hash: str,
                               deadline_epoch_seconds: int) -> str:
    if (type(chain_id) is not int or not 0 < chain_id < UINT256
            or type(amount_base_units) is not int or not 0 < amount_base_units < UINT256
            or type(min_output_base_units) is not int
            or not 0 < min_output_base_units < UINT256
            or type(deadline_epoch_seconds) is not int
            or not 0 < deadline_epoch_seconds < 1 << 64):
        raise MachineError("invalid vault order quantity, chain or deadline")
    words = [EXECUTION_DOMAIN, _address_word(vault_address, "vault address"),
             _word(chain_id, "chain id"), _address_word(registry_address, "registry address"),
             _hash_word(binding_hash, "binding hash"),
             _address_word(input_asset_address, "input asset"),
             _word(amount_base_units, "input amount"),
             _address_word(target_address, "target address"),
             _address_word(output_asset_address, "output asset"),
             _word(min_output_base_units, "minimum output"),
             _hash_word(route_hash, "route hash"),
             _word(deadline_epoch_seconds, "deadline")]
    return hashlib.sha256(b"".join(words)).hexdigest()


def compute_vault_batch_digest(*, vault_address: str, registry_address: str,
                               chain_id: int, parent_basket_hash: str,
                               order_digests: list[str],
                               batch_deadline_epoch_seconds: int) -> tuple[str, str]:
    if not isinstance(order_digests, list) or not 2 <= len(order_digests) <= 8:
        raise MachineError("atomic vault batch needs 2 to 8 orders")
    if (type(chain_id) is not int or not 0 < chain_id < UINT256
            or type(batch_deadline_epoch_seconds) is not int
            or not 0 < batch_deadline_epoch_seconds < 1 << 64):
        raise MachineError("invalid batch chain or deadline")
    encoded_array = (_word(32, "array offset") + _word(len(order_digests), "array length")
                     + b"".join(_hash_word(item, "order digest") for item in order_digests))
    orders_hash = hashlib.sha256(encoded_array).hexdigest()
    words = [BATCH_DOMAIN, _address_word(vault_address, "vault address"),
             _word(chain_id, "chain id"), _address_word(registry_address, "registry address"),
             _hash_word(parent_basket_hash, "parent basket hash"),
             _hash_word(orders_hash, "orders hash"),
             _word(batch_deadline_epoch_seconds, "batch deadline")]
    return orders_hash, hashlib.sha256(b"".join(words)).hexdigest()


def prepare_vault_batch(commitment: dict, template: dict, signed_bundle: dict,
                        trust_roots: dict, state: dict, policy: dict,
                        context: dict, leg_specs: list[dict], *, prepared_at: str,
                        batch_deadline_epoch_seconds: int) -> dict:
    """Replay signed sources and compile every noncash leg, never a subset."""
    if not verify_authenticated_basket(commitment, template, signed_bundle,
                                       trust_roots, state, policy):
        raise MachineError("authenticated parent basket does not replay")
    require_keys(context, {"schema_version", "network", "chain_id",
                           "registry_address", "vault_address",
                           "input_asset_address", "input_asset_decimals"},
                 "VaultBatchContext")
    if context["schema_version"] != CONTEXT_VERSION or context["network"] != commitment["network"]:
        raise MachineError("vault context version or network mismatch")
    chain_id = context["chain_id"]
    if type(chain_id) is not int or not 0 < chain_id < UINT256:
        raise MachineError("invalid vault chain id")
    decimals = context["input_asset_decimals"]
    if type(decimals) is not int or not 0 <= decimals <= 36:
        raise MachineError("invalid input asset decimals")
    registry_address = context["registry_address"]
    vault_address = context["vault_address"]
    input_asset = context["input_asset_address"]
    for value, label in ((registry_address, "registry address"),
                         (vault_address, "vault address"),
                         (input_asset, "input asset")):
        _address_word(value, label)
    if vault_address == registry_address:
        raise MachineError("vault and registry must be different contracts")
    at = datetime.fromisoformat(utc(prepared_at))
    parent_expiry = _epoch_seconds(commitment["valid_until"])
    if (type(batch_deadline_epoch_seconds) is not int
            or not 0 < batch_deadline_epoch_seconds <= parent_expiry):
        raise MachineError("invalid batch deadline")
    batch_deadline = datetime.fromtimestamp(batch_deadline_epoch_seconds,
                                            tz=timezone.utc)
    if (at < datetime.fromisoformat(commitment["as_of"])
            or not at < batch_deadline
            or batch_deadline_epoch_seconds > parent_expiry):
        raise MachineError("vault batch is outside parent validity")
    legs = commitment["basket"]["legs"]
    if not 2 <= len(legs) <= 8:
        raise MachineError("atomic vault batch needs 2 to 8 noncash legs")
    if (not isinstance(leg_specs, list) or len(leg_specs) != len(legs)
            or any(not isinstance(item, dict) for item in leg_specs)):
        raise MachineError("every noncash leg needs one execution specification")
    specs = {}
    for raw in leg_specs:
        require_keys(raw, LEG_KEYS, "VaultLegSpec")
        name = ident(raw["product_id"], "product id")
        if name in specs:
            raise MachineError("duplicate vault leg specification")
        specs[name] = raw
    if set(specs) != {item["product_id"] for item in legs}:
        raise MachineError("vault legs must exactly cover selected products")
    source = commitment["authenticated_source"]
    source_hash = digest(source)
    prepared = []
    for leg in legs:
        spec = specs[leg["product_id"]]
        policy_id = spec["policy_id"]
        policy_hash = spec["policy_hash"]
        target = spec["target_address"]
        output = spec["output_asset_address"]
        for value, label in ((policy_id, "leg policy id"),
                             (policy_hash, "leg policy hash")):
            _hash_word(value, label)
        for value, label in ((target, "target address"),
                             (output, "output asset")):
            _address_word(value, label)
        if output == input_asset:
            raise MachineError("input and output assets must differ")
        amount = _amount_base_units(leg["amount"], decimals)
        minimum = _positive_uint(spec["min_output_base_units"], "minimum output")
        deadline = spec["deadline_epoch_seconds"]
        if (type(deadline) is not int or not batch_deadline_epoch_seconds <= deadline
                <= parent_expiry or deadline >= 1 << 64):
            raise MachineError("leg deadline is outside batch/parent validity")
        route_hex = spec["route_data_hex"]
        if (not isinstance(route_hex, str) or not 8 <= len(route_hex) <= 8192
                or len(route_hex) % 2 or re.fullmatch(r"[0-9a-f]+", route_hex) is None):
            raise MachineError("route calldata must be bounded lowercase hex")
        child_hash = digest({"domain": LEG_DOMAIN,
                             "parent_commitment_hash": commitment["commitment_hash"],
                             "parent_basket_hash": commitment["basket_hash"],
                             "product_id": leg["product_id"],
                             "amount_base_units": str(amount),
                             "policy_id": policy_id, "policy_hash": policy_hash,
                             "target_address": target,
                             "output_asset_address": output})
        binding_words = [DOMAIN, _address_word(registry_address, "registry address"),
                         _word(chain_id, "chain id"),
                         _hash_word(policy_id, "policy id"),
                         _hash_word(policy_hash, "policy hash"),
                         _hash_word(commitment["state_root"], "state root"),
                         _hash_word(commitment["basket_hash"], "basket hash"),
                         _hash_word(child_hash, "child commitment"),
                         _address_word(input_asset, "input asset"),
                         _address_word(target, "target address"),
                         _word(amount, "leg amount"), _word(parent_expiry, "expiry")]
        binding_hash = hashlib.sha256(b"".join(binding_words)).hexdigest()
        route_hash = hashlib.sha256(bytes.fromhex(route_hex)).hexdigest()
        order_digest = compute_vault_order_digest(
            vault_address=vault_address, registry_address=registry_address,
            chain_id=chain_id, binding_hash=binding_hash,
            input_asset_address=input_asset, amount_base_units=amount,
            target_address=target, output_asset_address=output,
            min_output_base_units=minimum, route_hash=route_hash,
            deadline_epoch_seconds=deadline)
        binding = {"schema_version": "economic-vault-leg-registry-binding-1",
                   "basket_commitment_hash": child_hash,
                   "policy_id": policy_id, "policy_hash": policy_hash,
                   "state_root": commitment["state_root"],
                   "basket_hash": commitment["basket_hash"],
                   "chain_id": chain_id, "registry_address": registry_address,
                   "asset_address": input_asset, "target_address": target,
                   "amount_base_units": str(amount),
                   "valid_until_epoch_seconds": parent_expiry,
                   "binding_hash": binding_hash,
                   "authenticated_source_hash": source_hash,
                   "signed_assembly_hash": source["signed_assembly_hash"],
                   "trust_root_hash": source["trust_root_hash"],
                   "source_trust": source["status"],
                   "registry_state": "NOT_QUERIED",
                   "transaction_status": "NOT_BUILT",
                   "execution_authority": "NONE"}
        prepared.append({"product_id": leg["product_id"],
                         "amount_base_units": str(amount),
                         "output_asset_address": output,
                         "min_output_base_units": str(minimum),
                         "route_data_hex": route_hex, "route_hash": route_hash,
                         "deadline_epoch_seconds": deadline,
                         "child_commitment_hash": child_hash,
                         "registry_binding": binding,
                         "order_digest": order_digest})
    orders = sorted(prepared, key=lambda item: item["registry_binding"]["binding_hash"])
    orders_hash, batch_digest = compute_vault_batch_digest(
        vault_address=vault_address, registry_address=registry_address,
        chain_id=chain_id, parent_basket_hash=commitment["basket_hash"],
        order_digests=[item["order_digest"] for item in orders],
        batch_deadline_epoch_seconds=batch_deadline_epoch_seconds)
    result = {"schema_version": VERSION, "parent_commitment_hash": commitment["commitment_hash"],
              "parent_basket_hash": commitment["basket_hash"],
              "authenticated_source_hash": source_hash,
              "prepared_at": utc(prepared_at), "context_hash": digest(context),
              "chain_id": chain_id, "vault_address": vault_address,
              "registry_address": registry_address,
              "input_asset_address": input_asset,
              "batch_deadline_epoch_seconds": batch_deadline_epoch_seconds,
              "orders": orders, "orders_hash": orders_hash,
              "batch_digest": batch_digest,
              "registry_status": "NOT_QUERIED", "vault_status": "NOT_QUERIED",
              "signature_status": "NOT_SIGNED", "transaction_status": "NOT_BUILT",
              "execution_authority": "NONE"}
    result["plan_hash"] = digest(result)
    return result


def verify_vault_batch(plan: dict, commitment: dict, template: dict,
                       signed_bundle: dict, trust_roots: dict, state: dict,
                       policy: dict, context: dict, leg_specs: list[dict], *,
                       prepared_at: str, batch_deadline_epoch_seconds: int) -> bool:
    if not isinstance(plan, dict):
        return False
    try:
        return plan == prepare_vault_batch(
            commitment, template, signed_bundle, trust_roots, state, policy,
            context, leg_specs, prepared_at=prepared_at,
            batch_deadline_epoch_seconds=batch_deadline_epoch_seconds)
    except (MachineError, KeyError, TypeError, ValueError, ZeroDivisionError):
        return False
