"""PR05 intent boundary and direct PR01/PR04 connection tests."""

import copy
import json
import unittest

from economic_machine.plan_compiler import REQUEST_VERSION
from economic_machine.snapshot_assembly import SnapshotAssembler
from finance_service.context import AuthenticatedContext
from finance_service.intent_service import IntentService
from finance_service.mandate_service import MandateService
from finance_service.model_provider import ModelProviderError
from finance_service.model_usage import InMemoryModelUsageStore
from finance_service.plan_service import PlanService
from finance_service.repository import InMemoryMandateRepository
from test_economic_mandate import fixture as mandate_fixture
from test_economic_tron_cashflow import quote
from test_economic_tron_sources import AT, CONFIG, SCOPE, ADDRESSES, capture, fixtures, modify


class FakeProvider:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return {"content": json.dumps(answer, ensure_ascii=False), "provider": "kiln",
            "model_id": "qwen3-32b", "model_revision": "fixture-revision",
            "request_id": "request-fixture", "input_tokens": 100,
            "output_tokens": 40, "attempts": 1, "latency_ms": 25,
            "response_sha256": "c" * 64,
            "energy": {"kind": "UNMEASURED", "joules": None,
                       "measurement_source": None}}


class StaticSnapshots:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def read(self, context):
        return copy.deepcopy(self.snapshot)


def answer(patch, evidence, *, missing=None):
    return {"schema_version": "financial-intent-draft-1", "intent": "REVISE",
        "patch": patch, "evidence": evidence, "missing": missing or [],
        "reason_codes": []}


