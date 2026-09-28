"""Bind signed product claims to a non-executable basket commitment.

The signed evidence must be replayed, and its validity must cover the whole
plan window. A signature attests a claim, not the truth of market or position
state. This module never signs, submits, or settles a transaction.
"""

from datetime import datetime

from .basket import commit_basket
from .signed_evidence import assemble_signed_portfolio_inputs
from .values import MachineError, digest, utc


VERSION = "economic-basket-commitment-2"
SOURCE_STATUS = "PINNED_SIGNATURES_NOT_ECONOMIC_TRUTH"


def _valid_through(signed_bundle: dict, template: dict, until: str) -> None:
    expiry = datetime.fromisoformat(utc(until))
    age_ms = template["max_age_ms"]
    for key in ("observations", "assumptions"):
        for record in signed_bundle[key]:
            source = record["payload"]
            source_end = datetime.fromisoformat(utc(source["valid_until"]))
            observed = datetime.fromisoformat(utc(
                source["observed_at" if key == "observations" else "generated_at"]))
            age = expiry - observed
            age_us = (age.days * 86400 + age.seconds) * 1000000 + age.microseconds
            if expiry > source_end or not 0 < age_us <= age_ms * 1000:
                raise MachineError("signed product input expires before basket plan")
    positions = signed_bundle["positions"]["payload"]
    observed = datetime.fromisoformat(utc(positions["observed_at"]))
    age = expiry - observed
    age_us = (age.days * 86400 + age.seconds) * 1000000 + age.microseconds
    if not 0 < age_us <= age_ms * 1000:
        raise MachineError("signed position snapshot expires before basket plan")


def commit_authenticated_basket(template: dict, signed_bundle: dict, trust_roots: dict,
                                state: dict, policy: dict, *, valid_until: str) -> dict:
    """Replay signatures, portfolio choice and state/policy before hashing."""
    signed = assemble_signed_portfolio_inputs(template, signed_bundle, trust_roots)
    _valid_through(signed_bundle, template, valid_until)
    assembly = signed["assembly"]
    base = commit_basket(assembly["selection_request"], assembly["portfolio_verdict"],
                         state, policy, valid_until=valid_until)
    result = {key: value for key, value in base.items() if key != "commitment_hash"}
    result["schema_version"] = VERSION
    result["authenticated_source"] = {
        "status": SOURCE_STATUS,
        "signed_assembly_hash": signed["signed_assembly_hash"],
        "signed_bundle_hash": signed["signed_bundle_hash"],
        "trust_root_hash": signed["trust_root_hash"],
        "attestation_hash": digest(signed["attestations"]),
        "unsigned_assembly_hash": assembly["assembly_hash"],
    }
    result["commitment_hash"] = digest({"domain": VERSION, "payload": result})
    return result


def verify_authenticated_basket(commitment: dict, template: dict, signed_bundle: dict,
                                trust_roots: dict, state: dict, policy: dict) -> bool:
    if not isinstance(commitment, dict) or commitment.get("schema_version") != VERSION:
        return False
    try:
        return commitment == commit_authenticated_basket(
            template, signed_bundle, trust_roots, state, policy,
            valid_until=commitment["valid_until"])
    except (MachineError, KeyError, TypeError, ValueError, ZeroDivisionError):
        return False
