"""Persist PR08 execution facts and enqueue only their next safe read task."""

from datetime import datetime, timedelta

from economic_machine.position_reconciliation import verify_reconciliation
from economic_machine.tron_execution import verify_execution_result
from economic_machine.values import MachineError, utc

from .context import AuthenticatedContext


class ExecutionService:
    def __init__(self, repository, clock):
        self.repository = repository
        self.clock = clock

    def _context(self, context, claimed_scope):
        if not isinstance(context, AuthenticatedContext):
            raise MachineError("authenticated service context required")
        at = utc(self.clock())
        return context.authorize(at, claimed_scope), at

    @staticmethod
    def _expiry(at, seconds=300):
        return (datetime.fromisoformat(at) + timedelta(seconds=seconds)).isoformat()

    def record_execution(self, context, execution, *, expected_version):
        if not verify_execution_result(execution):
            raise MachineError("committed PR08 execution result required")
        scope, at = self._context(context, execution.get("scope"))
        status = execution.get("status")
        allowed = {"SUBMISSION_UNKNOWN", "SOLID_BODY_RECEIPT_PENDING",
                   "SOLID_EXECUTED_PENDING_POST_STATE", "SOLID_EXECUTION_FAILED"}
        if status not in allowed or execution.get("execution_authority") != "NONE":
            raise MachineError("unsupported or over-authorized execution result")
        subject_id = "tx-" + execution["txid"]
        stored = self.repository.put_record(scope, "EXECUTION", subject_id,
            execution, expected_version=expected_version, at=at)
        job = None
        if status in {"SUBMISSION_UNKNOWN", "SOLID_BODY_RECEIPT_PENDING"}:
            job = self.repository.enqueue_job(scope, role="WATCH",
                routine_id="watch-original-txid", job_kind="OBSERVE_TRON_TRANSACTION",
                subject_id=subject_id,
                dependency_hash=execution["execution_result_hash"],
                payload={"txid": execution["txid"],
                         "execution_result_hash": execution["execution_result_hash"],
                         "safe_next_action": execution["safe_next_action"]},
                not_before=at, expires_at=self._expiry(at), max_attempts=4, priority=80)
        elif status == "SOLID_EXECUTED_PENDING_POST_STATE":
            job = self.repository.enqueue_job(scope, role="VAULT",
                routine_id="reconcile-position", job_kind="RECONCILE_POSITION",
                subject_id=subject_id,
                dependency_hash=execution["execution_result_hash"],
                payload={"txid": execution["txid"],
                         "execution_result_hash": execution["execution_result_hash"],
                         "receipt_record_hash": execution["receipt_record_hash"]},
                not_before=at, expires_at=self._expiry(at), max_attempts=4, priority=90)
        return {"record": stored, "job": job,
            "service_status": "HELD_FAILURE" if status == "SOLID_EXECUTION_FAILED" else
                              "NEXT_READ_QUEUED" if job is not None else "RECORDED",
            "execution_authority": "NONE"}

    def record_reconciliation(self, context, reconciliation, *, expected_version):
        if not verify_reconciliation(reconciliation):
            raise MachineError("committed PR08 reconciliation required")
        scope, at = self._context(context, reconciliation.get("scope"))
        status = reconciliation.get("status")
        if status not in {"RECONCILED", "DISPUTED"} or reconciliation.get(
                "execution_authority") != "NONE":
            raise MachineError("unsupported or over-authorized reconciliation")
        subject_id = "tx-" + reconciliation["txid"]
        record_id = subject_id + ":" + reconciliation["step_id"]
        stored = self.repository.put_record(scope, "RECONCILIATION", record_id,
            reconciliation, expected_version=expected_version, at=at)
        job = None
        if status == "DISPUTED":
            job = self.repository.enqueue_job(scope, role="ALPHA",
                routine_id="investigate-post-state", job_kind="INVESTIGATE_RECONCILIATION",
                subject_id=subject_id,
                dependency_hash=reconciliation["reconciliation_hash"],
                payload={"txid": reconciliation["txid"],
                         "reconciliation_hash": reconciliation["reconciliation_hash"],
                         "reason_codes": reconciliation["reason_codes"]},
                not_before=at, expires_at=self._expiry(at, 1800),
                max_attempts=2, priority=100)
        return {"record": stored, "job": job,
            "service_status": "POSITION_VERIFIED" if status == "RECONCILED" else
                              "DISPUTED_AND_HELD",
            "capital_status": reconciliation["capital_status"],
            "execution_authority": "NONE"}