class IntentServiceTests(unittest.TestCase):
    def setUp(self):
        self.raw = mandate_fixture()
        self.raw["scope"] = copy.deepcopy(SCOPE)
        self.raw["terms"]["withdrawals"] = []
        self.context = AuthenticatedContext(**SCOPE, session_id="session-intent",
            issued_at="2026-09-28T11:00:00Z", expires_at="2026-09-28T12:10:00Z",
            trace_id=self.raw["trace_id"])
        self.repo = InMemoryMandateRepository()
        self.mandates = MandateService(self.repo, lambda: AT)
        aggregate = self.mandates.create(self.context, self.raw)
        self.aggregate = self.mandates.confirm(self.context, self.raw["mandate_id"],
            expected_version=aggregate["version"],
            expected_draft_hash=aggregate["revisions"][-1]["draft_hash"])
        self.usage = InMemoryModelUsageStore()

    def test_model_proposal_is_unconfirmed_and_repeat_uses_zero_provider_calls(self):
        message = "즉시 현금을 50%로 바꿔줘"
        provider = FakeProvider([answer(
            {"immediate_cash": {"kind": "BPS", "value": 5000}},
            {"immediate_cash": "50%"})])
        service = IntentService(self.repo, provider, self.usage, lambda: AT)
        first = service.propose_revision(self.context, self.raw["mandate_id"], message)
        self.assertEqual(first["status"], "DRAFT_READY")
        self.assertEqual(first["candidate"]["terms"]["immediate_cash"]["value"], 5000)
        self.assertEqual((first["execution_authority"], first["chain_status"]),
                         ("NONE", "NOT_SUBMITTED"))
        stored = self.repo.read(SCOPE, self.raw["mandate_id"])
        self.assertEqual((stored["version"], stored["revisions"][-1]["status"]),
                         (self.aggregate["version"], "CONFIRMED"))
        again = service.propose_revision(self.context, self.raw["mandate_id"], message)
        self.assertEqual(again["candidate_draft_hash"], first["candidate_draft_hash"])
        self.assertEqual(provider.calls, 1)
        events = self.usage.for_trace(self.context.trace_id)
        self.assertEqual([item["outcome"] for item in events], ["SUCCEEDED", "CACHE_HIT"])
        self.assertIsNone(events[1]["input_tokens"])

    def test_missing_debt_limit_yields_fixed_question_without_candidate(self):
        message = "담보 대출을 허용할게"
        provider = FakeProvider([answer({}, {}, missing=["max_debt"])])
        result = IntentService(self.repo, provider, self.usage, lambda: AT).propose_revision(
            self.context, self.raw["mandate_id"], message)
        self.assertEqual(result["status"], "NEEDS_INFORMATION")
        self.assertEqual(result["questions"][0]["field"], "max_debt")
        self.assertIsNone(result["candidate"])

    def test_cross_field_conflict_is_a_bounded_candidate_rejection(self):
        message = "기준 자산만 USDD로 바꿔줘"
        provider = FakeProvider([answer({"base_asset": "USDD"},
                                        {"base_asset": "USDD"})])
        result = IntentService(self.repo, provider, self.usage, lambda: AT).propose_revision(
            self.context, self.raw["mandate_id"], message)
        self.assertEqual(result["status"], "CANDIDATE_REJECTED")
        self.assertEqual(result["reason_codes"], ["CANDIDATE_CONSTRAINT_CONFLICT"])
        self.assertIsNone(result["candidate"])
        self.assertEqual(len(self.repo.read(SCOPE, self.raw["mandate_id"])["revisions"]), 1)

    def test_invalid_output_and_bounded_provider_failure_do_not_mutate_mandate(self):
        malicious = {**answer({}, {}), "transaction": {"sign": True}}
        provider = FakeProvider([malicious, ModelProviderError("RATE_LIMITED", {
            "provider": "kiln", "model_id": "qwen3-32b", "model_revision": None,
            "request_id": None, "input_tokens": None, "output_tokens": None,
            "attempts": 2, "latency_ms": 20, "response_sha256": None})])
        service = IntentService(self.repo, provider, self.usage, lambda: AT)
        rejected = service.propose_revision(self.context, self.raw["mandate_id"], "지금 전송해")
        failed = service.propose_revision(self.context, self.raw["mandate_id"], "위험을 바꿔줘")
        self.assertEqual(rejected["status"], "MODEL_OUTPUT_REJECTED")
        self.assertEqual((failed["status"], failed["reason_codes"]),
                         ("MODEL_UNAVAILABLE", ["RATE_LIMITED"]))
        stored = self.repo.read(SCOPE, self.raw["mandate_id"])
        self.assertEqual(len(stored["revisions"]), 1)

    def test_confirmed_condition_change_changes_pr04_plan_commitment(self):
        assembler = SnapshotAssembler(CONFIG)
        captures = fixtures(mode="LIVE_READ", wallet=True)
        modify(captures, "justlend_usdd_rewards_v1", lambda payload: payload["data"].update(
            {contract: {"USDD": "0.03"} for _, _, contract, _, _ in ADDRESSES}))
        modify(captures, "justlend_markets_v1",
               lambda payload: payload["data"]["tokenList"][1].update(supplyRate="0.08"))
        captures.append(capture("tron_chain_parameters", {"chainParameter": [
            {"key": "getUnfreezeDelayDays", "value": 14},
            {"key": "getEnergyFee", "value": 100},
            {"key": "getTransactionFee", "value": 1000}]}, mode="LIVE_READ"))
        snapshot = assembler.assemble(captures, as_of=AT, scope=SCOPE)
        plan_service = PlanService(self.repo, StaticSnapshots(snapshot), assembler, lambda: AT)
        request = {"schema_version": REQUEST_VERSION,
            "snapshot_hash": snapshot["snapshot_hash"], "grid_step_bps": 2000,
            "min_plan_distance_bps": 2000, "max_plan_age_seconds": 300,
            "scenarios": ["market"], "product_templates": [
                {"product_id": name, "max_bps": 4000, "current_bps": 0,
                 "daily_loss_bps": daily, "stress_loss_bps": {"market": stress},
                 "quote": quote(name)}
                for name, daily, stress in (("justlend.v1.jUSDT", 25, 100),
                                            ("justlend.v1.jUSDD", 100, 400))]}
        before = plan_service.compare(self.context, self.raw["mandate_id"], request)
        message = "즉시 현금을 50%로 바꿔줘"
        provider = FakeProvider([answer(
            {"immediate_cash": {"kind": "BPS", "value": 5000}},
            {"immediate_cash": "50%"})])
        proposal = IntentService(self.repo, provider, self.usage, lambda: AT).propose_revision(
            self.context, self.raw["mandate_id"], message)
        revised = self.mandates.revise(self.context, proposal["candidate"],
                                       expected_version=self.aggregate["version"])
        self.mandates.confirm(self.context, self.raw["mandate_id"],
            expected_version=revised["version"],
            expected_draft_hash=proposal["candidate_draft_hash"])
        after = plan_service.compare(self.context, self.raw["mandate_id"], request)
        self.assertNotEqual(before["comparison_hash"], after["comparison_hash"])
        self.assertNotEqual(before["plans"], after["plans"])
        self.assertTrue(all(plan["cash_bps"] >= 5000 for plan in after["plans"]))


if __name__ == "__main__":
    unittest.main()
