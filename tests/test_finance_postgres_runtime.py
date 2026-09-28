import asyncio
import base64
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from economic_machine.values import MachineError, digest
from finance_service.postgres_repository import PostgresOperationalRepository
from scripts.apply_postgres_migrations import apply

ROOT = Path(__file__).resolve().parents[1]
AT = "2026-09-29T03:00:00Z"
SCOPE = {"tenant_id": "tenant-a", "owner_id": "owner-a",
         "wallet": "41" + "11" * 20, "network": "tron-mainnet"}
OTHER_SCOPE = {"tenant_id": "tenant-b", "owner_id": "owner-b",
               "wallet": "41" + "22" * 20, "network": "tron-mainnet"}
REQUIRED_ENV = ("PR11_POSTGRES_ADMIN_DSN", "PR11_POSTGRES_API_DSN",
                "PR11_POSTGRES_WORKER_DSN")


@unittest.skipUnless(all(os.environ.get(key) for key in REQUIRED_ENV),
                     "isolated PostgreSQL DSNs not configured")
class PostgresRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg

        cls.psycopg = psycopg
        cls.admin_dsn = os.environ["PR11_POSTGRES_ADMIN_DSN"]
        cls.api_dsn = os.environ["PR11_POSTGRES_API_DSN"]
        cls.worker_dsn = os.environ["PR11_POSTGRES_WORKER_DSN"]

    def setUp(self):
        with self.psycopg.connect(self.admin_dsn, autocommit=True) as db:
            db.execute("TRUNCATE finance_service_outbox,finance_service_jobs,"
                "finance_service_records,finance_public_observations,"
                "finance_service_routines,finance_service_journal RESTART IDENTITY CASCADE")
        self.repo = PostgresOperationalRepository(self.api_dsn, self.worker_dsn)

    def enqueue(self, scope, subject, dependency, *, priority=0, max_attempts=3):
        return self.repo.enqueue_job(scope, role="VAULT", routine_id="treasury",
            job_kind="REVIEW", subject_id=subject, dependency_hash=digest(dependency),
            payload={"subject": subject}, not_before=AT,
            expires_at="2026-09-29T04:00:00Z", priority=priority,
            max_attempts=max_attempts)

    def test_migrations_are_hash_pinned_and_runtime_roles_are_separate(self):
        with self.psycopg.connect(self.admin_dsn) as db:
            migrations = db.execute("SELECT version,sha256 FROM finance_schema_migrations "
                                    "ORDER BY version").fetchall()
            roles = dict(db.execute("SELECT rolname,rolsuper FROM pg_roles WHERE rolname IN "
                "('pr11_api','pr11_worker')").fetchall())
        self.assertEqual([row[0] for row in migrations],
                         ["009_finance_service", "011_finance_postgres_runtime"])
        self.assertTrue(all(len(row[1]) == 64 for row in migrations))
        self.assertEqual(roles, {"pr11_api": False, "pr11_worker": False})
        api_only = PostgresOperationalRepository(self.api_dsn)
        self.assertFalse(api_only.health()["worker_credential_loaded"])
        with self.assertRaisesRegex(MachineError, "worker DSN is required"):
            api_only.claim_job(worker_id="api-must-not-claim", at=AT)

    def test_api_entrypoint_refuses_worker_credential(self):
        env = dict(os.environ)
        env.update({
            "FINANCE_SERVICE_SESSION_KEY_ID": "key-1",
            "FINANCE_SERVICE_SESSION_HMAC_B64": base64.b64encode(b"k" * 32).decode(),
            "FINANCE_SERVICE_POSTGRES_API_DSN": self.api_dsn,
            "FINANCE_SERVICE_POSTGRES_WORKER_DSN": self.worker_dsn,
            "PYTHONPATH": str(ROOT / "src"),
        })
        result = subprocess.run(
            [sys.executable, "-c", "import finance_service.entrypoint"],
            cwd=ROOT, env=env, text=True, capture_output=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("API service must not load the PostgreSQL worker DSN",
                      result.stderr)

    def test_applied_migration_hash_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            original = ROOT / "db/migrations/009_finance_service.sql"
            tampered = directory / original.name
            tampered.write_text(original.read_text() + "\n-- changed after apply\n")
            current = ROOT / "db/migrations/011_finance_postgres_runtime.sql"
            with self.assertRaisesRegex(RuntimeError, "applied migration hash changed"):
                apply(self.admin_dsn, [tampered, current])

    def test_rls_scope_optimistic_version_and_restart_recovery(self):
        created = self.repo.put_record(SCOPE, "PLAN", "plan-1", {"value": 1},
                                       expected_version=0, at=AT)
        self.assertEqual(created["version"], 1)
        with self.assertRaisesRegex(MachineError, "authenticated scope"):
            self.repo.get_record(OTHER_SCOPE, "PLAN", "plan-1")
        with self.assertRaisesRegex(MachineError, "version conflict"):
            self.repo.put_record(SCOPE, "PLAN", "plan-1", {"value": 2},
                                 expected_version=0, at=AT)
        restarted = PostgresOperationalRepository(self.api_dsn, self.worker_dsn)
        self.assertEqual(restarted.get_record(SCOPE, "PLAN", "plan-1")["body"],
                         {"value": 1})
        self.assertEqual(restarted.health()["backend"], "POSTGRESQL")
        with self.psycopg.connect(self.api_dsn) as db:
            db.execute("SELECT set_config('app.finance_scope_hash',%s,true)",
                       (digest({"domain": "finance-service-scope-1",
                                "scope": SCOPE}),))
            self.assertEqual(db.execute("SELECT count(*) FROM finance_service_records").fetchone()[0], 1)
            db.execute("SELECT set_config('app.finance_scope_hash',%s,true)",
                       (digest({"domain": "finance-service-scope-1",
                                "scope": OTHER_SCOPE}),))
            self.assertEqual(db.execute("SELECT count(*) FROM finance_service_records").fetchone()[0], 0)

    def test_account_serialization_completion_and_expired_lease_recovery(self):
        first = self.enqueue(SCOPE, "first", {"n": 1}, priority=100, max_attempts=2)
        second = self.enqueue(SCOPE, "second", {"n": 2}, priority=90)
        other = self.enqueue(OTHER_SCOPE, "other", {"n": 3}, priority=80)
        lease_one = self.repo.claim_job(worker_id="worker-1", at=AT, lease_seconds=5)
        lease_two = self.repo.claim_job(worker_id="worker-2", at=AT, lease_seconds=5)
        self.assertEqual(lease_one["job_id"], first["job_id"])
        self.assertEqual(lease_two["job_id"], other["job_id"])
        self.assertIsNone(self.repo.claim_job(worker_id="worker-3", at=AT))
        completed = self.repo.complete_job(job_id=lease_one["job_id"],
            worker_id="worker-1", lease_token=lease_one["lease_token"],
            result={"status": "reviewed"}, at="2026-09-29T03:00:04Z")
        self.assertEqual(completed["status"], "SUCCEEDED")
        lease_three = self.repo.claim_job(worker_id="worker-3",
            at="2026-09-29T03:00:05Z", lease_seconds=5)
        self.assertEqual(lease_three["job_id"], second["job_id"])
        recovered = PostgresOperationalRepository(self.api_dsn, self.worker_dsn).claim_job(
            worker_id="worker-4", at="2026-09-29T03:00:10Z", lease_seconds=5)
        self.assertEqual(recovered["job_id"], second["job_id"])
        self.assertEqual(recovered["attempts"], 2)

    def test_concurrent_workers_never_claim_the_same_job(self):
        jobs = [self.enqueue(None, "public-" + str(i), {"n": i}, priority=10)
                for i in range(4)]
        barrier = __import__("threading").Barrier(4)

        def claim(index):
            barrier.wait()
            repo = PostgresOperationalRepository(self.api_dsn, self.worker_dsn)
            return repo.claim_job(worker_id="parallel-" + str(index), at=AT)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            leases = list(pool.map(claim, range(4)))
        claimed = [item["job_id"] for item in leases]
        self.assertEqual(len(set(claimed)), 4)
        self.assertEqual(set(claimed), {item["job_id"] for item in jobs})

    def test_observation_routine_and_logical_snapshot_survive_restart(self):
        observation = self.repo.put_public_observation("justlend", "markets", {"v": "1"},
            observed_at="2026-09-29T02:00:00Z", valid_until="2026-09-29T02:30:00Z")
        self.assertTrue(observation["changed"])
        refresh = self.repo.schedule_expired_observations(at=AT)
        self.assertEqual(len(refresh), 1)
        self.repo.put_routine(SCOPE, role="WATCH", routine_id="position-watch",
            responsibility="position TTL", status="ACTIVE", interval_seconds=60,
            next_due_at=AT, dependency_hash=digest({"position": 1}), at=AT)
        due = self.repo.schedule_due_routines(at=AT)
        self.assertEqual(len(due), 1)
        destination = Path("/tmp/pr11-postgres-logical-snapshot.json")
        exported = self.repo.export_verified_snapshot(destination)
        self.assertEqual(exported["status"], "VERIFIED_LOGICAL_SNAPSHOT")
        document = json.loads(destination.read_text())
        self.assertEqual(document["journal_root"], self.repo.journal_root())
        self.assertTrue(self.repo.verify_journal())

    def test_tampered_global_journal_blocks_scoped_mutation(self):
        self.repo.put_record(SCOPE, "PLAN", "plan-1", {"value": 1},
                             expected_version=0, at=AT)
        with self.psycopg.connect(self.admin_dsn) as db:
            db.execute("UPDATE finance_service_journal SET body_json='{}'::jsonb "
                       "WHERE ordinal=1")
            db.commit()
        self.assertFalse(self.repo.verify_journal())
        self.assertFalse(self.repo.health()["journal_integrity"])
        with self.assertRaisesRegex(MachineError, "journal integrity"):
            self.repo.put_record(SCOPE, "PLAN", "plan-2", {"value": 2},
                                 expected_version=0, at=AT)
        import httpx

        from finance_service.api import create_app
        app = create_app(session_verifier=object(), repository=self.repo, clock=lambda: AT)

        async def health():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                return await client.get("/healthz")
        response = asyncio.run(health())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "storage journal integrity failed")


if __name__ == "__main__":
    unittest.main()
