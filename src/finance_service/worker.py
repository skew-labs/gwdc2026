"""One bounded worker turn over the durable service outbox."""

from collections.abc import Callable
from dataclasses import dataclass

from economic_machine.values import MachineError, ident, utc


@dataclass(frozen=True)
class RetryableJobError(Exception):
    code: str


@dataclass(frozen=True)
class PermanentJobError(Exception):
    code: str


class FinanceWorker:
    def __init__(self, repository, handlers: dict[str, Callable[[dict], dict]], *,
                 worker_id: str, role: str | None = None, lease_seconds: int = 30,
                 base_backoff_seconds: int = 5):
        self.repository = repository
        self.handlers = dict(handlers)
        self.worker_id = ident(worker_id, "worker id")
        self.role = role
        self.lease_seconds = lease_seconds
        self.base_backoff_seconds = base_backoff_seconds

    def run_one(self, *, at: str) -> dict:
        at = utc(at)
        job = self.repository.claim_job(worker_id=self.worker_id, role=self.role,
            at=at, lease_seconds=self.lease_seconds)
        if job is None:
            return {"status": "IDLE", "worker_id": self.worker_id,
                    "execution_authority": "NONE"}
        handler = self.handlers.get(job["job_kind"])
        if handler is None:
            failed = self.repository.fail_job(job_id=job["job_id"],
                worker_id=self.worker_id, lease_token=job["lease_token"],
                error_code="HANDLER_NOT_REGISTERED", retryable=False, at=at,
                base_backoff_seconds=self.base_backoff_seconds)
            return {"status": failed["status"], "job": failed,
                    "execution_authority": "NONE"}
        try:
            result = handler(job)
            if not isinstance(result, dict):
                raise PermanentJobError("HANDLER_RESULT_NOT_OBJECT")
        except RetryableJobError as exc:
            failed = self.repository.fail_job(job_id=job["job_id"],
                worker_id=self.worker_id, lease_token=job["lease_token"],
                error_code=ident(exc.code, "retryable job error"), retryable=True,
                at=at, base_backoff_seconds=self.base_backoff_seconds)
            return {"status": failed["status"], "job": failed,
                    "execution_authority": "NONE"}
        except PermanentJobError as exc:
            failed = self.repository.fail_job(job_id=job["job_id"],
                worker_id=self.worker_id, lease_token=job["lease_token"],
                error_code=ident(exc.code, "permanent job error"), retryable=False,
                at=at, base_backoff_seconds=self.base_backoff_seconds)
            return {"status": failed["status"], "job": failed,
                    "execution_authority": "NONE"}
        except MachineError:
            failed = self.repository.fail_job(job_id=job["job_id"],
                worker_id=self.worker_id, lease_token=job["lease_token"],
                error_code="MACHINE_VALIDATION_FAILED", retryable=False, at=at,
                base_backoff_seconds=self.base_backoff_seconds)
            return {"status": failed["status"], "job": failed,
                    "execution_authority": "NONE"}
        completed = self.repository.complete_job(job_id=job["job_id"],
            worker_id=self.worker_id, lease_token=job["lease_token"],
            result=result, at=at)
        return {"status": "SUCCEEDED", "job": completed,
                "execution_authority": "NONE"}
