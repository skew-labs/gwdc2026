#!/usr/bin/env python3
"""Create an online SQLite snapshot encrypted to an external SSH recipient."""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from economic_machine.values import canonical, digest


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--age-bin", type=Path, required=True)
    parser.add_argument("--recipient-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if not args.database.is_file() or not args.age_bin.is_file() or not args.recipient_file.is_file():
        raise SystemExit("database, age binary and recipient file must exist")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.output.parent) as temp:
        snapshot = Path(temp) / "service.sqlite3"
        source = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite backup integrity check failed")
        finally:
            target.close()
            source.close()
        plain_hash = sha256(snapshot)
        subprocess.run([str(args.age_bin), "--encrypt", "--recipients-file",
            str(args.recipient_file), "--output", str(args.output), str(snapshot)],
            check=True, stdin=subprocess.DEVNULL)
    os.chmod(args.output, 0o600)
    recipient_hash = sha256(args.recipient_file)
    manifest = {"schema_version": "encrypted-offhost-backup-1",
        "created_at": datetime.now(UTC).isoformat(), "format": "age-encryption.org/v1",
        "source_kind": "SQLITE_ONLINE_BACKUP", "plaintext_sha256": plain_hash,
        "ciphertext_sha256": sha256(args.output),
        "ciphertext_bytes": args.output.stat().st_size,
        "recipient_file_sha256": recipient_hash,
        "offhost_status": "PENDING_UPLOAD"}
    manifest["manifest_hash"] = digest(manifest)
    args.manifest.write_bytes(canonical(manifest) + b"\n")
    os.chmod(args.manifest, 0o600)
    print(json.dumps({"output": str(args.output), "manifest": str(args.manifest),
                      "ciphertext_sha256": manifest["ciphertext_sha256"],
                      "ciphertext_bytes": manifest["ciphertext_bytes"]}, sort_keys=True))


if __name__ == "__main__":
    main()
