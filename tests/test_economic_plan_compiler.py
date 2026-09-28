"""Complete-grid plan compilation, replay and abstention tests."""

import copy
import unittest
from decimal import Decimal

from economic_machine.plan_compiler import (
    REQUEST_VERSION, compare_plans, compile_plan_intent,
    verify_comparison, verify_plan_intent,
)
from economic_machine.snapshot_assembly import SnapshotAssembler
from economic_machine.values import MachineError
from test_economic_mandate import confirmed, fixture as mandate_fixture
from test_economic_tron_cashflow import quote, rate, sources, vault_quote
from test_economic_tron_sources import AT, CONFIG, modify


def mandate():
    raw = mandate_fixture()
    raw["scope"]["network"] = "tron-mainnet"
    raw["terms"]["withdrawals"] = []
    return raw


def template(product_id, *, maximum=4000, daily=25, stress=100, quoted=None):
    return {"product_id": product_id, "max_bps": maximum, "current_bps": 0,
            "daily_loss_bps": daily,
            "stress_loss_bps": {"market": stress},
            "quote": copy.deepcopy(quoted or quote(product_id))}


def request(snapshot, templates=None, *, step=2000, distance=2000):
    return {"schema_version": REQUEST_VERSION,
            "snapshot_hash": snapshot["snapshot_hash"],
            "grid_step_bps": step, "min_plan_distance_bps": distance,
            "max_plan_age_seconds": 300, "scenarios": ["market"],
            "product_templates": templates or [
                template("justlend.v1.jUSDT", stress=100),
                template("justlend.v1.jUSDD", daily=100, stress=400),
                template("usdd.vault.TRX-A", stress=1000, quoted=vault_quote()),
            ]}


