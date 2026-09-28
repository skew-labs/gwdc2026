import asyncio
import copy
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from test_economic_position_reconciliation import (
    AT_AFTER,
    after_account,
    execution_context,
    post_state,
)
from test_economic_tron_execution import observation, signed_context

from economic_machine.position_reconciliation import reconcile_position
from economic_machine.tron_execution import (
    assess_execution_observation,
    prepare_submission,
)
from economic_machine.values import MachineError
from finance_service.auth import HmacSessionVerifier
from finance_service.context import AuthenticatedContext
from finance_service.execution_service import ExecutionService
from finance_service.operational_repository import OperationalRepository
from finance_service.scheduler import FinanceScheduler
from finance_service.worker import FinanceWorker, RetryableJobError

AT = "2026-09-29T03:00:00Z"
SCOPE = {"tenant_id": "tenant-a", "owner_id": "owner-a",
         "wallet": "41" + "11" * 20, "network": "tron-mainnet"}
OTHER = {"tenant_id": "tenant-b", "owner_id": "owner-b",
         "wallet": "41" + "22" * 20, "network": "tron-mainnet"}


def assertion(scope=SCOPE):
    return {"schema_version": "verified-wallet-assertion-1",
        "assertion_id": "assertion-1", "scope": copy.deepcopy(scope),
        "challenge_hash": "1" * 64, "signature_hash": "2" * 64,
        "verified_at": "2026-09-29T02:59:00Z",
        "valid_until": "2026-09-29T04:00:00Z",
        "verification_status": "VERIFIED", "verifier_id": "wallet-proof-adapter",
        "evidence_hash": "3" * 64, "execution_authority": "NONE"}


def enqueue(repo, scope=SCOPE, *, subject="plan-1", dependency="a", max_attempts=3,
            expires="2026-09-29T04:00:00Z"):
    return repo.enqueue_job(scope, role="VAULT", routine_id="treasury-manager",
        job_kind="RECONCILE_POSITION", subject_id=subject,
        dependency_hash=dependency * 64, payload={"plan_hash": "f" * 64},
        not_before=AT, expires_at=expires, max_attempts=max_attempts, priority=10)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.now = AT
        self.active = True
        self.verifier = HmacSessionVerifier({"key-1": b"k" * 32},
            active_key_id="key-1", clock=lambda: self.now,
            is_session_active=lambda _session, _scope: self.active)

    def test_verified_assertion_issues_tamper_evident_scope_bound_session(self):
        token = self.verifier.issue(assertion(), session_id="session-1",
                                    trace_id="trace-1", ttl_seconds=600)
        context = self.verifier.authenticate(token)
        self.assertEqual(context.scope, SCOPE)
        self.assertNotIn("11" * 32, token)
        encoded, signature = token.split(".")
        tampered = encoded[:-1] + ("A" if encoded[-1] != "A" else "B") + "." + signature
        with self.assertRaises(MachineError):
            self.verifier.authenticate(tampered)
        self.active = False
        with self.assertRaisesRegex(MachineError, "revoked"):
            self.verifier.authenticate(token)

    def test_expired_assertion_session_and_extra_credential_fields_fail_closed(self):
        bad = assertion()
        bad["private_key"] = "never"
        with self.assertRaisesRegex(MachineError, "exactly"):
            self.verifier.issue(bad, session_id="session-1", trace_id="trace-1",
                                ttl_seconds=600)
        token = self.verifier.issue(assertion(), session_id="session-1",
                                    trace_id="trace-1", ttl_seconds=60)
        self.now = "2026-09-29T03:01:00Z"
        with self.assertRaisesRegex(MachineError, "expired"):
            self.verifier.authenticate(token)


class OperationalRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "operations.sqlite3"
        self.repo = OperationalRepository(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_private_records_are_scope_isolated_and_optimistically_serialized(self):
        first = self.repo.put_record(SCOPE, "PLAN", "plan-1", {"value": 1},
                                     expected_version=0, at=AT)
        self.assertEqual(first["version"], 1)
        with self.assertRaisesRegex(MachineError, "authenticated scope"):
            self.repo.get_record(OTHER, "PLAN", "plan-1")

        def change(value):
            try:
                self.repo.put_record(SCOPE, "PLAN", "plan-1", {"value": value},
                                     expected_version=1, at="2026-09-29T03:00:01Z")
                return "committed"
            except MachineError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, (2, 3)))
        self.assertCountEqual(results, ["committed", "conflict"])
        self.assertTrue(self.repo.verify_journal())

    def test_dependency_idempotence_and_sensitive_payload_rejection(self):
        first = enqueue(self.repo)
        same = enqueue(self.repo)
        changed = enqueue(self.repo, dependency="b")
        self.assertFalse(first["idempotent"])
        self.assertTrue(same["idempotent"])
        self.assertNotEqual(first["job_id"], changed["job_id"])
        for field in ("private_key", "privateKey", "x-api-key", "seedPhrase"):
            with self.subTest(field=field), self.assertRaisesRegex(
                    MachineError, "sensitive credential"):
                self.repo.enqueue_job(SCOPE, role="VAULT", routine_id="x",
                    job_kind="RECONCILE_POSITION", subject_id="secret-" + field,
                    dependency_hash="c" * 64, payload={field: "no"},
                    not_before=AT, expires_at="2026-09-29T04:00:00Z")

    def test_active_job_count_is_bounded_per_authenticated_scope(self):
        for index in range(128):
            self.repo.enqueue_job(SCOPE, role="WATCH", routine_id="bounded-watch",
                job_kind="MONITOR_POLICY", subject_id="policy-" + str(index),
                dependency_hash=f"{index + 1:064x}", payload={"index": index},
                not_before=AT, expires_at="2026-09-29T04:00:00Z")
        with self.assertRaisesRegex(MachineError, "active job limit"):
            self.repo.enqueue_job(SCOPE, role="WATCH", routine_id="bounded-watch",
                job_kind="MONITOR_POLICY", subject_id="policy-overflow",
                dependency_hash="f" * 64, payload={"index": 129},
                not_before=AT, expires_at="2026-09-29T04:00:00Z")

    def test_same_wallet_jobs_serialize_but_independent_wallet_can_run(self):
        enqueue(self.repo, subject="plan-1", dependency="a")
        enqueue(self.repo, subject="plan-2", dependency="b")
        same_wallet_other_tenant = {**OTHER, "wallet": SCOPE["wallet"]}
        enqueue(self.repo, same_wallet_other_tenant, subject="plan-3", dependency="c")
        enqueue(self.repo, OTHER, subject="plan-4", dependency="d")
        lease_one = self.repo.claim_job(worker_id="worker-1", at=AT)
        lease_two = self.repo.claim_job(worker_id="worker-2", at=AT)
        self.assertNotEqual(lease_one["account_key"], lease_two["account_key"])
        self.repo.complete_job(job_id=lease_one["job_id"], worker_id="worker-1",
            lease_token=lease_one["lease_token"], result={"ok": True}, at=AT)
        lease_three = self.repo.claim_job(worker_id="worker-3", at=AT)
        self.assertEqual(lease_three["account_key"], lease_one["account_key"])

    def test_crash_expiry_reclaims_same_job_then_stops_at_attempt_limit(self):
        created = enqueue(self.repo, max_attempts=2)
        first = self.repo.claim_job(worker_id="worker-1", at=AT, lease_seconds=5)
        second = self.repo.claim_job(worker_id="worker-2", at="2026-09-29T03:00:05Z",
                                     lease_seconds=5)
        self.assertEqual((created["job_id"], first["job_id"], second["job_id"]),
                         (created["job_id"],) * 3)
        self.assertEqual(second["attempts"], 2)
        self.assertIsNone(self.repo.claim_job(worker_id="worker-3",
            at="2026-09-29T03:00:10Z", lease_seconds=5))
        self.assertEqual(self.repo.list_jobs(SCOPE)[0]["status"], "FAILED")

    def test_retry_backoff_is_bounded_and_worker_never_gains_execution_authority(self):
        enqueue(self.repo, max_attempts=2)
        worker = FinanceWorker(self.repo, {"RECONCILE_POSITION": lambda _job: (
            _ for _ in ()).throw(RetryableJobError("PROVIDER_UNAVAILABLE"))},
            worker_id="worker-1", base_backoff_seconds=5)
        first = worker.run_one(at=AT)
        self.assertEqual(first["status"], "RETRY_WAIT")
        self.assertEqual(worker.run_one(at="2026-09-29T03:00:04Z")["status"], "IDLE")
        second = worker.run_one(at="2026-09-29T03:00:05Z")
        self.assertEqual(second["status"], "FAILED")
        self.assertEqual(second["execution_authority"], "NONE")

    def test_unchanged_observation_still_schedules_ttl_refresh_once(self):
        value = {"price": "1.0", "source": "official"}
        first = self.repo.put_public_observation("tron-source", "usdt-rate", value,
            observed_at=AT, valid_until="2026-09-29T03:01:00Z")
        same = self.repo.put_public_observation("tron-source", "usdt-rate", value,
            observed_at="2026-09-29T03:00:30Z", valid_until="2026-09-29T03:01:30Z")
        self.assertTrue(first["changed"])
        self.assertFalse(same["changed"])
        self.assertEqual(first["version"], same["version"])
        scheduler = FinanceScheduler(self.repo)
        self.assertEqual(scheduler.tick(at="2026-09-29T03:01:00Z")["scheduled_count"], 0)
        due = scheduler.tick(at="2026-09-29T03:01:30Z")
        repeated = scheduler.tick(at="2026-09-29T03:01:30Z")
        self.assertEqual(due["scheduled_count"], 1)
        self.assertEqual(due["observation_job_ids"], repeated["observation_job_ids"])

    def test_roles_pause_and_rebalance_cost_cooldown_gate(self):
        self.repo.put_routine(SCOPE, role="WATCH", routine_id="watch-risk",
            responsibility="Watch liquidation and source TTL", status="ACTIVE",
            interval_seconds=60, next_due_at=AT, dependency_hash="a" * 64, at=AT)
        self.repo.put_routine(SCOPE, role="ALPHA", routine_id="alpha-plan",
            responsibility="Refresh allocation candidates", status="PAUSED",
            interval_seconds=60, next_due_at=AT, dependency_hash="b" * 64, at=AT)
        tick = FinanceScheduler(self.repo).tick(at=AT)
        self.assertEqual(len(tick["routine_job_ids"]), 1)
        with self.repo.connect() as db:
            advanced = db.execute("SELECT count(*) FROM service_journal "
                "WHERE event_kind='ROUTINE_ADVANCED'").fetchone()[0]
        self.assertEqual(advanced, 1)
        self.assertEqual(FinanceScheduler(self.repo).tick(at=AT)["routine_job_ids"], [])
        held = self.repo.schedule_rebalance(SCOPE, routine_id="treasury-manager",
            subject_id="portfolio-1", dependency_hash="c" * 64,
            payload={"plan_hash": "d" * 64}, expected_benefit_base_units="10",
            estimated_cost_base_units="10", last_completed_at=None,
            cooldown_seconds=300, at=AT, expires_at="2026-09-29T03:10:00Z")
        cooldown = self.repo.schedule_rebalance(SCOPE, routine_id="treasury-manager",
            subject_id="portfolio-1", dependency_hash="d" * 64,
            payload={"plan_hash": "e" * 64}, expected_benefit_base_units="20",
            estimated_cost_base_units="10", last_completed_at="2026-09-29T02:59:00Z",
            cooldown_seconds=300, at=AT, expires_at="2026-09-29T03:10:00Z")
        self.assertEqual(held["reason_codes"], ["EXPECTED_BENEFIT_NOT_ABOVE_COST"])
        self.assertEqual(cooldown["reason_codes"], ["REBALANCE_COOLDOWN_ACTIVE"])

    def test_backup_restore_preserves_records_jobs_and_journal_root(self):
        self.repo.put_record(SCOPE, "PLAN", "plan-1", {"value": 1},
                             expected_version=0, at=AT)
        enqueue(self.repo)
        root = self.repo.journal_root()
        backup_path = Path(self.temp.name) / "backup.sqlite3"
        result = self.repo.backup_to(backup_path)
        restored = OperationalRepository(backup_path)
        self.assertEqual(result["journal_root"], root)
        self.assertEqual(restored.journal_root(), root)
        self.assertEqual(restored.get_record(SCOPE, "PLAN", "plan-1")["body"],
                         {"value": 1})

    def test_tampered_journal_is_detected(self):
        enqueue(self.repo)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE service_journal SET body_json='{}' WHERE ordinal=1")
        self.assertFalse(self.repo.verify_journal())
        with self.assertRaisesRegex(MachineError, "mutation denied"):
            enqueue(self.repo, subject="another", dependency="b")
        with self.assertRaisesRegex(MachineError, "journal integrity"):
            self.repo.backup_to(Path(self.temp.name) / "bad.sqlite3")

    def test_pr08_unknown_and_solid_execution_schedule_only_the_next_safe_read(self):
        service = ExecutionService(self.repo, lambda: "2026-09-28T12:02:30Z")
        _, _, _, _, validation = signed_context()
        record = prepare_submission(validation, at="2026-09-28T12:01:00Z")
        unknown = assess_execution_observation(record, observation(record, "NOT_OBSERVED"))
        context = AuthenticatedContext(**unknown["scope"], session_id="session-a",
            issued_at="2026-09-28T12:00:00Z", expires_at="2026-09-28T13:00:00Z",
            trace_id="trace-a")
        watched = service.record_execution(context, unknown, expected_version=0)
        self.assertEqual(watched["job"]["job_kind"], "OBSERVE_TRON_TRANSACTION")
        self.assertEqual(watched["job"]["role"], "WATCH")
        _, _, _, _, solid = execution_context()
        reconciler = service.record_execution(context, solid, expected_version=1)
        self.assertEqual(reconciler["job"]["job_kind"], "RECONCILE_POSITION")
        self.assertEqual(reconciler["job"]["role"], "VAULT")
        self.assertEqual(reconciler["execution_authority"], "NONE")

    def test_pr08_reconciliation_is_scope_bound_and_dispute_opens_investigation(self):
        graph, _, before, approved, execution = execution_context()
        after = after_account(before, supplied=False)
        after["snapshot_hash"] = "8" * 64
        disputed = reconcile_position(graph, approved, execution, before,
            post_state(execution, after), at=AT_AFTER)
        context = AuthenticatedContext(**disputed["scope"], session_id="session-a",
            issued_at="2026-09-28T12:00:00Z", expires_at="2026-09-28T13:00:00Z",
            trace_id="trace-a")
        service = ExecutionService(self.repo, lambda: AT_AFTER)
        result = service.record_reconciliation(context, disputed, expected_version=0)
        self.assertEqual(result["service_status"], "DISPUTED_AND_HELD")
        self.assertEqual(result["job"]["job_kind"], "INVESTIGATE_RECONCILIATION")
        self.assertEqual(result["capital_status"], "LOCKED_DISPUTED")
        other = AuthenticatedContext(**OTHER, session_id="session-b",
            issued_at="2026-09-28T12:00:00Z", expires_at="2026-09-28T13:00:00Z",
            trace_id="trace-b")
        with self.assertRaisesRegex(MachineError, "authenticated context"):
            service.record_reconciliation(other, disputed, expected_version=0)


