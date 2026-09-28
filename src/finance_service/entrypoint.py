"""Hosted service entrypoint with PostgreSQL and SQLite reference profiles."""

import base64
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from .api import create_app
from .auth import HmacSessionVerifier
from .operational_repository import OperationalRepository
from .product_workspace import ProductWorkspaceService


def _required(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(name + " is required")
    return value


def build_app():
    key_id = _required("FINANCE_SERVICE_SESSION_KEY_ID")
    try:
        secret = base64.b64decode(_required("FINANCE_SERVICE_SESSION_HMAC_B64"),
                                  validate=True)
    except ValueError as exc:
        raise RuntimeError("invalid session HMAC base64") from exc
    postgres_api_dsn = os.environ.get("FINANCE_SERVICE_POSTGRES_API_DSN")
    postgres_worker_dsn = os.environ.get("FINANCE_SERVICE_POSTGRES_WORKER_DSN")
    if postgres_worker_dsn:
        raise RuntimeError("API service must not load the PostgreSQL worker DSN")
    if postgres_api_dsn:
        from .postgres_repository import PostgresOperationalRepository

        repository = PostgresOperationalRepository(postgres_api_dsn)
    else:
        path = Path(_required("FINANCE_SERVICE_REFERENCE_SQLITE_PATH"))
        repository = OperationalRepository(path)
    verifier = HmacSessionVerifier({key_id: secret}, active_key_id=key_id,
        clock=lambda: datetime.now(UTC).isoformat())
    web_root = os.environ.get("FINANCE_SERVICE_WEB_ROOT")
    story_path = os.environ.get("FINANCE_SERVICE_DEMO_STORY_PATH")
    demo_story = None if not story_path else json.loads(Path(story_path).read_text())
    product_service = ProductWorkspaceService(
        repository, lambda: datetime.now(UTC).isoformat())
    return create_app(session_verifier=verifier, repository=repository,
        clock=lambda: datetime.now(UTC).isoformat(), product_service=product_service,
        web_root=web_root, demo_story=demo_story)


app = build_app()
