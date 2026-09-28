"""Market observations and model assumptions enter the optimizer separately."""

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from economic_machine.basket import commit_basket, verify_basket
from economic_machine.cli import main
from economic_machine.product_adapter import assemble_portfolio_inputs, verify_portfolio_inputs
from economic_machine.values import MachineError, digest


CASE = Path(__file__).resolve().parents[1] / "cases" / "economic_portfolio_hedged_demo.json"
TEMPLATE_CASE = CASE.parent / "economic_portfolio_template_demo.json"
BUNDLE_CASE = CASE.parent / "economic_product_bundle_demo.json"


class ProductAdapterTests(unittest.TestCase):
    def setUp(self):
        request = json.loads(CASE.read_text())
        products = {item["product_id"]: item for item in request.pop("products")}
        request["schema_version"] = "economic-portfolio-template-1"
        self.template = request
        kinds = {"tron_lend": "LENDING", "tron_short": "PERP", "stable_yield": "LENDING"}
        capacities = {"tron_lend": "600", "tron_short": "500", "stable_yield": "800"}
        markets = {
            "tron_lend": {"deposit_asset": "USDT", "supply_apy_bps": 600,
                          "entry_fee_bps": 5, "exit_fee_bps": 5,
                          "redemption_days": 2, "redemption_enabled": True,
                          "utilization_bps": 8000},
            "tron_short": {"mark_price": "10", "index_price": "10",
                           "price_unit": "USDT/TRX", "spread_bps": 10,
                           "fee_bps": 5, "impact_bps": 5, "funding_bps_per_8h": -2},
            "stable_yield": {"deposit_asset": "USDT", "supply_apy_bps": 300,
                             "entry_fee_bps": 2, "exit_fee_bps": 3,
                             "redemption_days": 1, "redemption_enabled": True,
                             "utilization_bps": 5000},
        }
        manifests, observations, assumptions = [], [], []
        for name, product in products.items():
            kind = kinds[name]
            manifest = {"schema_version": "economic-product-adapter-1", "product_id": name,
                        "network": request["network"], "asset": request["asset"],
                        "base_asset": "TRX" if kind == "PERP" else "USDT",
                        "quote_asset": "USDT",
                        "group": product["group"], "kind": kind,
                        "max_bps": product["max_bps"],
                        "min_liquidity_days": product["liquidity_days"]}
            if kind == "PERP":
                manifest["max_basis_bps"] = 100
            observed = {"schema_version": "economic-product-observation-1",
                        "product_id": name, "network": request["network"],
                        "asset": request["asset"], "kind": kind,
                        "base_asset": manifest["base_asset"], "quote_asset": "USDT",
                        "notional_unit": "USDT", "market_status": "ACTIVE",
                        "source_id": "test_feed", "source_record_hash": "a" * 64,
                        "observed_at": "2026-09-25T12:00:00+00:00",
                        "valid_until": "2026-09-25T12:02:00+00:00",
                        "tradable_notional": capacities[name], "market": markets[name]}
            assumption = {"schema_version": "economic-product-assumptions-1",
                          "product_id": name, "model_id": "risk_model_demo",
                          "model_hash": "e" * 64, "observation_hash": digest(observed),
                          "generated_at": "2026-09-25T12:00:30+00:00",
                          "valid_until": "2026-09-25T12:02:00+00:00",
                          "expected_return_bps": product["expected_return_bps"],
                          "scenario_pnl_bps": product["scenario_pnl_bps"],
                          "factor_loadings_bps": product["factor_loadings_bps"]}
            manifests.append(manifest)
            observations.append(observed)
            assumptions.append(assumption)
        self.bundle = {"schema_version": "economic-product-bundle-1",
                       "manifests": manifests, "observations": observations,
                       "assumptions": assumptions,
                       "positions": {"schema_version": "economic-position-snapshot-1",
                                     "owner_id": request["owner_id"],
                                     "network": request["network"], "asset": request["asset"],
                                     "observed_at": "2026-09-25T12:00:00+00:00",
                                     "source_id": "test_portfolio",
                                     "source_record_hash": "d" * 64,
                                     "current_weights_bps": {name: 0 for name in products}}}

    def test_three_kinds_are_typed_and_replayable_without_source_authority(self):
        result = assemble_portfolio_inputs(self.template, self.bundle)
        request = result["selection_request"]
        book = {item["product_id"]: item for item in request["products"]}
        self.assertEqual({name: book[name]["trade_cost_bps"] for name in book},
                         {"tron_lend": 10, "tron_short": 20, "stable_yield": 5})
        self.assertEqual(result["portfolio_verdict"]["selected_candidate_id"], "hedged")
        self.assertEqual(result["portfolio_verdict"]["evaluated"][0]["candidate_id"], "hedged")
        self.assertEqual(result["source_trust"], "CLAIMED_NOT_VERIFIED")
        self.assertEqual((result["execution_authority"], result["chain_status"]),
                         ("NONE", "NOT_SUBMITTED"))
        self.assertTrue(verify_portfolio_inputs(result, self.template, self.bundle))
        forged = copy.deepcopy(result)
        forged["selection_request"]["products"][0]["trade_cost_bps"] = 0
        self.assertFalse(verify_portfolio_inputs(forged, self.template, self.bundle))

    def test_observation_model_binding_and_identity_are_required(self):
        changed = copy.deepcopy(self.bundle)
        changed["observations"][1]["market"]["funding_bps_per_8h"] = 100
        with self.assertRaisesRegex(MachineError, "do not bind"):
            assemble_portfolio_inputs(self.template, changed)
        changed = copy.deepcopy(self.bundle)
        changed["observations"][0]["asset"] = "TRX"
        with self.assertRaisesRegex(MachineError, "identity"):
            assemble_portfolio_inputs(self.template, changed)
        changed = copy.deepcopy(self.bundle)
        changed["observations"][0]["notional_unit"] = "TRX"
        with self.assertRaisesRegex(MachineError, "unit mismatch"):
            assemble_portfolio_inputs(self.template, changed)
        changed = copy.deepcopy(self.bundle)
        changed["positions"]["current_weights_bps"]["tron_short"] = 11000
        with self.assertRaisesRegex(MachineError, "typed range"):
            assemble_portfolio_inputs(self.template, changed)

    def test_expired_quote_large_basis_and_withdrawal_horizon_fail_or_abstain(self):
        changed = copy.deepcopy(self.bundle)
        changed["observations"][0]["valid_until"] = self.template["as_of"]
        changed["assumptions"][0]["observation_hash"] = digest(changed["observations"][0])
        with self.assertRaisesRegex(MachineError, "expired"):
            assemble_portfolio_inputs(self.template, changed)
        changed = copy.deepcopy(self.bundle)
        changed["observations"][1]["market"]["mark_price"] = "10.2"
        changed["assumptions"][1]["observation_hash"] = digest(changed["observations"][1])
        with self.assertRaisesRegex(MachineError, "basis exceeds"):
            assemble_portfolio_inputs(self.template, changed)
        changed = copy.deepcopy(self.bundle)
        changed["observations"][1]["tradable_notional"] = "300"
        changed["assumptions"][1]["observation_hash"] = digest(changed["observations"][1])
        result = assemble_portfolio_inputs(self.template, changed)
        short = next(item for item in result["selection_request"]["products"]
                     if item["product_id"] == "tron_short")
        self.assertEqual(short["max_bps"], 3000)
        self.assertEqual(result["portfolio_verdict"]["status"], "ABSTAIN")
        changed = copy.deepcopy(self.bundle)
        changed["observations"][0]["market"]["redemption_days"] = 30
        changed["assumptions"][0]["observation_hash"] = digest(changed["observations"][0])
        result = assemble_portfolio_inputs(self.template, changed)
        lend = next(item for item in result["selection_request"]["products"]
                    if item["product_id"] == "tron_lend")
        self.assertEqual(lend["liquidity_days"], 30)
        self.assertEqual(result["portfolio_verdict"]["status"], "ABSTAIN")
        changed = copy.deepcopy(self.bundle)
        changed["observations"][0]["market"]["redemption_enabled"] = False
        changed["assumptions"][0]["observation_hash"] = digest(changed["observations"][0])
        with self.assertRaisesRegex(MachineError, "redemption is not available"):
            assemble_portfolio_inputs(self.template, changed)

    def test_spot_spread_is_rounded_up_and_never_understates_cost(self):
        changed = copy.deepcopy(self.bundle)
        manifest = changed["manifests"][2]
        manifest["kind"] = "SPOT"
        manifest["max_spread_bps"] = 10
        observed = changed["observations"][2]
        observed["kind"] = "SPOT"
        observed["market"] = {"bid_price": "1", "ask_price": "1.00051",
                              "price_unit": "USDT/USDT",
                              "fee_bps": 0, "impact_bps": 0}
        changed["assumptions"][2]["observation_hash"] = digest(observed)
        result = assemble_portfolio_inputs(self.template, changed)
        spot = next(item for item in result["selection_request"]["products"]
                    if item["product_id"] == "stable_yield")
        self.assertEqual(spot["trade_cost_bps"], 6)
        observed["market"]["price_unit"] = "TRX/USDT"
        changed["assumptions"][2]["observation_hash"] = digest(observed)
        with self.assertRaisesRegex(MachineError, "price unit mismatch"):
            assemble_portfolio_inputs(self.template, changed)
        observed["market"]["price_unit"] = "USDT/USDT"
        observed["market"]["bid_price"] = "1.1"
        changed["assumptions"][2]["observation_hash"] = digest(observed)
        with self.assertRaisesRegex(MachineError, "crossed"):
            assemble_portfolio_inputs(self.template, changed)

    def test_assembled_verdict_replays_into_non_executable_basket(self):
        assembly = assemble_portfolio_inputs(self.template, self.bundle)
        state = json.loads((CASE.parent / "economic_basket_state_demo.json").read_text())
        policy = json.loads((CASE.parent / "economic_basket_policy_demo.json").read_text())
        policy["allowed_products"] = sorted(item["product_id"]
                                             for item in assembly["selection_request"]["products"])
        commitment = commit_basket(assembly["selection_request"],
                                   assembly["portfolio_verdict"], state, policy,
                                   valid_until="2026-09-25T12:03:00+00:00")
        self.assertTrue(verify_basket(commitment, assembly["selection_request"], state, policy))
        self.assertEqual(commitment["execution_status"], "NO_BASKET_EXECUTOR")
        changed = copy.deepcopy(assembly["selection_request"])
        changed["products"][1]["source_hash"] = "0" * 64
        self.assertFalse(verify_basket(commitment, changed, state, policy))

    def test_cli_assembles_and_verifies_original_inputs(self):
        self.assertEqual(json.loads(TEMPLATE_CASE.read_text()), self.template)
        self.assertEqual(json.loads(BUNDLE_CASE.read_text()), self.bundle)
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            template_path, bundle_path = TEMPLATE_CASE, BUNDLE_CASE
            result_path = directory / "result.json"
            output = io.StringIO()
            with patch.object(sys, "argv", ["economic-machine", "portfolio-assemble",
                                            "--request", str(template_path),
                                            "--bundle", str(bundle_path)]):
                with contextlib.redirect_stdout(output):
                    main()
            result_path.write_text(output.getvalue())
            output = io.StringIO()
            with patch.object(sys, "argv", ["economic-machine", "portfolio-assemble-verify",
                                            "--request", str(template_path),
                                            "--bundle", str(bundle_path),
                                            "--observation", str(result_path)]):
                with contextlib.redirect_stdout(output):
                    main()
            self.assertTrue(json.loads(output.getvalue())["verified_replay"])


if __name__ == "__main__":
    unittest.main()
