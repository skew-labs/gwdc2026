import copy
import unittest

from economic_machine.position_reconciliation import reconcile_position
from economic_machine.tron_execution import (assess_execution_observation,
    prepare_submission, record_broadcast_attempt)
from economic_machine.tx_graph import compile_lifecycle_graph
from economic_machine.values import MachineError
from test_economic_tron_execution import (attempt, observation, signed_context,
    validate_graph_step, wallet_for)
import test_economic_tx_graph as fixtures


AT_AFTER = "2026-09-28T12:02:30Z"


def execution_context(*, operation="JUSTLEND_SUPPLY", allowance=1_000_000_000):
    graph, step, before, approved, validation = signed_context(
        operation=operation, allowance=allowance)
    record = prepare_submission(validation, at="2026-09-28T12:01:00Z")
    record = record_broadcast_attempt(record, attempt(record, transport="NODE_RESPONSE",
        result="ACCEPTED", error=None))
    execution = assess_execution_observation(record, observation(record))
    return graph, step, before, approved, execution


def after_account(before, *, supplied=True):
    result = copy.deepcopy(before)
    result["snapshot_hash"] = "9" * 64
    result["observed_at"] = "2026-09-28T12:02:00Z"
    result["valid_until"] = "2026-09-28T12:04:00Z"
    if supplied:
        result["balances"][0]["amount_base_units"] = "0"
        result["positions"][0]["shares_base_units"] = "99000000000"
        result["positions"][0]["underlying_base_units"] = "1000000000"
    return result


def post_state(execution, account):
    height = execution["block"]["number"]
    return {"schema_version": "economic-post-state-evidence-1",
        "network": execution["scope"]["network"], "txid": execution["txid"],
        "block_number": height,
        "block_id": height.to_bytes(8, "big").hex() + "34" * 24,
        "observed_at": account["observed_at"], "source_hash": "7" * 64,
        "completeness": "REQUIRED_FIELDS_COMPLETE", "account": account}


