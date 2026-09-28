"""Check exact optimum certificates and real-source gating."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from finagent.store import Store
from research.fdc.formal_optima import run_once, solve, verify


class FormalOptimaTests(unittest.TestCase):
    def test_two_market_exact_optimum_and_dominated_plan(self):
        scenario = {"budget_atoms": "1000", "min_immediate_atoms": "200",
                    "total_risk_fraction": "0.75", "shared_risk_fraction": "1",
                    "horizon_days": 30,
                    "markets": [{"id": "higher", "supply_rate": "0.1",
                                 "cash_proxy_atoms": "300"},
                                {"id": "lower", "supply_rate": "0.05",
                                 "cash_proxy_atoms": "500"}]}
        target = solve(scenario)
        verify(scenario, target)
        self.assertEqual(target["allocations_atoms"], {"higher": "300", "lower": "450"})
        self.assertEqual(target["hold_atoms"], "250")
        inferior = copy.deepcopy(target)
        inferior["allocations_atoms"] = {"higher": "250", "lower": "500"}
        with self.assertRaisesRegex(ValueError, "dominated"):
            verify(scenario, inferior)
        more_reserve = {**scenario, "min_immediate_atoms": "500"}
        self.assertEqual(solve(more_reserve)["allocations_atoms"],
                         {"higher": "300", "lower": "200"})

    def test_zero_rate_holds_all(self):
        scenario = {"budget_atoms": "1000", "min_immediate_atoms": "0",
                    "total_risk_fraction": "1", "shared_risk_fraction": "1",
                    "horizon_days": 365,
                    "markets": [{"id": "flat", "supply_rate": "0",
                                 "cash_proxy_atoms": "1000"}]}
        target = solve(scenario)
        verify(scenario, target)
        self.assertEqual(target["hold_atoms"], "1000")

    def test_source_proven_labels_and_idempotence(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        store = Store(root / "source")
        address = "T" + "A" * 33
        contract_payload = {"networks": {"mainnet": {"jtokens": {
            "jUSDT": {"delegator": {"address": {"base58": address}}, "status": "active"}}}}}
        raw = json.dumps(contract_payload).encode()
        store.save_snapshot({"id": "justlend_contracts", "url": "https://docs.justlend.org/developers/contracts.json"},
                            "2026-09-24T00:00:00+00:00", raw, contract_payload, [], [])
        market_payload = {"data": {"tokenList": [{"address": address, "symbol": "jUSDT",
                                                   "underlyingSymbol": "USDT",
                                                   "underlyingAddress": "T" + "B" * 33,
                                                   "underlyingDecimal": 6,
                                                   "supplyRate": "0.1", "cash": "1000"}]}}
        raw = json.dumps(market_payload).encode()
        source = {"id": "justlend_markets_v1", "url": "https://openapi.just.network/lend/jtoken"}
        market = {"market_address": address, "network": "tron_mainnet",
                  "jtoken_symbol": "jUSDT", "underlying_symbol": "USDT",
                  "underlying_address": "T" + "B" * 33,
                  "underlying_decimals": 6, "status": "active"}
        facts = [{"subject_id": address, "metric_id": metric,
                  "raw_value": value, "canonical_value": value,
                  "unit": unit, "quality": "VALID", "json_path": path}
                 for metric, value, unit, path in (
                     ("supply_apy", "0.1", "annual_fraction", "/data/tokenList/0/supplyRate"),
                     ("available_cash", "1000", "underlying_tokens", "/data/tokenList/0/cash"))]
        store.save_snapshot(source, "2026-09-24T00:00:01+00:00", raw,
                            market_payload, [market], facts)
        result = run_once(store.root, root / "optima")
        self.assertEqual(result["new_optima"], 108)
        self.assertEqual(result["unique_market_states"], 1)
        self.assertGreater(result["new_counterfactual_pairs"], 0)
        self.assertEqual(result["real_customer_optimum_count"], 0)
        self.assertEqual(run_once(store.root, root / "optima")["new_optima"], 0)
        batch = next((root / "optima" / "batches").glob("*.jsonl"))
        case = json.loads(batch.read_text().splitlines()[0])
        self.assertEqual(case["record_type"], "FORMAL_OPTIMUM")
        self.assertTrue(case["formal_optimality_verified"])
        self.assertFalse(case["product_actionable"])
        self.assertNotIn("target", case["input"])


if __name__ == "__main__":
    unittest.main()
