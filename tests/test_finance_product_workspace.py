import asyncio
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from economic_machine.values import MachineError, digest
from finance_service.auth import HmacSessionVerifier
from finance_service.context import AuthenticatedContext
from finance_service.operational_repository import OperationalRepository
from finance_service.product_workspace import (
    VERSION,
    ProductWorkspaceService,
    normalize_product_story,
)

ROOT = Path(__file__).resolve().parents[1]
STORY = json.loads((ROOT / "cases/product_workspace_story.json").read_text())
AT = "2026-09-28T10:50:00Z"


def rehash(story):
    story = copy.deepcopy(story)
    story.pop("story_hash", None)
    story["story_hash"] = digest({"domain": VERSION, "story": story})
    return story


def context(scope=None):
    scope = STORY["scope"] if scope is None else scope
    return AuthenticatedContext(**scope, session_id="session-product",
        issued_at="2026-09-28T10:49:00Z", expires_at="2026-09-28T10:53:00Z",
        trace_id="trace-product")


def assertion(scope=None):
    scope = STORY["scope"] if scope is None else scope
    return {"schema_version": "verified-wallet-assertion-1",
        "assertion_id": "assertion-product", "scope": copy.deepcopy(scope),
        "challenge_hash": "1" * 64, "signature_hash": "2" * 64,
        "verified_at": "2026-09-28T10:49:00Z",
        "valid_until": "2026-09-28T10:53:00Z", "verification_status": "VERIFIED",
        "verifier_id": "wallet-proof-adapter", "evidence_hash": "3" * 64,
        "execution_authority": "NONE"}


class ProductStoryContractTests(unittest.TestCase):
    def test_checked_in_story_has_two_runs_plans_vault_and_null_usage(self):
        story = normalize_product_story(STORY)
        self.assertEqual([run["run_id"] for run in story["runs"]], ["A", "B"])
        self.assertNotEqual(story["runs"][0]["comparison_hash"],
                            story["runs"][1]["comparison_hash"])
        self.assertEqual(len(story["plans"]), 2)
        self.assertTrue(any(product["kind"] == "COLLATERAL_DEBT"
                            for product in story["products"]))
        usage = story["runs"][0]["model_usage"]
        self.assertEqual(usage["status"], "NOT_RUN")
        self.assertIsNone(usage["prompt_tokens"])
        self.assertEqual((story["approval"]["signature_status"],
                          story["approval"]["chain_status"]),
                         ("NOT_REQUESTED", "NOT_SUBMITTED"))

    def test_replay_cannot_claim_model_measurements_signature_or_transaction(self):
        measured = copy.deepcopy(STORY)
        measured["runs"][0]["model_usage"]["prompt_tokens"] = 0
        with self.assertRaisesRegex(MachineError, "remain null"):
            normalize_product_story(rehash(measured))
        signed = copy.deepcopy(STORY)
        signed["approval"].update(status="READY", signature_status="SIGNED")
        with self.assertRaisesRegex(MachineError, "cannot present a live approval"):
            normalize_product_story(rehash(signed))
        submitted = copy.deepcopy(STORY)
        submitted["approval"].update(chain_status="SUBMITTED", txid=None)
        with self.assertRaises(MachineError):
            normalize_product_story(rehash(submitted))

    def test_missing_usdd_vault_and_cross_plan_approval_fail_closed(self):
        no_vault = copy.deepcopy(STORY)
        no_vault["products"] = [item for item in no_vault["products"]
                                if not item["product_id"].startswith("usdd.vault.")]
        with self.assertRaisesRegex(MachineError, "USDD Vault"):
            normalize_product_story(rehash(no_vault))
        wrong = copy.deepcopy(STORY)
        wrong["approval"]["plan_hash"] = "f" * 64
        with self.assertRaisesRegex(MachineError, "absent"):
            normalize_product_story(rehash(wrong))

    def test_identifiers_source_eligibility_and_approval_window_are_strict(self):
        invalid_id = copy.deepcopy(STORY)
        invalid_id["story_id"] = "contains spaces"
        with self.assertRaisesRegex(MachineError, "product story"):
            normalize_product_story(rehash(invalid_id))
        ambiguous_source = copy.deepcopy(STORY)
        ambiguous_source["products"][0]["source"]["state_eligible"] = 1
        with self.assertRaisesRegex(MachineError, "source eligibility"):
            normalize_product_story(rehash(ambiguous_source))
        half_timed = copy.deepcopy(STORY)
        half_timed["approval"]["prepared_at"] = AT
        with self.assertRaisesRegex(MachineError, "timestamps must be paired"):
            normalize_product_story(rehash(half_timed))

    def test_plan_budget_product_and_approval_scope_are_bound(self):
        absent_product = copy.deepcopy(STORY)
        absent_product["plans"][0]["weights"][0]["product_id"] = "missing.product"
        with self.assertRaisesRegex(MachineError, "absent opportunity"):
            normalize_product_story(rehash(absent_product))
        broken_budget = copy.deepcopy(STORY)
        broken_budget["plans"][0]["cash_amount"] = "7998"
        with self.assertRaisesRegex(MachineError, "conserve mandate capital"):
            normalize_product_story(rehash(broken_budget))
        other_network = copy.deepcopy(STORY)
        other_network["approval"]["network"] = "tron-nile"
        with self.assertRaisesRegex(MachineError, "asset or network"):
            normalize_product_story(rehash(other_network))
        wrong_amount = copy.deepcopy(STORY)
        wrong_amount["approval"]["amount"] = "5999"
        with self.assertRaisesRegex(MachineError, "selected plan"):
            normalize_product_story(rehash(wrong_amount))


class ProductWorkspaceServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = OperationalRepository(Path(self.temp.name) / "product.sqlite3")
        self.service = ProductWorkspaceService(self.repo, lambda: AT)
        self.ctx = context()
        self.service.publish(self.ctx, STORY, expected_version=0)

    def tearDown(self):
        self.temp.cleanup()

    def test_story_is_scope_isolated_and_replay_acknowledgement_has_no_authority(self):
        other_scope = {**STORY["scope"], "tenant_id": "tenant-other",
                       "owner_id": "owner-other"}
        with self.assertRaisesRegex(MachineError, "authenticated scope"):
            self.service.read(context(other_scope), STORY["story_id"])
        request = {"decision": "ACKNOWLEDGE_REPLAY", "story_revision": 1,
            "plan_hash": STORY["plans"][0]["plan_hash"], "approval_hash": None,
            "expected_decision_version": 0}
        result = self.service.decide(self.ctx, STORY["story_id"], request)
        self.assertEqual(result["decision"]["status"], "REPLAY_REVIEWED")
        self.assertEqual((result["decision"]["execution_authority"],
                          result["decision"]["signature_status"],
                          result["decision"]["chain_status"]),
                         ("NONE", "NOT_REQUESTED", "NOT_SUBMITTED"))

    def test_replay_wallet_review_and_stale_revision_are_rejected(self):
        wallet = {"decision": "OPEN_WALLET_REVIEW", "story_revision": 1,
            "plan_hash": STORY["plans"][1]["plan_hash"], "approval_hash": None,
            "expected_decision_version": 0}
        with self.assertRaisesRegex(MachineError, "unavailable"):
            self.service.decide(self.ctx, STORY["story_id"], wallet)
        revised = copy.deepcopy(STORY)
        revised["revision"] = 2
        revised["headline"] = "정책 변경 뒤 새 계획"
        self.service.publish(self.ctx, rehash(revised), expected_version=1)
        stale = {**wallet, "decision": "ACKNOWLEDGE_REPLAY"}
        with self.assertRaisesRegex(MachineError, "revision changed"):
            self.service.decide(self.ctx, STORY["story_id"], stale)
        malformed = {**stale, "story_revision": True}
        with self.assertRaisesRegex(MachineError, "decision revision"):
            self.service.decide(self.ctx, STORY["story_id"], malformed)
        changed_approval = {**stale, "story_revision": 2, "approval_hash": "f" * 64}
        with self.assertRaisesRegex(MachineError, "approval commitment changed"):
            self.service.decide(self.ctx, STORY["story_id"], changed_approval)


@unittest.skipUnless(importlib.util.find_spec("fastapi") and importlib.util.find_spec("httpx"),
                     "service optional dependencies not installed")
class ProductApiTests(unittest.TestCase):
    def test_static_demo_and_authenticated_workspace_do_not_cross_scopes(self):
        import httpx

        from finance_service.api import create_app
        with tempfile.TemporaryDirectory() as temp:
            repo = OperationalRepository(Path(temp) / "api.sqlite3")
            service = ProductWorkspaceService(repo, lambda: AT)
            service.publish(context(), STORY, expected_version=0)
            verifier = HmacSessionVerifier({"key-1": b"k" * 32}, active_key_id="key-1",
                clock=lambda: AT)
            token = verifier.issue(assertion(), session_id="session-product",
                                   trace_id="trace-product", ttl_seconds=120)
            other_scope = {**STORY["scope"], "wallet": "41" + "22" * 20,
                           "owner_id": "owner-other"}
            other = verifier.issue(assertion(other_scope), session_id="session-other",
                                   trace_id="trace-other", ttl_seconds=120)
            app = create_app(session_verifier=verifier, repository=repo, clock=lambda: AT,
                product_service=service, web_root=ROOT / "web", demo_story=STORY)

            async def scenario():
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport,
                        base_url="http://test") as client:
                    home = await client.get("/")
                    self.assertEqual(home.status_code, 200)
                    self.assertIn("WHOLLET", home.text)
                    demo = await client.get("/api/demo/story")
                    self.assertEqual(demo.json()["mode"], "HISTORICAL_REPLAY")
                    actual = await client.get("/v1/workspaces/pr10-replay-demo",
                        headers={"Authorization": "Bearer " + token})
                    self.assertEqual(actual.json()["story_hash"], STORY["story_hash"])
                    denied = await client.get("/v1/workspaces/pr10-replay-demo",
                        headers={"Authorization": "Bearer " + other})
                    self.assertEqual(denied.status_code, 409)
                    wallet = await client.post(
                        "/v1/workspaces/pr10-replay-demo/decisions",
                        headers={"Authorization": "Bearer " + token}, json={
                            "decision": "OPEN_WALLET_REVIEW", "story_revision": 1,
                            "plan_hash": STORY["plans"][1]["plan_hash"],
                            "approval_hash": None, "expected_decision_version": 0})
                    self.assertEqual(wallet.status_code, 409)
            asyncio.run(scenario())


class StaticWorkspaceTests(unittest.TestCase):
    def test_browser_code_uses_text_nodes_and_preserves_unknown_measurements(self):
        script = (ROOT / "web/app.js").read_text()
        self.assertNotIn("innerHTML", script)
        self.assertIn("signMessageV2", script)
        self.assertNotIn("privateKey", script)
        self.assertIn("sendRawTransaction", script)
        self.assertIn("sendTrx(address, 1, address)", script)
        self.assertIn("tron-nile", script)
        self.assertIn("미측정", script)
        self.assertIn("/api/demo/story", script)
        self.assertIn("/v1/agent/intent", script)
        self.assertIn("whollet:selected:", script)
        self.assertIn("NOT_SUBMITTED", (ROOT / "cases/product_workspace_story.json").read_text())


if __name__ == "__main__":
    unittest.main()
