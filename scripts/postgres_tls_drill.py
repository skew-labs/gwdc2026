"""Verify PostgreSQL TLS-only transport with certificate and hostname checks."""

import argparse
import hashlib
import json
import os
import shutil
from datetime import UTC, datetime

from scripts.postgres_resilience_drill import (
    _basebackup,
    _owner_secret,
    _pg_env,
    _run,
    _safe_path,
    _start,
    _stop,
)


def run(args):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("psycopg is required for the TLS drill") from exc
    pg_bin = _safe_path(args.pg_bin, "PostgreSQL binary")
    root = _safe_path(args.work_dir, "work")
    evidence_path = _safe_path(args.evidence, "evidence")
    if root.exists() or evidence_path.exists():
        raise RuntimeError("TLS drill paths must not already exist")
    bundled_lib = pg_bin.parents[3] / "lib" / "x86_64-linux-gnu"
    if bundled_lib.is_dir():
        existing = os.environ.get("LD_LIBRARY_PATH")
        os.environ["LD_LIBRARY_PATH"] = (str(bundled_lib) if not existing
                                         else str(bundled_lib) + ":" + existing)
    root.mkdir(parents=True, mode=0o700)
    data, socket_dir, certs = root / "data", root / "socket", root / "certs"
    certs.mkdir(mode=0o700)
    dsn = _owner_secret(args.dsn_file)
    source_env, values = _pg_env(dsn)
    started = datetime.now(UTC).isoformat()
    try:
        manifest_hash = _basebackup(pg_bin, data, source_env)
        ca_key, ca_cert = certs / "ca.key", certs / "ca.crt"
        server_key, server_csr = certs / "server.key", certs / "server.csr"
        server_cert, extensions = certs / "server.crt", certs / "server.ext"
        extensions.write_text("subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n")
        _run([args.openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
              "-keyout", str(ca_key), "-out", str(ca_cert), "-subj",
              "/CN=GWDC-PR12-TEST-CA", "-days", "1", "-sha256"], timeout=60)
        _run([args.openssl, "req", "-new", "-newkey", "rsa:2048", "-nodes",
              "-keyout", str(server_key), "-out", str(server_csr), "-subj",
              "/CN=127.0.0.1", "-sha256"], timeout=60)
        _run([args.openssl, "x509", "-req", "-in", str(server_csr),
              "-CA", str(ca_cert), "-CAkey", str(ca_key), "-CAcreateserial",
              "-out", str(server_cert), "-days", "1", "-sha256",
              "-extfile", str(extensions)], timeout=60)
        server_key.chmod(0o600)
        original_hba = (data / "pg_hba.conf").read_text()
        (data / "pg_hba.conf").write_text(
            "hostnossl all all 127.0.0.1/32 reject\n"
            "hostssl all all 127.0.0.1/32 trust\n" + original_hba)
        _start(pg_bin, data, args.port, socket_dir, extra_lines=(
            "ssl = 'on'", "ssl_cert_file = '" + str(server_cert) + "'",
            "ssl_key_file = '" + str(server_key) + "'",
            "ssl_ca_file = '" + str(ca_cert) + "'"))
        try:
            plaintext_rejected = False
            try:
                psycopg.connect(host="127.0.0.1", port=args.port,
                    user=values["user"], dbname="postgres", sslmode="disable",
                    connect_timeout=3).close()
            except psycopg.OperationalError:
                plaintext_rejected = True
            if not plaintext_rejected:
                raise RuntimeError("plaintext PostgreSQL connection was accepted")
            with psycopg.connect(host="127.0.0.1", port=args.port,
                    user=values["user"], dbname="postgres", sslmode="verify-full",
                    sslrootcert=str(ca_cert), connect_timeout=3) as db:
                ssl = db.execute("SELECT ssl,version,cipher FROM pg_stat_ssl "
                                 "WHERE pid=pg_backend_pid()").fetchone()
            if not ssl or not ssl[0]:
                raise RuntimeError("verified PostgreSQL connection did not use TLS")
        finally:
            _stop(pg_bin, data)
        result = {"schema_version": "gwdc-postgres-tls-drill-1", "status": "PASS",
            "started_at": started, "completed_at": datetime.now(UTC).isoformat(),
            "plaintext_rejected": plaintext_rejected, "sslmode": "verify-full",
            "tls_version": ssl[1], "cipher": ssl[2],
            "ca_certificate_sha256": hashlib.sha256(ca_cert.read_bytes()).hexdigest(),
            "server_certificate_sha256": hashlib.sha256(server_cert.read_bytes()).hexdigest(),
            "backup_manifest_sha256": manifest_hash, "source_modified": False}
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
    parser.add_argument("--port", type=int, default=55444)
    parser.add_argument("--openssl", default="/usr/bin/openssl")
    parser.add_argument("--keep-work-dir", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit("invalid TLS drill port")
    print(json.dumps(run(args), sort_keys=True))


if __name__ == "__main__":
    main()
