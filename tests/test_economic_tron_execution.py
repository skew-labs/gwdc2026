import copy
import hashlib
import unittest

from economic_machine.approval import (build_wallet_signature_request, confirm_approval,
                                       prepare_approval_card)
from economic_machine.preflight import compile_preflight
from economic_machine.signed_tx_validation import validate_signed_transaction
from economic_machine.tron_crypto import keccak256
import economic_machine.tron_crypto as crypto
from economic_machine.tron_execution import (assess_execution_observation,
    prepare_exact_broadcast, prepare_submission, record_broadcast_attempt)
from economic_machine.values import MachineError, digest
from test_economic_approval import AT, anchor, confirmation, session
from test_economic_signed_tx_validation import raw_transaction
import test_economic_tx_graph as fixtures


def sign_hash(message_hash, private_key):
    z = int.from_bytes(message_hash, "big")
    nonce = int.from_bytes(hashlib.sha256(
        private_key.to_bytes(32, "big") + message_hash).digest(), "big") % crypto._N or 1
    point = crypto._multiply(nonce, crypto._G)
    r = point[0] % crypto._N
    s = (crypto._inverse(nonce, crypto._N) * (z + r * private_key)) % crypto._N
    recovery = point[1] & 1
    if s > crypto._HALF_N:
        s, recovery = crypto._N - s, recovery ^ 1
    return r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([recovery])


def wallet_for(private_key):
    public = crypto._multiply(private_key, crypto._G)
    return "41" + keccak256(public[0].to_bytes(32, "big")
        + public[1].to_bytes(32, "big"))[-20:].hex()


def validate_graph_step(graph, before, step, simulation, private_key):
    manifest = compile_preflight(graph, before, [simulation], at=AT)
    active = session(graph["scope"])
    card = prepare_approval_card(graph, manifest, active, step["step_id"],
        execution_path="DIRECT_WALLET", nonce="0", prepared_at=AT,
        expires_at="2026-09-28T12:03:00Z")
    approved = confirm_approval(card, active, confirmation(card, graph["scope"]))
    request = build_wallet_signature_request(approved, active, anchor(), issued_at=AT)
    raw = raw_transaction(request)
    txid = hashlib.sha256(raw).digest()
    signed = {"visible": False, "txID": txid.hex(), "raw_data_hex": raw.hex(),
              "signature": [sign_hash(txid, private_key).hex()]}
    validation = validate_signed_transaction(signed, request, active, [], at=AT)
    return approved, validation


def signed_context(*, operation="JUSTLEND_SUPPLY", allowance=1_000_000_000):
    private_key = int.from_bytes(bytes.fromhex("31" * 32), "big")
    wallet = wallet_for(private_key)
    old_scope = copy.deepcopy(fixtures.SCOPE)
    fixtures.SCOPE = {**fixtures.SCOPE, "wallet": wallet}
    try:
        product, snapshot, intent, account, quote, bindings, graph = fixtures.supply_graph(
            allowance=allowance)
        account["positions"] = [{"product_id": product["product_id"],
            "shares_base_units": "0", "underlying_base_units": "0"}]
        before = fixtures.fresh(account)
        step = next(item for item in graph["steps"] if item["operation"] == operation)
        simulation = (fixtures.simulation(step, decoded=True) if operation == "TRC20_APPROVE"
            else fixtures.simulation(step, decoded="0", outputs=[fixtures.asset(
                "jUSDT", fixtures.SHARE, 8, 99_000_000_000)]))
        approved, validation = validate_graph_step(
            graph, before, step, simulation, private_key)
        return graph, step, before, approved, validation
    finally:
        fixtures.SCOPE = old_scope


def attempt(record, *, attempt_id="attempt-1", transport="AFTER_SEND_UNKNOWN",
            result="UNKNOWN", error="TIMEOUT"):
    return {"schema_version": "economic-tron-submission-attempt-1",
        "attempt_id": attempt_id, "txid": record["txid"],
        "signed_payload_hash": record["signed_payload_hash"], "attempted_at": AT,
        "transport_status": transport, "node_result": result,
        "response_hash": "a" * 64, "error_code": error}


