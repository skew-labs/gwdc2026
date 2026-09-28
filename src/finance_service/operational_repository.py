"""Durable tenant records and bounded worker outbox for PR09.

SQLite is the executable single-host reference used for deterministic recovery
tests.  The PostgreSQL migration carries the same ownership, lease, idempotency
and account-serialization fields for the hosted deployment adapter.
"""

import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from economic_machine.mandate import normalize_scope
from economic_machine.values import MachineError, canonical, digest, ident, utc

ROLES = {"ALPHA", "VAULT", "WATCH"}
ROUTINE_STATES = {"ACTIVE", "PAUSED", "HELD", "FAILED"}
JOB_TERMINAL = {"SUCCEEDED", "FAILED", "HELD_EXPIRED"}
_HASH = re.compile(r"[0-9a-f]{64}")


SCHEMA = """
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS service_records (
 scope_hash TEXT NOT NULL, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 wallet TEXT NOT NULL, network TEXT NOT NULL, record_kind TEXT NOT NULL,
 record_id TEXT NOT NULL, version INTEGER NOT NULL, body_json TEXT NOT NULL,
 body_hash TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(scope_hash,record_kind,record_id)
);
CREATE TABLE IF NOT EXISTS service_jobs (
 job_id TEXT PRIMARY KEY, scope_hash TEXT, scope_json TEXT, account_key TEXT,
 role TEXT NOT NULL, routine_id TEXT NOT NULL, job_kind TEXT NOT NULL,
 subject_id TEXT NOT NULL, dependency_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
 payload_hash TEXT NOT NULL, priority INTEGER NOT NULL, status TEXT NOT NULL,
 attempts INTEGER NOT NULL, max_attempts INTEGER NOT NULL, created_at TEXT NOT NULL,
 available_at TEXT NOT NULL, expires_at TEXT NOT NULL, lease_owner TEXT,
 lease_token TEXT, lease_expires_at TEXT, result_json TEXT, result_hash TEXT,
 error_code TEXT,
 UNIQUE(scope_hash,job_kind,subject_id,dependency_hash)
);
CREATE INDEX IF NOT EXISTS service_job_ready
 ON service_jobs(status,available_at,priority,created_at);
CREATE INDEX IF NOT EXISTS service_job_account
 ON service_jobs(account_key,status,lease_expires_at);
CREATE TABLE IF NOT EXISTS service_outbox (
 event_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES service_jobs(job_id),
 event_kind TEXT NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, delivered_at TEXT
);
CREATE TABLE IF NOT EXISTS public_observations (
 source_id TEXT NOT NULL, observation_key TEXT NOT NULL, version INTEGER NOT NULL,
 value_json TEXT NOT NULL, value_hash TEXT NOT NULL, observed_at TEXT NOT NULL,
 valid_until TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(source_id,observation_key)
);
CREATE TABLE IF NOT EXISTS service_routines (
 scope_hash TEXT NOT NULL, scope_json TEXT NOT NULL, role TEXT NOT NULL,
 routine_id TEXT NOT NULL, responsibility TEXT NOT NULL, status TEXT NOT NULL,
 interval_seconds INTEGER NOT NULL, next_due_at TEXT NOT NULL,
 dependency_hash TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(scope_hash,routine_id)
);
CREATE TABLE IF NOT EXISTS service_journal (
 ordinal INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
 event_kind TEXT NOT NULL, scope_hash TEXT, input_hash TEXT NOT NULL,
 output_hash TEXT NOT NULL, body_json TEXT NOT NULL, previous_hash TEXT NOT NULL,
 event_hash TEXT NOT NULL
);
"""


def _hash(value, label):
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise MachineError("invalid " + label)
    return value


def _scope(scope):
    normalized = normalize_scope(scope)
    return normalized, digest({"domain": "finance-service-scope-1", "scope": normalized})


def _account_key(scope):
    return digest({"domain": "finance-account-serialization-1",
                   "network": scope["network"], "wallet": scope["wallet"]})


def _json(value):
    return canonical(value).decode("utf-8")


