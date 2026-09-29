"""Bounded PostgreSQL pools, TLS policy and reloadable secret-file credentials."""

from __future__ import annotations

import os
import stat
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from economic_machine.values import MachineError


def read_owner_file(path, *, label, max_size=8_192):
    """Read one regular file without following links or crossing owner boundaries."""
    path = Path(path)
    if not path.is_absolute():
        raise MachineError(label + " file path must be absolute")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MachineError(label + " file is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                or metadata.st_uid != os.geteuid()):
            raise MachineError(
                label + " file must be an owner-owned owner-only regular file")
        if not 1 <= metadata.st_size <= max_size:
            raise MachineError(label + " file size is outside policy")
        with os.fdopen(descriptor, encoding="utf-8") as source:
            descriptor = -1
            value = source.read(max_size + 1)
    except (OSError, UnicodeError) as exc:
        raise MachineError(label + " file cannot be read") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    fingerprint = (metadata.st_dev, metadata.st_ino, metadata.st_size,
                   metadata.st_mtime_ns)
    return value, fingerprint


@dataclass(frozen=True)
class PostgresRuntimePolicy:
    min_pool_size: int = 1
    max_pool_size: int = 8
    max_waiting: int = 32
    pool_timeout_seconds: float = 3.0
    connect_timeout_seconds: int = 5
    statement_timeout_ms: int = 5_000
    lock_timeout_ms: int = 1_000
    idle_transaction_timeout_ms: int = 10_000
    max_connection_lifetime_seconds: int = 1_800
    max_connection_idle_seconds: int = 300

    def __post_init__(self):
        integer_bounds = (
            ("min pool size", self.min_pool_size, 0, 16),
            ("max pool size", self.max_pool_size, 1, 64),
            ("max waiting", self.max_waiting, 1, 1024),
            ("connect timeout", self.connect_timeout_seconds, 1, 30),
            ("statement timeout", self.statement_timeout_ms, 100, 120_000),
            ("lock timeout", self.lock_timeout_ms, 50, 30_000),
            ("idle transaction timeout", self.idle_transaction_timeout_ms,
             1_000, 300_000),
            ("connection lifetime", self.max_connection_lifetime_seconds, 60, 86_400),
            ("connection idle", self.max_connection_idle_seconds, 10, 3_600),
        )
        for label, value, minimum, maximum in integer_bounds:
            if type(value) is not int or not minimum <= value <= maximum:
                raise MachineError("invalid PostgreSQL " + label)
        if self.min_pool_size > self.max_pool_size:
            raise MachineError("PostgreSQL minimum pool exceeds maximum")
        if (not isinstance(self.pool_timeout_seconds, (int, float))
                or not 0.1 <= self.pool_timeout_seconds <= 30):
            raise MachineError("invalid PostgreSQL pool timeout")


class DsnSecretSource:
    """Read a DSN from a direct test value or a strict owner-only file."""

    def __init__(self, *, value=None, path=None, label="PostgreSQL DSN"):
        if (value is None) == (path is None):
            raise MachineError(label + " requires exactly one source")
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise MachineError("invalid " + label)
        self._value = None if value is None else value.strip()
        self._path = None if path is None else Path(path)
        self.label = label

    @property
    def reloadable(self):
        return self._path is not None

    def read(self):
        if self._path is None:
            return self._value, ("direct",)
        value, fingerprint = read_owner_file(
            self._path, label=self.label, max_size=8_192)
        value = value.strip()
        if not value or "\n" in value or "\r" in value:
            raise MachineError(self.label + " file must contain one non-empty line")
        return value, fingerprint


def _pool_imports():
    try:
        from psycopg import conninfo
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except ImportError as exc:
        raise RuntimeError("psycopg and psycopg-pool are required") from exc
    return conninfo, dict_row, ConnectionPool


def validate_postgres_dsn(dsn, *, allow_insecure_localhost,
                          connect_timeout_seconds):
    conninfo, _, _ = _pool_imports()
    try:
        values = conninfo.conninfo_to_dict(dsn)
    except Exception as exc:
        raise MachineError("invalid PostgreSQL connection string") from exc
    hosts = [item.strip() for item in values.get("host", "").split(",")]
    local = bool(hosts) and all(item in {"localhost", "127.0.0.1", "::1"}
                                or item.startswith("/") for item in hosts)
    if allow_insecure_localhost and local:
        pass
    elif values.get("sslmode") != "verify-full":
        raise MachineError("PostgreSQL TLS requires sslmode=verify-full")
    else:
        root = values.get("sslrootcert")
        if not root or not Path(root).is_absolute() or not Path(root).is_file():
            raise MachineError("PostgreSQL TLS root certificate is unavailable")
    configured_timeout = values.get("connect_timeout")
    try:
        timeout_exceeded = (configured_timeout is not None
                            and int(configured_timeout) > connect_timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise MachineError("invalid PostgreSQL DSN connect timeout") from exc
    if timeout_exceeded:
        raise MachineError("PostgreSQL DSN connect timeout exceeds runtime policy")
    return values


class PostgresPoolRuntime:
    """Own API/worker pools and atomically rotate file-backed credentials."""

    def __init__(self, api_source, worker_source=None, *, policy=None,
                 allow_insecure_localhost=False):
        if not isinstance(api_source, DsnSecretSource):
            raise MachineError("PostgreSQL API secret source is required")
        if worker_source is not None and not isinstance(worker_source, DsnSecretSource):
            raise MachineError("invalid PostgreSQL worker secret source")
        self.api_source = api_source
        self.worker_source = worker_source
        self.policy = policy or PostgresRuntimePolicy()
        self.allow_insecure_localhost = allow_insecure_localhost
        self._lock = threading.RLock()
        self._generation = 0
        self._reloads = 0
        self._fingerprints = None
        self._api_pool = None
        self._worker_pool = None
        self.reload(force=True)

    def _read_sources(self):
        api_dsn, api_fingerprint = self.api_source.read()
        worker = None if self.worker_source is None else self.worker_source.read()
        worker_dsn = None if worker is None else worker[0]
        fingerprints = (api_fingerprint, None if worker is None else worker[1])
        return api_dsn, worker_dsn, fingerprints

    def _new_pool(self, dsn, name):
        _, dict_row, ConnectionPool = _pool_imports()
        validate_postgres_dsn(dsn,
            allow_insecure_localhost=self.allow_insecure_localhost,
            connect_timeout_seconds=self.policy.connect_timeout_seconds)
        pool = ConnectionPool(
            conninfo=dsn,
            kwargs={"row_factory": dict_row,
                    "connect_timeout": self.policy.connect_timeout_seconds,
                    "application_name": "gwdc-finance-" + name},
            min_size=self.policy.min_pool_size,
            max_size=self.policy.max_pool_size,
            max_waiting=self.policy.max_waiting,
            timeout=float(self.policy.pool_timeout_seconds),
            max_lifetime=float(self.policy.max_connection_lifetime_seconds),
            max_idle=float(self.policy.max_connection_idle_seconds),
            check=ConnectionPool.check_connection,
            name="gwdc-finance-" + name,
            open=True,
        )
        try:
            pool.wait(timeout=float(self.policy.connect_timeout_seconds + 1))
        except Exception:
            pool.close(timeout=1)
            raise
        return pool

    def reload(self, *, force=False):
        with self._lock:
            api_dsn, worker_dsn, fingerprints = self._read_sources()
            if not force and fingerprints == self._fingerprints:
                return False
            api_pool = self._new_pool(api_dsn, "api")
            try:
                worker_pool = (None if worker_dsn is None
                               else self._new_pool(worker_dsn, "worker"))
            except Exception:
                api_pool.close(timeout=1)
                raise
            old_api, old_worker = self._api_pool, self._worker_pool
            self._api_pool, self._worker_pool = api_pool, worker_pool
            self._fingerprints = fingerprints
            self._generation += 1
            if not force:
                self._reloads += 1
        for pool in (old_api, old_worker):
            if pool is not None:
                pool.close(timeout=1)
        return True

    def reload_if_changed(self):
        if self.api_source.reloadable or (
                self.worker_source is not None and self.worker_source.reloadable):
            return self.reload(force=False)
        return False

    @contextmanager
    def connection(self, *, worker=False):
        self.reload_if_changed()
        with ExitStack() as stack:
            with self._lock:
                pool = self._worker_pool if worker else self._api_pool
                if pool is None:
                    raise MachineError(
                        "PostgreSQL worker DSN is required for cross-scope operation")
                connection = stack.enter_context(pool.connection(
                    timeout=float(self.policy.pool_timeout_seconds)))
            yield connection

    def stats(self):
        with self._lock:
            pools = {"api": self._api_pool, "worker": self._worker_pool}
            generation, reloads = self._generation, self._reloads
        result = {"generation": generation, "credential_reloads": reloads}
        for role, pool in pools.items():
            if pool is None:
                continue
            result[role] = {key: value for key, value in pool.get_stats().items()
                            if isinstance(value, (int, float))}
        return result

    def close(self):
        with self._lock:
            pools = (self._api_pool, self._worker_pool)
            self._api_pool = self._worker_pool = None
        for pool in pools:
            if pool is not None:
                pool.close(timeout=3)


class RuntimeMetrics:
    """Thread-safe process metrics without tenant or credential labels."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counters = {"api_transactions": 0, "worker_transactions": 0,
                          "transaction_failures": 0, "pool_timeouts": 0}
        self._duration_ms = {"api": 0.0, "worker": 0.0}

    def transaction(self, role, duration_seconds, *, failed=False, pool_timeout=False):
        with self._lock:
            self._counters[role + "_transactions"] += 1
            self._duration_ms[role] += duration_seconds * 1000
            if failed:
                self._counters["transaction_failures"] += 1
            if pool_timeout:
                self._counters["pool_timeouts"] += 1

    def snapshot(self):
        with self._lock:
            return {"counters": dict(self._counters),
                    "transaction_duration_ms": dict(self._duration_ms),
                    "captured_at_monotonic": time.monotonic()}

    def prometheus(self, pool_stats):
        snapshot = self.snapshot()
        lines = []
        for name, value in sorted(snapshot["counters"].items()):
            lines.append("gwdc_finance_" + name + "_total " + str(value))
        for role, value in sorted(snapshot["transaction_duration_ms"].items()):
            lines.append("gwdc_finance_transaction_duration_ms_total{role=\""
                         + role + "\"} " + format(value, ".3f"))
        for role in ("api", "worker"):
            values = pool_stats.get(role)
            if not isinstance(values, dict):
                continue
            for key in ("pool_size", "pool_available", "requests_waiting",
                        "requests_errors", "requests_num"):
                if key in values:
                    lines.append("gwdc_finance_pool_" + key + "{role=\"" + role
                                 + "\"} " + str(values[key]))
        return "\n".join(lines) + "\n"