class PlanCompilerTests(unittest.TestCase):
    def setUp(self):
        self.assembler = SnapshotAssembler(CONFIG)
        self.captures = sources()
        modify(self.captures, "justlend_markets_v1",
               lambda payload: payload["data"]["tokenList"][1].update(supplyRate="0.08"))
        self.snapshot = self.assembler.assemble(self.captures, as_of=AT)
        self.raw = mandate()
        self.record = confirmed(self.raw)
        self.request = request(self.snapshot)

    def compare(self, raw=None, snapshot=None, request_value=None, at=AT):
        return compare_plans(confirmed(raw or self.raw), snapshot or self.snapshot,
                             request_value or self.request,
                             assembler=self.assembler, at=at)

    def test_two_plans_are_deterministic_complete_and_policy_compliant(self):
        first = self.compare()
        self.assertEqual(first, self.compare())
        self.assertEqual(first["status"], "COMPARISON_READY")
        self.assertEqual(first["enumerated_candidates"], 56)
        self.assertTrue(first["complete_enumeration"])
        self.assertEqual([plan["name"] for plan in first["plans"]],
                         ["CONSERVATIVE", "GROWTH"])
        self.assertTrue(all(plan["cash_bps"] >= 3000 for plan in first["plans"]))
        self.assertLessEqual(max(Decimal(plan["principal_base"])
                                 for plan in first["plans"]), Decimal("7000"))
        self.assertEqual(first["execution_authority"], "NONE")
        self.assertEqual(first["chain_status"], "NOT_SUBMITTED")
        self.assertEqual(first["assumption_status"],
                         "EXPLICIT_UNVERIFIED_MARKET_AND_RISK_ASSUMPTIONS")

    def test_vault_is_visible_with_exact_exclusion_reason(self):
        result = self.compare()
        self.assertEqual(result["vault_comparison"]["universe"], ["usdd.vault.TRX-A"])
        self.assertEqual(result["vault_comparison"]["eligible_candidate_count"], 0)
        self.assertGreater(result["product_exclusions"]["usdd.vault.TRX-A"]
                           ["BORROWING_NOT_CONSENTED"], 0)

    def test_one_product_and_cash_can_still_produce_two_plans(self):
        req = request(self.snapshot, [template("justlend.v1.jUSDT")])
        result = self.compare(request_value=req)
        self.assertEqual(result["status"], "COMPARISON_READY")
        self.assertEqual(result["enumerated_candidates"], 6)
        self.assertEqual([p["weights_bps"]["justlend.v1.jUSDT"] for p in result["plans"]],
                         [2000, 4000])

    def test_no_two_plans_abstains_and_explains_adjustable_condition(self):
        req = request(self.snapshot,
                      [template("justlend.v1.jUSDT", maximum=2000)], distance=4000)
        result = self.compare(request_value=req)
        self.assertEqual(result["status"], "NO_TWO_VIABLE_PLANS")
        self.assertEqual(result["plans"], [])
        self.assertIn("RAISE_PRODUCT_CAP_OR_ADD_ANOTHER_PRODUCT",
                      result["adjustable_condition_hints"])

    def test_grid_budget_and_untrusted_score_are_rejected(self):
        too_large = copy.deepcopy(self.request)
        too_large["grid_step_bps"] = 100
        with self.assertRaisesRegex(MachineError, "enumeration budget"):
            self.compare(request_value=too_large)
        scored = copy.deepcopy(self.request)
        scored["product_templates"][0]["llm_score"] = 0.99
        with self.assertRaisesRegex(MachineError, "requires exactly"):
            self.compare(request_value=scored)

    def test_daily_loss_limit_is_enforced_separately_from_stress(self):
        req = request(self.snapshot, [
            template("justlend.v1.jUSDT", daily=1000, stress=0)])
        result = self.compare(request_value=req)
        self.assertIn("DAILY_LOSS_LIMIT", result["exclusion_histogram"])
        self.assertIn("RAISE_DAILY_LOSS_LIMIT_OR_USE_LOWER_RISK_PRODUCTS",
                      result["adjustable_condition_hints"])

    def test_trx_products_are_rejected_by_zero_price_exposure_policy(self):
        native = quote("tron.native.stake")
        native["native"] = {"voting_rate": rate("0.04", "SIMPLE_APR"),
                            "lock_remaining_seconds": 0, "resource_recovery_seconds": 0,
                            "window": None, "rental": None}
        req = request(self.snapshot, [template("tron.native.stake", quoted=native)])
        result = self.compare(request_value=req)
        reasons = result["product_exclusions"]["tron.native.stake"]
        self.assertTrue("PRICE_EXPOSURE_LIMIT:TRX" in reasons or "ACTION_NOT_ALLOWED" in reasons)
        self.assertEqual(result["plans"], [])

    def test_vault_collateral_amount_uses_trx_price_not_base_asset(self):
        raw = copy.deepcopy(self.raw)
        raw["terms"]["borrowing"].update(
            consent=True, max_debt={"asset": "USDT", "amount": "2500"})
        raw["terms"]["allowed_actions"].append("MINT_USDD")
        raw["terms"]["protocol_caps_bps"]["usdd"] = 5000
        raw["terms"]["price_exposure_caps_bps"]["TRX"] = 5000
        raw["terms"]["price_exposure_caps_bps"]["USDD"] = 5000
        vault = vault_quote()
        vault["principal"] = "8000"
        req = request(self.snapshot, [template("usdd.vault.TRX-A", quoted=vault)])
        result = self.compare(raw=raw, request_value=req)
        self.assertEqual(result["status"], "COMPARISON_READY")
        self.assertEqual([p["cashflows"][0]["principal_asset"] for p in result["plans"]],
                         ["TRX", "TRX"])
        self.assertEqual([p["cashflows"][0]["principal_amount"] for p in result["plans"]],
                         ["8000", "16000"])
        self.assertEqual([p["cashflows"][0]["detail"]["stored_debt_usdd"]
                          for p in result["plans"]], ["1000", "2000"])

    def test_strx_exit_quantities_scale_with_candidate_principal(self):
        raw = copy.deepcopy(self.raw)
        raw["terms"]["allowed_actions"].append("STAKE")
        raw["terms"]["price_exposure_caps_bps"]["TRX"] = 5000
        strx = quote("justlend.strx")
        strx["exit_tranches"] = [{"amount": "1000", "after_seconds": 30}]
        req = request(self.snapshot, [template("justlend.strx", quoted=strx)])
        result = self.compare(raw=raw, request_value=req)
        self.assertEqual(result["status"], "COMPARISON_READY")
        for plan in result["plans"]:
            row = plan["cashflows"][0]
            scaled = plan["cashflow_assumptions"]["quotes"][0]["exit_tranches"]
            self.assertEqual(scaled, [{"amount": row["principal_amount"],
                                       "after_seconds": 30}])

    def test_strx_scaling_cannot_repair_a_malformed_basis_quote(self):
        strx = quote("justlend.strx")
        strx["exit_tranches"] = [{"amount": "999", "after_seconds": 30}]
        req = request(self.snapshot, [template("justlend.strx", quoted=strx)])
        with self.assertRaisesRegex(MachineError, "conserve principal"):
            self.compare(request_value=req)

    def test_snapshot_fee_debt_and_revision_changes_break_replay(self):
        result = self.compare()
        self.assertTrue(verify_comparison(result, self.record, self.snapshot, self.request,
                                          assembler=self.assembler, at=AT))
        forged_snapshot = copy.deepcopy(self.snapshot)
        forged_snapshot["facts"]["justlend.v1.jUSDT.supply_apy"]["value"] = "0.99"
        self.assertFalse(verify_comparison(result, self.record, forged_snapshot, self.request,
                                           assembler=self.assembler, at=AT))
        fee = copy.deepcopy(self.request)
        fee["product_templates"][0]["quote"]["costs_base"]["entry"] = "2"
        self.assertFalse(verify_comparison(result, self.record, self.snapshot, fee,
                                           assembler=self.assembler, at=AT))
        debt = copy.deepcopy(self.request)
        debt["product_templates"][2]["quote"]["vault"]["stored_debt_usdd"] = "1001"
        self.assertFalse(verify_comparison(result, self.record, self.snapshot, debt,
                                           assembler=self.assembler, at=AT))
        revised = copy.deepcopy(self.raw)
        revised["revision"] = 2
        self.assertFalse(verify_comparison(result, confirmed(revised), self.snapshot, self.request,
                                           assembler=self.assembler, at=AT))

    def test_intent_replays_selected_plan_without_execution_authority(self):
        comparison = self.compare()
        intent = compile_plan_intent(
            comparison, self.record, self.snapshot, self.request,
            selected_plan="GROWTH", assembler=self.assembler, at=AT,
            valid_until="2026-09-28T12:04:00Z")
        self.assertEqual(intent["status"], "INTENT_PREPARED")
        self.assertEqual(intent["execution_authority"], "NONE")
        self.assertEqual(intent["signature_status"], "NOT_REQUESTED")
        self.assertEqual(intent["chain_status"], "NOT_SUBMITTED")
        self.assertIn("PR06_TRANSACTION_GRAPH_REQUIRED", intent["blockers"])
        self.assertIn("PR07_WALLET_SIGNATURE_REQUIRED", intent["blockers"])
        self.assertIn("LIVE_QUOTE_AND_RISK_ASSUMPTIONS_REQUIRED", intent["blockers"])
        self.assertTrue(verify_plan_intent(
            intent, comparison, self.record, self.snapshot, self.request,
            assembler=self.assembler, at=AT))
        intent["cash_amount"] = "9999"
        self.assertFalse(verify_plan_intent(
            intent, comparison, self.record, self.snapshot, self.request,
            assembler=self.assembler, at=AT))

    def test_stale_snapshot_is_rejected_even_before_any_viable_leg(self):
        req = request(self.snapshot, [template("justlend.v1.jUSDT", maximum=0)])
        with self.assertRaisesRegex(MachineError, "stale"):
            self.compare(request_value=req, at="2026-09-28T12:16:00Z")


if __name__ == "__main__":
    unittest.main()
