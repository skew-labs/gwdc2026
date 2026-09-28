"""Explicit local verification entrypoint; production PostgreSQL wiring is separate."""

import base64
import os
from datetime import UTC, datetime
from pathlib import Path

from .api import create_app
from .auth import HmacSessionVerifier
from .operational_repository import OperationalRepository


def _required(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(name + " is required")
    return value


def build_app():
    path = Path(_required("FINANCE_SERVICE_REFERENCE_SQLITE_PATH"))
    key_id = _required("FINANCE_SERVICE_SESSION_KEY_ID")
    try:
        secret = base64.b64decode(_required("FINANCE_SERVICE_SESSION_HMAC_B64"),
                                  validate=True)
    except ValueError as exc:
        raise RuntimeError("invalid session HMAC base64") from exc
    repository = OperationalRepository(path)
    verifier = HmacSessionVerifier({key_id: secret}, active_key_id=key_id,
        clock=lambda: datetime.now(UTC).isoformat())
    return create_app(session_verifier=verifier, repository=repository,
        clock=lambda: datetime.now(UTC).isoformat())


app = build_app()
