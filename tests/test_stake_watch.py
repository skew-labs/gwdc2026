"""Native Watch reuses account notifications and never obtains trade authority."""

import copy, unittest
from unittest.mock import patch
from finance_service.portfolio_review import confirmed_policy
import test_stake_execution as fixtures


class NativeWatchTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.StakeTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.test_exact_stake_then_vote_lifecycle_and_actual_fees()
        self.version, self.state = self.f.b.load(self.f.ctx)
        self.state["confirmed_review_inputs"]["a" * 64] = {
            "assumptions": {"daily_loss_bps": 100, "stress_loss_bps": 1000},
            "constraints": {"max_fee": {"value": "1", "symbol": "TRX", "decimals": 6}},
        }
        self.market = copy.deepcopy(self.f.market)
        self.market.update(
            expires_at="2026-09-29T12:05:00+00:00", block="100", unfreeze_days=1
        )
        self.market["evidence"]["account"] = self.f.after

    def run_review(self):
        with patch("finance_service.stake_watch.read", return_value=self.market):
            return self.f.b.reviews.refresh(self.f.ctx, self.state)

    def test_live_hold_excludes_sunk_entry_fees_and_never_broadcasts(self):
        r = self.run_review()
        self.assertEqual(r["status"], "HOLD")
        self.assertEqual(r["execution_authority"], "NONE")
        self.assertEqual(r["hold"]["change_cost"], "0")
        self.assertEqual(r["paid_fees"], "0.5")
        self.assertLessEqual(float(r["alternatives"][0]["net_improvement"]), 0)
        self.assertEqual(self.f.broadcasts, 2)
        self.assertEqual(self.state["workspace"]["notifications"], [])

    def test_cash_breach_creates_one_account_alert_and_rechecks_latest_balance(self):
        self.f.after["balance"] = 1000000
        r = self.run_review()
        self.assertEqual(r["status"], "POLICY_BREACH")
        self.assertIn("CASH_BELOW_CONFIRMED_MINIMUM", r["checks"])
        self.run_review()
        self.assertEqual(len(self.state["workspace"]["notifications"]), 1)
        self.f.after["balance"] = 299500000
        r = self.run_review()
        self.assertEqual(r["status"], "HOLD")
        self.assertIsNotNone(self.state["workspace"]["notifications"][0]["resolved_at"])

    def test_incomplete_or_external_position_never_becomes_hold(self):
        self.f.after["votes"][0]["vote_count"] = 599
        r = self.run_review()
        self.assertEqual(r["status"], "DATA_UNAVAILABLE")
        self.assertIsNone(self.state["workspace"]["stake_position"]["net_income"])
        self.assertEqual(self.f.broadcasts, 2)