class PositionReconciliationTests(unittest.TestCase):
    def test_solid_receipt_plus_actual_position_and_balance_reconciles(self):
        graph, step, before, approved, execution = execution_context()
        after = after_account(before)
        result = reconcile_position(graph, approved, execution, before,
                                    post_state(execution, after), at=AT_AFTER)
        self.assertEqual(result["status"], "RECONCILED")
        self.assertEqual(result["position_status"], "VERIFIED_POST_STATE")
        self.assertEqual(result["capital_status"], "RELEASE_ELIGIBLE")
        self.assertEqual(result["actual_deltas"]["input_balance_delta"], "1000000000")
        self.assertEqual(result["actual_deltas"]["share_delta"], "99000000000")

    def test_successful_call_without_position_is_disputed_and_stays_locked(self):
        graph, step, before, approved, execution = execution_context()
        after = after_account(before, supplied=False)
        after["snapshot_hash"] = "8" * 64
        result = reconcile_position(graph, approved, execution, before,
                                    post_state(execution, after), at=AT_AFTER)
        self.assertEqual(result["status"], "DISPUTED")
        self.assertEqual(result["capital_status"], "LOCKED_DISPUTED")
        self.assertIn("INPUT_DECREASE", result["reason_codes"])
        self.assertIn("POSITION_SHARE_INCREASE", result["reason_codes"])

    def test_approve_reconciliation_does_not_claim_a_deposit_or_release_graph(self):
        graph, step, before, approved, execution = execution_context(
            operation="TRC20_APPROVE", allowance=0)
        after = after_account(before, supplied=False)
        after["allowances"][0]["amount_base_units"] = "1000000000"
        result = reconcile_position(graph, approved, execution, before,
                                    post_state(execution, after), at=AT_AFTER)
        self.assertEqual(result["operation"], "TRC20_APPROVE")
        self.assertEqual(result["status"], "RECONCILED")
        self.assertEqual(result["capital_status"], "GRAPH_LOCKED")
        self.assertEqual([item["kind"] for item in result["checks"]],
                         ["ALLOWANCE_EQUALS"])
        self.assertEqual(result["actual_deltas"], {})

    def test_post_state_requires_same_tx_network_complete_fields_and_bounded_block(self):
        graph, step, before, approved, execution = execution_context()
        after = after_account(before)
        base = post_state(execution, after)
        cases = (("txid", "f" * 64), ("network", "tron-nile"),
                 ("completeness", "PARTIAL"),
                 ("block_number", execution["block"]["number"] + 65))
        for field, value in cases:
            bad = copy.deepcopy(base)
            bad[field] = value
            if field == "block_number":
                bad["block_id"] = value.to_bytes(8, "big").hex() + "34" * 24
            with self.subTest(field=field), self.assertRaises(MachineError):
                reconcile_position(graph, approved, execution, before, bad, at=AT_AFTER)

    def test_missing_position_is_unknown_not_zero(self):
        graph, step, before, approved, execution = execution_context()
        after = after_account(before)
        after["positions"] = []
        result = reconcile_position(graph, approved, execution, before,
                                    post_state(execution, after), at=AT_AFTER)
        self.assertEqual(result["status"], "DISPUTED")
        self.assertIn("COMPLETE_BEFORE_AND_AFTER_POSITION_REQUIRED",
                      result["reason_codes"])
        self.assertIsNone(result["actual_deltas"]["share_delta"])

    def test_redeem_underlying_needs_received_balance_and_burned_position_not_wallet_input(self):
        private_key = int.from_bytes(bytes.fromhex("31" * 32), "big")
        wallet = wallet_for(private_key)
        old_scope = copy.deepcopy(fixtures.SCOPE)
        fixtures.SCOPE = {**old_scope, "wallet": wallet}
        try:
            product = fixtures.product()
            snapshot = fixtures.snapshot(product)
            before = fixtures.account(snapshot,
                [fixtures.asset("USDT", fixtures.TOKEN, 6, 100)],
                positions=[{"product_id": product["product_id"],
                    "shares_base_units": "1000", "underlying_base_units": "1000"}],
                liquidity=[{"product_id": product["product_id"],
                    "amount_base_units": "1000"}])
            request = {"schema_version": fixtures.LIFECYCLE_VERSION,
                "scope": copy.deepcopy(fixtures.SCOPE),
                "snapshot_hash": snapshot["snapshot_hash"],
                "product_id": product["product_id"],
                "identity_hash": product["identity_hash"],
                "capability_hash": product["capability_hash"],
                "operation": "JUSTLEND_REDEEM_UNDERLYING",
                "input": fixtures.asset("USDT", fixtures.TOKEN, 6, 500),
                "minimum_output": fixtures.asset("USDT", fixtures.TOKEN, 6, 500),
                "recipient": wallet, "resource": None, "vault_id": None,
                "claim_after": None, "requested_at": "2026-09-28T12:00:00Z",
                "valid_until": "2026-09-29T12:00:00Z"}
            graph = compile_lifecycle_graph(product, before, request,
                [fixtures.binding("JUSTLEND_REDEEM_UNDERLYING", fixtures.MARKET,
                                  product)], at=fixtures.AT, fee_limit_sun="1000")
            step = graph["steps"][0]
            before = fixtures.fresh(before)
            simulated = fixtures.simulation(step, decoded="0",
                outputs=[fixtures.asset("USDT", fixtures.TOKEN, 6, 500)])
            approved, validation = validate_graph_step(
                graph, before, step, simulated, private_key)
            record = prepare_submission(validation, at="2026-09-28T12:01:00Z")
            record = record_broadcast_attempt(record, attempt(record,
                transport="NODE_RESPONSE", result="ACCEPTED", error=None))
            execution = assess_execution_observation(record, observation(record))
            after = copy.deepcopy(before)
            after["snapshot_hash"] = "6" * 64
            after["observed_at"] = "2026-09-28T12:02:00Z"
            after["valid_until"] = "2026-09-28T12:04:00Z"
            after["balances"][0]["amount_base_units"] = "600"
            after["positions"][0]["shares_base_units"] = "750"
            after["positions"][0]["underlying_base_units"] = "500"
            result = reconcile_position(graph, approved, execution, before,
                post_state(execution, after), at=AT_AFTER)
            self.assertEqual(result["status"], "RECONCILED")
            self.assertEqual(result["actual_deltas"]["output_balance_delta"], "500")
            self.assertEqual(result["actual_deltas"]["underlying_decrease"], "500")
            self.assertEqual(result["actual_deltas"]["share_decrease"], "250")
        finally:
            fixtures.SCOPE = old_scope


if __name__ == "__main__":
    unittest.main()
