"""Verify operator-pinned Ed25519 evidence before assembling a portfolio.

This checks signatures over exact canonical bytes, not the truth of prices,
positions or model assumptions. It never handles private keys or transactions.
The OpenSSL executable is an explicit, hash-pinned cold-path dependency.
"""

import hashlib
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from .product_adapter import (BUNDLE_VERSION, TEMPLATE_KEYS, TEMPLATE_VERSION,
                              assemble_portfolio_inputs)
from .values import MachineError, canonical, digest, ident, require_keys, utc


VERSION = "economic-signed-portfolio-assembly-1"
BUNDLE_VERSION_SIGNED = "economic-signed-product-bundle-1"
RECORD_VERSION = "economic-signed-evidence-1"
ROOTS_VERSION = "economic-evidence-trust-roots-1"
DOMAIN = "ECONOMIC_SIGNED_EVIDENCE_V1"
ROLES = ("MARKET", "MODEL", "POSITION")
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")
MAX_SIGNED_MESSAGE = 65536
# RFC 8032 section 7.1, test 2 (the one-byte message 0x72).
ED25519_TEST_PUBLIC = "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"
ED25519_TEST_SIGNATURE = (
    "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
    "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"
)


def _hex(value: object, size: int, label: str) -> str:
    if (not isinstance(value, str) or len(value) != size * 2
            or re.fullmatch(r"[0-9a-f]+", value) is None):
        raise MachineError(label + " must be lowercase fixed-width hex")
    return value


def _roots(raw: dict, template: dict) -> tuple[dict, dict, str]:
    require_keys(raw, {"schema_version", "openssl_path", "openssl_sha256",
                       "thresholds", "issuers", "manifest_hashes"}, "EvidenceTrustRoots")
    if raw["schema_version"] != ROOTS_VERSION:
        raise MachineError("unsupported trust root version")
    path_text = raw["openssl_path"]
    if not isinstance(path_text, str) or len(path_text) > 512:
        raise MachineError("OpenSSL executable path required")
    path = Path(path_text)
    if (not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK)
            or not 0 < path.stat().st_size <= 50000000):
        raise MachineError("pinned OpenSSL executable unavailable")
    expected_hash = _hex(raw["openssl_sha256"], 32, "OpenSSL executable hash")
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
        raise MachineError("OpenSSL executable hash mismatch")
    _openssl_self_test(str(path))
    thresholds = raw["thresholds"]
    require_keys(thresholds, set(ROLES), "EvidenceThresholds")
    for role in ROLES:
        minimum = 2 if role == "MARKET" else 1
        if type(thresholds[role]) is not int or not minimum <= thresholds[role] <= 8:
            raise MachineError("invalid evidence signature threshold")
    issuers = raw["issuers"]
    if not isinstance(issuers, list) or not 4 <= len(issuers) <= 32:
        raise MachineError("bounded issuer registry required")
    registry, keys_seen = {}, set()
    at = datetime.fromisoformat(utc(template["as_of"]))
    for issuer in issuers:
        require_keys(issuer, {"issuer_id", "role", "public_key_hex", "network",
                              "asset", "valid_from", "valid_until", "revoked"},
                     "EvidenceIssuer")
        name = ident(issuer["issuer_id"], "evidence issuer")
        if name in registry or issuer["role"] not in ROLES:
            raise MachineError("duplicate or invalid evidence issuer")
        key = _hex(issuer["public_key_hex"], 32, "Ed25519 public key")
        if key in keys_seen:
            raise MachineError("evidence quorum reuses a public key")
        keys_seen.add(key)
        if (issuer["network"] != template["network"]
                or issuer["asset"] != template["asset"]
                or type(issuer["revoked"]) is not bool):
            raise MachineError("evidence issuer scope invalid")
        start, end = (datetime.fromisoformat(utc(issuer[item]))
                      for item in ("valid_from", "valid_until"))
        if start >= end:
            raise MachineError("evidence issuer validity interval invalid")
        registry[name] = {**issuer, "active": not issuer["revoked"] and start <= at < end,
                          "start": start, "end": end}
    for role in ROLES:
        if sum(item["active"] and item["role"] == role for item in registry.values()) < thresholds[role]:
            raise MachineError("insufficient active evidence issuers")
    manifests = raw["manifest_hashes"]
    if not isinstance(manifests, dict) or not 1 <= len(manifests) <= 16:
        raise MachineError("pinned manifest hash map required")
    for product_id, hash_value in manifests.items():
        ident(product_id, "pinned product id")
        _hex(hash_value, 32, "pinned manifest hash")
    return registry, thresholds, str(path)


