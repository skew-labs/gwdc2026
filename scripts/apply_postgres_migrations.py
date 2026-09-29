"""Apply immutable finance-service PostgreSQL migrations with a hash ledger."""

import argparse
import hashlib
import json
import re
from pathlib import Path


def migration_body(path):
    raw = path.read_text()
    lines = raw.splitlines()
    if lines and lines[0].strip().upper() == "BEGIN;":
        lines = lines[1:]
    if lines and lines[-1].strip().upper() == "COMMIT;":
        lines = lines[:-1]
    return "\n".join(lines).strip() + "\n"


DESTRUCTIVE_SQL = re.compile(
    r"\b(DROP\s+TABLE|DROP\s+COLUMN|TRUNCATE\s+(?!ON\b)(?:TABLE\s+)?|"
    r"ALTER\s+COLUMN\s+[^;]+\s+TYPE|"
    r"DELETE\s+FROM)\b", re.IGNORECASE)


def _backup_evidence(path):
    if path is None:
        return None
    path = Path(path)
    document = json.loads(path.read_text())
    if (document.get("schema_version") != "gwdc-postgres-resilience-drill-1"
            or document.get("status") != "PASS"
            or document.get("pitr", {}).get("status") != "PASS"):
        raise RuntimeError("destructive migration backup evidence is not verified")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply(dsn, paths, *, backup_evidence=None):
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
        db.execute("ALTER TABLE finance_schema_migrations ADD COLUMN IF NOT EXISTS "
                   "risk_class text NOT NULL DEFAULT 'LEGACY' CHECK "
                   "(risk_class IN ('LEGACY','ADDITIVE','DESTRUCTIVE'))")
        db.execute("ALTER TABLE finance_schema_migrations ADD COLUMN IF NOT EXISTS "
                   "backup_evidence_sha256 text CHECK (backup_evidence_sha256 IS NULL OR "
                   "backup_evidence_sha256 ~ '^[0-9a-f]{64}$')")
        for path in paths:
            version = path.stem
            sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            body = migration_body(path)
            risk_class = "DESTRUCTIVE" if DESTRUCTIVE_SQL.search(body) else "ADDITIVE"
            row = db.execute("SELECT sha256 FROM finance_schema_migrations "
                             "WHERE version=%s", (version,)).fetchone()
            if row is not None:
                if row[0] != sha256:
                    raise RuntimeError("applied migration hash changed: " + version)
                db.execute("UPDATE finance_schema_migrations SET risk_class=%s "
                           "WHERE version=%s AND risk_class='LEGACY'",
                           (risk_class, version))
                results.append({"version": version, "sha256": sha256,
                                "status": "ALREADY_APPLIED"})
                continue
            evidence_hash = _backup_evidence(backup_evidence) if (
                risk_class == "DESTRUCTIVE") else None
            if risk_class == "DESTRUCTIVE" and evidence_hash is None:
                raise RuntimeError("destructive migration requires verified PITR evidence")
            db.execute(body, prepare=False)
            db.execute("INSERT INTO finance_schema_migrations"
                "(version,sha256,risk_class,backup_evidence_sha256) VALUES (%s,%s,%s,%s)",
                (version, sha256, risk_class, evidence_hash))
            results.append({"version": version, "sha256": sha256, "status": "APPLIED"})
        db.commit()
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--migration-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "db/migrations")
    parser.add_argument("--backup-evidence", type=Path)
    args = parser.parse_args()
    paths = [args.migration_dir / name for name in
             ("009_finance_service.sql", "011_finance_postgres_runtime.sql",
              "012_finance_resilience.sql", "013_finance_journal_audit.sql")]
    if any(not path.is_file() for path in paths):
        raise SystemExit("required PostgreSQL migration is missing")
    for result in apply(args.dsn, paths, backup_evidence=args.backup_evidence):
        print(result["version"], result["status"], result["sha256"])


if __name__ == "__main__":
    main()
