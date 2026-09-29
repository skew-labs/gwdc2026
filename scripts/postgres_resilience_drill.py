"""Run isolated PostgreSQL physical-backup, PITR and failover drills.

The source cluster is read only to pg_basebackup. All writes happen on temporary
clones under --work-dir. No connection string is written to the evidence output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from economic_machine.values import MachineError
from finance_service.postgres_runtime import read_owner_file


def _imports():
    try:
        import psycopg
        from psycopg import conninfo, sql
    except ImportError as exc:
        raise RuntimeError("psycopg is required for the PostgreSQL drill") from exc
    return psycopg, conninfo, sql


def _owner_secret(path):
    try:
        raw, _ = read_owner_file(path, label="DSN", max_size=8_192)
    except MachineError as exc:
        raise RuntimeError(str(exc)) from exc
    value = raw.strip()
    if not value or "\n" in value or "\r" in value:
        raise RuntimeError("DSN file must contain exactly one connection string")
    return value


def _safe_path(path, label):
    path = Path(path).resolve()
    if not path.is_absolute() or not re.fullmatch(r"[/A-Za-z0-9._-]+", str(path)):
        raise RuntimeError(label + " path is outside drill policy")
    return path


def _run(command, *, env=None, timeout=120):
    result = subprocess.run(command, env=env, text=True, capture_output=True,
                            timeout=timeout, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout)[-2_000:]
        raise RuntimeError(Path(command[0]).name + " failed: " + detail)
    return result.stdout


def _pg_env(dsn, *, port=None, database=None):
    _, conninfo, _ = _imports()
    values = conninfo.conninfo_to_dict(dsn)
    mapping = {"host": "PGHOST", "port": "PGPORT", "user": "PGUSER",
               "password": "PGPASSWORD", "dbname": "PGDATABASE",
               "sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT",
               "sslcert": "PGSSLCERT", "sslkey": "PGSSLKEY"}
    env = dict(os.environ)
    for source, target in mapping.items():
        if values.get(source):
            env[target] = str(values[source])
    if port is not None:
        env["PGHOST"], env["PGPORT"] = "127.0.0.1", str(port)
    if database is not None:
        env["PGDATABASE"] = database
    return env, values


def _basebackup(pg_bin, destination, env, *, write_recovery=False):
    destination = Path(destination)
    if destination.exists():
        raise RuntimeError("basebackup destination already exists")
    command = [str(pg_bin / "pg_basebackup"), "--pgdata", str(destination),
               "--format=plain", "--wal-method=stream", "--checkpoint=fast",
               "--manifest-checksums=SHA256", "--no-password"]
    if write_recovery:
        command.append("--write-recovery-conf")
    _run(command, env=env, timeout=180)
    _run([str(pg_bin / "pg_verifybackup"), str(destination)], env=env, timeout=120)
    manifest = destination / "backup_manifest"
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


def _append_config(data, lines):
    path = Path(data) / "postgresql.auto.conf"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n# gwdc isolated resilience drill\n")
        for line in lines:
            handle.write(line + "\n")


def _start(pg_bin, data, port, socket_dir, *, extra_lines=()):
    socket_dir.mkdir(parents=True, exist_ok=True)
    if extra_lines:
        _append_config(data, extra_lines)
    options = "-p " + str(port) + " -k " + str(socket_dir) + " -c listen_addresses=127.0.0.1"
    _run([str(pg_bin / "pg_ctl"), "-D", str(data),
          "-l", str(Path(data) / "drill-postgres.log"), "-o", options,
          "start", "-w"], timeout=60)


def _stop(pg_bin, data):
    if (Path(data) / "postmaster.pid").exists():
        _run([str(pg_bin / "pg_ctl"), "-D", str(data), "stop", "-m", "fast", "-w"],
             timeout=60)


def _connect(values, port, database="postgres", *, autocommit=True):
    psycopg, _, _ = _imports()
    kwargs = {"host": "127.0.0.1", "port": port,
              "user": values.get("user"), "dbname": database,
              "connect_timeout": 3, "autocommit": autocommit}
    if values.get("password"):
        kwargs["password"] = values["password"]
    return psycopg.connect(**kwargs)


def _ensure_database(values, port, name):
    _, _, sql = _imports()
    with _connect(values, port) as db:
        exists = db.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()
        if exists is None:
            db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def _wait(predicate, *, timeout=30, interval=0.1, label="condition"):
    psycopg, _, _ = _imports()
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            last = type(exc).__name__
        time.sleep(interval)
    raise RuntimeError("timed out waiting for " + label + ": " + str(last))


def _is_promoted(values, port):
    with _connect(values, port) as db:
        return not db.execute("SELECT pg_is_in_recovery()").fetchone()[0]


def _pitr_drill(pg_bin, source_env, values, root, ports):
    primary, base, restored = root / "pitr-primary", root / "pitr-base", root / "pitr-restored"
    archive, socket_a, socket_b = root / "pitr-archive", root / "socket-pitr-a", root / "socket-pitr-b"
    archive.mkdir()
    source_manifest = _basebackup(pg_bin, primary, source_env)
    archive_command = "test ! -f '" + str(archive) + "/%f' && cp %p '" + str(archive) + "/%f'"
    _start(pg_bin, primary, ports[0], socket_a, extra_lines=(
        "archive_mode = 'on'", "archive_command = '" + archive_command.replace("'", "''") + "'",
        "archive_timeout = '1s'", "wal_level = 'replica'"))
    try:
        _ensure_database(values, ports[0], "gwdc_pr12_pitr")
        with _connect(values, ports[0], "gwdc_pr12_pitr") as db:
            db.execute("CREATE TABLE IF NOT EXISTS recovery_markers "
                       "(marker text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now())")
        clone_env, _ = _pg_env("host=127.0.0.1 port=" + str(ports[0]) +
                               " user=" + values["user"] + " dbname=postgres")
        base_manifest = _basebackup(pg_bin, base, clone_env)
        with _connect(values, ports[0], "gwdc_pr12_pitr") as db:
            db.execute("INSERT INTO recovery_markers(marker) VALUES ('BEFORE_TARGET')")
        with _connect(values, ports[0]) as db:
            restore_lsn = db.execute(
                "SELECT pg_create_restore_point('gwdc_pr12_restore_target')").fetchone()[0]
        with _connect(values, ports[0], "gwdc_pr12_pitr") as db:
            db.execute("INSERT INTO recovery_markers(marker) VALUES ('AFTER_TARGET')")
        with _connect(values, ports[0]) as db:
            archived_before = db.execute(
                "SELECT archived_count FROM pg_stat_archiver").fetchone()[0]
            db.execute("SELECT pg_switch_wal()").fetchone()
            _wait(lambda: db.execute("SELECT archived_count>%s AND failed_count=0 "
                                     "FROM pg_stat_archiver",
                                     (archived_before,)).fetchone()[0],
                  label="WAL archive")
    finally:
        _stop(pg_bin, primary)
    shutil.copytree(base, restored)
    restore_command = "cp '" + str(archive) + "/%f' %p"
    _append_config(restored, (
        "restore_command = '" + restore_command.replace("'", "''") + "'",
        "recovery_target_name = 'gwdc_pr12_restore_target'",
        "recovery_target_action = 'promote'",
        "recovery_target_inclusive = 'on'"))
    (restored / "recovery.signal").touch()
    _start(pg_bin, restored, ports[1], socket_b)
    try:
        _wait(lambda: _is_promoted(values, ports[1]), label="PITR promotion")
        with _connect(values, ports[1], "gwdc_pr12_pitr") as db:
            markers = [row[0] for row in db.execute(
                "SELECT marker FROM recovery_markers ORDER BY marker").fetchall()]
        if markers != ["BEFORE_TARGET"]:
            raise RuntimeError("PITR restored the wrong marker set: " + repr(markers))
    finally:
        _stop(pg_bin, restored)
    return {"status": "PASS", "source_backup_manifest_sha256": source_manifest,
            "pitr_base_manifest_sha256": base_manifest,
            "restore_point_lsn": str(restore_lsn), "restored_markers": markers,
            "after_target_excluded": True}


def _failover_drill(pg_bin, source_env, values, root, ports):
    primary, standby = root / "failover-primary", root / "failover-standby"
    socket_a, socket_b = root / "socket-failover-a", root / "socket-failover-b"
    primary_manifest = _basebackup(pg_bin, primary, source_env)
    _start(pg_bin, primary, ports[0], socket_a)
    try:
        _ensure_database(values, ports[0], "gwdc_pr12_failover")
        with _connect(values, ports[0], "gwdc_pr12_failover") as db:
            db.execute("CREATE TABLE IF NOT EXISTS failover_markers "
                       "(marker text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now())")
        clone_env, _ = _pg_env("host=127.0.0.1 port=" + str(ports[0]) +
                               " user=" + values["user"] + " dbname=postgres")
        standby_manifest = _basebackup(pg_bin, standby, clone_env, write_recovery=True)
        _start(pg_bin, standby, ports[1], socket_b)
        with _connect(values, ports[0], "gwdc_pr12_failover") as db:
            db.execute("INSERT INTO failover_markers(marker) VALUES ('REPLICATED')")
            primary_lsn = db.execute("SELECT pg_current_wal_flush_lsn()").fetchone()[0]

        def replicated():
            with _connect(values, ports[1], "gwdc_pr12_failover") as db:
                recovery = db.execute("SELECT pg_is_in_recovery()").fetchone()[0]
                count = db.execute("SELECT count(*) FROM failover_markers "
                                   "WHERE marker='REPLICATED'").fetchone()[0]
                replay_lsn = db.execute("SELECT pg_last_wal_replay_lsn()").fetchone()[0]
                return (recovery and count == 1 and replay_lsn is not None,
                        str(replay_lsn) if replay_lsn is not None else None)

        def replayed():
            result = replicated()
            return result if result[0] else False

        _, replay_lsn = _wait(replayed, label="standby replay")
        _stop(pg_bin, primary)
        ready = subprocess.run([str(pg_bin / "pg_isready"), "-h", "127.0.0.1",
            "-p", str(ports[0])], text=True, capture_output=True, check=False)
        if ready.returncode == 0:
            raise RuntimeError("primary fencing check failed")
        _run([str(pg_bin / "pg_ctl"), "-D", str(standby), "promote", "-w"], timeout=60)
        _wait(lambda: _is_promoted(values, ports[1]), label="standby promotion")
        with _connect(values, ports[1], "gwdc_pr12_failover") as db:
            db.execute("INSERT INTO failover_markers(marker) VALUES ('PROMOTED_WRITE')")
            markers = [row[0] for row in db.execute(
                "SELECT marker FROM failover_markers ORDER BY marker").fetchall()]
        if markers != ["PROMOTED_WRITE", "REPLICATED"]:
            raise RuntimeError("promoted standby verification failed")
    finally:
        _stop(pg_bin, primary)
        _stop(pg_bin, standby)
    return {"status": "PASS", "primary_fenced_before_promotion": True,
            "source_backup_manifest_sha256": primary_manifest,
            "standby_backup_manifest_sha256": standby_manifest,
            "primary_flush_lsn": str(primary_lsn), "standby_replay_lsn": replay_lsn,
            "promoted_markers": markers, "promoted_write": True}


def run(args):
    pg_bin = _safe_path(args.pg_bin, "PostgreSQL binary")
    bundled_lib = pg_bin.parents[3] / "lib" / "x86_64-linux-gnu"
    if bundled_lib.is_dir():
        existing = os.environ.get("LD_LIBRARY_PATH")
        os.environ["LD_LIBRARY_PATH"] = (str(bundled_lib) if not existing
                                         else str(bundled_lib) + ":" + existing)
    root = _safe_path(args.work_dir, "work")
    evidence_path = _safe_path(args.evidence, "evidence")
    if root.exists():
        raise RuntimeError("drill work directory already exists")
    if evidence_path.exists():
        raise RuntimeError("drill evidence already exists")
    root.mkdir(parents=True, mode=0o700)
    dsn = _owner_secret(args.dsn_file)
    source_env, values = _pg_env(dsn)
    started = datetime.now(UTC).isoformat()
    try:
        pitr = _pitr_drill(pg_bin, source_env, values, root,
                           (args.port_base, args.port_base + 1))
        failover = _failover_drill(pg_bin, source_env, values, root,
                                   (args.port_base + 2, args.port_base + 3))
        result = {"schema_version": "gwdc-postgres-resilience-drill-1",
                  "status": "PASS", "started_at": started,
                  "completed_at": datetime.now(UTC).isoformat(),
                  "source_modified": False, "pitr": pitr, "failover": failover}
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        return result
    finally:
        if not args.keep_work_dir and root.exists():
            shutil.rmtree(root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pg-bin", required=True)
    parser.add_argument("--dsn-file", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--port-base", type=int, default=55440)
    parser.add_argument("--keep-work-dir", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port_base <= 65000 or args.port_base + 3 > 65535:
        raise SystemExit("invalid drill port range")
    print(json.dumps(run(args), sort_keys=True))


if __name__ == "__main__":
    main()
