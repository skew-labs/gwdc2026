"""Run and record the finance journal's full deterministic integrity audit."""

import argparse
import json

from finance_service.postgres_repository import PostgresOperationalRepository


def audit(dsn_file, *, allow_insecure_localhost=False):
    repository = PostgresOperationalRepository(api_dsn_file=dsn_file,
        allow_insecure_localhost=allow_insecure_localhost)
    try:
        health = repository.health()
        status = health["journal_deep_audit"]
        if not health["journal_integrity"] or not status["audited_root"]:
            raise RuntimeError("finance journal deep audit failed")
        return {"schema_version": "gwdc-finance-journal-audit-1",
                "status": "PASS", "audited_root": status["audited_root"],
                "audited_at": status["audited_at"],
                "head_mismatches": status["head_mismatches"]}
    finally:
        repository.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn-file", required=True)
    parser.add_argument("--allow-insecure-localhost", action="store_true")
    args = parser.parse_args()
    print(json.dumps(audit(args.dsn_file,
                           allow_insecure_localhost=args.allow_insecure_localhost),
                     sort_keys=True))


if __name__ == "__main__":
    main()
