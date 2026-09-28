"""Context supplied by a trusted authentication adapter, never by request JSON.

Constructing this object does not perform wallet-signature verification. A
production session verifier is a separate required adapter (PR 09).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from economic_machine.mandate import normalize_scope
from economic_machine.values import MachineError, ident, utc


@dataclass(frozen=True)
class AuthenticatedContext:
    tenant_id: str
    owner_id: str
    wallet: str
    network: str
    session_id: str
    issued_at: str
    expires_at: str
    trace_id: str

    @property
    def scope(self) -> dict:
        return normalize_scope({key: getattr(self, key) for key in
                                ("tenant_id", "owner_id", "wallet", "network")})

    def authorize(self, at: str, claimed_scope: dict | None = None) -> dict:
        scope = self.scope
        ident(self.session_id, "session id")
        ident(self.trace_id, "trace id")
        if not datetime.fromisoformat(utc(self.issued_at)) <= datetime.fromisoformat(utc(at)) < datetime.fromisoformat(utc(self.expires_at)):
            raise MachineError("authenticated session not effective or expired")
        if claimed_scope is not None and normalize_scope(claimed_scope) != scope:
            raise MachineError("request scope differs from authenticated context")
        return scope


class SessionVerifier(Protocol):
    def authenticate(self, credential: str) -> AuthenticatedContext:
        """Validate server-side session/ownership; do not trust owner in body."""
        ...