@unittest.skipUnless(importlib.util.find_spec("fastapi") and importlib.util.find_spec("httpx"),
                     "service optional dependencies not installed")
class FastApiTests(unittest.TestCase):
    def test_api_uses_bearer_scope_and_rejects_private_key_payload(self):
        import httpx

        from finance_service.api import create_app
        with tempfile.TemporaryDirectory() as temp:
            repo = OperationalRepository(Path(temp) / "api.sqlite3")
            verifier = HmacSessionVerifier({"key-1": b"k" * 32},
                active_key_id="key-1", clock=lambda: AT)
            token_a = verifier.issue(assertion(SCOPE), session_id="session-a",
                                      trace_id="trace-a", ttl_seconds=600)
            token_b = verifier.issue(assertion(OTHER), session_id="session-b",
                                      trace_id="trace-b", ttl_seconds=600)
            app = create_app(session_verifier=verifier, repository=repo, clock=lambda: AT)
            headers_a, headers_b = ({"Authorization": "Bearer " + token_a},
                                    {"Authorization": "Bearer " + token_b})

            async def scenario():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                        base_url="http://test") as client:
                    saved = await client.put("/v1/records/USER_NOTE/note-1", headers=headers_a,
                        json={"expected_version": 0, "body": {"value": 1}})
                    self.assertEqual(saved.status_code, 200)
                    self.assertEqual((await client.get("/v1/records/USER_NOTE/note-1",
                        headers=headers_a)).status_code, 200)
                    self.assertEqual((await client.get("/v1/records/USER_NOTE/note-1",
                        headers=headers_b)).status_code, 409)
                    rejected = await client.put("/v1/records/USER_NOTE/secret",
                        headers=headers_a,
                        json={"expected_version": 0, "body": {"private_key": "never"}})
                    self.assertEqual(rejected.status_code, 409)
                    forged = await client.put("/v1/records/EXECUTION/tx-fake",
                        headers=headers_a,
                        json={"expected_version": 0, "body": {"status": "success"}})
                    self.assertEqual(forged.status_code, 422)
                    forbidden_job = await client.post("/v1/jobs", headers=headers_a,
                        json={"role": "VAULT", "routine_id": "no-broadcast",
                              "job_kind": "BROADCAST_TRANSACTION",
                              "subject_id": "tx-fake", "dependency_hash": "f" * 64,
                              "payload": {"txid": "fake"}, "ttl_seconds": 60,
                              "max_attempts": 1, "priority": 100})
                    self.assertEqual(forbidden_job.status_code, 422)
                    jobs = await client.get("/v1/jobs", headers=headers_a)
                    self.assertNotIn("lease_token", json.dumps(jobs.json()))

            asyncio.run(scenario())


class MigrationTests(unittest.TestCase):
    def test_postgres_migration_contains_rls_outbox_and_skip_locked_claim(self):
        sql = (Path(__file__).resolve().parents[1] /
               "db/migrations/009_finance_service.sql").read_text()
        for required in ("ENABLE ROW LEVEL SECURITY", "finance_service_outbox",
                         "FOR UPDATE SKIP LOCKED", "account_key",
                         "max_attempts", "finance_public_observations"):
            self.assertIn(required, sql)


if __name__ == "__main__":
    unittest.main()
