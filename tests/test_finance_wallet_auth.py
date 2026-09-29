"""TRON V2 wallet sign-in, replay consumption and authenticated agent API."""

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import economic_machine.tron_crypto as crypto
from economic_machine.tron_crypto import keccak256
from economic_machine.tron_sources import address_base58
from finance_service.agent_intent import AgentIntentService
from finance_service.api import create_app
from finance_service.auth import HmacSessionVerifier
from finance_service.model_usage import OperationalModelUsageStore
from finance_service.operational_repository import OperationalRepository
from finance_service.wallet_auth import WalletAuthService, message_hash


AT = "2026-09-29T12:00:00+00:00"


def sign_hash(value, private_key):
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


def wallet_for(private_key):
    public = crypto._multiply(private_key, crypto._G)
    return "41" + keccak256(public[0].to_bytes(32, "big")
        + public[1].to_bytes(32, "big"))[-20:].hex()


class FakeProvider:
    def complete(self, _messages):
        answer = {"schema_version": "financial-intent-draft-1", "intent": "PLAN",
            "patch": {"capital": [{"asset": "USDT", "amount": "10000"}],
                      "base_asset": "USDT"},
            "evidence": {"capital": "10,000 USDT", "base_asset": "USDT"},
            "missing": ["risk_profile", "horizon_seconds", "immediate_cash",
                        "withdrawals", "borrowing_consent"], "reason_codes": []}
        return {"content": json.dumps(answer), "provider": "kiln",
            "model_id": "qwen3-32b", "model_revision": "fixture", "request_id": "r1",
            "input_tokens": 20, "output_tokens": 10, "attempts": 1, "latency_ms": 12,
            "response_sha256": "c" * 64,
            "energy": {"kind": "UNMEASURED", "joules": None,
                       "measurement_source": None}}


class WalletAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = OperationalRepository(Path(self.temp.name) / "auth.sqlite3")
        self.verifier = HmacSessionVerifier({"key-one": b"k" * 32},
            active_key_id="key-one", clock=lambda: AT)
        self.service = WalletAuthService(self.repository, self.verifier, lambda: AT,
                                         domain="test.whollet")
        self.private_key = int.from_bytes(bytes.fromhex("35" * 32), "big")
        self.wallet_hex = wallet_for(self.private_key)
        self.address = address_base58(self.wallet_hex)

    def tearDown(self):
        self.temp.cleanup()

    def authenticated(self):
        challenge = self.service.challenge(self.address, "tron-nile")
        signature = sign_hash(message_hash(challenge["message"]), self.private_key).hex()
        return challenge, self.service.verify(self.address, "tron-nile",
                                              challenge["nonce"], signature)

    def test_v2_signature_creates_bounded_nonexecuting_session(self):
        challenge, result = self.authenticated()
        context = self.verifier.authenticate(result["session_token"])
        self.assertEqual(context.wallet, self.wallet_hex)
        self.assertEqual(context.network, "tron-nile")
        self.assertEqual(result["execution_authority"], "NONE")
        self.assertIn("no transaction or asset authority", challenge["message"])

    def test_challenge_is_consumed_on_failed_signature_and_cannot_replay(self):
        challenge = self.service.challenge(self.address, "tron-nile")
        other = int.from_bytes(bytes.fromhex("36" * 32), "big")
        signature = sign_hash(message_hash(challenge["message"]), other).hex()
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.service.verify(self.address, "tron-nile", challenge["nonce"], signature)
        with self.assertRaisesRegex(ValueError, "consumed"):
            self.service.verify(self.address, "tron-nile", challenge["nonce"], signature)

    def test_agent_endpoint_requires_wallet_session_and_persists_usage(self):
        try:
            import httpx
        except ImportError:
            self.skipTest("service optional dependency not installed")
        challenge, session = self.authenticated()
        agent = AgentIntentService(FakeProvider(), OperationalModelUsageStore(self.repository),
                                   lambda: AT)
        app = create_app(session_verifier=self.verifier, repository=self.repository,
            clock=lambda: AT, agent_intent_service=agent, wallet_auth_service=self.service)

        async def scenario():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                denied = await client.post("/v1/agent/intent",
                                           json={"message": "10,000 USDT"})
                self.assertEqual(denied.status_code, 422)
                response = await client.post("/v1/agent/intent",
                    headers={"Authorization": "Bearer " + session["session_token"]},
                    json={"message": "10,000 USDT"})
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["status"], "NEEDS_INFORMATION")
                self.assertEqual(body["execution_authority"], "NONE")
                record = self.repository.get_record(self.verifier.authenticate(
                    session["session_token"]).scope, "MODEL_USAGE",
                    "usage-" + body["usage_event_id"])
                self.assertEqual(record["body"]["model_id"], "qwen3-32b")
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
