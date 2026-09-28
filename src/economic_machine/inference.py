"""Untrusted inference boundary for the deterministic Economic Machine.

A model may suggest a typed EconomicProgram or report ambiguity. It cannot
register a program, write an EconomicState, reserve capital, or authorize a
transaction. The caller-provided scope is a local review boundary, not a
signature or proof of what the user said.
"""

import json
import re
from datetime import datetime

from .compiler import _check_sandbox, compile_program
from .state import normalize_state, state_root
from .values import MachineError, canonical, decimal, digest, ident, require_keys, utc


SCOPE_VERSION = "economic-inference-scope-1"
DRAFT_VERSION = "economic-inference-draft-1"
ASSESSMENT_VERSION = "economic-inference-assessment-1"
MAX_SCOPE_BYTES = 8192
MAX_DRAFT_BYTES = 65536


def _hex_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise MachineError(label + " must be a SHA-256 hex digest")
    return value


def _bounded_json(value: dict, maximum: int, label: str) -> dict:
    encoded = canonical(value)
    if len(encoded) > maximum:
        raise MachineError(label + " exceeds its size limit")
    return json.loads(encoded)


def normalize_inference_scope(value: dict) -> dict:
    """Validate a local review scope without treating it as user authorization."""
    require_keys(value, {"schema_version", "request_hash", "state_root", "program_id",
                         "owner_id", "agent_id", "network", "sandbox",
                         "allowed_fact_paths"}, "InferenceScope")
    if value["schema_version"] != SCOPE_VERSION:
        raise MachineError("unsupported inference scope version")
    for key in ("request_hash", "state_root"):
        _hex_digest(value[key], key)
    for key in ("program_id", "owner_id", "agent_id", "network"):
        ident(value[key], key)
    paths = value["allowed_fact_paths"]
    if (not isinstance(paths, list) or not 1 <= len(paths) <= 64
            or any(not isinstance(path, str) for path in paths)):
        raise MachineError("allowed fact paths must be a bounded list")
    for path in paths:
        ident(path, "allowed fact path")
    if len(set(paths)) != len(paths):
        raise MachineError("duplicate allowed fact path")
    _check_sandbox(value["sandbox"])
    return _bounded_json(value, MAX_SCOPE_BYTES, "inference scope")


def normalize_inference_draft(value: dict) -> dict:
    """Validate the transport envelope, including unused model output."""
    require_keys(value, {"schema_version", "model_id", "model_hash", "request_hash",
                         "state_root", "created_at", "expires_at", "confidence",
                         "ambiguities", "program"}, "InferenceDraft")
    if value["schema_version"] != DRAFT_VERSION:
        raise MachineError("unsupported inference draft version")
    ident(value["model_id"], "model id")
    for key in ("model_hash", "request_hash", "state_root"):
        _hex_digest(value[key], key)
    created_at = utc(value["created_at"])
    expires_at = utc(value["expires_at"])
    if datetime.fromisoformat(expires_at) <= datetime.fromisoformat(created_at):
        raise MachineError("inference expiry must follow creation")
    confidence = decimal(value["confidence"])
    if confidence > 1:
        raise MachineError("inference confidence must be between zero and one")
    ambiguities = value["ambiguities"]
    if (not isinstance(ambiguities, list) or len(ambiguities) > 16
            or any(not isinstance(item, str) for item in ambiguities)):
        raise MachineError("ambiguities must be a bounded list of reason codes")
    for item in ambiguities:
        ident(item, "ambiguity reason")
    if len(set(ambiguities)) != len(ambiguities):
        raise MachineError("duplicate ambiguity reason")
    if value["program"] is not None and not isinstance(value["program"], dict):
        raise MachineError("inferred program must be an object or null")
    return _bounded_json({**value, "created_at": created_at, "expires_at": expires_at},
                         MAX_DRAFT_BYTES, "inference draft")


