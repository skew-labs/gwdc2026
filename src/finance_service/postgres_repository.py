"""PostgreSQL operational repository for the hosted finance service.

The API and worker use different database roles.  API transactions set a
scope hash before touching RLS-protected rows.  Worker transactions use the
separately provisioned worker role for cross-scope scheduling and leases.
Neither connection carries signing or chain execution authority.
"""

import json
import secrets
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from economic_machine.values import MachineError, canonical, digest, ident, utc

from .alerts import evaluate_storage_alerts
from .operational_repository import (
    ROLES,
    ROUTINE_STATES,
    _account_key,
    _hash,
    _reject_sensitive,
    _scope,
)
from .postgres_runtime import (
    DsnSecretSource,
    PostgresPoolRuntime,
    PostgresRuntimePolicy,
    RuntimeMetrics,
)


def _driver():
    try:
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise RuntimeError("PostgreSQL service dependencies are not installed") from exc
    return psycopg, dict_row, Jsonb


def _json_value(value):
    if value is None or isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _timestamp(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return utc(value)


def _normalized_row(row):
    result = dict(row)
    for key, value in tuple(result.items()):
        if isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


class PostgresOperationalRepository:
    """RLS-scoped API state plus worker leases on PostgreSQL 16 or newer."""

    MAX_DEEP_AUDIT_AGE_SECONDS = 600

    def __init__(self, api_dsn=None, worker_dsn=None, *, api_dsn_file=None,
                 worker_dsn_file=None, runtime_policy=None,
                 allow_insecure_localhost=False, pool_runtime=None):
        if pool_runtime is None:
            api_source = DsnSecretSource(value=api_dsn, path=api_dsn_file,
                                         label="PostgreSQL API DSN")
            worker_source = None
            if worker_dsn is not None or worker_dsn_file is not None:
                worker_source = DsnSecretSource(value=worker_dsn, path=worker_dsn_file,
                                                label="PostgreSQL worker DSN")
            pool_runtime = PostgresPoolRuntime(api_source, worker_source,
                policy=runtime_policy or PostgresRuntimePolicy(),
                allow_insecure_localhost=allow_insecure_localhost)
        if not isinstance(pool_runtime, PostgresPoolRuntime):
            raise MachineError("invalid PostgreSQL pool runtime")
        self.runtime = pool_runtime
        self.metrics = RuntimeMetrics()
        with self.connect() as db:
            identity = db.execute("SELECT current_user AS role,"
                "current_setting('server_version_num') AS server_version_num").fetchone()
            version = identity["server_version_num"]
            self.api_role = identity["role"]
            self.server_version = int(version)
            if self.server_version < 160000:
                raise MachineError("PostgreSQL 16 or newer is required")
            required = db.execute(
                "SELECT to_regclass('finance_service_records') AS records,"
                "to_regclass('finance_service_jobs') AS jobs,"
                "to_regclass('finance_service_journal') AS journal,"
                "to_regclass('finance_service_journal_heads') AS heads,"
                "to_regclass('finance_service_journal_audit') AS audit").fetchone()
            if any(required[key] is None for key in required):
                raise MachineError("finance service PostgreSQL migrations are incomplete")
        if self.runtime.worker_source is not None:
            with self.connect(worker=True) as db:
                worker = db.execute("SELECT current_user AS role,"
                    "pg_has_role(current_user,'gwdc_finance_worker','member') "
                    "AS authorized").fetchone()
            if worker["role"] == self.api_role or not worker["authorized"]:
                self.close()
                raise MachineError("PostgreSQL API and worker roles must be separated")
            self.worker_role = worker["role"]
        else:
            self.worker_role = None
        self.verify_journal()

    @contextmanager
    def connect(self, *, scope_hash=None, worker=False, repeatable_read=False):
        role = "worker" if worker else "api"
        started = time.monotonic()
        failed = False
        pool_timeout = False
        try:
            with self.runtime.connection(worker=worker) as db:
                if repeatable_read:
                    db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                db.execute("SELECT set_config('statement_timeout',%s,true),"
                    "set_config('lock_timeout',%s,true),"
                    "set_config('idle_in_transaction_session_timeout',%s,true)",
                    (str(self.runtime.policy.statement_timeout_ms),
                     str(self.runtime.policy.lock_timeout_ms),
                     str(self.runtime.policy.idle_transaction_timeout_ms)))
                identity = db.execute("SELECT current_user AS role,"
                    "pg_has_role(current_user,'gwdc_finance_api','member') AS api,"
                    "pg_has_role(current_user,'gwdc_finance_worker','member') AS worker"
                    ).fetchone()
                if worker and not identity["worker"]:
                    raise MachineError("PostgreSQL worker role is not authorized")
                if not worker and (not identity["api"] or identity["worker"]):
                    raise MachineError("PostgreSQL API role is not scope-only")
                if worker:
                    self.worker_role = identity["role"]
                else:
                    self.api_role = identity["role"]
                if scope_hash is not None:
                    _hash(scope_hash, "database scope hash")
                    db.execute("SELECT set_config('app.finance_scope_hash', %s, true)",
                               (scope_hash,))
                yield db
                db.commit()
        except Exception as exc:
            failed = True
            pool_timeout = type(exc).__name__ in {"PoolTimeout", "TooManyRequests"}
            raise
        finally:
            self.metrics.transaction(role, time.monotonic() - started,
                                     failed=failed, pool_timeout=pool_timeout)

    @staticmethod
    def _legacy_root(db):
        row = db.execute("SELECT event_hash FROM finance_service_journal "
            "WHERE journal_version=1 ORDER BY ordinal DESC LIMIT 1").fetchone()
        return "0" * 64 if row is None else row["event_hash"]

    @classmethod
    def _stream_id(cls, scope_hash):
        return "public" if scope_hash is None else _hash(scope_hash, "journal scope hash")

    @classmethod
    def _stream_genesis(cls, db, stream_id):
        return digest({"domain": "finance-service-journal-stream-genesis-1",
                       "legacy_root": cls._legacy_root(db), "stream_id": stream_id})

    @classmethod
    def _lock_stream(cls, db, scope_hash):
        stream_id = cls._stream_id(scope_hash)
        genesis = cls._stream_genesis(db, stream_id)
        db.execute("INSERT INTO finance_service_journal_heads"
            "(stream_id,sequence,event_hash) VALUES (%s,0,%s) "
            "ON CONFLICT (stream_id) DO NOTHING", (stream_id, genesis))
        head = db.execute("SELECT * FROM finance_service_journal_heads "
                          "WHERE stream_id=%s FOR UPDATE", (stream_id,)).fetchone()
        latest = db.execute("SELECT stream_sequence,event_hash FROM "
            "finance_service_journal WHERE journal_version=2 AND stream_id=%s "
            "ORDER BY stream_sequence DESC LIMIT 1", (stream_id,)).fetchone()
        if latest is None:
            valid = head["sequence"] == 0 and head["event_hash"] == genesis
        else:
            valid = (head["sequence"] == latest["stream_sequence"]
                     and head["event_hash"] == latest["event_hash"])
        if not valid:
            raise MachineError("service journal stream head mismatch")
        return stream_id, head

    @staticmethod
    def _verify_journal_db(db):
        previous = "0" * 64
        rows = db.execute(
            "SELECT * FROM finance_service_journal WHERE journal_version=1 "
            "ORDER BY ordinal").fetchall()
        for expected, raw in enumerate(rows, 1):
            row = _normalized_row(raw)
            body = _json_value(row["body_json"])
            event = {"ordinal": row["ordinal"], "event_id": row["event_id"],
                "event_kind": row["event_kind"], "scope_hash": row["scope_hash"],
                "input_hash": row["input_hash"], "output_hash": row["output_hash"],
                "body": body, "previous_hash": row["previous_hash"]}
            if (row["ordinal"] != expected or row["previous_hash"] != previous
                    or row["event_hash"] != digest(
                        {"domain": "finance-service-journal-event-1", "event": event})):
                return False
            previous = row["event_hash"]
        legacy_root = previous
        heads = {row["stream_id"]: row for row in db.execute(
            "SELECT * FROM finance_service_journal_heads ORDER BY stream_id").fetchall()}
        stream_rows = db.execute("SELECT * FROM finance_service_journal "
            "WHERE journal_version=2 ORDER BY stream_id,stream_sequence").fetchall()
        sequences = {}
        hashes = {}
        for raw in stream_rows:
            row = _normalized_row(raw)
            stream_id = row["stream_id"]
            expected_sequence = sequences.get(stream_id, 0) + 1
            prior = hashes.get(stream_id, digest({
                "domain": "finance-service-journal-stream-genesis-1",
                "legacy_root": legacy_root, "stream_id": stream_id}))
            body = _json_value(row["body_json"])
            event = {"event_id": row["event_id"], "event_kind": row["event_kind"],
                "scope_hash": row["scope_hash"], "input_hash": row["input_hash"],
                "output_hash": row["output_hash"], "body": body,
                "previous_hash": row["previous_hash"]}
            calculated = digest({"domain": "finance-service-journal-event-2",
                "stream_id": stream_id, "stream_sequence": expected_sequence,
                "event": event})
            if (row["stream_sequence"] != expected_sequence
                    or row["previous_hash"] != prior
                    or row["event_hash"] != calculated):
                return False
            sequences[stream_id], hashes[stream_id] = expected_sequence, calculated
        if set(heads) != set(sequences):
            return False
        for stream_id, sequence in sequences.items():
            if (heads[stream_id]["sequence"] != sequence
                    or heads[stream_id]["event_hash"] != hashes[stream_id]):
                return False
        return True

    @classmethod
    def _prepare_mutation(cls, db, scope_hash):
        audit = db.execute("SELECT integrity FROM finance_service_journal_audit "
                           "WHERE singleton=true").fetchone()
        if audit is None or not audit["integrity"]:
            raise MachineError("service journal integrity failed; mutation denied")

    @classmethod
    def _assert_read_safe(cls, db, scope_hash):
        audit = db.execute("SELECT integrity FROM finance_service_journal_audit "
                           "WHERE singleton=true").fetchone()
        if audit is None or not audit["integrity"]:
            raise MachineError("service journal integrity failed; read denied")
        stream_id = cls._stream_id(scope_hash)
        row = db.execute("SELECT head.sequence,head.event_hash,"
            "latest.stream_sequence,latest.latest_hash FROM "
            "finance_service_journal_heads head LEFT JOIN LATERAL "
            "(SELECT stream_sequence,event_hash AS latest_hash FROM "
            "finance_service_journal WHERE journal_version=2 AND stream_id=head.stream_id "
            "ORDER BY stream_sequence DESC LIMIT 1) latest ON true "
            "WHERE head.stream_id=%s", (stream_id,)).fetchone()
        if row is not None and (row["sequence"] != row["stream_sequence"]
                                or row["event_hash"] != row["latest_hash"]):
            raise MachineError("service journal stream head mismatch; read denied")

    @classmethod
    def _append(cls, db, event_id, event_kind, scope_hash, input_hash, output_hash, body):
        _, _, Jsonb = _driver()
        event_id = ident(event_id, "service event id")
        event_kind = ident(event_kind, "service event kind")
        _hash(input_hash, "service event input hash")
        _hash(output_hash, "service event output hash")
        stream_id, head = cls._lock_stream(db, scope_hash)
        sequence, previous = head["sequence"] + 1, head["event_hash"]
        event = {"event_id": event_id, "event_kind": event_kind,
            "scope_hash": scope_hash, "input_hash": input_hash,
            "output_hash": output_hash, "body": body, "previous_hash": previous}
        event_hash = digest({"domain": "finance-service-journal-event-2",
                             "stream_id": stream_id,
                             "stream_sequence": sequence, "event": event})
        db.execute("INSERT INTO finance_service_journal "
            "(event_id,event_kind,scope_hash,input_hash,output_hash,body_json,"
            "previous_hash,event_hash,journal_version,stream_id,stream_sequence) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,2,%s,%s)",
            (event_id, event_kind, scope_hash, input_hash, output_hash, Jsonb(body),
             previous, event_hash, stream_id, sequence))
        db.execute("UPDATE finance_service_journal_heads SET sequence=%s,event_hash=%s,"
                   "updated_at=clock_timestamp() WHERE stream_id=%s",
                   (sequence, event_hash, stream_id))
        return event_hash

    def verify_journal(self):
        with self.connect(repeatable_read=True) as db:
            integrity = self._verify_journal_db(db)
            root = self._journal_root_db(db) if integrity else None
        with self.connect() as db:
            db.execute("UPDATE finance_service_journal_audit SET integrity=%s,"
                "audited_root=%s,audited_at=clock_timestamp(),failure_code=%s "
                "WHERE singleton=true", (integrity, root,
                    None if integrity else "HASH_CHAIN_MISMATCH"))
            return integrity

    @classmethod
    def _journal_root_db(cls, db):
        heads = [{"stream_id": row["stream_id"], "sequence": row["sequence"],
                  "event_hash": row["event_hash"]} for row in db.execute(
            "SELECT * FROM finance_service_journal_heads ORDER BY stream_id").fetchall()]
        return digest({"domain": "finance-service-journal-root-2",
                       "legacy_root": cls._legacy_root(db), "streams": heads})

    def journal_root(self):
        with self.connect(repeatable_read=True) as db:
            if not self._verify_journal_db(db):
                raise MachineError("service journal integrity failed")
            root = self._journal_root_db(db)
        return root

    @classmethod
    def _quick_journal_status(cls, db):
        audit = db.execute("SELECT integrity,audited_root,audited_at,failure_code "
            "FROM finance_service_journal_audit WHERE singleton=true").fetchone()
        mismatches = db.execute("SELECT "
            "(SELECT count(*) FROM finance_service_journal_heads head "
            "LEFT JOIN LATERAL (SELECT stream_sequence,event_hash FROM "
            "finance_service_journal WHERE journal_version=2 "
            "AND stream_id=head.stream_id ORDER BY stream_sequence DESC LIMIT 1) "
            "latest ON true WHERE latest.stream_sequence IS NULL "
            "OR head.sequence IS DISTINCT FROM latest.stream_sequence "
            "OR head.event_hash IS DISTINCT FROM latest.event_hash) + "
            "(SELECT count(DISTINCT journal.stream_id) FROM finance_service_journal journal "
            "LEFT JOIN finance_service_journal_heads head "
            "ON head.stream_id=journal.stream_id WHERE journal.journal_version=2 "
            "AND head.stream_id IS NULL) AS count").fetchone()["count"]
        audited_at = None if audit is None else audit["audited_at"]
        age_seconds = None
        if audited_at is not None:
            age_seconds = max(0.0, (datetime.now(audited_at.tzinfo)
                                    - audited_at).total_seconds())
        fresh = (age_seconds is not None
                 and age_seconds <= cls.MAX_DEEP_AUDIT_AGE_SECONDS)
        quick_integrity = bool(audit and audit["integrity"] and mismatches == 0)
        return {"quick_integrity": quick_integrity,
                "deep_audit_fresh": fresh,
                "deep_audit_age_seconds": age_seconds,
                "audited_at": _timestamp(audited_at),
                "audited_root": None if audit is None else audit["audited_root"],
                "failure_code": None if audit is None else audit["failure_code"],
                "head_mismatches": mismatches}

    def health(self):
        with self.connect() as api:
            status = self._quick_journal_status(api)
        pool_stats = self.runtime.stats()
        journal_integrity = (status["quick_integrity"]
                             and status["deep_audit_fresh"])
        alerts = evaluate_storage_alerts(
            journal_integrity=status["quick_integrity"],
            journal_audit_fresh=status["deep_audit_fresh"],
            pool_stats=pool_stats, metrics=self.metrics.snapshot())
        return {"backend": "POSTGRESQL", "server_version": self.server_version,
                "api_role": "SCOPED", "api_database_role": self.api_role,
                "worker_credential_loaded": self.worker_role is not None,
                "credential_generation": pool_stats["generation"],
                "journal_integrity": journal_integrity,
                "journal_mode": "PARTITIONED_STREAM_V2",
                "journal_deep_audit": {
                    "fresh": status["deep_audit_fresh"],
                    "age_seconds": status["deep_audit_age_seconds"],
                    "audited_at": status["audited_at"],
                    "audited_root": status["audited_root"],
                    "failure_code": status["failure_code"],
                    "head_mismatches": status["head_mismatches"],
                },
                "alerts": alerts}

    def prometheus_metrics(self):
        return self.metrics.prometheus(self.runtime.stats())

    def reload_credentials(self):
        return self.runtime.reload(force=True)

    def close(self):
        self.runtime.close()

    def put_record(self, scope, record_kind, record_id, body, *, expected_version, at):
        _, _, Jsonb = _driver()
        scope, scope_hash = _scope(scope)
        kind = ident(record_kind, "record kind")
        record_id = ident(record_id, "record id")
        if type(expected_version) is not int or not 0 <= expected_version < 1 << 31:
            raise MachineError("invalid expected record version")
        if not isinstance(body, dict):
            raise MachineError("service record body must be an object")
        _reject_sensitive(body, "record")
        at, body_hash = utc(at), digest(body)
        with self.connect(scope_hash=scope_hash) as db:
            self._prepare_mutation(db, scope_hash)
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                       (scope_hash + ":" + kind + ":" + record_id,))
            row = db.execute("SELECT version,body_hash,updated_at FROM "
                "finance_service_records WHERE scope_hash=%s AND record_kind=%s "
                "AND record_id=%s FOR UPDATE", (scope_hash, kind, record_id)).fetchone()
            version = 0 if row is None else row["version"]
            if version != expected_version:
                raise MachineError("service record version conflict")
            if row is not None and row["body_hash"] == body_hash:
                return {"scope": scope, "record_kind": kind, "record_id": record_id,
                    "version": version, "body": deepcopy(body), "body_hash": body_hash,
                    "updated_at": _timestamp(row["updated_at"]), "idempotent": True}
            next_version = version + 1
            db.execute("INSERT INTO finance_service_records "
                "(scope_hash,tenant_id,owner_id,wallet,network,record_kind,record_id,"
                "version,body_json,body_hash,updated_at) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT "
                "(scope_hash,record_kind,record_id) DO UPDATE SET "
                "version=excluded.version,body_json=excluded.body_json,"
                "body_hash=excluded.body_hash,updated_at=excluded.updated_at",
                (scope_hash, scope["tenant_id"], scope["owner_id"], scope["wallet"],
                 scope["network"], kind, record_id, next_version, Jsonb(body), body_hash, at))
            event_id = "record:" + digest({"scope_hash": scope_hash, "kind": kind,
                "record_id": record_id, "version": next_version})
            self._append(db, event_id, "RECORD_COMMITTED", scope_hash,
                "0" * 64 if row is None else row["body_hash"], body_hash,
                {"record_kind": kind, "record_id": record_id, "version": next_version})
        return {"scope": scope, "record_kind": kind, "record_id": record_id,
            "version": next_version, "body": deepcopy(body), "body_hash": body_hash,
            "updated_at": at, "idempotent": False}

    def get_record(self, scope, record_kind, record_id):
        scope, scope_hash = _scope(scope)
        kind = ident(record_kind, "record kind")
        record_id = ident(record_id, "record id")
        with self.connect(scope_hash=scope_hash) as db:
            self._assert_read_safe(db, scope_hash)
            row = db.execute("SELECT * FROM finance_service_records WHERE scope_hash=%s "
                "AND record_kind=%s AND record_id=%s",
                (scope_hash, kind, record_id)).fetchone()
        if row is None:
            raise MachineError("service record not found in authenticated scope")
        body = _json_value(row["body_json"])
        if digest(body) != row["body_hash"]:
            raise MachineError("stored service record commitment mismatch")
        return {"scope": scope, "record_kind": kind, "record_id": record_id,
            "version": row["version"], "body": body, "body_hash": row["body_hash"],
            "updated_at": _timestamp(row["updated_at"])}

    def enqueue_job(self, scope, *, role, routine_id, job_kind, subject_id,
                    dependency_hash, payload, not_before, expires_at,
                    max_attempts=3, priority=0):
        _, _, Jsonb = _driver()
        if role not in ROLES:
            raise MachineError("unsupported financial employee role")
        routine_id = ident(routine_id, "routine id")
        job_kind = ident(job_kind, "job kind")
        subject_id = ident(subject_id, "job subject")
        dependency_hash = _hash(dependency_hash, "job dependency hash")
        if not isinstance(payload, dict):
            raise MachineError("job payload must be an object")
        _reject_sensitive(payload)
        start, end = utc(not_before), utc(expires_at)
        if not start < end or type(max_attempts) is not int or not 1 <= max_attempts <= 8:
            raise MachineError("invalid bounded job lifetime or attempts")
        if type(priority) is not int or not -100 <= priority <= 100:
            raise MachineError("invalid job priority")
        if scope is None:
            normalized, scope_hash, account_key, scope_key = None, None, None, "public"
        else:
            normalized, scope_hash = _scope(scope)
            account_key, scope_key = _account_key(normalized), scope_hash
        payload_hash = digest(payload)
        job_id = digest({"domain": "finance-service-job-1", "scope": scope_key,
            "job_kind": job_kind, "subject_id": subject_id,
            "dependency_hash": dependency_hash})
        with self.connect(scope_hash=scope_hash, worker=scope is None) as db:
            self._prepare_mutation(db, scope_hash)
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                       ("job:" + job_id,))
            row = db.execute("SELECT * FROM finance_service_jobs WHERE job_id=%s",
                             (job_id,)).fetchone()
            if row is not None:
                if (row["payload_hash"] != payload_hash or row["role"] != role
                        or row["routine_id"] != routine_id):
                    raise MachineError("idempotent job key has conflicting payload")
                return {**self._job(row), "idempotent": True}
            active_limit = 1024 if scope_hash is None else 128
            active = db.execute("SELECT count(*) AS n FROM finance_service_jobs WHERE "
                "scope_hash IS NOT DISTINCT FROM %s AND status IN "
                "('PENDING','LEASED','RETRY_WAIT')", (scope_hash,)).fetchone()["n"]
            if active >= active_limit:
                raise MachineError("active job limit reached for scope")
            db.execute("INSERT INTO finance_service_jobs "
                "(job_id,scope_hash,scope_json,account_key,role,routine_id,job_kind,"
                "subject_id,dependency_hash,payload_json,payload_hash,priority,status,"
                "attempts,max_attempts,created_at,available_at,expires_at) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',0,%s,%s,%s,%s)",
                (job_id, scope_hash, None if normalized is None else Jsonb(normalized),
                 account_key, role, routine_id, job_kind, subject_id, dependency_hash,
                 Jsonb(payload), payload_hash, priority, max_attempts, start, start, end))
            event_id = "job-ready:" + job_id
            db.execute("INSERT INTO finance_service_outbox "
                "(event_id,job_id,event_kind,payload_hash,status,created_at) "
                "VALUES (%s,%s,'WORK_AVAILABLE',%s,'PENDING',%s)",
                (event_id, job_id, payload_hash, start))
            self._append(db, event_id, "JOB_ENQUEUED", scope_hash, dependency_hash,
                payload_hash, {"job_id": job_id, "role": role, "routine_id": routine_id,
                               "job_kind": job_kind, "subject_id": subject_id})
            row = db.execute("SELECT * FROM finance_service_jobs WHERE job_id=%s",
                             (job_id,)).fetchone()
        return {**self._job(row), "idempotent": False}

    @staticmethod
    def _job(raw):
        row = _normalized_row(raw)
        row["scope"] = _json_value(row.pop("scope_json"))
        row["payload"] = _json_value(row.pop("payload_json"))
        row["result"] = _json_value(row.pop("result_json"))
        return row

    def _recover_expired_leases(self, db, at):
        rows = db.execute("SELECT * FROM finance_service_jobs WHERE status='LEASED' "
                          "AND lease_expires_at<=%s ORDER BY job_id FOR UPDATE", (at,)).fetchall()
        for row in rows:
            status = "FAILED" if row["attempts"] >= row["max_attempts"] else "RETRY_WAIT"
            error = "LEASE_EXPIRED_MAX_ATTEMPTS" if status == "FAILED" else "LEASE_EXPIRED"
            db.execute("UPDATE finance_service_jobs SET status=%s,available_at=%s,"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,error_code=%s "
                "WHERE job_id=%s", (status, at, error, row["job_id"]))
            self._append(db, "lease-expired:" + row["job_id"] + ":" + str(row["attempts"]),
                "JOB_LEASE_EXPIRED", row["scope_hash"], row["payload_hash"],
                digest({"status": status, "attempts": row["attempts"]}),
                {"job_id": row["job_id"], "status": status, "attempts": row["attempts"]})
            if status == "RETRY_WAIT":
                event_id = "job-retry:" + row["job_id"] + ":" + str(row["attempts"])
                db.execute("INSERT INTO finance_service_outbox "
                    "(event_id,job_id,event_kind,payload_hash,status,created_at) "
                    "VALUES (%s,%s,'WORK_RETRY',%s,'PENDING',%s)",
                    (event_id, row["job_id"], row["payload_hash"], at))

    def claim_job(self, *, worker_id, at, lease_seconds=30, role=None):
        worker_id, at = ident(worker_id, "worker id"), utc(at)
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 3600:
            raise MachineError("invalid worker lease duration")
        if role is not None and role not in ROLES:
            raise MachineError("unsupported worker role")
        lease_end = (datetime.fromisoformat(at) + timedelta(seconds=lease_seconds)).isoformat()
        with self.connect(worker=True) as db:
            self._recover_expired_leases(db, at)
            expired = db.execute("SELECT * FROM finance_service_jobs WHERE status IN "
                "('PENDING','RETRY_WAIT') AND expires_at<=%s ORDER BY job_id FOR UPDATE",
                (at,)).fetchall()
            for row in expired:
                db.execute("UPDATE finance_service_jobs SET status='HELD_EXPIRED',"
                    "error_code='JOB_TTL_EXPIRED' WHERE job_id=%s", (row["job_id"],))
                self._append(db, "job-expired:" + row["job_id"], "JOB_TTL_EXPIRED",
                    row["scope_hash"], row["payload_hash"], digest({"status": "HELD_EXPIRED"}),
                    {"job_id": row["job_id"], "expired_at": at})
            selected = db.execute("SELECT candidate.* FROM finance_service_jobs candidate "
                "WHERE candidate.status IN ('PENDING','RETRY_WAIT') "
                "AND candidate.available_at<=%s AND candidate.expires_at>%s "
                "AND (%s::text IS NULL OR candidate.role=%s) AND (candidate.account_key IS NULL "
                "OR NOT EXISTS (SELECT 1 FROM finance_service_jobs running WHERE "
                "running.account_key=candidate.account_key AND running.status='LEASED')) "
                "ORDER BY candidate.priority DESC,candidate.created_at,candidate.job_id "
                "FOR UPDATE OF candidate SKIP LOCKED LIMIT 1", (at, at, role, role)).fetchone()
            if selected is None:
                return None
            attempt = selected["attempts"] + 1
            token = secrets.token_hex(32)
            changed = db.execute("UPDATE finance_service_jobs SET status='LEASED',attempts=%s,"
                "lease_owner=%s,lease_token=%s,lease_expires_at=%s,error_code=NULL "
                "WHERE job_id=%s AND status IN ('PENDING','RETRY_WAIT')",
                (attempt, worker_id, token, lease_end, selected["job_id"])).rowcount
            if changed != 1:
                raise MachineError("job lease compare-and-set failed")
            db.execute("UPDATE finance_service_outbox SET status='DELIVERED',delivered_at=%s "
                       "WHERE job_id=%s AND status='PENDING'", (at, selected["job_id"]))
            self._append(db, "job-lease:" + selected["job_id"] + ":" + str(attempt),
                "JOB_LEASED", selected["scope_hash"], selected["payload_hash"], token,
                {"job_id": selected["job_id"], "worker_id": worker_id,
                 "attempt": attempt, "lease_expires_at": lease_end})
            row = db.execute("SELECT * FROM finance_service_jobs WHERE job_id=%s",
                             (selected["job_id"],)).fetchone()
        return self._job(row)

    def complete_job(self, *, job_id, worker_id, lease_token, result, at):
        _, _, Jsonb = _driver()
        job_id = _hash(job_id, "job id")
        worker_id = ident(worker_id, "worker id")
        lease_token, at = _hash(lease_token, "worker lease token"), utc(at)
        if not isinstance(result, dict):
            raise MachineError("job result must be an object")
        _reject_sensitive(result, "result")
        result_hash = digest(result)
        with self.connect(worker=True) as db:
            row = db.execute("SELECT * FROM finance_service_jobs WHERE job_id=%s FOR UPDATE",
                             (job_id,)).fetchone()
            if row is None:
                raise MachineError("job not found")
            self._prepare_mutation(db, row["scope_hash"])
            if row["status"] == "SUCCEEDED":
                if row["result_hash"] != result_hash:
                    raise MachineError("completed job result conflict")
                return {**self._job(row), "idempotent": True}
            if (row["status"] != "LEASED" or row["lease_owner"] != worker_id
                    or row["lease_token"] != lease_token
                    or datetime.fromisoformat(at) >= row["lease_expires_at"]):
                raise MachineError("active matching worker lease required")
            db.execute("UPDATE finance_service_jobs SET status='SUCCEEDED',result_json=%s,"
                "result_hash=%s,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,"
                "error_code=NULL WHERE job_id=%s", (Jsonb(result), result_hash, job_id))
            self._append(db, "job-complete:" + job_id, "JOB_COMPLETED", row["scope_hash"],
                row["payload_hash"], result_hash, {"job_id": job_id,
                    "worker_id": worker_id, "attempts": row["attempts"]})
            done = db.execute("SELECT * FROM finance_service_jobs WHERE job_id=%s",
                              (job_id,)).fetchone()
        return {**self._job(done), "idempotent": False}

    def fail_job(self, *, job_id, worker_id, lease_token, error_code,
                 retryable, at, base_backoff_seconds=5):
        job_id = _hash(job_id, "job id")
        worker_id = ident(worker_id, "worker id")
        lease_token = _hash(lease_token, "worker lease token")
        error_code, at = ident(error_code, "worker error code"), utc(at)
        if type(retryable) is not bool or type(base_backoff_seconds) is not int or not (
                1 <= base_backoff_seconds <= 600):
            raise MachineError("invalid retry policy")
        with self.connect(worker=True) as db:
            row = db.execute("SELECT * FROM finance_service_jobs WHERE job_id=%s FOR UPDATE",
                             (job_id,)).fetchone()
            if (row is None or row["status"] != "LEASED" or row["lease_owner"] != worker_id
                    or row["lease_token"] != lease_token
                    or datetime.fromisoformat(at) >= row["lease_expires_at"]):
                raise MachineError("active matching worker lease required")
            self._prepare_mutation(db, row["scope_hash"])
            delay = min(3600, base_backoff_seconds * (2 ** (row["attempts"] - 1)))
            retry_at = (datetime.fromisoformat(at) + timedelta(seconds=delay)).isoformat()
            expires_at = row["expires_at"].isoformat()
            can_retry = retryable and row["attempts"] < row["max_attempts"] and (
                retry_at < expires_at)
            status, available = ("RETRY_WAIT", retry_at) if can_retry else ("FAILED", at)
            db.execute("UPDATE finance_service_jobs SET status=%s,available_at=%s,"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,error_code=%s "
                "WHERE job_id=%s", (status, available, error_code, job_id))
            self._append(db, "job-fail:" + job_id + ":" + str(row["attempts"]),
                "JOB_RETRY_SCHEDULED" if can_retry else "JOB_FAILED", row["scope_hash"],
                row["payload_hash"], digest({"status": status, "error": error_code,
                                             "available_at": available}),
                {"job_id": job_id, "status": status, "error_code": error_code,
                 "attempts": row["attempts"], "available_at": available})
            if can_retry:
                event_id = "job-retry:" + job_id + ":" + str(row["attempts"])
                db.execute("INSERT INTO finance_service_outbox "
                    "(event_id,job_id,event_kind,payload_hash,status,created_at) "
                    "VALUES (%s,%s,'WORK_RETRY',%s,'PENDING',%s)",
                    (event_id, job_id, row["payload_hash"], at))
            failed = db.execute("SELECT * FROM finance_service_jobs WHERE job_id=%s",
                                (job_id,)).fetchone()
        return self._job(failed)

    def list_jobs(self, scope, *, limit=100):
        _, scope_hash = _scope(scope)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise MachineError("invalid job list limit")
        with self.connect(scope_hash=scope_hash) as db:
            self._assert_read_safe(db, scope_hash)
            rows = db.execute("SELECT * FROM finance_service_jobs WHERE scope_hash=%s "
                "ORDER BY created_at DESC,job_id LIMIT %s", (scope_hash, limit)).fetchall()
        return [self._job(row) for row in rows]

    def put_public_observation(self, source_id, observation_key, value, *, observed_at,
                               valid_until):
        _, _, Jsonb = _driver()
        source_id = ident(source_id, "observation source")
        observation_key = ident(observation_key, "observation key")
        if not isinstance(value, dict):
            raise MachineError("public observation value must be an object")
        start, end = utc(observed_at), utc(valid_until)
        if not start < end:
            raise MachineError("public observation validity window is empty")
        value_hash = digest(value)
        with self.connect(worker=True) as db:
            self._prepare_mutation(db, None)
            row = db.execute("SELECT * FROM finance_public_observations WHERE source_id=%s "
                "AND observation_key=%s FOR UPDATE", (source_id, observation_key)).fetchone()
            if (row is not None and row["value_hash"] == value_hash
                    and _timestamp(row["observed_at"]) == start
                    and _timestamp(row["valid_until"]) == end):
                return {"source_id": source_id, "observation_key": observation_key,
                    "version": row["version"], "value": deepcopy(value),
                    "value_hash": value_hash, "observed_at": start,
                    "valid_until": end, "changed": False, "idempotent": True}
            changed = row is None or row["value_hash"] != value_hash
            version = 1 if row is None else row["version"] + (1 if changed else 0)
            db.execute("INSERT INTO finance_public_observations "
                "(source_id,observation_key,version,value_json,value_hash,observed_at,"
                "valid_until,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT "
                "(source_id,observation_key) DO UPDATE SET version=excluded.version,"
                "value_json=excluded.value_json,value_hash=excluded.value_hash,"
                "observed_at=excluded.observed_at,valid_until=excluded.valid_until,"
                "updated_at=excluded.updated_at", (source_id, observation_key, version,
                 Jsonb(value), value_hash, start, end, start))
            event_id = "obs:" + digest({"source_id": source_id,
                "observation_key": observation_key, "version": version,
                "observed_at": start, "valid_until": end})
            self._append(db, event_id, "PUBLIC_OBSERVATION_CHANGED" if changed else
                "PUBLIC_OBSERVATION_REFRESHED", None,
                "0" * 64 if row is None else row["value_hash"],
                value_hash if changed else digest({"value_hash": value_hash,
                                                   "valid_until": end}),
                {"source_id": source_id, "observation_key": observation_key,
                 "version": version, "valid_until": end, "changed": changed})
        return {"source_id": source_id, "observation_key": observation_key,
            "version": version, "value": deepcopy(value), "value_hash": value_hash,
            "observed_at": start, "valid_until": end, "changed": changed,
            "idempotent": False}

    def schedule_expired_observations(self, *, at):
        at = utc(at)
        with self.connect(worker=True) as db:
            self._assert_read_safe(db, None)
            rows = db.execute("SELECT * FROM finance_public_observations WHERE "
                "valid_until<=%s ORDER BY source_id,observation_key", (at,)).fetchall()
        jobs = []
        for row in rows:
            dependency = digest({"value_hash": row["value_hash"],
                                 "valid_until": _timestamp(row["valid_until"])})
            jobs.append(self.enqueue_job(None, role="WATCH", routine_id="market-refresh",
                job_kind="REFRESH_OBSERVATION", subject_id=row["source_id"] + ":" + row[
                    "observation_key"], dependency_hash=dependency,
                payload={"source_id": row["source_id"],
                         "observation_key": row["observation_key"],
                         "stale_value_hash": row["value_hash"]}, not_before=at,
                expires_at=(datetime.fromisoformat(at) + timedelta(minutes=5)).isoformat(),
                max_attempts=3, priority=50))
        return jobs

    def put_routine(self, scope, *, role, routine_id, responsibility, status,
                    interval_seconds, next_due_at, dependency_hash, at):
        _, _, Jsonb = _driver()
        scope, scope_hash = _scope(scope)
        if role not in ROLES or status not in ROUTINE_STATES:
            raise MachineError("invalid employee routine role or status")
        routine_id = ident(routine_id, "routine id")
        if not isinstance(responsibility, str) or not 1 <= len(responsibility) <= 500:
            raise MachineError("routine responsibility required")
        if type(interval_seconds) is not int or not 30 <= interval_seconds <= 86400:
            raise MachineError("routine interval outside policy")
        next_due_at, at = utc(next_due_at), utc(at)
        dependency_hash = _hash(dependency_hash, "routine dependency hash")
        with self.connect(scope_hash=scope_hash) as db:
            self._prepare_mutation(db, scope_hash)
            db.execute("INSERT INTO finance_service_routines "
                "(scope_hash,scope_json,role,routine_id,responsibility,status,"
                "interval_seconds,next_due_at,dependency_hash,updated_at) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (scope_hash,routine_id) "
                "DO UPDATE SET role=excluded.role,responsibility=excluded.responsibility,"
                "status=excluded.status,interval_seconds=excluded.interval_seconds,"
                "next_due_at=excluded.next_due_at,dependency_hash=excluded.dependency_hash,"
                "updated_at=excluded.updated_at", (scope_hash, Jsonb(scope), role, routine_id,
                 responsibility, status, interval_seconds, next_due_at, dependency_hash, at))
            self._append(db, "routine:" + digest({"scope_hash": scope_hash,
                "routine_id": routine_id, "updated_at": at}), "ROUTINE_UPSERTED", scope_hash,
                dependency_hash, digest({"status": status, "next_due_at": next_due_at}),
                {"routine_id": routine_id, "role": role, "status": status,
                 "next_due_at": next_due_at})
        return {"scope": scope, "role": role, "routine_id": routine_id,
            "responsibility": responsibility, "status": status,
            "interval_seconds": interval_seconds, "next_due_at": next_due_at,
            "dependency_hash": dependency_hash, "updated_at": at}

    def schedule_due_routines(self, *, at):
        at = utc(at)
        with self.connect(worker=True) as db:
            rows = db.execute("SELECT * FROM finance_service_routines WHERE status='ACTIVE' "
                "AND next_due_at<=%s ORDER BY next_due_at,routine_id", (at,)).fetchall()
        jobs = []
        for row in rows:
            scope = _json_value(row["scope_json"])
            scheduled_for = _timestamp(row["next_due_at"])
            dependency = digest({"routine_dependency_hash": row["dependency_hash"],
                                 "scheduled_for": scheduled_for})
            expires = (datetime.fromisoformat(at) + timedelta(
                seconds=min(row["interval_seconds"], 3600))).isoformat()
            job = self.enqueue_job(scope, role=row["role"], routine_id=row["routine_id"],
                job_kind="RUN_ROUTINE", subject_id=row["routine_id"],
                dependency_hash=dependency,
                payload={"routine_id": row["routine_id"], "role": row["role"],
                         "responsibility": row["responsibility"],
                         "scheduled_for": scheduled_for}, not_before=at,
                expires_at=expires, max_attempts=3, priority=0)
            jobs.append(job)
            next_due = (datetime.fromisoformat(at) + timedelta(
                seconds=row["interval_seconds"])).isoformat()
            with self.connect(worker=True) as db:
                self._prepare_mutation(db, row["scope_hash"])
                changed = db.execute("UPDATE finance_service_routines SET next_due_at=%s,"
                    "updated_at=%s WHERE scope_hash=%s AND routine_id=%s AND next_due_at=%s",
                    (next_due, at, row["scope_hash"], row["routine_id"],
                     row["next_due_at"])).rowcount
                if changed == 1:
                    self._append(db, "routine-adv:" + digest({
                        "scope_hash": row["scope_hash"], "routine_id": row["routine_id"],
                        "scheduled_for": scheduled_for}), "ROUTINE_ADVANCED",
                        row["scope_hash"], dependency, digest({"next_due_at": next_due}),
                        {"routine_id": row["routine_id"], "scheduled_for": scheduled_for,
                         "next_due_at": next_due, "job_id": job["job_id"]})
        return jobs

    def list_routines(self, scope):
        scope, scope_hash = _scope(scope)
        with self.connect(scope_hash=scope_hash) as db:
            self._assert_read_safe(db, scope_hash)
            rows = db.execute("SELECT * FROM finance_service_routines WHERE scope_hash=%s "
                              "ORDER BY role,routine_id", (scope_hash,)).fetchall()
        return [{"scope": scope, "role": row["role"], "routine_id": row["routine_id"],
            "responsibility": row["responsibility"], "status": row["status"],
            "interval_seconds": row["interval_seconds"],
            "next_due_at": _timestamp(row["next_due_at"]),
            "dependency_hash": row["dependency_hash"],
            "updated_at": _timestamp(row["updated_at"])} for row in rows]

    def schedule_rebalance(self, scope, *, routine_id, subject_id, dependency_hash,
                           payload, expected_benefit_base_units,
                           estimated_cost_base_units, last_completed_at,
                           cooldown_seconds, at, expires_at):
        from .rebalance_gate import rebalance_reasons
        at = utc(at)
        reasons = rebalance_reasons(expected_benefit_base_units, estimated_cost_base_units,
            last_completed_at, cooldown_seconds, at)
        if reasons:
            return {"status": "HELD", "reason_codes": reasons,
                    "execution_authority": "NONE"}
        job = self.enqueue_job(scope, role="VAULT", routine_id=routine_id,
            job_kind="REBALANCE_REVIEW", subject_id=subject_id,
            dependency_hash=dependency_hash, payload=payload, not_before=at,
            expires_at=expires_at, max_attempts=2, priority=10)
        return {"status": "QUEUED", "reason_codes": [], "job": job,
                "execution_authority": "NONE"}

    def export_verified_snapshot(self, destination):
        destination = Path(destination)
        with self.connect(worker=True, repeatable_read=True) as db:
            if not self._verify_journal_db(db):
                raise MachineError("service journal integrity failed before export")
            tables = {}
            for name, order in (
                    ("finance_service_records", "scope_hash,record_kind,record_id"),
                    ("finance_service_jobs", "job_id"),
                    ("finance_service_outbox", "event_id"),
                    ("finance_public_observations", "source_id,observation_key"),
                    ("finance_service_routines", "scope_hash,routine_id"),
                    ("finance_service_journal", "ordinal"),
                    ("finance_service_journal_heads", "stream_id")):
                rows = db.execute(f"SELECT * FROM {name} ORDER BY {order}").fetchall()
                normalized = []
                for row in rows:
                    item = _normalized_row(row)
                    for key in tuple(item):
                        if key.endswith("_json"):
                            item[key] = _json_value(item[key])
                    normalized.append(item)
                tables[name] = normalized
            root = self._journal_root_db(db)
        payload = {"schema_version": "finance-postgres-logical-snapshot-1",
                   "journal_root": root, "tables": tables}
        snapshot_hash = digest(payload)
        document = {**payload, "snapshot_hash": snapshot_hash}
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(canonical(document))
        restored = json.loads(destination.read_text())
        claimed = restored.pop("snapshot_hash")
        if claimed != digest(restored) or claimed != snapshot_hash:
            raise MachineError("PostgreSQL logical snapshot verification failed")
        return {"path": str(destination), "journal_root": root,
                "snapshot_hash": snapshot_hash, "status": "VERIFIED_LOGICAL_SNAPSHOT"}
