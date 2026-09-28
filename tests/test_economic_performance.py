import copy
import unittest

from economic_machine.performance import (build_performance_ledger,
                                           compare_forecast_actual,
                                           tron_fee_cost_entry)
from economic_machine.position_reconciliation import reconcile_position
from economic_machine.values import MachineError, digest
from test_economic_position_reconciliation import (AT_AFTER, after_account,
    execution_context, post_state)


START = "2026-09-28T12:00:00Z"
END = "2026-09-28T13:00:00Z"


def entry(entry_id, kind, amount, evidence="a"):
    return {"entry_id": entry_id, "kind": kind, "amount_base_units": str(amount),
        "observed_at": "2026-09-28T12:30:00Z", "evidence_hash": evidence * 64}


def reconciliation():
    graph, step, before, approved, execution = execution_context()
    after = after_account(before)
    return reconcile_position(graph, approved, execution, before,
                              post_state(execution, after), at=AT_AFTER)


def ledger_inputs(rec):
    return {"scope": rec["scope"], "base_asset": "USDT", "base_decimals": 6,
        "period_start": START, "period_end": END,
        "opening_nav_base_units": "1000000000",
        "closing_nav_base_units": "1106000000",
        "external_flows": [entry("deposit-1", "DEPOSIT", 100_000_000)],
        "return_components": [entry("interest-1", "INTEREST_INCOME", 10_000_000),
            entry("reward-1", "REWARD_INCOME", 2_000_000),
            entry("price-1", "PRICE_PNL", -1_000_000)],
        "cost_components": [entry("network-1", "NETWORK_FEE", 1_000_000),
            entry("protocol-1", "PROTOCOL_FEE", 1_000_000),
            entry("debt-1", "DEBT_COST", 3_000_000)],
        "opening_state_hash": "1" * 64, "closing_state_hash": "2" * 64,
        "valuation_evidence_hash": "3" * 64,
        "reconciliation_records": [rec]}


class PerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rec = reconciliation()

    def test_external_deposit_is_excluded_and_costs_are_attributed(self):
        ledger = build_performance_ledger(**ledger_inputs(self.rec))
        self.assertEqual(ledger["external_net_flow_base_units"], "100000000")
        self.assertEqual(ledger["investment_pnl_base_units"], "6000000")
        self.assertEqual(ledger["attributed_pnl_base_units"], "6000000")
        self.assertEqual(ledger["debt_principal_treatment"],
                         "BALANCE_SHEET_NOT_RETURN")

    def test_unbalanced_attribution_is_rejected_instead_of_inventing_other_income(self):
        values = ledger_inputs(self.rec)
        values["closing_nav_base_units"] = "1107000000"
        with self.assertRaisesRegex(MachineError, "does not reconcile"):
            build_performance_ledger(**values)

    def test_only_reconciled_execution_records_can_enter_ledger(self):
        values = ledger_inputs(self.rec)
        bad = copy.deepcopy(self.rec)
        bad["status"] = "DISPUTED"
        values["reconciliation_records"] = [bad]
        with self.assertRaisesRegex(MachineError, "reconciled scoped"):
            build_performance_ledger(**values)

    def test_reconciliation_must_fall_inside_accounting_period(self):
        values = ledger_inputs(self.rec)
        bad = copy.deepcopy(self.rec)
        bad["reconciled_at"] = "2026-09-28T11:59:59+00:00"
        bad.pop("reconciliation_hash")
        bad["reconciliation_hash"] = digest({"domain": bad["schema_version"],
                                               "reconciliation": bad})
        values["reconciliation_records"] = [bad]
        with self.assertRaisesRegex(MachineError, "outside the accounting period"):
            build_performance_ledger(**values)

    def test_forecast_is_compared_as_assumption_on_same_network_and_period(self):
        ledger = build_performance_ledger(**ledger_inputs(self.rec))
        forecast = {"schema_version": "economic-performance-forecast-1",
            "scope": copy.deepcopy(self.rec["scope"]), "base_asset": "USDT",
            "base_decimals": 6, "period_start": START, "period_end": END,
            "expected_net_income_base_units": "5000000",
            "assumption_hash": "4" * 64, "mode": "REPLAY"}
        compared = compare_forecast_actual(forecast, ledger)
        self.assertEqual(compared["variance_base_units"], "1000000")
        self.assertEqual(compared["classification"], "ABOVE_EXPECTATION")
        self.assertEqual(compared["forecast_truth_status"], "ASSUMPTION_NOT_GROUND_TRUTH")
        forecast["scope"]["network"] = "tron-nile"
        with self.assertRaisesRegex(MachineError, "scopes differ"):
            compare_forecast_actual(forecast, ledger)

    def test_solidified_tron_receipt_fee_uses_price_evidence_and_sun_units(self):
        graph, step, before, approved, execution = execution_context()
        converted = tron_fee_cost_entry(execution, entry_id="tron-fee-1",
            observed_at="2026-09-28T12:02:00Z", base_asset="USDT", base_decimals=6,
            trx_price_base_units_per_trx="2000000", price_evidence_hash="5" * 64)
        self.assertEqual(converted["evidence"]["fee_sun"], "1000000")
        self.assertEqual(converted["entry"]["amount_base_units"], "2000000")
        self.assertEqual(converted["evidence"]["fee_basis"], "RECEIPT_TOTAL_FEE")


if __name__ == "__main__":
    unittest.main()
