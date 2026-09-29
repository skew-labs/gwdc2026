#!/usr/bin/env python3
"""Exercise wallet auth and the configured Qwen route without exposing secrets."""

import argparse
import asyncio
import hashlib
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

from economic_machine.tron_crypto import keccak256
import economic_machine.tron_crypto as crypto
from economic_machine.tron_sources import address_base58
from economic_machine.values import canonical, digest
from finance_service.entrypoint import build_app
from finance_service.wallet_auth import message_hash


def _sign_hash(value, private_key):
    z = int.from_bytes(value, "big")
    nonce = int.from_bytes(hashlib.sha256(
        private_key.to_bytes(32, "big") + value).digest(), "big") % crypto._N or 1
    point = crypto._multiply(nonce, crypto._G)
    r = point[0] % crypto._N
    s = (crypto._inverse(nonce, crypto._N) * (z + r * private_key)) % crypto._N
    recovery = point[1] & 1
    if s > crypto._HALF_N:
        s, recovery = crypto._N - s, recovery ^ 1
    return r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([recovery])


def _wallet(private_key):
    public = crypto._multiply(private_key, crypto._G)
    return "41" + keccak256(public[0].to_bytes(32, "big")
        + public[1].to_bytes(32, "big"))[-20:].hex()


async def probe(message, output):
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("service dependencies are required") from exc
    app = build_app()
    private_key = secrets.randbelow(crypto._N - 1) + 1
    wallet_hex = _wallet(private_key)
    address = address_base58(wallet_hex)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://probe") as client:
        health = await client.get("/healthz")
        challenge = await client.post("/v1/auth/challenges", json={
            "address": address, "network": "tron-nile"})
        challenge.raise_for_status()
        challenge_body = challenge.json()
        signature = _sign_hash(message_hash(challenge_body["message"]), private_key).hex()
        session = await client.post("/v1/auth/sessions", json={"address": address,
            "network": "tron-nile", "nonce": challenge_body["nonce"],
            "signature": signature})
        session.raise_for_status()
        session_body = session.json()
        result = await client.post("/v1/agent/intent",
            headers={"Authorization": "Bearer " + session_body["session_token"]},
            json={"message": message})
        result.raise_for_status()
        body = result.json()
    evidence = {"schema_version": "live-agent-probe-1",
        "probed_at": datetime.now(UTC).isoformat(),
        "service_health_status": health.status_code,
        "storage_backend": health.json().get("storage", {}).get("backend"),
        "wallet_auth": {"challenge_status": challenge.status_code,
            "session_status": session.status_code,
            "wallet_sha256": hashlib.sha256(wallet_hex.encode()).hexdigest(),
            "network": session_body["network"],
            "execution_authority": session_body["execution_authority"]},
        "model_call": {"http_status": result.status_code, "status": body["status"],
            "reason_codes": body["reason_codes"],
            "usage_event_id": body["usage_event_id"],
            "request_hash": body.get("request_hash"),
            "execution_authority": body["execution_authority"],
            "signature_status": body["signature_status"],
            "chain_status": body["chain_status"]},
        "message_sha256": hashlib.sha256(message.encode()).hexdigest()}
    evidence["evidence_hash"] = digest(evidence)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical(evidence) + b"\n")
    print(json.dumps({"output": str(output), "evidence_hash": evidence["evidence_hash"],
                      "model_status": body["status"],
                      "reason_codes": body["reason_codes"]}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(probe(args.message, args.output))


if __name__ == "__main__":
    main()
