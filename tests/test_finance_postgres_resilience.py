import asyncio
import concurrent.futures
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from economic_machine.values import MachineError
from finance_service.postgres_repository import PostgresOperationalRepository
from finance_service.postgres_runtime import (
    DsnSecretSource,
    PostgresRuntimePolicy,
    validate_postgres_dsn,
)
from scripts.apply_postgres_migrations import apply
from scripts.postgres_physical_backup import _attestation

AT = "2026-09-29T03:00:00Z"
REQUIRED_ENV = ("PR11_POSTGRES_ADMIN_DSN", "PR11_POSTGRES_API_DSN",
                "PR11_POSTGRES_WORKER_DSN")


def scope(index):
    return {"tenant_id": "tenant-" + str(index), "owner_id": "owner-" + str(index),
            "wallet": "41" + format(index + 1, "040x"), "network": "tron-mainnet"}


@unittest.skipUnless(all(os.environ.get(key) for key in REQUIRED_ENV),
                     "isolated PostgreSQL DSNs not configured")
class PostgresResilienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg

        cls.psycopg = psycopg
        cls.admin_dsn = os.environ["PR11_POSTGRES_ADMIN_DSN"]
        cls.api_dsn = os.environ["PR11_POSTGRES_API_DSN"]
        cls.worker_dsn = os.environ["PR11_POSTGRES_WORKER_DSN"]
        with psycopg.connect(cls.admin_dsn, autocommit=True) as db:
            db.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE "
                "rolname='pr12_api_rotated') THEN CREATE ROLE pr12_api_rotated "
                "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE; END IF; END $$")
            db.execute("GRANT gwdc_finance_api TO pr12_api_rotated")
        cls.rotated_dsn = "postgresql://pr12_api_rotated@127.0.0.1:55432/gwdc_pr11"

    def setUp(self):
        self.repositories = []
        with self.psycopg.connect(self.admin_dsn, autocommit=True) as db:
            db.execute("TRUNCATE finance_service_outbox,finance_service_jobs,"
                "finance_service_records,finance_public_observations,"
                "finance_service_routines,finance_service_journal_heads,"
                "finance_service_journal RESTART IDENTITY CASCADE")
            db.execute("UPDATE finance_service_journal_audit SET integrity=true,"
                       "audited_root=NULL,audited_at=NULL,failure_code=NULL")

    def tearDown(self):
        for repository in self.repositories:
            repository.close()

    def repository(self, *, policy=None, api_dsn=None, api_dsn_file=None,
                   worker=True):
        repository = PostgresOperationalRepository(
            self.api_dsn if api_dsn is None and api_dsn_file is None else api_dsn,
            self.worker_dsn if worker else None,
            api_dsn_file=api_dsn_file, runtime_policy=policy,
            allow_insecure_localhost=True)
        self.repositories.append(repository)
        return repository

    def test_tls_policy_and_owner_only_secret_file_fail_closed(self):
        with self.assertRaisesRegex(MachineError, "sslmode=verify-full"):
            validate_postgres_dsn("postgresql://user@example.com/db",
                allow_insecure_localhost=False, connect_timeout_seconds=5)
        with self.assertRaisesRegex(MachineError, "root certificate"):
            validate_postgres_dsn(
                "postgresql://user@example.com/db?sslmode=verify-full",
                allow_insecure_localhost=False, connect_timeout_seconds=5)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "api.dsn"
            path.write_text(self.api_dsn)
            path.chmod(0o644)
            with self.assertRaisesRegex(MachineError, "owner-only"):
                DsnSecretSource(path=path).read()
            target = Path(temp) / "target.dsn"
            target.write_text(self.api_dsn)
            target.chmod(0o600)
            link = Path(temp) / "link.dsn"
            link.symlink_to(target)
            with self.assertRaisesRegex(MachineError, "unavailable"):
                DsnSecretSource(path=link).read()

    def test_destructive_schema_change_requires_verified_pitr_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            migration = Path(temp) / "999_destructive.sql"
            migration.write_text("DROP TABLE finance_service_records;\n")
            with self.assertRaisesRegex(RuntimeError, "requires verified PITR"):
                apply(self.admin_dsn, [migration])
        with self.psycopg.connect(self.admin_dsn) as db:
            table = db.execute("SELECT to_regclass('finance_service_records')").fetchone()[0]
            risks = dict(db.execute("SELECT version,risk_class FROM "
                                    "finance_schema_migrations").fetchall())
        self.assertEqual(table, "finance_service_records")
        self.assertEqual(set(risks.values()), {"ADDITIVE"})

    def test_backup_destination_requires_owner_only_off_host_encryption_attestation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "destination.json"
            path.write_text(json.dumps({
                "schema_version": "gwdc-backup-destination-1",
                "destination_id": "remote-vault-a",
                "encrypted_at_rest": True,
                "off_host": False,
            }))
            path.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "off-host attested"):
                _attestation(path)
            document = json.loads(path.read_text())
            document["off_host"] = True
            path.write_text(json.dumps(document))
            verified, sha256 = _attestation(path)
            self.assertEqual(verified["destination_id"], "remote-vault-a")
            self.assertEqual(len(sha256), 64)
            link = Path(temp) / "destination-link.json"
            link.symlink_to(path)
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                _attestation(link)

    def test_secret_file_atomic_rotation_replaces_pool_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "api.dsn"
            path.write_text(self.api_dsn)
            path.chmod(0o600)
            repository = self.repository(api_dsn_file=path, worker=False)
            first = repository.health()
            with repository.runtime.connection() as checked_out_before_rotation:
                replacement = Path(temp) / "next.dsn"
                replacement.write_text(self.rotated_dsn)
                replacement.chmod(0o600)
                os.replace(replacement, path)
                second = repository.health()
                self.assertEqual(checked_out_before_rotation.execute(
                    "SELECT current_user").fetchone()["current_user"], "pr11_api")
            self.assertEqual(first["api_database_role"], "pr11_api")
            self.assertEqual(second["api_database_role"], "pr12_api_rotated")
            self.assertEqual(first["credential_generation"], 1)
            self.assertEqual(second["credential_generation"], 2)

    def test_pool_is_bounded_times_out_and_exports_sanitized_metrics(self):
        policy = PostgresRuntimePolicy(min_pool_size=1, max_pool_size=1,
            max_waiting=1, pool_timeout_seconds=0.2, statement_timeout_ms=500)
        repository = self.repository(policy=policy, worker=False)
        result = []
        with repository.runtime.connection():
            thread = threading.Thread(target=lambda: result.append(self._health_error(repository)))
            thread.start()
            thread.join(timeout=2)
        self.assertEqual(result, ["PoolTimeout"])
        metrics = repository.prometheus_metrics()
        self.assertIn("gwdc_finance_pool_timeouts_total 1", metrics)
        self.assertNotIn("tenant-", metrics)
        self.assertNotIn("postgresql://", metrics)
        alerts = repository.health()["alerts"]
        self.assertIn({"code": "DATABASE_POOL_TIMEOUT_OBSERVED",
                       "severity": "WARNING"}, alerts)

    @staticmethod
    def _health_error(repository):
        from psycopg_pool import PoolTimeout

        try:
            repository.health()
        except PoolTimeout as exc:
            return type(exc).__name__
        return "NO_ERROR"

    def test_statement_timeout_cancels_query_and_pool_recovers(self):
        policy = PostgresRuntimePolicy(statement_timeout_ms=100,
                                       lock_timeout_ms=100)
        repository = self.repository(policy=policy, worker=False)
        with (self.assertRaisesRegex(self.psycopg.errors.QueryCanceled,
                                    "statement timeout"),
              repository.connect() as db):
            db.execute("SELECT pg_sleep(0.5)").fetchone()
        self.assertEqual(repository.health()["backend"], "POSTGRESQL")

    def test_independent_scope_streams_append_concurrently(self):
        repository = self.repository()
        barrier = threading.Barrier(8)

        def write(index):
            barrier.wait()
            return repository.put_record(scope(index), "PLAN", "plan-" + str(index),
                {"value": index}, expected_version=0, at=AT)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(write, range(8)))
        self.assertEqual(len(rows), 8)
        with self.psycopg.connect(self.admin_dsn) as db:
            heads = db.execute("SELECT count(*),min(sequence),max(sequence) "
                               "FROM finance_service_journal_heads").fetchone()
            versions = db.execute("SELECT array_agg(DISTINCT journal_version) "
                                  "FROM finance_service_journal").fetchone()[0]
        self.assertEqual(heads, (8, 1, 1))
        self.assertEqual(versions, [2])
        self.assertTrue(repository.verify_journal())

    def test_metrics_endpoint_contains_no_customer_labels(self):
        import httpx

        from finance_service.api import create_app
        repository = self.repository(worker=False)
        app = create_app(session_verifier=object(), repository=repository, clock=lambda: AT)

        async def request():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                return await client.get("/metrics")
        response = asyncio.run(request())
        self.assertEqual(response.status_code, 200)
        self.assertIn("gwdc_finance_api_transactions_total", response.text)
        self.assertNotIn("wallet", response.text)

    def test_health_uses_quick_heads_and_rejects_stale_deep_audit(self):
        from unittest import mock

        repository = self.repository(worker=False)
        with mock.patch.object(repository, "_verify_journal_db",
                               side_effect=AssertionError("deep scan on health")):
            healthy = repository.health()
        self.assertTrue(healthy["journal_deep_audit"]["fresh"])
        with self.psycopg.connect(self.admin_dsn, autocommit=True) as db:
            db.execute("UPDATE finance_service_journal_audit SET "
                       "audited_at=clock_timestamp()-interval '11 minutes'")
        stale = repository.health()
        self.assertFalse(stale["journal_integrity"])
        self.assertIn({"code": "JOURNAL_DEEP_AUDIT_STALE",
                       "severity": "CRITICAL"}, stale["alerts"])
        self.assertTrue(repository.verify_journal())
        self.assertTrue(repository.health()["journal_integrity"])
        repository.put_record(scope(1), "PLAN", "plan-1", {"value": 1},
                              expected_version=0, at=AT)
        with self.psycopg.connect(self.admin_dsn, autocommit=True) as db:
            db.execute("UPDATE finance_service_journal_heads SET event_hash=%s",
                       ("f" * 64,))
        mismatched = repository.health()
        self.assertFalse(mismatched["journal_integrity"])
        self.assertEqual(mismatched["journal_deep_audit"]["head_mismatches"], 1)


if __name__ == "__main__":
    unittest.main()
