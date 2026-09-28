"""TRON-specific yield math uses synthetic inputs and grants no trade rights."""

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from economic_machine.cli import main
from economic_machine.tron_yield import plan_tron_yield, verify_tron_yield_plan
from economic_machine.values import MachineError


CASE = (Path(__file__).resolve().parents[1] /
        "cases" / "economic_tron_yield_demo.json")


class TronYieldTests(unittest.TestCase):
    def setUp(self):
        self.request = json.loads(CASE.read_text())
        self.book = {item["product_id"]: item for item in self.request["opportunities"]}

    def test_plans_compare_justlend_usdd_energy_and_vault(self):
        result = plan_tron_yield(self.request)
        self.assertEqual(result["status"], "PLANS_COMPARED")
        self.assertEqual(result["required_kind_gaps"], [])
        self.assertEqual(len(result["plans"]), 3)
        self.assertEqual([item["name"] for item in result["plans"]],
                         ["CONSERVATIVE", "GROWTH", "USDD_VAULT_COMPARISON"])
        self.assertEqual(result["vault_comparison_status"], "INCLUDED_IN_PLANS")
        self.assertGreater(result["plans"][2]["weights_bps"]["usdd_vault"], 0)
        self.assertNotEqual(result["plans"][0]["weights_bps"],
                            result["plans"][1]["weights_bps"])
        self.assertEqual(result["rate_method"],
                         "SIMPLE_ANNUALIZED_LINEAR_HORIZON_PROXY")
        self.assertEqual(result["execution_authority"], "NONE")
        self.assertEqual(result["chain_status"], "NOT_SUBMITTED")
        details = {item["product_id"]: item for item in result["opportunity_details"]}
        self.assertEqual(details["justlend_usdd"]["reward_net_bps"], 300)
        self.assertEqual(details["justlend_usdd"]["annualized_rate_bps"], 500)
        self.assertEqual(details["justlend_strx"]["annualized_rate_bps"], 600)
        vault = details["usdd_vault"]["vault_risk"]
        self.assertEqual(vault["net_rate_on_collateral_bps"], 760)
        self.assertEqual(vault["deployed_value_usdt"], "400")
        self.assertEqual(vault["current_collateral_ratio_bps"], "25000")
        self.assertEqual(vault["stressed_collateral_ratios_bps"]["trx_down"], "17500")
        self.assertTrue(verify_tron_yield_plan(result, self.request))
        altered = copy.deepcopy(result)
        altered["plans"][0]["horizon_net_return_bps"] = "9999"
        self.assertFalse(verify_tron_yield_plan(altered, self.request))

    def test_vault_principal_accrued_fee_and_price_must_reconcile(self):
        terms = self.book["usdd_vault"]["vault_terms"]
        terms["debt_value_usdt"] = "440"
        with self.assertRaisesRegex(MachineError, "vault debt must equal"):
            plan_tron_yield(self.request)
        terms["accrued_fee_usdd"] = "40"
        result = plan_tron_yield(self.request)
        detail = next(item for item in result["opportunity_details"]
                      if item["product_id"] == "usdd_vault")
        self.assertEqual(detail["vault_risk"]["net_rate_on_collateral_bps"], 756)
        self.assertEqual(detail["vault_risk"]["deployed_value_usdt"], "400")
        terms["usdd_price_usdt"] = "0.99"
        with self.assertRaisesRegex(MachineError, "vault debt must equal"):
            plan_tron_yield(self.request)

    def test_vault_liquidation_and_debt_cap_withhold_only_that_opportunity(self):
        self.book["usdd_vault"]["vault_terms"][
            "collateral_shock_bps_by_scenario"]["trx_down"] = -5000
        result = plan_tron_yield(self.request)
        self.assertIn({"product_id": "usdd_vault",
                       "reason": "VAULT_LIQUIDATION_SCENARIO",
                       "scenario": "trx_down"},
                      result["excluded_opportunities"])
        self.assertEqual(result["status"], "PLANS_COMPARED")
        self.assertEqual(result["vault_comparison_status"], "VAULT_OPPORTUNITY_EXCLUDED")
        self.book["usdd_vault"]["vault_terms"][
            "collateral_shock_bps_by_scenario"]["trx_down"] = -3000
        self.request["max_vault_debt_to_collateral_bps"] = 3000
        result = plan_tron_yield(self.request)
        self.assertIn({"product_id": "usdd_vault", "reason": "VAULT_DEBT_CAP"},
                      result["excluded_opportunities"])

    def test_vault_scenario_cannot_hide_collateral_or_debt_loss(self):
        self.book["usdd_vault"]["scenario_pnl_bps"]["trx_down"] = -2000
        with self.assertRaisesRegex(MachineError, "vault scenario PnL understates"):
            plan_tron_yield(self.request)
        self.book["usdd_vault"]["scenario_pnl_bps"]["trx_down"] = -3500
        self.book["usdd_vault"]["scenario_pnl_bps"]["usdd_depeg"] = 0
        with self.assertRaisesRegex(MachineError, "vault scenario PnL understates"):
            plan_tron_yield(self.request)

    def test_energy_exit_and_strx_aggregate_cannot_be_misrepresented(self):
        self.book["trx_energy"]["withdrawal_days"] = 1
        with self.assertRaisesRegex(MachineError, "understates chain delay"):
            plan_tron_yield(self.request)
        self.book["trx_energy"]["withdrawal_days"] = 15
        self.book["justlend_strx"]["annualized_components_bps"]["energy_rental"] = 100
        with self.assertRaisesRegex(MachineError, "double-counted"):
            plan_tron_yield(self.request)

    def test_coverage_and_unknown_rates_abstain(self):
        self.request["opportunities"] = [
            item for item in self.request["opportunities"]
            if item["kind"] != "USDD_VAULT_STRATEGY"]
        result = plan_tron_yield(self.request)
        self.assertEqual(result["status"], "ABSTAIN")
        self.assertEqual(result["required_kind_gaps"], ["USDD_VAULT_STRATEGY"])
        self.assertEqual(result["plans"], [])
        self.assertEqual(result["vault_comparison_status"],
                         "MISSING_REQUIRED_OPPORTUNITY")
        self.assertEqual(result["execution_authority"], "NONE")

    def test_cli_plan_and_replay(self):
        with tempfile.TemporaryDirectory(prefix="tron-yield-cli-") as folder:
            case = Path(folder) / "request.json"
            output = Path(folder) / "result.json"
            case.write_text(json.dumps(self.request))
            stream = io.StringIO()
            with patch.object(sys, "argv", ["economic-machine", "tron-yield-plan",
                                            "--request", str(case)]):
                with contextlib.redirect_stdout(stream):
                    main()
            result = json.loads(stream.getvalue())
            self.assertEqual(result, plan_tron_yield(self.request))
            output.write_text(stream.getvalue())
            stream = io.StringIO()
            with patch.object(sys, "argv", ["economic-machine", "tron-yield-verify",
                                            "--request", str(case),
                                            "--observation", str(output)]):
                with contextlib.redirect_stdout(stream):
                    main()
            self.assertEqual(json.loads(stream.getvalue()), {"verified_replay": True})
