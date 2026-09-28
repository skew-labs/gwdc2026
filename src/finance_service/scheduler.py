"""TTL and routine scheduler with dependency-keyed idempotency."""

from economic_machine.values import utc


class FinanceScheduler:
    def __init__(self, repository):
        self.repository = repository

    def tick(self, *, at: str) -> dict:
        at = utc(at)
        observation_jobs = self.repository.schedule_expired_observations(at=at)
        routine_jobs = self.repository.schedule_due_routines(at=at)
        return {"schema_version": "finance-scheduler-tick-1", "at": at,
            "observation_job_ids": [item["job_id"] for item in observation_jobs],
            "routine_job_ids": [item["job_id"] for item in routine_jobs],
            "scheduled_count": len(observation_jobs) + len(routine_jobs),
            "execution_authority": "NONE"}
