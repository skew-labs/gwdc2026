"""One-time TRON Sign-In challenges and short-lived service sessions."""

import hashlib
import re
import secrets
from datetime import datetime, timedelta

from economic_machine.tron_crypto import keccak256, recover_tron_address
from economic_machine.tron_sources import address_base58, address_hex
from economic_machine.values import MachineError, digest, utc


NETWORKS = {"tron-mainnet": "Mainnet", "tron-nile": "Nile", "tron-shasta": "Shasta"}


def _scope(address, network):
    wallet = address_hex(address)
    if network not in NETWORKS:
        raise MachineError("unsupported TRON sign-in network")
    return {"tenant_id": "gwdc-live", "owner_id": "tron-" + wallet[2:18],
            "wallet": wallet, "network": network}


def message_hash(message: str) -> bytes:
    if not isinstance(message, str) or not 1 <= len(message) <= 4096:
        raise MachineError("invalid TRON sign-in message")
    raw = message.encode("utf-8")
    return keccak256(b"\x19TRON Signed Message:\n" + str(len(raw)).encode("ascii") + raw)


class WalletAuthService:
    def __init__(self, repository, session_verifier, clock, *, domain="whollet.gwdc"):
        self.repository = repository
        self.sessions = session_verifier
        self.clock = clock
        if not isinstance(domain, str) or re.fullmatch(r"[a-z0-9.-]{1,120}", domain) is None:
            raise MachineError("invalid wallet authentication domain")
        self.domain = domain

    def challenge(self, address: str, network: str) -> dict:
        now = datetime.fromisoformat(utc(self.clock()))
        scope = _scope(address, network)
        nonce = "n" + secrets.token_hex(16)
        expires = now + timedelta(minutes=5)
        message = ("WHOLLET sign-in\n"
            f"Domain: {self.domain}\n"
            f"Wallet: {address_base58(scope['wallet'])}\n"
            f"Network: {NETWORKS[network]}\n"
            f"Nonce: {nonce}\n"
            f"Issued At: {now.isoformat()}\n"
            f"Expiration Time: {expires.isoformat()}\n"
            "Purpose: authenticate only; no transaction or asset authority")
        body = {"schema_version": "tron-wallet-challenge-1", "nonce": nonce,
            "scope": scope, "message": message, "issued_at": now.isoformat(),
            "expires_at": expires.isoformat(), "status": "PENDING",
            "signature_hash": None}
        self.repository.put_record(scope, "WALLET_CHALLENGE", nonce, body,
            expected_version=0, at=now.isoformat())
        return {"schema_version": body["schema_version"], "nonce": nonce,
            "message": message, "address": address_base58(scope["wallet"]),
            "wallet_hex": scope["wallet"], "network": network,
            "expires_at": expires.isoformat(), "execution_authority": "NONE"}

    def verify(self, address: str, network: str, nonce: str, signature: str) -> dict:
        scope = _scope(address, network)
        if not isinstance(nonce, str) or re.fullmatch(r"n[0-9a-f]{32}", nonce) is None:
            raise MachineError("invalid or consumed wallet challenge")
        now = datetime.fromisoformat(utc(self.clock()))
        record = self.repository.get_record(scope, "WALLET_CHALLENGE", nonce)
        body = record["body"]
        if body.get("status") != "PENDING":
            raise MachineError("invalid or consumed wallet challenge")
        consumed = {**body, "status": "CONSUMED", "signature_hash": None}
        self.repository.put_record(scope, "WALLET_CHALLENGE", nonce, consumed,
            expected_version=record["version"], at=now.isoformat())
        if now >= datetime.fromisoformat(body["expires_at"]):
            raise MachineError("wallet challenge expired")
        if not isinstance(signature, str) or re.fullmatch(r"(?:0x)?[0-9a-fA-F]{130}", signature) is None:
            raise MachineError("malformed TRON wallet signature")
        raw_signature = bytes.fromhex(signature.removeprefix("0x"))
        recovered = recover_tron_address(message_hash(body["message"]), raw_signature)
        if recovered != scope["wallet"]:
            raise MachineError("wallet signature does not match challenge address")
        signature_hash = hashlib.sha256(raw_signature).hexdigest()
        consumed["signature_hash"] = signature_hash
        self.repository.put_record(scope, "WALLET_CHALLENGE", nonce, consumed,
            expected_version=record["version"] + 1, at=now.isoformat())
        assertion = {"schema_version": "verified-wallet-assertion-1",
            "assertion_id": "assert-" + nonce, "scope": scope,
            "challenge_hash": digest(body["message"]), "signature_hash": signature_hash,
            "verified_at": now.isoformat(),
            "valid_until": min(now + timedelta(minutes=15),
                               datetime.fromisoformat(body["expires_at"]) + timedelta(minutes=15)).isoformat(),
            "verification_status": "VERIFIED", "verifier_id": "tron-sign-message-v2",
            "evidence_hash": digest({"nonce": nonce, "wallet": scope["wallet"],
                                     "network": network, "signature_hash": signature_hash}),
            "execution_authority": "NONE"}
        token = self.sessions.issue(assertion, session_id="session-" + secrets.token_hex(12),
            trace_id="trace-" + secrets.token_hex(12), ttl_seconds=900)
        return {"schema_version": "finance-wallet-session-result-1",
            "session_token": token, "expires_at": assertion["valid_until"],
            "wallet": scope["wallet"], "wallet_base58": address_base58(scope["wallet"]),
            "network": network, "execution_authority": "NONE"}