def _message(role: str, payload: dict, signed_at: str, issuer_id: str) -> bytes:
    message = canonical({"domain": DOMAIN, "role": role,
                         "signed_at": signed_at, "issuer_id": issuer_id,
                         "payload": payload})
    if len(message) > MAX_SIGNED_MESSAGE:
        raise MachineError("signed evidence message too large")
    return message


def _ed25519_verify(openssl_path: str, public_key_hex: str,
                    message: bytes, signature_hex: str) -> None:
    """Run the pinned OpenSSL binary without shell or private key access."""
    with tempfile.TemporaryDirectory(prefix="economic-evidence-") as directory:
        root = Path(directory)
        key_path, message_path, sig_path = (root / name for name in
                                            ("public.der", "message.bin", "signature.bin"))
        key_path.write_bytes(ED25519_SPKI_PREFIX + bytes.fromhex(public_key_hex))
        message_path.write_bytes(message)
        sig_path.write_bytes(bytes.fromhex(signature_hex))
        try:
            result = subprocess.run(
                [openssl_path, "pkeyutl", "-verify", "-rawin", "-pubin",
                 "-keyform", "DER", "-inkey", str(key_path), "-in", str(message_path),
                 "-sigfile", str(sig_path)],
                capture_output=True, timeout=8, check=False,
                env={"OPENSSL_CONF": "/dev/null"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MachineError("Ed25519 verifier unavailable") from exc
        if result.returncode != 0:
            raise MachineError("Ed25519 evidence signature invalid")


def _openssl_self_test(openssl_path: str) -> None:
    """Require the verifier to accept and reject the RFC 8032 known answer."""
    _ed25519_verify(openssl_path, ED25519_TEST_PUBLIC, b"\x72", ED25519_TEST_SIGNATURE)
    try:
        _ed25519_verify(openssl_path, ED25519_TEST_PUBLIC, b"\x73", ED25519_TEST_SIGNATURE)
    except MachineError as exc:
        if str(exc) != "Ed25519 evidence signature invalid":
            raise
    else:
        raise MachineError("OpenSSL Ed25519 negative self-test failed")


def _record(raw: dict, expected_role: str, registry: dict,
            thresholds: dict, openssl_path: str, as_of: str) -> tuple[dict, dict]:
    require_keys(raw, {"schema_version", "role", "payload", "signed_at",
                       "signatures"},
                 "SignedEvidence")
    if raw["schema_version"] != RECORD_VERSION or raw["role"] != expected_role:
        raise MachineError("signed evidence role or version mismatch")
    payload, signatures = raw["payload"], raw["signatures"]
    if not isinstance(payload, dict) or not isinstance(signatures, list) or not 1 <= len(signatures) <= 8:
        raise MachineError("bounded signed evidence required")
    signed_at = datetime.fromisoformat(utc(raw["signed_at"]))
    event_key = "generated_at" if expected_role == "MODEL" else "observed_at"
    event_time = datetime.fromisoformat(utc(payload.get(event_key)))
    if not event_time <= signed_at <= datetime.fromisoformat(utc(as_of)):
        raise MachineError("evidence signing time is outside decision window")
    seen = set()
    for signature in signatures:
        require_keys(signature, {"issuer_id", "signature_hex"}, "EvidenceSignature")
        name = ident(signature["issuer_id"], "evidence signer")
        if name in seen:
            raise MachineError("duplicate evidence signature issuer")
        seen.add(name)
        issuer = registry.get(name)
        if (issuer is None or not issuer["active"] or issuer["role"] != expected_role
                or not issuer["start"] <= signed_at < issuer["end"]):
            raise MachineError("evidence signer is not active for this role")
        sig = _hex(signature["signature_hex"], 64, "Ed25519 signature")
        message = _message(expected_role, payload, signed_at.isoformat(), name)
        _ed25519_verify(openssl_path, issuer["public_key_hex"], message, sig)
    if len(seen) < thresholds[expected_role]:
        raise MachineError("evidence signature threshold not met")
    return payload, {"role": expected_role, "payload_hash": digest(payload),
                     "signed_at": signed_at.isoformat(),
                     "signers": sorted(seen), "signature_count": len(seen)}


def assemble_signed_portfolio_inputs(template: dict, signed_bundle: dict,
                                     trust_roots: dict) -> dict:
    """Authenticate typed claims, then run the ordinary deterministic adapter."""
    require_keys(template, TEMPLATE_KEYS, "PortfolioTemplate")
    if template["schema_version"] != TEMPLATE_VERSION:
        raise MachineError("unsupported portfolio template version")
    require_keys(signed_bundle, {"schema_version", "manifests", "observations",
                                 "assumptions", "positions"}, "SignedProductBundle")
    if signed_bundle["schema_version"] != BUNDLE_VERSION_SIGNED:
        raise MachineError("unsupported signed product bundle")
    registry, thresholds, openssl_path = _roots(trust_roots, template)
    manifests = signed_bundle["manifests"]
    if not isinstance(manifests, list) or not 1 <= len(manifests) <= 16:
        raise MachineError("bounded pinned product manifests required")
    seen = set()
    for manifest in manifests:
        if not isinstance(manifest, dict):
            raise MachineError("product manifest malformed")
        name = ident(manifest.get("product_id"), "product id")
        if name in seen or trust_roots["manifest_hashes"].get(name) != digest(manifest):
            raise MachineError("product manifest does not match pinned hash")
        seen.add(name)
    if seen != set(trust_roots["manifest_hashes"]):
        raise MachineError("pinned product manifest set mismatch")
    claims = {"schema_version": BUNDLE_VERSION, "manifests": manifests}
    attestation = []
    for source, target, role in (("observations", "observations", "MARKET"),
                                 ("assumptions", "assumptions", "MODEL")):
        records = signed_bundle[source]
        if not isinstance(records, list) or len(records) != len(manifests):
            raise MachineError("signed product evidence set incomplete")
        claims[target] = []
        for record in records:
            payload, proof = _record(record, role, registry, thresholds,
                                     openssl_path, template["as_of"])
            claims[target].append(payload)
            attestation.append(proof)
    positions, proof = _record(signed_bundle["positions"], "POSITION", registry,
                               thresholds, openssl_path, template["as_of"])
    claims["positions"] = positions
    attestation.append(proof)
    assembly = assemble_portfolio_inputs(template, claims)
    result = {"schema_version": VERSION, "status": "SIGNED_CLAIMS_ASSEMBLED",
              "source_trust": "PINNED_SIGNATURES_NOT_ECONOMIC_TRUTH",
              "execution_authority": "NONE", "chain_status": "NOT_SUBMITTED",
              "trust_root_hash": digest(trust_roots),
              "signed_bundle_hash": digest(signed_bundle),
              "attestations": attestation, "assembly": assembly}
    result["signed_assembly_hash"] = digest(result)
    return result


def verify_signed_portfolio_inputs(result: dict, template: dict, signed_bundle: dict,
                                   trust_roots: dict) -> bool:
    if not isinstance(result, dict):
        return False
    try:
        return result == assemble_signed_portfolio_inputs(template, signed_bundle, trust_roots)
    except (MachineError, KeyError, TypeError, ValueError, ZeroDivisionError):
        return False
