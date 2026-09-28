"""Synthetic evidence tests for a locked economic execution; no chain I/O."""

import json
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from economic_machine.execution_evidence import ExecutionEvidenceVerifier
from economic_machine.runtime import MachineRuntime
from economic_machine.state import state_root
from economic_machine.values import MachineError


BASE = Path(__file__).resolve().parents[1] / "cases"
AT = "2026-09-25T12:01:00+00:00"


class SyntheticVerifier(ExecutionEvidenceVerifier):
    """Only a fixture. Never treat these checks as real chain verification."""

    def verify_authorization(self, intent, evidence):
        return evidence["signature_ref"] == "fixture_signature"

    def verify_submission(self, intent, authorization, evidence):
        return (authorization["signature_ref"] == "fixture_signature"
                and evidence["submission_ref"] == "fixture_submission")

    def verify_finality(self, submission, evidence):
        return (submission["txid"] == evidence["txid"]
                and evidence["raw_receipt_hash"] == "d" * 64)

    def verify_account_snapshot(self, finality, state, evidence):
        return (finality["raw_receipt_hash"] == "d" * 64
                and evidence["snapshot_ref"] == "fixture_snapshot")

    def verify_execution_exception(self, intent, submission, finality, evidence):
        return (evidence["evidence_ref"] == "fixture_exception"
                and submission["txid"] == evidence["txid"])


class ExecutionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = MachineRuntime(Path(self.temp.name) / "machine.sqlite3")
        self.world = json.loads((BASE / "economic_state_demo.json").read_text())
        self.program = json.loads((BASE / "economic_program_demo.json").read_text())
        self.runtime.install_state(self.world)
        self.runtime.register_program(self.program)
        self.receipt = self.runtime.evaluate(self.program["program_id"], at=AT)
        self.intent = self.receipt["intent"]
        self.hash = self.receipt["receipt_hash"]
        self.verifier = SyntheticVerifier()
        self.runtime.begin_execution(self.hash, at=AT)

    def evidence(self):
        return {
            "AUTHORIZE": {"owner_id": self.intent["owner_id"],
                          "intent_hash": self.intent["intent_hash"],
                          "signed_at": AT, "signature_ref": "fixture_signature"},
            "SUBMIT": {"network": self.intent["network"],
                       "intent_hash": self.intent["intent_hash"],
                       "txid": "fixture_tx", "submitted_at": AT,
                       "signed_payload_hash": "c" * 64,
                       "submission_ref": "fixture_submission"},
            "FINALIZE": {"network": self.intent["network"], "txid": "fixture_tx",
                         "finalized_at": "2026-09-25T12:02:00+00:00",
                         "block_height": 19, "raw_receipt_hash": "d" * 64,
                         "execution_status": "SUCCESS"}}

    def advance(self, *stages):
        evidence = self.evidence()
        for stage in stages:
            self.runtime.record_execution_evidence(
                self.hash, stage=stage, evidence=evidence[stage],
                verifier=self.verifier)

    def post_state(self, *, balance=None):
        expected = self.intent["expected"]
        asset = self.receipt["action"]["asset"]
        agent = self.intent["agent_id"]
        delta = {"schema_version": "econ-state-delta-1",
                 "owner_id": self.world["owner_id"], "network": self.world["network"],
                 "sequence": 1, "previous_root": state_root(self.world),
                 "as_of": "2026-09-25T12:03:00+00:00", "evidence_hash": "e" * 64,
                 "fact_updates": {}, "quote_updates": {},
                 "account_updates": {
                     "balances": {asset: balance or expected["balance_after"]},
                     "exposures": {agent: {asset: expected["exposure_after"]}},
                     "daily_losses": {agent: {asset: expected["daily_loss_after"]}}}}
        self.runtime.ingest(delta, event_id="post-state-fixture-1")
        state = self.runtime.latest_state()
        return {"owner_id": state["owner_id"], "network": state["network"],
                "txid": "fixture_tx", "state_root": state_root(state),
                "sequence": state["sequence"], "observed_at": state["as_of"],
                "snapshot_ref": "fixture_snapshot"}

    def exception_evidence(self, kind, *, prior_receipt_hash=None):
        return {"network": self.intent["network"],
                "intent_hash": self.intent["intent_hash"],
                "txid": "fixture_tx", "kind": kind,
                "observed_at": "2026-09-25T12:04:00+00:00",
                "block_height": 19, "prior_receipt_hash": prior_receipt_hash,
                "raw_evidence_hash": "f" * 64,
                "evidence_ref": "fixture_exception"}

    def test_release_requires_finality_and_matching_independent_snapshot(self):
        proof = self.evidence()
        with self.assertRaisesRegex(MachineError, "out of order"):
            self.runtime.record_execution_evidence(
                self.hash, stage="SUBMIT", evidence=proof["SUBMIT"], verifier=self.verifier)
        self.advance("AUTHORIZE", "SUBMIT", "FINALIZE")
        self.assertEqual(self.runtime.intent_status(self.hash, at=AT)["execution_stage"],
                         "FINALIZE")
        self.assertEqual(self.runtime.status()["execution_locked_reservations"], 1)
        with self.assertRaisesRegex(MachineError, "unresolved execution lock"):
            self.runtime.evaluate(self.program["program_id"], at=AT)
        snapshot = self.post_state()
        result = self.runtime.record_execution_evidence(
            self.hash, stage="RECONCILE", evidence=snapshot, verifier=self.verifier)
        self.assertFalse(result["capital_locked"])
        self.assertEqual(self.runtime.status()["reconciled_reservations"], 1)
        self.assertEqual(self.runtime.status()["execution_locked_reservations"], 0)
        self.assertEqual(self.runtime.intent_status(self.hash, at=AT)["execution_stage"],
                         "RECONCILE")
        self.assertTrue(self.runtime.verify_receipt(self.hash))
        self.assertTrue(self.runtime.record_execution_evidence(
            self.hash, stage="RECONCILE", evidence=snapshot,
            verifier=self.verifier)["idempotent"])

    def test_invalid_or_conflicting_external_evidence_never_releases_capital(self):
        proof = self.evidence()
        wrong = {**proof["AUTHORIZE"], "signature_ref": "unverified"}
        with self.assertRaisesRegex(MachineError, "not verified"):
            self.runtime.record_execution_evidence(
                self.hash, stage="AUTHORIZE", evidence=wrong, verifier=self.verifier)
        self.advance("AUTHORIZE")
        with self.assertRaisesRegex(MachineError, "conflicting evidence"):
            self.runtime.record_execution_evidence(
                self.hash, stage="AUTHORIZE", evidence=wrong, verifier=self.verifier)
        late = {**proof["SUBMIT"], "submitted_at": "2026-09-25T13:00:00+00:00"}
        with self.assertRaisesRegex(MachineError, "live authorization"):
            self.runtime.record_execution_evidence(
                self.hash, stage="SUBMIT", evidence=late, verifier=self.verifier)
        self.advance("SUBMIT")
        wrong_chain = {**proof["FINALIZE"], "network": "foreign-network"}
        with self.assertRaisesRegex(MachineError, "successful submitted action"):
            self.runtime.record_execution_evidence(
                self.hash, stage="FINALIZE", evidence=wrong_chain, verifier=self.verifier)
        self.advance("FINALIZE")
        snapshot = self.post_state(balance="1000")
        with self.assertRaisesRegex(MachineError, "postcondition differs"):
            self.runtime.record_execution_evidence(
                self.hash, stage="RECONCILE", evidence=snapshot, verifier=self.verifier)
        self.assertEqual(self.runtime.status()["execution_locked_reservations"], 1)

    def test_projection_tampering_denies_release_and_intent_status(self):
        self.advance("AUTHORIZE", "SUBMIT", "FINALIZE")
        snapshot = self.post_state()
        with sqlite3.connect(self.runtime.db_path) as db:
            db.execute("UPDATE reservations SET status='RECONCILED' WHERE reservation_id=?",
                       (self.hash,))
        with self.assertRaisesRegex(MachineError, "projection differs"):
            self.runtime.record_execution_evidence(
                self.hash, stage="RECONCILE", evidence=snapshot, verifier=self.verifier)
        with self.assertRaisesRegex(MachineError, "projection differs"):
            self.runtime.intent_status(self.hash, at=AT)

    def test_paused_program_and_expired_intent_still_hold_capital_until_reconcile(self):
        self.runtime.pause_program(self.program["program_id"], reason="MARKET_HALT")
        self.runtime.tick(at="2026-09-25T14:00:00+00:00")
        self.assertTrue(self.runtime.intent_status(
            self.hash, at="2026-09-25T14:00:00+00:00")["capital_locked"])
        self.advance("AUTHORIZE", "SUBMIT", "FINALIZE")
        snapshot = self.post_state()
        self.runtime.record_execution_evidence(
            self.hash, stage="RECONCILE", evidence=snapshot, verifier=self.verifier)
        self.assertFalse(self.runtime.intent_status(
            self.hash, at="2026-09-25T14:00:00+00:00")["capital_locked"])

    def test_no_verifier_reverted_finality_and_unverified_snapshot_keep_lock(self):
        proof = self.evidence()
        with self.assertRaisesRegex(MachineError, "verifier required"):
            self.runtime.record_execution_evidence(
                self.hash, stage="AUTHORIZE", evidence=proof["AUTHORIZE"],
                verifier=None)
        self.advance("AUTHORIZE", "SUBMIT")
        reverted = {**proof["FINALIZE"], "execution_status": "REVERTED"}
        with self.assertRaisesRegex(MachineError, "successful submitted action"):
            self.runtime.record_execution_evidence(
                self.hash, stage="FINALIZE", evidence=reverted, verifier=self.verifier)
        self.advance("FINALIZE")
        snapshot = self.post_state()
        wrong_snapshot = {**snapshot, "snapshot_ref": "unverified_snapshot"}
        with self.assertRaisesRegex(MachineError, "not verified"):
            self.runtime.record_execution_evidence(
                self.hash, stage="RECONCILE", evidence=wrong_snapshot,
                verifier=self.verifier)
        self.assertEqual(self.runtime.status()["execution_locked_reservations"], 1)

    def test_equivalent_utc_spellings_do_not_change_timing_order(self):
        proof = self.evidence()
        proof["AUTHORIZE"]["signed_at"] = "2026-09-25T12:01:00Z"
        proof["SUBMIT"]["submitted_at"] = "2026-09-25T12:01:00Z"
        proof["FINALIZE"]["finalized_at"] = "2026-09-25T12:02:00Z"
        for stage in ("AUTHORIZE", "SUBMIT", "FINALIZE"):
            self.runtime.record_execution_evidence(
                self.hash, stage=stage, evidence=proof[stage],
                verifier=self.verifier)
        snapshot = self.post_state()
        result = self.runtime.record_execution_evidence(
            self.hash, stage="RECONCILE", evidence=snapshot,
            verifier=self.verifier)
        self.assertFalse(result["capital_locked"])

    def test_reverted_submission_revokes_unsent_intents_and_halts_runtime(self):
        second = copy.deepcopy(self.program)
        second["program_id"] = "second-agent-program"
        second["sandbox"]["max_exposure"] = "1000"
        second["sandbox"]["capital_limit"] = "1000"
        self.runtime.register_program(second)
        pending = self.runtime.evaluate(second["program_id"], at=AT)
        self.assertEqual(pending["terminal_state"], "AWAITING_AUTHORIZATION")
        self.advance("AUTHORIZE", "SUBMIT")
        exception = self.exception_evidence("REVERTED")
        result = self.runtime.record_execution_exception(
            self.hash, evidence=exception, verifier=self.verifier)
        self.assertEqual(result["revoked_reservations"], [pending["receipt_hash"]])
        self.assertTrue(result["runtime_halted"])
        self.assertTrue(result["capital_locked"])
        self.assertEqual(self.runtime.intent_status(self.hash, at=AT)["execution_stage"],
                         "EXCEPTION")
        self.assertEqual(self.runtime.status()["execution_locked_reservations"], 1)
        self.assertEqual(self.runtime.status()["unsent_reservations"], 0)
        with self.assertRaisesRegex(MachineError, "halted by unresolved"):
            self.runtime.evaluate(second["program_id"], at=AT)
        with self.assertRaisesRegex(MachineError, "halted by unresolved"):
            self.runtime.begin_execution(pending["receipt_hash"], at=AT)
        self.assertTrue(self.runtime.record_execution_exception(
            self.hash, evidence=exception, verifier=self.verifier)["idempotent"])
        with self.assertRaisesRegex(MachineError, "conflicting execution exception"):
            self.runtime.record_execution_exception(
                self.hash, evidence={**exception, "kind": "PARTIAL_FILL"},
                verifier=self.verifier)

    def test_partial_fill_after_finality_stays_locked_without_reconciliation(self):
        self.advance("AUTHORIZE", "SUBMIT", "FINALIZE")
        exception = self.exception_evidence("PARTIAL_FILL",
                                             prior_receipt_hash="d" * 64)
        self.runtime.record_execution_exception(
            self.hash, evidence=exception, verifier=self.verifier)
        snapshot = self.post_state()
        self.assertTrue(self.runtime.status()["execution_halt"])
        with self.assertRaisesRegex(MachineError, "out of order"):
            self.runtime.record_execution_evidence(
                self.hash, stage="RECONCILE", evidence=snapshot,
                verifier=self.verifier)
        self.assertEqual(self.runtime.status()["execution_locked_reservations"], 1)

    def test_post_reconciliation_reorg_relocks_and_suppresses_new_decisions(self):
        self.advance("AUTHORIZE", "SUBMIT", "FINALIZE")
        snapshot = self.post_state()
        self.runtime.record_execution_evidence(
            self.hash, stage="RECONCILE", evidence=snapshot,
            verifier=self.verifier)
        self.assertEqual(self.runtime.status()["execution_locked_reservations"], 0)
        exception = self.exception_evidence("REORG", prior_receipt_hash="d" * 64)
        with self.assertRaisesRegex(MachineError, "reorg does not bind"):
            self.runtime.record_execution_exception(
                self.hash, evidence={**exception, "block_height": 20},
                verifier=self.verifier)
        self.assertFalse(self.runtime.status()["execution_halt"])
        self.runtime.record_execution_exception(
            self.hash, evidence=exception, verifier=self.verifier)
        self.assertEqual(self.runtime.status()["execution_locked_reservations"], 1)
        self.assertTrue(self.runtime.status()["execution_halt"])
        self.assertEqual(self.runtime.intent_status(self.hash, at=AT)["execution_stage"],
                         "EXCEPTION")
        state = self.runtime.latest_state()
        delta = {"schema_version": "econ-state-delta-1", "owner_id": state["owner_id"],
                 "network": state["network"], "sequence": 2,
                 "previous_root": state_root(state),
                 "as_of": "2026-09-25T12:05:00+00:00", "evidence_hash": "a" * 64,
                 "fact_updates": {}, "quote_updates": {},
                 "account_updates": {"balances": {"USDT": "1000"},
                                     "exposures": {}, "daily_losses": {}}}
        ingested = self.runtime.ingest(delta, event_id="reorg-account-state-2")
        self.assertTrue(ingested["execution_dispute_halt"])
        self.assertEqual(ingested["kernel_calls"], 0)
        self.assertEqual(ingested["receipt_hashes"], [])
        self.assertEqual(self.runtime.latest_state()["sequence"], 2)
        self.assertTrue(self.runtime.tick(at="2026-09-25T12:06:00+00:00")
                        ["execution_dispute_halt"])

    def test_unbound_or_unverified_exception_cannot_halt_runtime(self):
        self.advance("AUTHORIZE", "SUBMIT")
        exception = self.exception_evidence("REORG", prior_receipt_hash="d" * 64)
        with self.assertRaisesRegex(MachineError, "reorg does not bind"):
            self.runtime.record_execution_exception(
                self.hash, evidence=exception, verifier=self.verifier)
        wrong = self.exception_evidence("REVERTED")
        wrong["txid"] = "different_tx"
        with self.assertRaisesRegex(MachineError, "submitted transaction"):
            self.runtime.record_execution_exception(
                self.hash, evidence=wrong, verifier=self.verifier)
        wrong = self.exception_evidence("REVERTED")
        wrong["evidence_ref"] = "unverified"
        with self.assertRaisesRegex(MachineError, "not verified"):
            self.runtime.record_execution_exception(
                self.hash, evidence=wrong, verifier=self.verifier)
        self.assertFalse(self.runtime.status()["execution_halt"])


if __name__ == "__main__":
    unittest.main()