def _reject_sensitive(value, path="payload"):
    forbidden = {"privatekey", "mnemonic", "mnemonicphrase", "seed", "seedphrase",
                 "secretkey", "sshkey", "apikey", "accesstoken", "refreshtoken"}
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise MachineError("job payload keys must be strings")
            normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
            if (normalized_key in forbidden or normalized_key.endswith(
                    ("privatekey", "apikey", "accesstoken", "refreshtoken"))):
                raise MachineError("sensitive credential field rejected at " + path)
            _reject_sensitive(child, path + "." + key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive(child, path + "[" + str(index) + "]")


class OperationalRepository:
    """Crash-recoverable reference store with no signing or broadcast method."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _verify_journal_db(db):
        previous = "0" * 64
        rows = db.execute("SELECT * FROM service_journal ORDER BY ordinal").fetchall()
        for expected, row in enumerate(rows, 1):
            try:
                body = json.loads(row["body_json"])
            except json.JSONDecodeError:
                return False
            event = {"ordinal": row["ordinal"], "event_id": row["event_id"],
                "event_kind": row["event_kind"], "scope_hash": row["scope_hash"],
                "input_hash": row["input_hash"], "output_hash": row["output_hash"],
                "body": body, "previous_hash": row["previous_hash"]}
            if (row["ordinal"] != expected or row["previous_hash"] != previous
                    or row["event_hash"] != digest(
                        {"domain": "finance-service-journal-event-1", "event": event})):
                return False
            previous = row["event_hash"]
        return True

    @staticmethod
    def _append(db, event_id, event_kind, scope_hash, input_hash, output_hash, body):
        if not OperationalRepository._verify_journal_db(db):
            raise MachineError("service journal integrity failed; mutation denied")
        event_id, event_kind = ident(event_id, "service event id"), ident(
            event_kind, "service event kind")
        _hash(input_hash, "service event input hash")
        _hash(output_hash, "service event output hash")
        prior = db.execute("SELECT ordinal,event_hash FROM service_journal "
                           "ORDER BY ordinal DESC LIMIT 1").fetchone()
        ordinal = 1 if prior is None else prior["ordinal"] + 1
        previous = "0" * 64 if prior is None else prior["event_hash"]
        event = {"ordinal": ordinal, "event_id": event_id, "event_kind": event_kind,
            "scope_hash": scope_hash, "input_hash": input_hash,
            "output_hash": output_hash, "body": body, "previous_hash": previous}
        event_hash = digest({"domain": "finance-service-journal-event-1", "event": event})
        db.execute("INSERT INTO service_journal VALUES (?,?,?,?,?,?,?,?,?)",
            (ordinal, event_id, event_kind, scope_hash, input_hash, output_hash,
             _json(body), previous, event_hash))
        return event_hash

    def verify_journal(self):
        with self.connect() as db:
            return self._verify_journal_db(db)

    def journal_root(self):
        if not self.verify_journal():
            raise MachineError("service journal integrity failed")
        with self.connect() as db:
            row = db.execute("SELECT event_hash FROM service_journal "
                             "ORDER BY ordinal DESC LIMIT 1").fetchone()
        return "0" * 64 if row is None else row["event_hash"]

    def health(self):
        return {"backend": "SQLITE_REFERENCE",
                "journal_integrity": self.verify_journal()}

    def put_record(self, scope, record_kind, record_id, body, *, expected_version, at):
        scope, scope_hash = _scope(scope)
        kind, record_id = ident(record_kind, "record kind"), ident(record_id, "record id")
        if type(expected_version) is not int or not 0 <= expected_version < 1 << 31:
            raise MachineError("invalid expected record version")
        if not isinstance(body, dict):
            raise MachineError("service record body must be an object")
        _reject_sensitive(body, "record")
        at, body_hash = utc(at), digest(body)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._verify_journal_db(db):
                raise MachineError("service journal integrity failed; mutation denied")
            row = db.execute("SELECT version,body_hash,updated_at FROM service_records WHERE "
                "scope_hash=? AND record_kind=? AND record_id=?",
                (scope_hash, kind, record_id)).fetchone()
            version = 0 if row is None else row["version"]
            if version != expected_version:
                raise MachineError("service record version conflict")
            if row is not None and row["body_hash"] == body_hash:
                return {"scope": scope, "record_kind": kind, "record_id": record_id,
                    "version": version, "body": deepcopy(body), "body_hash": body_hash,
                    "updated_at": row["updated_at"], "idempotent": True}
            next_version = version + 1
            db.execute("INSERT INTO service_records VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scope_hash,record_kind,record_id) DO UPDATE SET "
                "version=excluded.version,body_json=excluded.body_json,"
                "body_hash=excluded.body_hash,updated_at=excluded.updated_at",
                (scope_hash, scope["tenant_id"], scope["owner_id"], scope["wallet"],
                 scope["network"], kind, record_id, next_version, _json(body), body_hash, at))
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
        kind, record_id = ident(record_kind, "record kind"), ident(record_id, "record id")
        with self.connect() as db:
            if not self._verify_journal_db(db):
                raise MachineError("service journal integrity failed; read denied")
            row = db.execute("SELECT * FROM service_records WHERE scope_hash=? "
                "AND record_kind=? AND record_id=?", (scope_hash, kind, record_id)).fetchone()
        if row is None:
            raise MachineError("service record not found in authenticated scope")
        body = json.loads(row["body_json"])
        if digest(body) != row["body_hash"]:
            raise MachineError("stored service record commitment mismatch")
        return {"scope": scope, "record_kind": kind, "record_id": record_id,
            "version": row["version"], "body": body, "body_hash": row["body_hash"],
            "updated_at": row["updated_at"]}

    def enqueue_job(self, scope, *, role, routine_id, job_kind, subject_id,
                    dependency_hash, payload, not_before, expires_at,
                    max_attempts=3, priority=0):
        if role not in ROLES:
            raise MachineError("unsupported financial employee role")
        routine_id = ident(routine_id, "routine id")
        job_kind, subject_id = ident(job_kind, "job kind"), ident(subject_id, "job subject")
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
            normalized, scope_hash, scope_json, account_key = None, None, None, None
            scope_key = "public"
        else:
            normalized, scope_hash = _scope(scope)
            scope_json, account_key = _json(normalized), _account_key(normalized)
            scope_key = scope_hash
        payload_hash = digest(payload)
        job_id = digest({"domain": "finance-service-job-1", "scope": scope_key,
            "job_kind": job_kind, "subject_id": subject_id,
            "dependency_hash": dependency_hash})
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._verify_journal_db(db):
                raise MachineError("service journal integrity failed; mutation denied")
            row = db.execute("SELECT * FROM service_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is not None:
                if (row["payload_hash"] != payload_hash or row["role"] != role
                        or row["routine_id"] != routine_id):
                    raise MachineError("idempotent job key has conflicting payload")
                return {**self._job(row), "idempotent": True}
            active_limit = 1024 if scope_hash is None else 128
            active_count = db.execute("SELECT count(*) AS n FROM service_jobs WHERE "
                "scope_hash IS ? AND status IN ('PENDING','LEASED','RETRY_WAIT')",
                (scope_hash,)).fetchone()["n"]
            if active_count >= active_limit:
                raise MachineError("active job limit reached for scope")
            db.execute("INSERT INTO service_jobs VALUES (" + ",".join(
                "?" for _ in range(24)) + ")",
                (job_id, scope_hash, scope_json, account_key, role, routine_id, job_kind,
                 subject_id, dependency_hash, _json(payload), payload_hash, priority,
                 "PENDING", 0, max_attempts, start, start, end, None, None, None,
                 None, None, None))
            event_id = "job-ready:" + job_id
            db.execute("INSERT INTO service_outbox VALUES (?,?,?,?,?,?,?)",
                (event_id, job_id, "WORK_AVAILABLE", payload_hash, "PENDING", start, None))
            self._append(db, event_id, "JOB_ENQUEUED", scope_hash, dependency_hash,
                payload_hash, {"job_id": job_id, "role": role, "routine_id": routine_id,
                               "job_kind": job_kind, "subject_id": subject_id})
            row = db.execute("SELECT * FROM service_jobs WHERE job_id=?", (job_id,)).fetchone()
        return {**self._job(row), "idempotent": False}

    @staticmethod
    def _job(row):
        result = dict(row)
        result["scope"] = None if row["scope_json"] is None else json.loads(row["scope_json"])
        result["payload"] = json.loads(row["payload_json"])
        result["result"] = None if row["result_json"] is None else json.loads(
            row["result_json"])
        for key in ("scope_json", "payload_json", "result_json"):
            result.pop(key)
        return result

    def _recover_expired_leases(self, db, at):
        rows = db.execute("SELECT * FROM service_jobs WHERE status='LEASED' "
                          "AND lease_expires_at<=? ORDER BY job_id", (at,)).fetchall()
        for row in rows:
            status = "FAILED" if row["attempts"] >= row["max_attempts"] else "RETRY_WAIT"
            error = "LEASE_EXPIRED_MAX_ATTEMPTS" if status == "FAILED" else "LEASE_EXPIRED"
            db.execute("UPDATE service_jobs SET status=?,available_at=?,lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL,error_code=? WHERE job_id=?",
                (status, at, error, row["job_id"]))
            self._append(db, "lease-expired:" + row["job_id"] + ":" + str(row["attempts"]),
                "JOB_LEASE_EXPIRED", row["scope_hash"], row["payload_hash"],
                digest({"status": status, "attempts": row["attempts"]}),
                {"job_id": row["job_id"], "status": status, "attempts": row["attempts"]})
            if status == "RETRY_WAIT":
                event_id = "job-retry:" + row["job_id"] + ":" + str(row["attempts"])
                db.execute("INSERT INTO service_outbox VALUES (?,?,?,?,?,?,?)",
                    (event_id, row["job_id"], "WORK_RETRY", row["payload_hash"],
                     "PENDING", at, None))

    def claim_job(self, *, worker_id, at, lease_seconds=30, role=None):
        worker_id, at = ident(worker_id, "worker id"), utc(at)
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 3600:
            raise MachineError("invalid worker lease duration")
        if role is not None and role not in ROLES:
            raise MachineError("unsupported worker role")
        lease_end = (datetime.fromisoformat(at) + timedelta(seconds=lease_seconds)).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._verify_journal_db(db):
                raise MachineError("service journal integrity failed; mutation denied")
            self._recover_expired_leases(db, at)
            expired = db.execute("SELECT * FROM service_jobs WHERE status IN "
                "('PENDING','RETRY_WAIT') AND expires_at<=? ORDER BY job_id", (at,)).fetchall()
            for row in expired:
                db.execute("UPDATE service_jobs SET status='HELD_EXPIRED',"
                    "error_code='JOB_TTL_EXPIRED' WHERE job_id=?", (row["job_id"],))
                self._append(db, "job-expired:" + row["job_id"], "JOB_TTL_EXPIRED",
                    row["scope_hash"], row["payload_hash"], digest({"status": "HELD_EXPIRED"}),
                    {"job_id": row["job_id"], "expired_at": at})
            query = ("SELECT * FROM service_jobs WHERE status IN ('PENDING','RETRY_WAIT') "
                     "AND available_at<=? AND expires_at>?" +
                     (" AND role=?" if role is not None else "") +
                     " ORDER BY priority DESC,created_at,job_id")
            params = (at, at, role) if role is not None else (at, at)
            candidates = db.execute(query, params).fetchall()
            selected = None
            for row in candidates:
                if row["account_key"] is not None and db.execute(
                        "SELECT 1 FROM service_jobs WHERE account_key=? AND status='LEASED' "
                        "LIMIT 1", (row["account_key"],)).fetchone() is not None:
                    continue
                selected = row
                break
            if selected is None:
                return None
            attempt = selected["attempts"] + 1
            token = secrets.token_hex(32)
            changed = db.execute("UPDATE service_jobs SET status='LEASED',attempts=?,"
                "lease_owner=?,lease_token=?,lease_expires_at=?,error_code=NULL "
                "WHERE job_id=? AND status IN ('PENDING','RETRY_WAIT')",
                (attempt, worker_id, token, lease_end, selected["job_id"])).rowcount
            if changed != 1:
                raise MachineError("job lease compare-and-set failed")
            db.execute("UPDATE service_outbox SET status='DELIVERED',delivered_at=? "
                       "WHERE job_id=? AND status='PENDING'", (at, selected["job_id"]))
            self._append(db, "job-lease:" + selected["job_id"] + ":" + str(attempt),
                "JOB_LEASED", selected["scope_hash"], selected["payload_hash"], token,
                {"job_id": selected["job_id"], "worker_id": worker_id,
                 "attempt": attempt, "lease_expires_at": lease_end})
            row = db.execute("SELECT * FROM service_jobs WHERE job_id=?",
                             (selected["job_id"],)).fetchone()
        return self._job(row)

    def complete_job(self, *, job_id, worker_id, lease_token, result, at):
        job_id = _hash(job_id, "job id")
        worker_id, lease_token, at = (ident(worker_id, "worker id"),
                                     _hash(lease_token, "worker lease token"), utc(at))
        if not isinstance(result, dict):
            raise MachineError("job result must be an object")
        _reject_sensitive(result, "result")
        result_hash = digest(result)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._verify_journal_db(db):
                raise MachineError("service journal integrity failed; mutation denied")
            row = db.execute("SELECT * FROM service_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise MachineError("job not found")
            if row["status"] == "SUCCEEDED":
                if row["result_hash"] != result_hash:
                    raise MachineError("completed job result conflict")
                return {**self._job(row), "idempotent": True}
            if (row["status"] != "LEASED" or row["lease_owner"] != worker_id
                    or row["lease_token"] != lease_token or at >= row["lease_expires_at"]):
                raise MachineError("active matching worker lease required")
            db.execute("UPDATE service_jobs SET status='SUCCEEDED',result_json=?,result_hash=?,"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,error_code=NULL "
                "WHERE job_id=?", (_json(result), result_hash, job_id))
            self._append(db, "job-complete:" + job_id, "JOB_COMPLETED", row["scope_hash"],
                row["payload_hash"], result_hash, {"job_id": job_id,
                    "worker_id": worker_id, "attempts": row["attempts"]})
            done = db.execute("SELECT * FROM service_jobs WHERE job_id=?", (job_id,)).fetchone()
        return {**self._job(done), "idempotent": False}

    def fail_job(self, *, job_id, worker_id, lease_token, error_code,
                 retryable, at, base_backoff_seconds=5):
        job_id, worker_id = _hash(job_id, "job id"), ident(worker_id, "worker id")
        lease_token, error_code, at = (_hash(lease_token, "worker lease token"),
                                       ident(error_code, "worker error code"), utc(at))
        if type(retryable) is not bool or type(base_backoff_seconds) is not int or not (
                1 <= base_backoff_seconds <= 600):
            raise MachineError("invalid retry policy")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._verify_journal_db(db):
                raise MachineError("service journal integrity failed; mutation denied")
            row = db.execute("SELECT * FROM service_jobs WHERE job_id=?", (job_id,)).fetchone()
            if (row is None or row["status"] != "LEASED" or row["lease_owner"] != worker_id
                    or row["lease_token"] != lease_token or at >= row["lease_expires_at"]):
                raise MachineError("active matching worker lease required")
            delay = min(3600, base_backoff_seconds * (2 ** (row["attempts"] - 1)))
            retry_at = (datetime.fromisoformat(at) + timedelta(seconds=delay)).isoformat()
            can_retry = (retryable and row["attempts"] < row["max_attempts"]
                         and retry_at < row["expires_at"])
            status, available = ("RETRY_WAIT", retry_at) if can_retry else ("FAILED", at)
            db.execute("UPDATE service_jobs SET status=?,available_at=?,lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL,error_code=? WHERE job_id=?",
                (status, available, error_code, job_id))
            self._append(db, "job-fail:" + job_id + ":" + str(row["attempts"]),
                "JOB_RETRY_SCHEDULED" if can_retry else "JOB_FAILED", row["scope_hash"],
                row["payload_hash"], digest({"status": status, "error": error_code,
                                             "available_at": available}),
                {"job_id": job_id, "status": status, "error_code": error_code,
                 "attempts": row["attempts"], "available_at": available})
            if can_retry:
                event_id = "job-retry:" + job_id + ":" + str(row["attempts"])
                db.execute("INSERT INTO service_outbox VALUES (?,?,?,?,?,?,?)",
                    (event_id, job_id, "WORK_RETRY", row["payload_hash"], "PENDING",
                     at, None))
            failed = db.execute("SELECT * FROM service_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._job(failed)

    def list_jobs(self, scope, *, limit=100):
        _, scope_hash = _scope(scope)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise MachineError("invalid job list limit")
        with self.connect() as db:
            if not self._verify_journal_db(db):
                raise MachineError("service journal integrity failed; read denied")
            rows = db.execute("SELECT * FROM service_jobs WHERE scope_hash=? "
                "ORDER BY created_at DESC,job_id LIMIT ?", (scope_hash, limit)).fetchall()
        return [self._job(row) for row in rows]

    def put_public_observation(self, source_id, observation_key, value, *, observed_at,
                               valid_until):
        source_id, observation_key = ident(source_id, "observation source"), ident(
            observation_key, "observation key")
        if not isinstance(value, dict):
            raise MachineError("public observation value must be an object")
        start, end = utc(observed_at), utc(valid_until)
        if not start < end:
            raise MachineError("public observation validity window is empty")
        value_hash = digest(value)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._verify_journal_db(db):
                raise MachineError("service journal integrity failed; mutation denied")
            row = db.execute("SELECT * FROM public_observations WHERE source_id=? AND "
                             "observation_key=?", (source_id, observation_key)).fetchone()
            if (row is not None and row["value_hash"] == value_hash
                    and row["observed_at"] == start and row["valid_until"] == end):
                return {"source_id": source_id, "observation_key": observation_key,
                    "version": row["version"], "value": deepcopy(value),
                    "value_hash": value_hash, "observed_at": start,
                    "valid_until": end, "changed": False, "idempotent": True}
            changed = row is None or row["value_hash"] != value_hash
            version = 1 if row is None else row["version"] + (1 if changed else 0)
            db.execute("INSERT INTO public_observations VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source_id,observation_key) DO UPDATE SET "
                "version=excluded.version,value_json=excluded.value_json,"
                "value_hash=excluded.value_hash,observed_at=excluded.observed_at,"
                "valid_until=excluded.valid_until,updated_at=excluded.updated_at",
                (source_id, observation_key, version, _json(value), value_hash,
                 start, end, start))
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
        with self.connect() as db:
            if not self._verify_journal_db(db):
                raise MachineError("service journal integrity failed; read denied")
            rows = db.execute("SELECT * FROM public_observations WHERE valid_until<=? "
                              "ORDER BY source_id,observation_key", (at,)).fetchall()
        jobs = []
        for row in rows:
            dependency = digest({"value_hash": row["value_hash"],
                                 "valid_until": row["valid_until"]})
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
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._verify_journal_db(db):
                raise MachineError("service journal integrity failed; mutation denied")
            db.execute("INSERT INTO service_routines VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scope_hash,routine_id) DO UPDATE SET role=excluded.role,"
                "responsibility=excluded.responsibility,status=excluded.status,"
                "interval_seconds=excluded.interval_seconds,next_due_at=excluded.next_due_at,"
                "dependency_hash=excluded.dependency_hash,updated_at=excluded.updated_at",
                (scope_hash, _json(scope), role, routine_id, responsibility, status,
                 interval_seconds, next_due_at, dependency_hash, at))
            self._append(db, "routine:" + digest({"scope_hash": scope_hash,
                "routine_id": routine_id, "updated_at": at}),
                "ROUTINE_UPSERTED", scope_hash, dependency_hash,
                digest({"status": status, "next_due_at": next_due_at}),
                {"routine_id": routine_id, "role": role, "status": status,
                 "next_due_at": next_due_at})
        return {"scope": scope, "role": role, "routine_id": routine_id,
            "responsibility": responsibility, "status": status,
            "interval_seconds": interval_seconds, "next_due_at": next_due_at,
            "dependency_hash": dependency_hash, "updated_at": at}

    def schedule_due_routines(self, *, at):
        at = utc(at)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM service_routines WHERE status='ACTIVE' "
                              "AND next_due_at<=? ORDER BY next_due_at,routine_id", (at,)).fetchall()
        jobs = []
        for row in rows:
            scope = json.loads(row["scope_json"])
            dependency = digest({"routine_dependency_hash": row["dependency_hash"],
                                 "scheduled_for": row["next_due_at"]})
            expires = (datetime.fromisoformat(at) + timedelta(
                seconds=min(row["interval_seconds"], 3600))).isoformat()
            job = self.enqueue_job(scope, role=row["role"], routine_id=row["routine_id"],
                job_kind="RUN_ROUTINE", subject_id=row["routine_id"],
                dependency_hash=dependency,
                payload={"routine_id": row["routine_id"], "role": row["role"],
                         "responsibility": row["responsibility"],
                         "scheduled_for": row["next_due_at"]},
                not_before=at, expires_at=expires, max_attempts=3, priority=0)
            jobs.append(job)
            next_due = (datetime.fromisoformat(at) + timedelta(
                seconds=row["interval_seconds"])).isoformat()
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if not self._verify_journal_db(db):
                    raise MachineError("service journal integrity failed; mutation denied")
                changed = db.execute("UPDATE service_routines SET next_due_at=?,updated_at=? "
                           "WHERE scope_hash=? AND routine_id=? AND next_due_at=?",
                           (next_due, at, row["scope_hash"], row["routine_id"],
                            row["next_due_at"])).rowcount
                if changed == 1:
                    self._append(db, "routine-adv:" + digest({
                        "scope_hash": row["scope_hash"],
                        "routine_id": row["routine_id"],
                        "scheduled_for": row["next_due_at"]}),
                        "ROUTINE_ADVANCED", row["scope_hash"], dependency,
                        digest({"next_due_at": next_due}),
                        {"routine_id": row["routine_id"],
                         "scheduled_for": row["next_due_at"],
                         "next_due_at": next_due, "job_id": job["job_id"]})
        return jobs

    def list_routines(self, scope):
        scope, scope_hash = _scope(scope)
        with self.connect() as db:
            if not self._verify_journal_db(db):
                raise MachineError("service journal integrity failed; read denied")
            rows = db.execute("SELECT * FROM service_routines WHERE scope_hash=? "
                              "ORDER BY role,routine_id", (scope_hash,)).fetchall()
        return [{"scope": scope, "role": row["role"], "routine_id": row["routine_id"],
            "responsibility": row["responsibility"], "status": row["status"],
            "interval_seconds": row["interval_seconds"], "next_due_at": row["next_due_at"],
            "dependency_hash": row["dependency_hash"], "updated_at": row["updated_at"]}
            for row in rows]

    def schedule_rebalance(self, scope, *, routine_id, subject_id, dependency_hash,
                           payload, expected_benefit_base_units,
                           estimated_cost_base_units, last_completed_at,
                           cooldown_seconds, at, expires_at):
        def amount(value, label):
            if not isinstance(value, str) or re.fullmatch(r"0|[1-9][0-9]*", value) is None:
                raise MachineError("invalid " + label)
            return int(value)
        benefit = amount(expected_benefit_base_units, "rebalance benefit")
        cost = amount(estimated_cost_base_units, "rebalance cost")
        if type(cooldown_seconds) is not int or not 0 <= cooldown_seconds <= 604800:
            raise MachineError("invalid rebalance cooldown")
        at = utc(at)
        reasons = []
        if benefit <= cost:
            reasons.append("EXPECTED_BENEFIT_NOT_ABOVE_COST")
        if last_completed_at is not None:
            cooldown_end = datetime.fromisoformat(utc(last_completed_at)) + timedelta(
                seconds=cooldown_seconds)
            if datetime.fromisoformat(at) < cooldown_end:
                reasons.append("REBALANCE_COOLDOWN_ACTIVE")
        if reasons:
            return {"status": "HELD", "reason_codes": reasons,
                    "execution_authority": "NONE"}
        job = self.enqueue_job(scope, role="VAULT", routine_id=routine_id,
            job_kind="REBALANCE_REVIEW", subject_id=subject_id,
            dependency_hash=dependency_hash, payload=payload, not_before=at,
            expires_at=expires_at, max_attempts=2, priority=10)
        return {"status": "QUEUED", "reason_codes": [], "job": job,
                "execution_authority": "NONE"}

    def backup_to(self, destination: Path):
        if not self.verify_journal():
            raise MachineError("service journal integrity failed before backup")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as source, sqlite3.connect(destination) as target:
            source.backup(target)
        restored = OperationalRepository(destination)
        if not restored.verify_journal() or restored.journal_root() != self.journal_root():
            raise MachineError("service backup journal verification failed")
        return {"path": str(destination), "journal_root": restored.journal_root(),
                "status": "VERIFIED_BACKUP"}