def observation(record, status="SOLID_EXECUTED", *, target=None):
    height = 900
    block_id = height.to_bytes(8, "big").hex() + "12" * 24
    anchor_height, tip_height = 1, 901
    details = None
    if status in {"SOLID_EXECUTED", "SOLID_EXECUTION_FAILED"}:
        details = {"block": {"number": height, "block_id": block_id,
            "timestamp_ms": 1790596890000},
            "call_target_address": target or record["contract_address"][2:],
            "execution_success": status == "SOLID_EXECUTED",
            "body_record_hash": "b" * 64, "receipt_record_hash": "c" * 64,
            "resource_receipt": {"total_fee_sun": "1000000",
                "energy_fee_sun": "800000", "bandwidth_fee_sun": "200000",
                "energy_usage_total": "13045", "bandwidth_usage": "345"},
            "logs": []}
    raw = {"schema_version": "economic-tron-transaction-observation-2",
        "txid": record["txid"], "status": status,
        "network_anchor": {"number": anchor_height,
            "block_id": anchor_height.to_bytes(8, "big").hex() + "00" * 24,
            "timestamp_ms": 1},
        "solid_tip_before": {"number": tip_height,
            "block_id": tip_height.to_bytes(8, "big").hex() + "11" * 24,
                             "timestamp_ms": 1790596900000},
        "solid_tip_after": {"number": tip_height,
            "block_id": tip_height.to_bytes(8, "big").hex() + "11" * 24,
                            "timestamp_ms": 1790596900000},
        "observed_at_ms": 1790596910000, "endpoint_fingerprint": "d" * 64,
        "details": details, "source_trust": "NODE_RESPONSE_ONLY",
        "settlement_status": "NOT_VERIFIED", "execution_authority": "NONE"}
    raw["observation_hash"] = digest(raw)
    return {"schema_version": "economic-tron-execution-observation-evidence-1",
        "network": record["scope"]["network"], "reader_config_hash": "e" * 64,
        "source_id": "solid-node-replay", "observation": raw}


