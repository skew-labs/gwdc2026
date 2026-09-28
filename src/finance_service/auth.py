"""Server-signed wallet sessions for the hosted finance service.

This module consumes a wallet assertion already verified by an authentication
adapter.  It never accepts a private key, mnemonic, RPC credential, or a scope
claimed by an API body.  Tokens are compact HMAC envelopes so a restarted API
can authenticate them without retaining signing material from the wallet.
"""

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timedelta

from economic_machine.mandate import normalize_scope
from economic_machine.values import (
    MachineError,
    canonical,
    digest,
    ident,
    require_keys,
    utc,
)

from .context import AuthenticatedContext, SessionVerifier

ASSERTION_VERSION = "verified-wallet-assertion-1"
TOKEN_VERSION = "finance-service-session-1"
_HASH = re.compile(r"[0-9a-f]{64}")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 8192 or re.fullmatch(
            r"[A-Za-z0-9_-]+", value) is None:
        raise MachineError("invalid session token encoding")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise MachineError("invalid session token encoding") from exc


def normalize_wallet_assertion(raw: dict, *, at: str) -> dict:
    require_keys(raw, {"schema_version", "assertion_id", "scope", "challenge_hash",
        "signature_hash", "verified_at", "valid_until", "verification_status",
        "verifier_id", "evidence_hash", "execution_authority"},
        "VerifiedWalletAssertionV1")
    if (raw["schema_version"] != ASSERTION_VERSION
            or raw["verification_status"] != "VERIFIED"
            or raw["execution_authority"] != "NONE"):
        raise MachineError("verified non-executing wallet assertion required")
    assertion_id = ident(raw["assertion_id"], "wallet assertion")
    verifier_id = ident(raw["verifier_id"], "wallet assertion verifier")
    for key in ("challenge_hash", "signature_hash", "evidence_hash"):
        if not isinstance(raw[key], str) or _HASH.fullmatch(raw[key]) is None:
            raise MachineError("invalid wallet assertion commitment")
    verified = datetime.fromisoformat(utc(raw["verified_at"]))
    expires = datetime.fromisoformat(utc(raw["valid_until"]))
    moment = datetime.fromisoformat(utc(at))
    if not verified <= moment < expires:
        raise MachineError("wallet assertion is not currently valid")
    return {**deepcopy(raw), "assertion_id": assertion_id,
        "scope": normalize_scope(raw["scope"]), "verifier_id": verifier_id,
        "verified_at": verified.isoformat(), "valid_until": expires.isoformat()}


class HmacSessionVerifier(SessionVerifier):
    """Issue and verify service sessions after external wallet verification."""

    def __init__(self, keys: dict[str, bytes], *, active_key_id: str,
                 clock: Callable[[], str], is_session_active: Callable[[str, dict], bool] | None = None,
                 max_session_seconds: int = 3600):
        if (not isinstance(keys, dict) or not keys or active_key_id not in keys
                or not 60 <= max_session_seconds <= 86400):
            raise MachineError("invalid session verifier configuration")
        normalized = {}
        for key_id, secret in keys.items():
            key_id = ident(key_id, "session key id")
            if not isinstance(secret, bytes) or len(secret) < 32:
                raise MachineError("session HMAC key must contain at least 32 bytes")
            normalized[key_id] = bytes(secret)
        self._keys = normalized
        self._active_key_id = active_key_id
        self._clock = clock
        self._is_session_active = is_session_active or (lambda _session_id, _scope: True)
        self._max_session_seconds = max_session_seconds

    def issue(self, assertion: dict, *, session_id: str, trace_id: str,
              ttl_seconds: int) -> str:
        now = utc(self._clock())
        assertion = normalize_wallet_assertion(assertion, at=now)
        if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= self._max_session_seconds:
            raise MachineError("session TTL is outside service policy")
        session_id, trace_id = ident(session_id, "session id"), ident(trace_id, "trace id")
        issued = datetime.fromisoformat(now)
        expiry = min(issued + timedelta(seconds=ttl_seconds),
                     datetime.fromisoformat(assertion["valid_until"]))
        payload = {"schema_version": TOKEN_VERSION, "key_id": self._active_key_id,
            "scope": assertion["scope"], "session_id": session_id, "trace_id": trace_id,
            "issued_at": issued.isoformat(), "expires_at": expiry.isoformat(),
            "assertion_hash": digest(assertion),
            "auth_context_hash": digest({"assertion_id": assertion["assertion_id"],
                "challenge_hash": assertion["challenge_hash"],
                "signature_hash": assertion["signature_hash"],
                "verifier_id": assertion["verifier_id"]})}
        encoded = _b64encode(canonical(payload))
        signature = hmac.new(self._keys[self._active_key_id], encoded.encode("ascii"),
                             hashlib.sha256).digest()
        return encoded + "." + _b64encode(signature)

    def authenticate(self, credential: str) -> AuthenticatedContext:
        if not isinstance(credential, str) or credential.count(".") != 1:
            raise MachineError("Bearer session token required")
        encoded, supplied = credential.split(".")
        try:
            payload = json.loads(_b64decode(encoded))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MachineError("invalid session token payload") from exc
        require_keys(payload, {"schema_version", "key_id", "scope", "session_id",
            "trace_id", "issued_at", "expires_at", "assertion_hash",
            "auth_context_hash"}, "FinanceServiceSessionV1")
        if payload["schema_version"] != TOKEN_VERSION:
            raise MachineError("unsupported service session version")
        key_id = ident(payload["key_id"], "session key id")
        key = self._keys.get(key_id)
        if key is None:
            raise MachineError("unknown session signing key")
        expected = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).digest()
        supplied_bytes = _b64decode(supplied)
        if len(supplied_bytes) != 32 or not hmac.compare_digest(supplied_bytes, expected):
            raise MachineError("session token signature mismatch")
        for key_name in ("assertion_hash", "auth_context_hash"):
            if not isinstance(payload[key_name], str) or _HASH.fullmatch(
                    payload[key_name]) is None:
                raise MachineError("invalid session token commitment")
        scope = normalize_scope(payload["scope"])
        context = AuthenticatedContext(**scope,
            session_id=ident(payload["session_id"], "session id"),
            trace_id=ident(payload["trace_id"], "trace id"),
            issued_at=utc(payload["issued_at"]), expires_at=utc(payload["expires_at"]))
        context.authorize(utc(self._clock()))
        if not self._is_session_active(context.session_id, scope):
            raise MachineError("service session is revoked or unknown")
        return context


def bearer_token(header: str) -> str:
    if not isinstance(header, str) or not header.startswith("Bearer "):
        raise MachineError("Bearer session token required")
    token = header[7:]
    if not token or any(char.isspace() for char in token):
        raise MachineError("Bearer session token required")
    return token
