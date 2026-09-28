"""Apply immutable finance-service PostgreSQL migrations with a hash ledger."""

import argparse
import hashlib
from pathlib import Path


def migration_body(path):
    raw = path.read_text()
    lines = raw.splitlines()
    if lines and lines[0].strip().upper() == "BEGIN;":
        lines = lines[1:]
    if lines and lines[-1].strip().upper() == "COMMIT;":
        lines = lines[:-1]
    return "\n".join(lines).strip() + "\n"


def apply(dsn, paths):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("psycopg is required") from exc
    results = []
    with psycopg.connect(dsn, autocommit=False) as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                   ("finance-service-schema-v1",))
        db.execute("CREATE TABLE IF NOT EXISTS finance_schema_migrations ("
                   "version text PRIMARY KEY, sha256 text NOT NULL CHECK "
                   "(sha256 ~ '^[0-9a-f]{64}$'), applied_at timestamptz NOT NULL "
                   "DEFAULT clock_timestamp())")
        for path in paths:
            version = path.stem
            sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            row = db.execute("SELECT sha256 FROM finance_schema_migrations "
                             "WHERE version=%s", (version,)).fetchone()
            if row is not None:
                if row[0] != sha256:
                    raise RuntimeError("applied migration hash changed: " + version)
                results.append({"version": version, "sha256": sha256,
                                "status": "ALREADY_APPLIED"})
                continue
            db.execute(migration_body(path), prepare=False)
            db.execute("INSERT INTO finance_schema_migrations(version,sha256) VALUES (%s,%s)",
                       (version, sha256))
            results.append({"version": version, "sha256": sha256, "status": "APPLIED"})
        db.commit()
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--migration-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "db/migrations")
    args = parser.parse_args()
    paths = [args.migration_dir / name for name in
             ("009_finance_service.sql", "011_finance_postgres_runtime.sql")]
    if any(not path.is_file() for path in paths):
        raise SystemExit("required PostgreSQL migration is missing")
    for result in apply(args.dsn, paths):
        print(result["version"], result["status"], result["sha256"])


if __name__ == "__main__":
    main()
