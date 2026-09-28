"""Read-only registration preparation and coherent batch observation.

An attestation message is a request to independent verifiers, not a
signature, registration, vault-balance check, or execution permit.
"""

from .chain_binding import compute_attestation_digest
from .tron_registry_read import assess_registry_observation, read_registry_observation
from .values import MachineError, digest
from .vault_batch import verify_vault_batch


MESSAGE_VERSION = "economic-vault-batch-attestations-1"
OBSERVATIONS_VERSION = "economic-vault-batch-registry-observations-1"
ASSESSMENT_VERSION = "economic-vault-batch-registry-assessment-1"
MAX_OBSERVATION_SPAN_MS = 60000


def _require_replayed_plan(plan: dict, commitment: dict, template: dict,
                           signed_bundle: dict, trust_roots: dict, state: dict,
                           policy: dict, context: dict, leg_specs: list[dict], *,
                           prepared_at: str, batch_deadline_epoch_seconds: int) -> None:
    if not verify_vault_batch(
            plan, commitment, template, signed_bundle, trust_roots, state,
            policy, context, leg_specs, prepared_at=prepared_at,
            batch_deadline_epoch_seconds=batch_deadline_epoch_seconds):
        raise MachineError("signed vault batch plan does not replay")


def prepare_batch_attestations(
        plan: dict, commitment: dict, template: dict, signed_bundle: dict,
        trust_roots: dict, state: dict, policy: dict, context: dict,
        leg_specs: list[dict], *, prepared_at: str,
        batch_deadline_epoch_seconds: int, attestor_epoch: int) -> dict:
    """Return exact child registration fields and raw hashes, never signatures."""
    _require_replayed_plan(
        plan, commitment, template, signed_bundle, trust_roots, state,
        policy, context, leg_specs, prepared_at=prepared_at,
        batch_deadline_epoch_seconds=batch_deadline_epoch_seconds)
    items = []
    for order in plan["orders"]:
        binding = order["registry_binding"]
        message_hash = compute_attestation_digest(binding, attestor_epoch)
        items.append({
            "product_id": order["product_id"],
            "parent_commitment_hash": plan["parent_commitment_hash"],
            "child_commitment_hash": order["child_commitment_hash"],
            "binding_hash": binding["binding_hash"],
            "policy_id": binding["policy_id"],
            "state_root": binding["state_root"],
            "basket_hash": binding["basket_hash"],
            "amount_base_units": binding["amount_base_units"],
            "valid_until_epoch_seconds": binding["valid_until_epoch_seconds"],
            "authenticated_source_hash": binding["authenticated_source_hash"],
            "signed_assembly_hash": binding["signed_assembly_hash"],
            "message_hash": message_hash,
            "signature_status": "NOT_COLLECTED",
            "registration_status": "NOT_SUBMITTED",
        })
    result = {"schema_version": MESSAGE_VERSION, "plan_hash": plan["plan_hash"],
              "registry_address": plan["registry_address"],
              "chain_id": plan["chain_id"], "attestor_epoch": attestor_epoch,
              "epoch_source": "CALLER_CLAIM_NOT_VERIFIED",
              "items": items, "status": "PREPARED_NOT_SIGNED",
              "execution_authority": "NONE"}
    result["preparation_hash"] = digest(result)
    return result


def observe_batch_registry(
        plan: dict, commitment: dict, template: dict, signed_bundle: dict,
        trust_roots: dict, state: dict, policy: dict, context: dict,
        leg_specs: list[dict], reader_config: dict, *, prepared_at: str,
        batch_deadline_epoch_seconds: int, transport=None) -> dict:
    """Read every child from a customer node after replaying the whole plan."""
    _require_replayed_plan(
        plan, commitment, template, signed_bundle, trust_roots, state,
        policy, context, leg_specs, prepared_at=prepared_at,
        batch_deadline_epoch_seconds=batch_deadline_epoch_seconds)
    items = []
    for order in plan["orders"]:
        binding = order["registry_binding"]
        observation = read_registry_observation(
            binding, reader_config, transport=transport)
        items.append({"binding_hash": binding["binding_hash"],
                      "observation": observation})
    result = {"schema_version": OBSERVATIONS_VERSION,
              "plan_hash": plan["plan_hash"], "items": items,
              "source_trust": "NODE_RESPONSE_ONLY",
              "execution_authority": "NONE"}
    result["observation_set_hash"] = digest(result)
    return result


