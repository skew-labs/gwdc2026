"""Create an atomic verified physical backup on an attested off-host target."""

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime

from economic_machine.values import MachineError
from finance_service.postgres_runtime import read_owner_file
from scripts.postgres_resilience_drill import (
    _basebackup,
    _owner_secret,
    _pg_env,
    _safe_path,
)


def _attestation(path):
    try:
        raw, _ = read_owner_file(path, label="backup destination attestation",
                                 max_size=16_384)
    except MachineError as exc:
        raise RuntimeError(str(exc)) from exc
    document = json.loads(raw)
    if (document.get("schema_version") != "gwdc-backup-destination-1"
            or document.get("encrypted_at_rest") is not True
            or document.get("off_host") is not True
            or not isinstance(document.get("destination_id"), str)
            or not document["destination_id"].strip()):
        raise RuntimeError("backup destination is not encrypted and off-host attested")
    return document, hashlib.sha256(raw.encode()).hexdigest()


def run(args):
    pg_bin = _safe_path(args.pg_bin, "PostgreSQL binary")
    root = _safe_path(args.backup_root, "backup root")
    evidence_root = _safe_path(args.evidence_root, "backup evidence root")
    if not root.is_dir() or not evidence_root.is_dir():
        raise RuntimeError("backup and evidence roots must already exist")
    bundled_lib = pg_bin.parents[3] / "lib" / "x86_64-linux-gnu"
    if bundled_lib.is_dir():
        existing = os.environ.get("LD_LIBRARY_PATH")
        os.environ["LD_LIBRARY_PATH"] = (str(bundled_lib) if not existing
                                         else str(bundled_lib) + ":" + existing)
    destination, attestation_hash = _attestation(args.destination_attestation)
    dsn = _owner_secret(args.dsn_file)
    env, _ = _pg_env(dsn)
    now = datetime.now(UTC)
    backup_id = "gwdc-" + now.strftime("%Y%m%dT%H%M%SZ")
    final = root / backup_id
    temporary = root / ("." + backup_id + ".partial")
    evidence_path = evidence_root / (backup_id + ".json")
    if final.exists() or temporary.exists() or evidence_path.exists():
        raise RuntimeError("backup identifier already exists")
    try:
        manifest_hash = _basebackup(pg_bin, temporary, env)
        temporary.rename(final)
    except (Exception, KeyboardInterrupt):
        if temporary.exists():
            import shutil
            shutil.rmtree(temporary)
        raise
    size_bytes = sum(path.stat().st_size for path in final.rglob("*") if path.is_file())
    result = {"schema_version": "gwdc-postgres-physical-backup-1",
        "status": "VERIFIED", "backup_id": backup_id,
        "started_at": now.isoformat(), "completed_at": datetime.now(UTC).isoformat(),
        "backup_manifest_sha256": manifest_hash, "size_bytes": size_bytes,
        "destination_id": destination["destination_id"],
        "destination_attestation_sha256": attestation_hash,
        "encrypted_at_rest": True, "off_host": True}
    evidence_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pg-bin", required=True)
    parser.add_argument("--dsn-file", required=True)
    parser.add_argument("--backup-root", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--destination-attestation", required=True)
    args = parser.parse_args()
    print(json.dumps(run(args), sort_keys=True))


if __name__ == "__main__":
    main()
