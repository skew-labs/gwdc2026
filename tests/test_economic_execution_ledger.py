import copy
import unittest

from economic_machine.execution_ledger import (apply_execution_result,
    apply_reconciliation, record_reorg, start_execution_ledger)
from economic_machine.position_reconciliation import reconcile_position
from economic_machine.tron_execution import (assess_execution_observation,
    prepare_submission, record_broadcast_attempt)
from economic_machine.values import MachineError, digest
from test_economic_position_reconciliation import (AT_AFTER, after_account,
    execution_context, post_state)
from test_economic_tron_execution import attempt, observation, signed_context


def reservation(graph, reservation_id="reservation-graph-1"):
    raw = {"schema_version": "economic-capital-reservation-evidence-1",
        "reservation_id": reservation_id, "scope": copy.deepcopy(graph["scope"]),
        "graph_hash": graph["graph_hash"],
        "step_hashes": sorted(item["step_hash"] for item in graph["steps"]
                              if item["kind"] == "OFFCHAIN_RESERVATION"),
        "status": "EXECUTION_LOCKED", "locked_at": "2026-09-28T12:00:59Z",
        "expires_at": "2026-09-28T12:04:00Z", "source_hash": "e" * 64,
        "execution_authority": "NONE"}
    raw["evidence_hash"] = digest({"domain": raw["schema_version"], "evidence": raw})
    return raw


def completed_context(*, supplied=True):
    graph, step, before, approved, execution = execution_context()
    after = after_account(before, supplied=supplied)
    if not supplied:
        after["snapshot_hash"] = "8" * 64
    rec = reconcile_position(graph, approved, execution, before,
                             post_state(execution, after), at=AT_AFTER)
    ledger = start_execution_ledger(graph, reservation(graph),
                                    started_at="2026-09-28T12:01:00Z")
    ledger = apply_execution_result(ledger, execution, at="2026-09-28T12:02:10Z")
    return graph, execution, rec, ledger


class ExecutionLedgerTests(unittest.TestCase):
    def test_terminal_reconciliation_makes_capital_release_eligible(self):
        graph, execution, rec, ledger = completed_context()
        ledger = apply_reconciliation(ledger, rec, at=AT_AFTER)
        self.assertEqual(ledger["status"], "COMPLETED")
        self.assertEqual(ledger["capital_status"], "RELEASE_ELIGIBLE")
        self.assertEqual(ledger["safe_next_action"], "RELEASE_CAPITAL_LOCK")

    def test_position_mismatch_halts_graph_and_keeps_capital_locked(self):
        graph, execution, rec, ledger = completed_context(supplied=False)
        self.assertEqual(rec["status"], "DISPUTED")
        ledger = apply_reconciliation(ledger, rec, at=AT_AFTER)
        self.assertEqual(ledger["status"], "HALTED")
        self.assertEqual(ledger["capital_status"], "LOCKED")
        self.assertEqual(ledger["safe_next_action"],
                         "INVESTIGATE_POST_STATE_MISMATCH")

    def test_solid_execution_failure_stops_remaining_graph(self):
        graph, step, before, approved, validation = signed_context()
        record = prepare_submission(validation, at="2026-09-28T12:01:00Z")
        record = record_broadcast_attempt(record, attempt(record))
        failed = assess_execution_observation(record, observation(
            record, "SOLID_EXECUTION_FAILED"))
        ledger = start_execution_ledger(graph, reservation(
            graph, "reservation-graph-2"), started_at="2026-09-28T12:01:00Z")
        ledger = apply_execution_result(ledger, failed, at="2026-09-28T12:02:10Z")
        self.assertEqual(ledger["status"], "HALTED")
        self.assertEqual(ledger["capital_status"], "LOCKED")
        target = next(item for item in ledger["steps"] if item["step_hash"] == step[
            "step_hash"])
        self.assertEqual(target["status"], "FAILED")

    def test_reorg_after_completion_relocks_and_binds_original_receipt_block(self):
        graph, execution, rec, ledger = completed_context()
        ledger = apply_reconciliation(ledger, rec, at=AT_AFTER)
        evidence = {"schema_version": "economic-graph-execution-exception-1",
            "step_id": rec["step_id"], "txid": rec["txid"],
            "receipt_record_hash": rec["receipt_record_hash"],
            "reconciliation_hash": rec["reconciliation_hash"],
            "observed_at": "2026-09-28T12:03:00Z",
            "prior_block_number": execution["block"]["number"],
            "prior_block_id": execution["block"]["block_id"],
            "source_hash": "f" * 64}
        relocked = record_reorg(ledger, evidence)
        self.assertEqual(relocked["status"], "HALTED")
        self.assertEqual(relocked["capital_status"], "LOCKED")
        bad = copy.deepcopy(evidence)
        bad["prior_block_id"] = (execution["block"]["number"].to_bytes(8, "big").hex()
                                 + "99" * 24)
        with self.assertRaisesRegex(MachineError, "does not bind"):
            record_reorg(ledger, bad)

    def test_ledger_start_requires_active_exact_graph_reservation_evidence(self):
        graph, step, before, approved, validation = signed_context()
        bad = reservation(graph)
        bad["graph_hash"] = "f" * 64
        bad.pop("evidence_hash")
        bad["evidence_hash"] = digest({"domain": bad["schema_version"], "evidence": bad})
        with self.assertRaisesRegex(MachineError, "does not bind"):
            start_execution_ledger(graph, bad, started_at="2026-09-28T12:01:00Z")


if __name__ == "__main__":
    unittest.main()
