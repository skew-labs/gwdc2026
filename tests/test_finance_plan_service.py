"""Authenticated live-snapshot plan service boundary tests."""

import copy
import unittest
from dataclasses import replace

from economic_machine.plan_compiler import REQUEST_VERSION
from economic_machine.snapshot_assembly import SnapshotAssembler
from economic_machine.values import MachineError
from finance_service.context import AuthenticatedContext
from finance_service.mandate_service import MandateService
from finance_service.plan_service import PlanService
from finance_service.repository import InMemoryMandateRepository
from test_economic_mandate import fixture as mandate_fixture
from test_economic_tron_cashflow import quote
from test_economic_tron_sources import AT, CONFIG, SCOPE, ADDRESSES, capture, fixtures, modify


class StaticSnapshots:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def read(self, context):
        return copy.deepcopy(self.snapshot)


class PlanServiceTests(unittest.TestCase):
    def setUp(self):
        self.assembler = SnapshotAssembler(CONFIG)
        self.captures = fixtures(mode="LIVE_READ", wallet=True)
        modify(self.captures, "justlend_usdd_rewards_v1", lambda payload: payload["data"].update(
            {contract: {"USDD": "0.03"} for _, _, contract, _, _ in ADDRESSES}))
        modify(self.captures, "justlend_markets_v1",
               lambda payload: payload["data"]["tokenList"][1].update(supplyRate="0.08"))
        self.captures.append(capture("tron_chain_parameters", {"chainParameter": [
            {"key": "getUnfreezeDelayDays", "value": 14},
            {"key": "getEnergyFee", "value": 100},
            {"key": "getTransactionFee", "value": 1000},
        ]}, mode="LIVE_READ"))
        self.snapshot = self.assembler.assemble(self.captures, as_of=AT, scope=SCOPE)
        self.raw = mandate_fixture()
        self.raw["scope"] = copy.deepcopy(SCOPE)
        self.raw["terms"]["withdrawals"] = []
        self.context = AuthenticatedContext(
            **SCOPE, session_id="session-plan", issued_at="2026-09-28T11:00:00Z",
            expires_at="2026-09-28T12:10:00Z", trace_id=self.raw["trace_id"])
        self.repo = InMemoryMandateRepository()
        mandates = MandateService(self.repo, lambda: AT)
        aggregate = mandates.create(self.context, self.raw)
        mandates.confirm(self.context, self.raw["mandate_id"],
                         expected_version=aggregate["version"],
                         expected_draft_hash=aggregate["revisions"][-1]["draft_hash"])
        self.snapshots = StaticSnapshots(self.snapshot)
        self.service = PlanService(self.repo, self.snapshots, self.assembler, lambda: AT)
        self.request = {"schema_version": REQUEST_VERSION,
            "snapshot_hash": self.snapshot["snapshot_hash"], "grid_step_bps": 2000,
            "min_plan_distance_bps": 2000, "max_plan_age_seconds": 300,
            "scenarios": ["market"], "product_templates": [
                {"product_id": name, "max_bps": 4000, "current_bps": 0,
                 "daily_loss_bps": daily,
                 "stress_loss_bps": {"market": stress}, "quote": quote(name)}
                for name, daily, stress in (("justlend.v1.jUSDT", 25, 100),
                                            ("justlend.v1.jUSDD", 100, 400))]}

    def test_authenticated_live_compare_and_prepare_are_non_executing(self):
        result = self.service.compare(self.context, self.raw["mandate_id"], self.request)
        intent = self.service.prepare_intent(
            self.context, self.raw["mandate_id"], self.request,
            expected_comparison_hash=result["comparison_hash"], selected_plan="GROWTH",
            valid_until="2026-09-28T12:04:00Z")
        self.assertEqual(result["status"], "COMPARISON_READY")
        self.assertEqual(intent["status"], "INTENT_PREPARED")
        self.assertEqual((intent["execution_authority"], intent["chain_status"]),
                         ("NONE", "NOT_SUBMITTED"))

    def test_fixture_foreign_scope_and_expired_context_are_rejected(self):
        fixture_snapshot = copy.deepcopy(self.snapshot)
        fixture_snapshot["mode"] = "FIXTURE"
        fixture_service = PlanService(self.repo, StaticSnapshots(fixture_snapshot),
                                      self.assembler, lambda: AT)
        with self.assertRaisesRegex(MachineError, "live snapshot"):
            fixture_service.compare(self.context, self.raw["mandate_id"], self.request)
        foreign = copy.deepcopy(self.snapshot)
        foreign["scope"]["owner_id"] = "owner-b"
        foreign_service = PlanService(self.repo, StaticSnapshots(foreign),
                                      self.assembler, lambda: AT)
        with self.assertRaisesRegex(MachineError, "scoped live snapshot"):
            foreign_service.compare(self.context, self.raw["mandate_id"], self.request)
        expired = replace(self.context, expires_at="2026-09-28T11:59:00Z")
        with self.assertRaisesRegex(MachineError, "expired"):
            self.service.compare(expired, self.raw["mandate_id"], self.request)

    def test_market_change_requires_fresh_user_review(self):
        comparison = self.service.compare(self.context, self.raw["mandate_id"], self.request)
        modify(self.captures, "justlend_markets_v1",
               lambda payload: payload["data"]["tokenList"][0].update(supplyRate="0.03"))
        self.snapshots.snapshot = self.assembler.assemble(
            self.captures, as_of=AT, scope=SCOPE)
        with self.assertRaises(MachineError):
            self.service.prepare_intent(
                self.context, self.raw["mandate_id"], self.request,
                expected_comparison_hash=comparison["comparison_hash"],
                selected_plan="GROWTH", valid_until="2026-09-28T12:04:00Z")

    def test_intent_cannot_outlive_authenticated_session(self):
        comparison = self.service.compare(self.context, self.raw["mandate_id"], self.request)
        short = replace(self.context, expires_at="2026-09-28T12:03:00Z")
        with self.assertRaisesRegex(MachineError, "session validity"):
            self.service.prepare_intent(
                short, self.raw["mandate_id"], self.request,
                expected_comparison_hash=comparison["comparison_hash"],
                selected_plan="GROWTH", valid_until="2026-09-28T12:04:00Z")

    def test_draft_mandate_cannot_compare(self):
        raw = copy.deepcopy(self.raw)
        raw["mandate_id"] = "draft-plan"
        repo = InMemoryMandateRepository()
        MandateService(repo, lambda: AT).create(self.context, raw)
        service = PlanService(repo, self.snapshots, self.assembler, lambda: AT)
        with self.assertRaisesRegex(MachineError, "confirmed"):
            service.compare(self.context, raw["mandate_id"], self.request)

    def test_pending_capital_hold_blocks_a_new_plan(self):
        aggregate = self.repo.read(SCOPE, self.raw["mandate_id"])

        def add_hold(current):
            current["holds"]["pending"] = {"status": "RECONCILIATION_REQUIRED"}
            return current

        self.repo.transact(SCOPE, self.raw["mandate_id"], aggregate["version"], add_hold)
        with self.assertRaisesRegex(MachineError, "pending capital holds"):
            self.service.compare(self.context, self.raw["mandate_id"], self.request)


if __name__ == "__main__":
    unittest.main()
