"""A selected allocation cannot be replayed under another state or policy."""

import copy
import json
import unittest
from decimal import Decimal
from pathlib import Path

from economic_machine.basket import commit_basket, verify_basket
from economic_machine.portfolio import select_portfolio
from economic_machine.values import MachineError


ROOT = Path(__file__).resolve().parents[1] / "cases"


class BasketCommitmentTests(unittest.TestCase):
    def setUp(self):
        self.request = json.loads((ROOT / "economic_portfolio_candidates_demo.json").read_text())
        self.state = json.loads((ROOT / "economic_basket_state_demo.json").read_text())
        self.policy = json.loads((ROOT / "economic_basket_policy_demo.json").read_text())
        self.verdict = select_portfolio(self.request)
        self.expiry = "2026-09-25T12:03:00+00:00"

    def commit(self):
        return commit_basket(self.request, self.verdict, self.state, self.policy,
                             valid_until=self.expiry)

    def test_exact_legs_capital_and_replay(self):
        first = self.commit()
        self.assertEqual(first, self.commit())
        self.assertEqual(first["basket"]["candidate_id"], "balanced")
        self.assertEqual([(x["product_id"], x["amount"]) for x in first["basket"]["legs"]],
                         [("lend_A", "200"), ("stable_B", "700")])
        self.assertEqual(first["basket"]["cash_amount"], "100")
        self.assertEqual(sum(Decimal(x["amount"]) for x in first["basket"]["legs"])
                         + Decimal(first["basket"]["cash_amount"]), Decimal("1000"))
        self.assertTrue(verify_basket(first, self.request, self.state, self.policy))
        self.assertEqual((first["execution_authority"], first["execution_status"],
                          first["chain_status"]),
                         ("NONE", "NO_BASKET_EXECUTOR", "NOT_SUBMITTED"))

    def test_changed_policy_state_expiry_or_leg_breaks_replay(self):
        first = self.commit()
        changed = copy.deepcopy(self.policy)
        changed["max_capital"] = "1100"
        self.assertFalse(verify_basket(first, self.request, self.state, changed))
        changed = copy.deepcopy(self.state)
        changed["sequence"] += 1
        self.assertFalse(verify_basket(first, self.request, changed, self.policy))
        changed = copy.deepcopy(first)
        changed["valid_until"] = "2026-09-25T12:04:00+00:00"
        self.assertFalse(verify_basket(changed, self.request, self.state, self.policy))
        changed = copy.deepcopy(first)
        changed["basket"]["legs"][0]["amount"] = "201"
        self.assertFalse(verify_basket(changed, self.request, self.state, self.policy))
        changed = copy.deepcopy(self.request)
        changed["products"][0]["source_hash"] = "e" * 64
        self.assertFalse(verify_basket(first, changed, self.state, self.policy))

    def test_supplied_verdict_must_replay_and_abstention_cannot_commit(self):
        forged = copy.deepcopy(self.verdict)
        forged["selected_candidate_id"] = "aggressive"
        with self.assertRaisesRegex(MachineError, "does not replay"):
            commit_basket(self.request, forged, self.state, self.policy,
                          valid_until=self.expiry)
        self.request["max_stress_loss"] = "50"
        with self.assertRaisesRegex(MachineError, "abstained"):
            commit_basket(self.request, select_portfolio(self.request), self.state,
                          self.policy, valid_until=self.expiry)

    def test_policy_allowlist_nav_and_staleness_fail_closed(self):
        changed = copy.deepcopy(self.policy)
        changed["allowed_products"] = ["stable_B"]
        with self.assertRaisesRegex(MachineError, "outside policy"):
            commit_basket(self.request, self.verdict, self.state, changed,
                          valid_until=self.expiry)
        changed = copy.deepcopy(self.state)
        changed["facts"]["portfolio.nav"]["value"] = "999"
        with self.assertRaisesRegex(MachineError, "capital fact"):
            commit_basket(self.request, self.verdict, changed, self.policy,
                          valid_until=self.expiry)
        changed["facts"]["portfolio.nav"]["value"] = "1000"
        changed["facts"]["portfolio.nav"]["observed_at"] = "2026-09-25T11:00:00+00:00"
        with self.assertRaisesRegex(MachineError, "stale"):
            commit_basket(self.request, self.verdict, changed, self.policy,
                          valid_until=self.expiry)

    def test_plan_lifetime_and_state_time_are_bounded(self):
        with self.assertRaisesRegex(MachineError, "maximum lifetime"):
            commit_basket(self.request, self.verdict, self.state, self.policy,
                          valid_until="2026-09-25T12:07:00+00:00")
        changed = copy.deepcopy(self.state)
        changed["as_of"] = "2026-09-25T12:02:00+00:00"
        with self.assertRaisesRegex(MachineError, "snapshot mismatch"):
            commit_basket(self.request, self.verdict, changed, self.policy,
                          valid_until=self.expiry)
        changed = copy.deepcopy(self.request)
        changed["max_age_ms"] = 120000
        with self.assertRaisesRegex(MachineError, "expires before"):
            commit_basket(changed, select_portfolio(changed), self.state, self.policy,
                          valid_until=self.expiry)
        fresh_nav = copy.deepcopy(self.state)
        fresh_nav["facts"]["portfolio.nav"]["observed_at"] = changed["as_of"]
        with self.assertRaisesRegex(MachineError, "portfolio input expires before"):
            commit_basket(changed, select_portfolio(changed), fresh_nav, self.policy,
                          valid_until=self.expiry)


if __name__ == "__main__":
    unittest.main()
