"""Bounded portfolio construction under explicit stress and authority limits."""

import copy
import json
import unittest
from decimal import Decimal, localcontext
from pathlib import Path

from economic_machine.portfolio import select_portfolio
from economic_machine.values import MachineError


CASE = Path(__file__).resolve().parents[1] / "cases" / "economic_portfolio_candidates_demo.json"


class PortfolioSelectionTests(unittest.TestCase):
    def setUp(self):
        self.request = json.loads(CASE.read_text(encoding="utf-8"))

    def test_stress_constraint_changes_selection_and_never_authorizes_trade(self):
        first = select_portfolio(self.request)
        self.assertEqual(first, select_portfolio(self.request))
        self.assertEqual(first["status"], "SELECTED")
        self.assertEqual(first["selected_candidate_id"], "balanced")
        self.assertEqual((first["execution_authority"], first["chain_status"]),
                         ("NONE", "NOT_SUBMITTED"))
        evaluated = {item["candidate_id"]: item for item in first["evaluated"]}
        self.assertEqual(evaluated["aggressive"]["reason_codes"], ["STRESS_LOSS_CAP"])
        self.assertEqual(evaluated["balanced"]["worst_stress_loss"], "130")
        self.assertEqual(evaluated["balanced"]["net_expected_bps"], "434.5")
        constrained = copy.deepcopy(self.request)
        constrained["group_caps_bps"]["LEND"] = 1000
        second = select_portfolio(constrained)
        self.assertEqual(second["selected_candidate_id"], "defensive")
        self.assertIn("GROUP_CAP", {item["candidate_id"]: item for item in
                                     second["evaluated"]}["balanced"]["reason_codes"])
        self.assertNotEqual(first["verdict_hash"], second["verdict_hash"])

    def test_all_candidates_ineligible_abstains(self):
        self.request["max_stress_loss"] = "50"
        result = select_portfolio(self.request)
        self.assertEqual(result["status"], "ABSTAIN")
        self.assertIsNone(result["selected_candidate_id"])
        self.assertTrue(all(not candidate["eligible"] for candidate in result["evaluated"]))

    def test_stale_floating_or_incomplete_assumptions_fail_closed(self):
        stale = copy.deepcopy(self.request)
        stale["products"][0]["observed_at"] = "2026-09-25T10:00:00+00:00"
        with self.assertRaisesRegex(MachineError, "stale"):
            select_portfolio(stale)
        floating = copy.deepcopy(self.request)
        floating["candidates"][0]["weights_bps"]["lend_A"] = 0.6
        with self.assertRaises(MachineError):
            select_portfolio(floating)
        incomplete = copy.deepcopy(self.request)
        del incomplete["products"][0]["stress_loss_bps"]["DEPEG"]
        with self.assertRaisesRegex(MachineError, "every stress scenario"):
            select_portfolio(incomplete)

    def test_deterministic_tie_break_and_candidate_scope(self):
        self.request["candidates"] = [
            {"candidate_id": "z", "weights_bps": {"lend_A": 2000, "stable_B": 7000}},
            {"candidate_id": "a", "weights_bps": {"lend_A": 2000, "stable_B": 7000}},
        ]
        result = select_portfolio(self.request)
        self.assertEqual(result["selected_candidate_id"], "a")
        self.assertEqual(result["selection_scope"], "SUPPLIED_CANDIDATES_ONLY")

    def test_large_capital_keeps_exact_stress_arithmetic(self):
        capital = "9" * 70
        self.request["capital"] = capital
        self.request["max_stress_loss"] = capital
        result = select_portfolio(self.request)
        balanced = {item["candidate_id"]: item for item in result["evaluated"]}["balanced"]
        with localcontext() as context:
            context.prec = 256
            expected = Decimal(capital) * Decimal(13) / Decimal(100)
        self.assertEqual(Decimal(balanced["worst_stress_loss"]), expected)


if __name__ == "__main__":
    unittest.main()
