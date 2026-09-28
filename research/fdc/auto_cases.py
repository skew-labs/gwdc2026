"""Incremental, source-reconciled TRON fact and change case builder.

These are narrow machine-verifiable observations, never allocation gold or
permission to train on third-party data. A collector-completion time does not
claim to be the market event time.
"""

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from finagent.store import Store


SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
 snapshot_id TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, source_id TEXT NOT NULL,
 output_sha256 TEXT NOT NULL, case_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS latest_fact (
 source_id TEXT NOT NULL, subject_id TEXT NOT NULL, metric_id TEXT NOT NULL,
 snapshot_id TEXT NOT NULL, fetched_at TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
 json_path TEXT NOT NULL, canonical_value TEXT, unit TEXT NOT NULL,
 quality TEXT NOT NULL,
 PRIMARY KEY(source_id, subject_id, metric_id)
);
CREATE TABLE IF NOT EXISTS processed_releases (
 release_id TEXT PRIMARY KEY, output_sha256 TEXT NOT NULL, case_count INTEGER NOT NULL,
 first_observations INTEGER NOT NULL, source_revisions INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS historical_identity (
 source_kind TEXT NOT NULL, identity TEXT NOT NULL, latest_digest TEXT NOT NULL,
 latest_record_id TEXT NOT NULL, latest_release TEXT NOT NULL,
 PRIMARY KEY(source_kind, identity)
);
"""
VALID = {"VALID", "VALID_ZERO"}
ELIGIBLE_PARSERS = {"0.3.0"}
LEGACY_PARSERS = {"0.1.0", "0.2.0"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _utc(value: str) -> datetime:
    at = datetime.fromisoformat(value)
    if at.tzinfo is None or at.utcoffset() != timezone.utc.utcoffset(at):
        raise ValueError("snapshot time must be UTC")
    return at


def _witness(snapshot: sqlite3.Row, fact: dict) -> dict:
    return {"snapshot_id": snapshot["id"], "raw_sha256": snapshot["sha256"],
            "source_url": snapshot["source_url"], "fetched_at": snapshot["fetched_at"],
            "json_path": fact["json_path"]}


def _case(snapshot: sqlite3.Row, fact: dict, kind: str, *, previous: dict | None,
          direction: str | None = None) -> dict:
    witnesses = [_witness(snapshot, fact)]
    if previous is not None:
        witnesses.insert(0, {"snapshot_id": previous["snapshot_id"],
                             "raw_sha256": previous["raw_sha256"],
                             "source_url": previous["source_url"],
                             "fetched_at": previous["fetched_at"],
                             "json_path": previous["json_path"]})
    identity = {"kind": kind, "source_id": snapshot["source_id"],
                "snapshot_id": snapshot["id"], "subject_id": fact["subject_id"],
                "metric_id": fact["metric_id"]}
    return {"schema_version": "1.0.0", "case_id": _sha(_canonical(identity)),
            "kind": kind, "source_id": snapshot["source_id"],
            "subject_id": fact["subject_id"], "metric_id": fact["metric_id"],
            "unit": fact["unit"], "quality": fact["quality"],
            "observed_value": fact["canonical_value"],
            "previous_value": None if previous is None else previous["canonical_value"],
            "direction": direction, "source_time_kind": "collector_completion",
            "source_event_time": None, "witnesses": witnesses,
            "rights_status": "unknown", "training_scope": "excluded_rights_unknown",
            "decision_gold": False, "product_actionable": False}


def _existing_witness(store: Store, old: sqlite3.Row) -> dict:
    with store.connect() as db:
        snapshot = db.execute("SELECT * FROM snapshots WHERE id=?",
                              (old["snapshot_id"],)).fetchone()
    if snapshot is None or snapshot["sha256"] != old["raw_sha256"]:
        raise ValueError("previous snapshot identity mismatch")
    payload = store.payload_for(snapshot)
    fact = next((item for item in store.facts_for(snapshot["id"])
                 if item["subject_id"] == old["subject_id"]
                 and item["metric_id"] == old["metric_id"]), None)
    if fact is None or fact["canonical_value"] != old["canonical_value"] \
            or fact["unit"] != old["unit"] or fact["quality"] != old["quality"]:
        raise ValueError("previous normalized fact changed")
    store.verify_fact(payload, fact)
    return {**dict(old), "source_url": snapshot["source_url"]}


def _cases_for_snapshot(store: Store, state: sqlite3.Connection,
                        snapshot: sqlite3.Row) -> tuple[list[dict], list[dict]]:
    if snapshot["parser_version"] not in ELIGIBLE_PARSERS:
        raise ValueError("snapshot parser cannot prove completion time")
    _utc(snapshot["fetched_at"])
    payload = store.payload_for(snapshot)
    cases = []
    updates = []
    for fact in sorted(store.facts_for(snapshot["id"]),
                       key=lambda item: (item["subject_id"], item["metric_id"])):
        store.verify_fact(payload, fact)
        old = state.execute(
            "SELECT * FROM latest_fact WHERE source_id=? AND subject_id=? AND metric_id=?",
            (snapshot["source_id"], fact["subject_id"], fact["metric_id"])).fetchone()
        previous = _existing_witness(store, old) if old is not None else None
        if old is not None and _utc(snapshot["fetched_at"]) < _utc(old["fetched_at"]):
            raise ValueError("out-of-order source snapshot")
        if old is not None and fact["unit"] != old["unit"]:
            raise ValueError("metric unit changed without a new contract")
        if old is None:
            kind = "FIRST_OBSERVED_VALUE" if fact["quality"] in VALID else "FIRST_OBSERVED_MISSING"
            cases.append(_case(snapshot, fact, kind, previous=None))
        elif fact["quality"] != old["quality"] and (fact["quality"] not in VALID or old["quality"] not in VALID):
            cases.append(_case(snapshot, fact, "QUALITY_TRANSITION", previous=previous))
        elif fact["quality"] in VALID and old["quality"] in VALID:
            before, after = Decimal(old["canonical_value"]), Decimal(fact["canonical_value"])
            if before != after:
                direction = "INCREASE" if after > before else "DECREASE"
                cases.append(_case(snapshot, fact, "OBSERVED_VALUE_CHANGE",
                                   previous=previous, direction=direction))
        updates.append((snapshot["source_id"], fact["subject_id"], fact["metric_id"],
                        snapshot["id"], snapshot["fetched_at"], snapshot["sha256"],
                        fact["json_path"], fact["canonical_value"], fact["unit"], fact["quality"]))
    return cases, updates


def _write_immutable(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError("existing case batch differs from replay")
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    # A crash can leave an uncommitted temporary file. The lock ensures no
    # other writer is using it; the final batch is verified above on replay.
    temporary.unlink(missing_ok=True)
    with temporary.open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def _historical_identity(row: dict) -> str:
    if row["source_kind"] == "usdd_tron_daily":
        return row["product_id"] + ":" + row["event_time"]
    meta = row["metadata"]
    return f"{row['product_id']}:{meta['transaction_id']}:{meta['event_index']}"


def _historical_digest(row: dict) -> str:
    values = {key: {"value": value["value"], "unit": value["unit"]}
              for key, value in row["measurements"].items()}
    return _sha(_canonical(values))


def _historical_case(row: dict, release_id: str, identity: str,
                     digest: str, old: sqlite3.Row | None) -> dict:
    kind = "HISTORICAL_FACT" if old is None else "SOURCE_REVISION"
    key = {"kind": kind, "source_kind": row["source_kind"],
           "identity": identity, "record_id": row["record_id"],
           "measurement_digest": digest}
    return {"schema_version": "1.0.0", "case_id": _sha(_canonical(key)),
            "kind": kind, "source_kind": row["source_kind"],
            "identity": identity, "release_id": release_id,
            "record_id": row["record_id"], "product_id": row["product_id"],
            "event_time": row["event_time"],
            "source_available_at": row["source_available_at"],
            "measurements": row["measurements"],
            "measurement_digest": digest,
            "previous_digest": None if old is None else old["latest_digest"],
            "raw_sha256": row["raw_sha256"], "source_url": row["source_url"],
            "json_pointer": row["json_pointer"], "rights_status": "unknown",
            "training_scope": "excluded_rights_unknown", "decision_gold": False,
            "product_actionable": False}


def _process_releases(store: Store, state: sqlite3.Connection,
                      output_dir: Path, release_root: Path) -> dict:
    from .validate_real_tron import validate_release

    if not release_root.is_dir():
        raise ValueError("historical release root unavailable")
    for old in state.execute("SELECT release_id,output_sha256 FROM processed_releases"):
        batch = output_dir / "history" / f"{old['release_id']}.jsonl"
        if not batch.is_file() or _sha(batch.read_bytes()) != old["output_sha256"]:
            raise ValueError("historical case batch missing or modified")
    releases = [path for path in release_root.iterdir()
                if path.is_dir() and (path / "manifest.json").is_file()]
    releases.sort(key=lambda path: (
        json.loads((path / "manifest.json").read_text(encoding="utf-8"))["created_at"],
        path.name))
    new_releases = new_cases = new_revisions = 0
    for release_dir in releases:
        if state.execute("SELECT 1 FROM processed_releases WHERE release_id=?",
                         (release_dir.name,)).fetchone():
            continue
        audit = validate_release(store, release_dir)
        if audit["release_id"] != release_dir.name:
            raise ValueError("historical release identity mismatch")
        rows = [json.loads(line) for line in
                (release_dir / "observations.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()]
        cases = []
        updates = []
        seen_here = set()
        for row in rows:
            identity = _historical_identity(row)
            key = (row["source_kind"], identity)
            if key in seen_here:
                raise ValueError("duplicate historical identity within release")
            seen_here.add(key)
            digest = _historical_digest(row)
            old = state.execute(
                "SELECT * FROM historical_identity WHERE source_kind=? AND identity=?", key
            ).fetchone()
            if old is not None and old["latest_digest"] == digest:
                continue
            cases.append(_historical_case(row, release_dir.name, identity, digest, old))
            updates.append((row["source_kind"], identity, digest, row["record_id"],
                            release_dir.name))
            if old is not None:
                new_revisions += 1
        content = b"".join(_canonical(case) + b"\n" for case in cases)
        batch = output_dir / "history" / f"{release_dir.name}.jsonl"
        batch.parent.mkdir(parents=True, exist_ok=True)
        _write_immutable(batch, content)
        with state:
            state.executemany(
                "INSERT INTO historical_identity VALUES (?,?,?,?,?) "
                "ON CONFLICT(source_kind,identity) DO UPDATE SET "
                "latest_digest=excluded.latest_digest,latest_record_id=excluded.latest_record_id,"
                "latest_release=excluded.latest_release", updates)
            state.execute("INSERT INTO processed_releases VALUES (?,?,?,?,?)",
                          (release_dir.name, _sha(content), len(cases),
                           sum(case["kind"] == "HISTORICAL_FACT" for case in cases),
                           sum(case["kind"] == "SOURCE_REVISION" for case in cases)))
        new_releases += 1
        new_cases += len(cases)
    totals = state.execute(
        "SELECT count(*) AS releases,coalesce(sum(case_count),0) AS cases,"
        "coalesce(sum(source_revisions),0) AS revisions FROM processed_releases"
    ).fetchone()
    return {"new_historical_releases": new_releases,
            "new_historical_cases": new_cases, "new_source_revisions": new_revisions,
            "total_historical_releases": totals["releases"],
            "total_historical_cases": totals["cases"],
            "total_source_revisions": totals["revisions"]}


def run_once(data_dir: Path, output_dir: Path,
             release_root: Path | None = None) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "engine.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = Store(data_dir)
        state = sqlite3.connect(output_dir / "index.sqlite3")
        state.row_factory = sqlite3.Row
        state.executescript(SCHEMA)
        for row in state.execute("SELECT snapshot_id,output_sha256 FROM processed"):
            batch = output_dir / "batches" / f"{row['snapshot_id']}.jsonl"
            if not batch.is_file() or _sha(batch.read_bytes()) != row["output_sha256"]:
                raise ValueError("processed case batch missing or modified")
        with store.connect() as db:
            snapshots = db.execute(
                "SELECT * FROM snapshots ORDER BY fetched_at,id").fetchall()
        new_snapshots = 0
        new_cases = 0
        legacy_snapshots = 0
        by_kind: dict[str, int] = {}
        for snapshot in snapshots:
            if state.execute("SELECT 1 FROM processed WHERE snapshot_id=?",
                             (snapshot["id"],)).fetchone():
                continue
            if snapshot["parser_version"] in LEGACY_PARSERS:
                legacy_snapshots += 1  # Earlier collector stamped request start.
                continue
            if snapshot["parser_version"] not in ELIGIBLE_PARSERS:
                raise ValueError("unknown parser version; source contract review needed")
            cases, updates = _cases_for_snapshot(store, state, snapshot)
            content = b"".join(_canonical(case) + b"\n" for case in cases)
            digest = _sha(content)
            batch = output_dir / "batches" / f"{snapshot['id']}.jsonl"
            batch.parent.mkdir(parents=True, exist_ok=True)
            _write_immutable(batch, content)
            with state:
                state.executemany(
                    "INSERT INTO latest_fact VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(source_id,subject_id,metric_id) DO UPDATE SET "
                    "snapshot_id=excluded.snapshot_id,fetched_at=excluded.fetched_at,"
                    "raw_sha256=excluded.raw_sha256,json_path=excluded.json_path,"
                    "canonical_value=excluded.canonical_value,unit=excluded.unit,"
                    "quality=excluded.quality", updates)
                state.execute("INSERT INTO processed VALUES (?,?,?,?,?)",
                              (snapshot["id"], snapshot["fetched_at"], snapshot["source_id"],
                               digest, len(cases)))
            new_snapshots += 1
            new_cases += len(cases)
            for case in cases:
                by_kind[case["kind"]] = by_kind.get(case["kind"], 0) + 1
        totals = state.execute(
            "SELECT count(*) AS snapshots,coalesce(sum(case_count),0) AS cases FROM processed"
        ).fetchone()
        history = (_process_releases(store, state, output_dir, release_root)
                   if release_root is not None else {})
        result = {"status": "RESEARCH_FACT_CASES_ONLY", "new_snapshots": new_snapshots,
                  "new_cases": new_cases, "new_by_kind": by_kind,
                  "skipped_legacy_snapshots": legacy_snapshots,
                  "total_snapshots": totals["snapshots"], "total_cases": totals["cases"],
                  "rights_status": "unknown", "decision_gold_count": 0,
                  "training_enabled": False, "product_actionable": False,
                  **history}
        summary = _canonical(result) + b"\n"
        temporary = output_dir / "latest.json.tmp"
        temporary.write_bytes(summary)
        temporary.replace(output_dir / "latest.json")
        state.close()
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--release-root", type=Path)
    args = parser.parse_args()
    try:
        result = run_once(args.data_dir, args.output_dir, args.release_root)
    except Exception as exc:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        failure = {"status": "RUN_FAILED", "at": datetime.now(timezone.utc).isoformat(),
                   "error_type": type(exc).__name__, "error": str(exc)[:300],
                   "training_enabled": False, "decision_gold_count": 0}
        attempt = args.output_dir / "latest_attempt.json"
        temporary = attempt.with_suffix(".json.tmp")
        temporary.write_bytes(_canonical(failure) + b"\n")
        temporary.replace(attempt)
        raise
    attempt = args.output_dir / "latest_attempt.json"
    temporary = attempt.with_suffix(".json.tmp")
    temporary.write_bytes(_canonical(result) + b"\n")
    temporary.replace(attempt)
    print(json.dumps(result, ensure_ascii=False, indent=2))
