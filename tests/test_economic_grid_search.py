"""A grid optimum requires complete, bounded enumeration of the defined grid."""

import copy
import json
import unittest
from pathlib import Path

from economic_machine.basket import commit_basket, verify_basket
from economic_machine.grid_search import search_grid, verify_grid
from economic_machine.values import MachineError


CASE = Path(__file__).resolve().parents[1] / "cases" / "economic_portfolio_grid_demo.json"


class GridSearchTests(unittest.TestCase):
    def setUp(self):
        self.request = json.loads(CASE.read_text(encoding="utf-8"))

    def test_complete_grid_has_replayable_bounded_optimum(self):
        result = search_grid(self.request)
        self.assertEqual(result["enumerated_candidates"], 231)
        self.assertTrue(result["complete_enumeration"])
        self.assertEqual(result["portfolio_verdict"]["status"], "SELECTED")
        self.assertEqual(result["portfolio_verdict"]["selection_scope"],
                         "SUPPLIED_CANDIDATES_ONLY")
        selected = next(item for item in result["portfolio_verdict"]["evaluated"]
                        if item["candidate_id"] == result["portfolio_verdict"]["selected_candidate_id"])
        self.assertEqual(selected["weights_bps"], {"lend_A": 3000, "stable_B": 6000})
        self.assertEqual((selected["net_expected_bps"], selected["worst_stress_loss"]),
                         ("474", "150"))
        self.assertEqual(result["execution_authority"], "NONE")
        self.assertTrue(verify_grid(result, self.request))
        for candidate in result["selection_request"]["candidates"]:
            weights = candidate["weights_bps"]
            self.assertLessEqual(sum(weights.values()), 10000)
            self.assertTrue(all(weight % 500 == 0 for weight in weights.values()))

    def test_grid_too_large_refuses_incomplete_optimum(self):
        self.request["grid_step_bps"] = 100
        with self.assertRaisesRegex(MachineError, "complete enumeration budget"):
            search_grid(self.request)

    def test_mutation_of_assumptions_or_claim_breaks_replay(self):
        result = search_grid(self.request)
        changed = copy.deepcopy(self.request)
        changed["products"][0]["expected_return_bps"] = 801
        self.assertFalse(verify_grid(result, changed))
        changed = copy.deepcopy(result)
        changed["complete_enumeration"] = False
        self.assertFalse(verify_grid(changed, self.request))
        changed = copy.deepcopy(result)
        changed["portfolio_verdict"]["selected_candidate_id"] = "grid_0000"
        self.assertFalse(verify_grid(changed, self.request))

    def test_grid_result_can_be_bound_to_state_and_policy_without_execution(self):
        result = search_grid(self.request)
        state = json.loads((CASE.parent / "economic_basket_state_demo.json").read_text())
        policy = json.loads((CASE.parent / "economic_basket_policy_demo.json").read_text())
        commitment = commit_basket(result["selection_request"],
                                   result["portfolio_verdict"], state, policy,
                                   valid_until="2026-09-25T12:03:00+00:00")
        self.assertTrue(verify_basket(commitment, result["selection_request"],
                                      state, policy))
        self.assertEqual(commitment["execution_status"], "NO_BASKET_EXECUTOR")


if __name__ == "__main__":
    unittest.main()