def _within_scope(program: dict, scope: dict) -> bool:
    for key in ("program_id", "owner_id", "agent_id", "network"):
        if program[key] != scope[key]:
            return False
    proposed = program["sandbox"]
    allowed = scope["sandbox"]
    if proposed["asset"] != allowed["asset"]:
        return False
    if not set(proposed["allowed_protocols"]).issubset(allowed["allowed_protocols"]):
        return False
    if datetime.fromisoformat(proposed["expires_at"]) > datetime.fromisoformat(allowed["expires_at"]):
        return False
    for key in ("capital_limit", "max_exposure", "max_total_cost", "max_daily_loss"):
        if decimal(proposed[key]) > decimal(allowed[key]):
            return False
    for ins in program["instructions"]:
        if ins["op"] == "OBSERVE" and ins["path"] not in scope["allowed_fact_paths"]:
            return False
        if (ins["op"] == "PRICE"
                and not set(ins["paths"]).issubset(scope["allowed_fact_paths"])):
            return False
        if (ins["op"] in {"QUOTE", "ALLOCATE"}
                and decimal(ins["amount"]) > decimal(allowed["capital_limit"])):
            return False
    return True


def assess_inference(draft: dict, scope: dict, world: dict, *, at: str) -> dict:
    """Return a non-executable decision about an LLM-produced proposal.

    A valid proposal is REVIEW_REQUIRED, never approved. A missing answer is
    ABSTAIN. An invalid/out-of-scope proposal is REJECTED. No model call occurs.
    """
    scope = normalize_inference_scope(scope)
    draft = normalize_inference_draft(draft)
    state = normalize_state(world)
    at = utc(at)
    if scope["owner_id"] != state["owner_id"] or scope["network"] != state["network"]:
        raise MachineError("inference scope owner/network mismatch")
    if scope["state_root"] != state_root(state):
        raise MachineError("inference scope is for a different state")
    if datetime.fromisoformat(at) < datetime.fromisoformat(state["as_of"]):
        raise MachineError("assessment time precedes state")
    if datetime.fromisoformat(at) >= datetime.fromisoformat(scope["sandbox"]["expires_at"]):
        raise MachineError("inference scope expired")
    reasons: list[str] = []
    compiled_hash = None
    if (draft["request_hash"] != scope["request_hash"]
            or draft["state_root"] != scope["state_root"]):
        status, reasons = "REJECTED", ["CONTEXT_MISMATCH"]
    elif datetime.fromisoformat(draft["created_at"]) > datetime.fromisoformat(at):
        status, reasons = "REJECTED", ["FUTURE_INFERENCE"]
    elif datetime.fromisoformat(at) >= datetime.fromisoformat(draft["expires_at"]):
        status, reasons = "ABSTAIN", ["STALE_INFERENCE"]
    elif draft["ambiguities"]:
        status, reasons = "ABSTAIN", ["MISSING_INFORMATION", *draft["ambiguities"]]
    elif draft["program"] is None:
        status, reasons = "ABSTAIN", ["NO_PROGRAM_PROPOSED"]
    else:
        try:
            compiled = compile_program(draft["program"])
        except MachineError:
            status, reasons = "REJECTED", ["COMPILE_REJECTED"]
        else:
            if _within_scope(compiled, scope):
                status = "REVIEW_REQUIRED"
                compiled_hash = compiled["program_hash"]
            else:
                status, reasons = "REJECTED", ["OUTSIDE_SCOPE"]
    result = {"schema_version": ASSESSMENT_VERSION, "status": status,
              "reason_codes": reasons, "candidate_hash": digest(draft),
              "scope_hash": digest(scope), "state_root": state_root(state),
              "state_sequence": state["sequence"], "at": at,
              "program_hash": compiled_hash, "model_id": draft["model_id"],
              "model_hash": draft["model_hash"],
              "confidence": draft["confidence"],
              "registration_status": "NOT_REGISTERED",
              "execution_authority": "NONE", "chain_status": "NOT_SUBMITTED"}
    result["assessment_hash"] = digest(result)
    return result