def assess_batch_registry(
        observations: dict, plan: dict, commitment: dict, template: dict,
        signed_bundle: dict, trust_roots: dict, state: dict, policy: dict,
        context: dict, leg_specs: list[dict], *, prepared_at: str,
        batch_deadline_epoch_seconds: int) -> dict:
    """Withhold mixed-height or mismatched child observations as a group."""
    _require_replayed_plan(
        plan, commitment, template, signed_bundle, trust_roots, state,
        policy, context, leg_specs, prepared_at=prepared_at,
        batch_deadline_epoch_seconds=batch_deadline_epoch_seconds)
    if (not isinstance(observations, dict) or
            set(observations) != {"schema_version", "plan_hash", "items",
                                  "source_trust", "execution_authority",
                                  "observation_set_hash"} or
            observations["schema_version"] != OBSERVATIONS_VERSION or
            observations["plan_hash"] != plan["plan_hash"] or
            observations["source_trust"] != "NODE_RESPONSE_ONLY" or
            observations["execution_authority"] != "NONE" or
            not isinstance(observations["items"], list) or
            len(observations["items"]) != len(plan["orders"])):
        raise MachineError("invalid vault batch observation set")
    recorded = dict(observations)
    claimed_hash = recorded.pop("observation_set_hash")
    if claimed_hash != digest(recorded):
        raise MachineError("vault batch observation set hash mismatch")
    assessments = []
    snapshots = []
    for order, item in zip(plan["orders"], observations["items"]):
        if (not isinstance(item, dict) or set(item) != {"binding_hash", "observation"}
                or item["binding_hash"] != order["registry_binding"]["binding_hash"]):
            raise MachineError("vault batch child binding mismatch")
        observation = item["observation"]
        assessment = assess_registry_observation(
            order["registry_binding"], observation)
        assessments.append(assessment)
        snapshots.append(observation)
    first = snapshots[0]
    identity = ("block_id", "number", "timestamp_ms", "runtime_sha256",
                "expected_runtime_sha256", "endpoint_fingerprint",
                "current_attestor_epoch", "paused")
    reasons = []
    if any(any(item.get(key) != first.get(key) for key in identity)
           for item in snapshots[1:]):
        reasons.append("MIXED_REGISTRY_SNAPSHOT")
    if any(item["asset_budget"] != first["asset_budget"] for item in snapshots[1:]):
        reasons.append("INCONSISTENT_ASSET_BUDGET")
    by_policy = {}
    amounts_by_policy = {}
    for order, observation in zip(plan["orders"], snapshots):
        policy_id = order["registry_binding"]["policy_id"]
        if policy_id in by_policy and observation["policy"] != by_policy[policy_id]:
            reasons.append("INCONSISTENT_POLICY_STATE")
        by_policy[policy_id] = observation["policy"]
        amounts_by_policy[policy_id] = (amounts_by_policy.get(policy_id, 0)
                                        + int(order["amount_base_units"]))
    total_input = sum(int(order["amount_base_units"]) for order in plan["orders"])
    if int(first["asset_budget"]["reserved"]) < total_input:
        reasons.append("ASSET_BATCH_NOT_RESERVED")
    if any(int(by_policy[policy_id]["reserved_amount"]) < amount
           for policy_id, amount in amounts_by_policy.items()):
        reasons.append("POLICY_BATCH_NOT_RESERVED")
    seen_at = [item.get("observed_at_ms") for item in snapshots]
    if (any(type(value) is not int or value < 0 for value in seen_at)
            or max(seen_at) - min(seen_at) > MAX_OBSERVATION_SPAN_MS):
        reasons.append("OBSERVATION_SPAN_EXCEEDED")
    if any(value // 1000 >= plan["batch_deadline_epoch_seconds"]
           for value in seen_at if type(value) is int):
        reasons.append("BATCH_DEADLINE_PASSED")
    if any(item["status"] != "OBSERVED_MATCH" for item in assessments):
        reasons.append("CHILD_REGISTRY_MISMATCH")
    result = {"schema_version": ASSESSMENT_VERSION,
              "plan_hash": plan["plan_hash"],
              "observation_set_hash": claimed_hash,
              "status": "OBSERVED_MATCH" if not reasons else "WITHHELD",
              "reason_codes": reasons,
              "child_assessments": assessments,
              "source_trust": "NODE_RESPONSE_ONLY",
              "execution_authority": "NONE"}
    result["assessment_hash"] = digest(result)
    return result
