"""Offline ABI-word binding for the non-custodial TRON basket registry."""

import copy
import json
import unittest
from pathlib import Path

from economic_machine.basket import commit_basket
from economic_machine.chain_binding import prepare_chain_binding, verify_chain_binding
from economic_machine.portfolio import select_portfolio
from economic_machine.values import MachineError


CASES = Path(__file__).resolve().parents[1] / "cases"


class ChainBindingTests(unittest.TestCase):
    def setUp(self):
        def read(name):
            return json.loads((CASES / name).read_text(encoding="utf-8"))
        self.request = read("economic_portfolio_candidates_demo.json")
        self.state = read("economic_basket_state_demo.json")
        self.policy = read("economic_basket_policy_demo.json")
        self.context = read("economic_basket_registry_context_demo.json")
        self.commitment = commit_basket(self.request, select_portfolio(self.request),
                                        self.state, self.policy,
                                        valid_until="2026-09-25T12:03:00+00:00")

    def bind(self):
        return prepare_chain_binding(self.commitment, self.request, self.state,
                                     self.policy, self.context)

    def test_fixed_width_binding_is_deterministic_and_prepare_only(self):
        result = self.bind()
        self.assertEqual(result, self.bind())
        self.assertEqual(result["binding_hash"],
                         "0ea599b2d24aec4af019a9c10726ef592e8e43ae206900566845a56dbc928fcc")
        self.assertEqual(result["amount_base_units"], "1000000000")
        self.assertEqual(result["registry_state"], "NOT_QUERIED")
        self.assertEqual(result["transaction_status"], "NOT_BUILT")
        self.assertEqual(result["execution_authority"], "NONE")
        self.assertTrue(verify_chain_binding(result, self.commitment, self.request,
                                             self.state, self.policy, self.context))

    def test_chain_address_policy_or_basket_mutation_changes_binding(self):
        first = self.bind()
        for field, value in (("chain_id", 777000002),
                             ("registry_address", "4" * 40),
                             ("asset_address", "5" * 40),
                             ("target_address", "6" * 40)):
            changed = copy.deepcopy(self.context)
            changed[field] = value
            self.assertFalse(verify_chain_binding(first, self.commitment,
                                                  self.request, self.state,
                                                  self.policy, changed))
            self.assertNotEqual(first["binding_hash"], prepare_chain_binding(
                self.commitment, self.request, self.state, self.policy, changed)["binding_hash"])
        changed = copy.deepcopy(self.commitment)
        changed["basket"]["cash_amount"] = "101"
        with self.assertRaisesRegex(MachineError, "does not replay"):
            prepare_chain_binding(changed, self.request, self.state, self.policy,
                                  self.context)
        changed = copy.deepcopy(self.policy)
        changed["max_capital"] = "1100"
        self.assertFalse(verify_chain_binding(first, self.commitment,
                                              self.request, self.state,
                                              changed, self.context))

    def test_fractional_base_unit_and_subsecond_expiry_are_rejected(self):
        changed = copy.deepcopy(self.request)
        changed["capital"] = "1000.0000001"
        changed["max_stress_loss"] = "200"
        state = copy.deepcopy(self.state)
        state["facts"]["portfolio.nav"]["value"] = changed["capital"]
        policy = copy.deepcopy(self.policy)
        policy["max_capital"] = "2000"
        commitment = commit_basket(changed, select_portfolio(changed), state,
                                   policy, valid_until="2026-09-25T12:03:00+00:00")
        with self.assertRaisesRegex(MachineError, "exact token-unit"):
            prepare_chain_binding(commitment, changed, state, policy, self.context)
        subsecond = commit_basket(self.request, select_portfolio(self.request),
                                  self.state, self.policy,
                                  valid_until="2026-09-25T12:03:00.000001+00:00")
        with self.assertRaisesRegex(MachineError, "whole second"):
            prepare_chain_binding(subsecond, self.request, self.state,
                                  self.policy, self.context)


if __name__ == "__main__":
    unittest.main()
