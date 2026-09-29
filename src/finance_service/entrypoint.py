"""Hosted service entrypoint with PostgreSQL and SQLite reference profiles."""

import base64
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from .api import create_app
from .auth import HmacSessionVerifier
from .agent_intent import AgentIntentService
from .model_provider import (ModelProviderError, OpenAICompatibleQwenProvider,
                             QwenProviderConfig, UnavailableQwenProvider)
from .model_usage import OperationalModelUsageStore
from .operational_repository import OperationalRepository
from .product_workspace import ProductWorkspaceService
from .tron_proof import TronProofService
from .wallet_auth import WalletAuthService


def _required(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(name + " is required")
    return value


def build_app():
    key_id = _required("FINANCE_SERVICE_SESSION_KEY_ID")
    session_direct = os.environ.get("FINANCE_SERVICE_SESSION_HMAC_B64")
    session_file = os.environ.get("FINANCE_SERVICE_SESSION_HMAC_FILE")
    if bool(session_direct) == bool(session_file):
        raise RuntimeError("exactly one session HMAC source is required")
    if session_file:
        from .postgres_runtime import read_owner_file

        session_direct, _ = read_owner_file(
            session_file, label="session HMAC", max_size=4096)
        session_direct = session_direct.strip()
    try:
        secret = base64.b64decode(session_direct, validate=True)
    except ValueError as exc:
        raise RuntimeError("invalid session HMAC base64") from exc
    if len(secret) < 32:
        raise RuntimeError("session HMAC must decode to at least 32 bytes")
    postgres_api_dsn = os.environ.get("FINANCE_SERVICE_POSTGRES_API_DSN")
    postgres_api_dsn_file = os.environ.get("FINANCE_SERVICE_POSTGRES_API_DSN_FILE")
    postgres_worker_dsn = os.environ.get("FINANCE_SERVICE_POSTGRES_WORKER_DSN")
    postgres_worker_dsn_file = os.environ.get("FINANCE_SERVICE_POSTGRES_WORKER_DSN_FILE")
    if postgres_worker_dsn or postgres_worker_dsn_file:
        raise RuntimeError("API service must not load the PostgreSQL worker DSN")
    if postgres_api_dsn and postgres_api_dsn_file:
        raise RuntimeError("API service received multiple PostgreSQL DSN sources")
    if postgres_api_dsn or postgres_api_dsn_file:
        from .postgres_repository import PostgresOperationalRepository

        local_test = os.environ.get(
            "FINANCE_SERVICE_POSTGRES_ALLOW_INSECURE_LOCALHOST") == "1"
        if postgres_api_dsn and not local_test:
            raise RuntimeError("hosted API PostgreSQL DSN must use an owner-only file")
        repository = PostgresOperationalRepository(postgres_api_dsn,
            api_dsn_file=postgres_api_dsn_file,
            allow_insecure_localhost=local_test)
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
    try:
        provider = OpenAICompatibleQwenProvider(QwenProviderConfig.from_environment())
    except ModelProviderError as exc:
        provider = UnavailableQwenProvider(exc)
    usage_store = OperationalModelUsageStore(repository)
    clock = lambda: datetime.now(UTC).isoformat()
    agent_intent_service = AgentIntentService(provider, usage_store, clock)
    wallet_auth_service = WalletAuthService(repository, verifier, clock,
        domain=os.environ.get("FINANCE_SERVICE_PUBLIC_DOMAIN", "whollet.gwdc"))
    tron_proof_service = TronProofService(repository, clock)
    app = create_app(session_verifier=verifier, repository=repository,
        clock=clock, product_service=product_service, web_root=web_root,
        demo_story=demo_story, agent_intent_service=agent_intent_service,
        wallet_auth_service=wallet_auth_service, tron_proof_service=tron_proof_service)
    gateway_file = os.environ.get("MACHINE_GATEWAY_SECRET_FILE")
    if gateway_file:
        from .postgres_runtime import read_owner_file
        from .machine_bridge import MachineBridge, router_for
        from .machine_observations import MachineObservations
        gateway_secret, _ = read_owner_file(gateway_file, label="Machine gateway secret")
        app.include_router(router_for(MachineBridge(repository, clock,
            agent_intent_service, MachineObservations(clock)), gateway_secret.strip()))
    return app


app = build_app()