class TronExecutionTests(unittest.TestCase):
    def setUp(self):
        self.graph, self.step, self.before, self.approved, validation = signed_context()
        self.validation = validation
        self.record = prepare_submission(validation, at=AT)

    def test_unknown_broadcast_keeps_same_txid_payload_and_capital_lock(self):
        request = prepare_exact_broadcast(self.record, at=AT)
        updated = record_broadcast_attempt(self.record, attempt(self.record))
        retry = prepare_exact_broadcast(updated, at=AT)
        self.assertEqual(request["txid"], retry["txid"])
        self.assertEqual(request["signed_payload_hash"], retry["signed_payload_hash"])
        self.assertEqual(request["signed_transaction"], retry["signed_transaction"])
        self.assertEqual(updated["status"], "SUBMISSION_UNKNOWN")
        self.assertEqual(updated["capital_status"], "LOCKED")
        self.assertFalse(updated["rebuild_allowed"])

    def test_attempt_cannot_change_txid_payload_or_claim_impossible_transport_result(self):
        for field, value in (("txid", "f" * 64), ("signed_payload_hash", "f" * 64)):
            bad = attempt(self.record)
            bad[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(MachineError, "changed"):
                record_broadcast_attempt(self.record, bad)
        bad = attempt(self.record, transport="AFTER_SEND_UNKNOWN", result="ACCEPTED")
        with self.assertRaisesRegex(MachineError, "impossible"):
            record_broadcast_attempt(self.record, bad)

    def test_rehashed_validation_metadata_cannot_separate_from_wallet_request(self):
        changed = copy.deepcopy(self.validation)
        changed["plan_hash"] = "f" * 64
        changed.pop("validation_hash")
        changed["validation_hash"] = digest({"domain": changed["schema_version"],
            "validation": changed})
        with self.assertRaisesRegex(MachineError, "no longer agree"):
            prepare_submission(changed, at=AT)

    def test_attempt_idempotence_and_conflicting_retry_evidence(self):
        once = record_broadcast_attempt(self.record, attempt(self.record))
        twice = record_broadcast_attempt(once, attempt(self.record))
        self.assertEqual(once, twice)
        conflicting = attempt(self.record)
        conflicting["response_hash"] = "f" * 64
        with self.assertRaisesRegex(MachineError, "conflicting"):
            record_broadcast_attempt(once, conflicting)

    def test_node_acceptance_is_not_execution_and_solid_receipt_still_needs_post_state(self):
        accepted = record_broadcast_attempt(self.record, attempt(self.record,
            transport="NODE_RESPONSE", result="ACCEPTED", error=None))
        self.assertEqual(accepted["status"], "NODE_ACCEPTED_UNCONFIRMED")
        result = assess_execution_observation(accepted, observation(accepted))
        self.assertEqual(result["status"], "SOLID_EXECUTED_PENDING_POST_STATE")
        self.assertEqual(result["position_status"], "NOT_RECONCILED")
        self.assertEqual(result["capital_status"], "LOCKED")

    def test_missing_or_failed_receipt_never_becomes_success(self):
        unknown = assess_execution_observation(self.record, observation(
            self.record, "NOT_OBSERVED"))
        attempted = record_broadcast_attempt(self.record, attempt(self.record))
        failed = assess_execution_observation(attempted, observation(
            attempted, "SOLID_EXECUTION_FAILED"))
        self.assertEqual(unknown["status"], "SUBMISSION_UNKNOWN")
        self.assertEqual(failed["status"], "SOLID_EXECUTION_FAILED")
        self.assertEqual(unknown["capital_status"], "LOCKED")
        self.assertEqual(failed["capital_status"], "LOCKED")

    def test_wrong_solidified_target_is_rejected(self):
        attempted = record_broadcast_attempt(self.record, attempt(self.record))
        with self.assertRaisesRegex(MachineError, "target"):
            assess_execution_observation(attempted, observation(
                attempted, target="99" * 20))

    def test_malformed_block_or_resource_receipt_is_rejected_after_rehash(self):
        attempted = record_broadcast_attempt(self.record, attempt(self.record))
        bad_block = observation(attempted)
        bad_block["observation"]["details"]["block"]["block_id"] = "0" * 64
        bad_block["observation"]["observation_hash"] = digest({key: value for key, value
            in bad_block["observation"].items() if key != "observation_hash"})
        with self.assertRaisesRegex(MachineError, "block commitment"):
            assess_execution_observation(attempted, bad_block)
        bad_fee = observation(attempted)
        bad_fee["observation"]["details"]["resource_receipt"]["energy_fee_sun"] = "-1"
        bad_fee["observation"]["observation_hash"] = digest({key: value for key, value
            in bad_fee["observation"].items() if key != "observation_hash"})
        with self.assertRaisesRegex(MachineError, "resource value"):
            assess_execution_observation(attempted, bad_fee)

    def test_existing_solidified_reader_connects_to_exact_signed_txid(self):
        from test_economic_tron_consumption_read import TronConsumptionReadTests
        attempted = record_broadcast_attempt(self.record, attempt(self.record,
            transport="NODE_RESPONSE", result="ACCEPTED", error=None))
        reader = TronConsumptionReadTests()
        reader.setUp()
        reader.raw_data_hex = attempted["signed_transaction"]["raw_data_hex"]
        reader.txid = attempted["txid"]
        reader.target = attempted["contract_address"][2:]
        reader.block_time = 1790596890000
        reader.inclusion = reader.block(500, reader.block_time)
        reader.inclusion["transactions"] = [{"txID": reader.txid,
            "raw_data_hex": reader.raw_data_hex}]
        reader.tip = reader.block(600, reader.block_time + 60000)
        reader.body = {"txID": reader.txid, "raw_data_hex": reader.raw_data_hex,
            "raw_data": {"contract": [{"type": "TriggerSmartContract",
                "parameter": {"value": {"contract_address": "41" + reader.target}}}]},
            "ret": [{"contractRet": "SUCCESS"}]}
        reader.info = {"id": reader.txid, "blockNumber": 500,
            "blockTimeStamp": reader.block_time, "receipt": {"result": "SUCCESS"},
            "log": []}
        observed = reader.observe()
        evidence = {"schema_version": "economic-tron-execution-observation-evidence-1",
            "network": attempted["scope"]["network"], "reader_config_hash": "e" * 64,
            "source_id": "existing-solidified-reader", "observation": observed}
        result = assess_execution_observation(attempted, evidence)
        self.assertEqual(result["status"], "SOLID_EXECUTED_PENDING_POST_STATE")
        self.assertEqual(result["txid"], attempted["txid"])


if __name__ == "__main__":
    unittest.main()
