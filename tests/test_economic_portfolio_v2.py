"""Versioned factor and signed-scenario portfolio decisions stay replayable."""

import copy
import json
import unittest
from pathlib import Path

from economic_machine.basket import commit_basket, verify_basket
from economic_machine.grid_search import search_grid, verify_grid
from economic_machine.portfolio import select_portfolio
from economic_machine.values import MachineError


CASES = Path(__file__).resolve().parents[1] / "cases"


class HedgedPortfolioTests(unittest.TestCase):
    def setUp(self):
        self.request = json.loads((CASES / "economic_portfolio_hedged_demo.json").read_text())

    def test_common_factor_and_signed_scenarios_change_selection(self):
        result = select_portfolio(self.request)
        self.assertEqual(result["schema_version"], "economic-portfolio-verdict-2")
        self.assertEqual(result["selected_candidate_id"], "hedged")
        book = {item["candidate_id"]: item for item in result["evaluated"]}
        hedged = book["hedged"]
        self.assertEqual(hedged["scenario_pnl_bps"], {
            "TRX_CRASH": "-215", "STABLE_DEPEG": "-805", "FUNDING_SPIKE": "-550"})
        self.assertEqual(hedged["worst_stress_loss"], "81.65")
        self.assertEqual(hedged["factor_net_exposure_bps"]["TRX"], "0")
        self.assertEqual(hedged["factor_gross_exposure_bps"]["TRX"], "7000")
        self.assertEqual(hedged["net_expected_bps"], "230.5")
        self.assertEqual(book["naked"]["reason_codes"],
                         ["FACTOR_NET_BOUNDS", "STRESS_LOSS_CAP"])
        self.assertEqual(book["overcrowded_hedge"]["reason_codes"], ["FACTOR_GROSS_CAP"])
        self.assertEqual((result["execution_authority"], result["chain_status"]),
                         ("NONE", "NOT_SUBMITTED"))

    def test_new_funding_shock_abstains_and_cannot_replay_old_choice(self):
        first = select_portfolio(self.request)
        changed = copy.deepcopy(self.request)
        changed["products"][1]["scenario_pnl_bps"]["FUNDING_SPIKE"] = -3000
        second = select_portfolio(changed)
        self.assertEqual(second["status"], "ABSTAIN")
        self.assertIsNone(second["selected_candidate_id"])
        self.assertNotEqual(first["verdict_hash"], second["verdict_hash"])
        self.assertIn("STRESS_LOSS_CAP", next(item for item in second["evaluated"]
                                               if item["candidate_id"] == "hedged")["reason_codes"])

    def test_missing_factors_float_and_reversed_bounds_fail_closed(self):
        changed = copy.deepcopy(self.request)
        del changed["products"][0]["factor_loadings_bps"]["TRX"]
        with self.assertRaisesRegex(MachineError, "every factor loading"):
            select_portfolio(changed)
        changed = copy.deepcopy(self.request)
        changed["products"][0]["scenario_pnl_bps"]["TRX_CRASH"] = -5000.0
        with self.assertRaises(MachineError):
            select_portfolio(changed)
        changed = copy.deepcopy(self.request)
        changed["factor_net_bounds_bps"]["TRX"] = {"min": 3000, "max": 2000}
        with self.assertRaisesRegex(MachineError, "reversed"):
            select_portfolio(changed)
        changed = copy.deepcopy(self.request)
        changed["products"][1]["observed_at"] = "2026-09-25T11:30:00+00:00"
        with self.assertRaisesRegex(MachineError, "allowed skew"):
            select_portfolio(changed)

    def test_complete_grid_and_basket_replay_include_factor_model(self):
        grid_request = copy.deepcopy(self.request)
        grid_request["schema_version"] = "economic-portfolio-grid-search-2"
        del grid_request["candidates"]
        grid_request["grid_step_bps"] = 2500
        result = search_grid(grid_request)
        self.assertTrue(result["complete_enumeration"])
        self.assertEqual(result["enumerated_candidates"], 35)
        self.assertEqual(result["portfolio_verdict"]["status"], "SELECTED")
        self.assertTrue(verify_grid(result, grid_request))
        state = json.loads((CASES / "economic_basket_state_demo.json").read_text())
        policy = json.loads((CASES / "economic_basket_policy_demo.json").read_text())
        policy["allowed_products"] = sorted(item["product_id"] for item in self.request["products"])
        commitment = commit_basket(result["selection_request"], result["portfolio_verdict"],
                                   state, policy, valid_until="2026-09-25T12:03:00+00:00")
        self.assertTrue(verify_basket(commitment, result["selection_request"], state, policy))
        self.assertEqual(commitment["execution_status"], "NO_BASKET_EXECUTOR")
        changed = copy.deepcopy(grid_request)
        changed["factor_gross_caps_bps"]["TRX"] = 6000
        self.assertFalse(verify_grid(result, changed))
        changed_selection = copy.deepcopy(result["selection_request"])
        changed_selection["products"][1]["scenario_pnl_bps"]["TRX_CRASH"] = 4000
        self.assertFalse(verify_basket(commitment, changed_selection, state, policy))


if __name__ == "__main__":
    unittest.main()
