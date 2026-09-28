"""BYON research runner: typed TRON changes -> policy-specific, non-executable decisions.

The hot monitoring path makes no model/API calls. This module intentionally has
no signing, broadcasting, or transaction preparation capability.
"""

import argparse
import fcntl
import hashlib
import json
import os
import socket
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .contracts import Need, canonical_decimal
from .planner import PlanUnavailable, compare
from .store import Store


VERSION = "fs1-research-runner-0.1.0"
REQUIRED_SOURCES = {
    "justlend_contracts": 86400,
    "justlend_markets_v1": 900,
    "justlend_usdd_rewards_v1": 900,
    "usdd_earn_apy": 900,
}
SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS policies (
  policy_id TEXT NOT NULL, version INTEGER NOT NULL,
  policy_hash TEXT NOT NULL, policy_json TEXT NOT NULL,
  registered_at TEXT NOT NULL, state TEXT NOT NULL,
  PRIMARY KEY(policy_id, version)
);
CREATE TABLE IF NOT EXISTS active_policies (
  policy_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
  current_decision_id TEXT
);
CREATE TABLE IF NOT EXISTS dependencies (
  policy_id TEXT NOT NULL, version INTEGER NOT NULL,
  source_id TEXT NOT NULL, subject_id TEXT NOT NULL, metric_id TEXT NOT NULL,
  PRIMARY KEY(policy_id, version, source_id, subject_id, metric_id)
);
CREATE INDEX IF NOT EXISTS dependency_lookup
 ON dependencies(source_id, subject_id, metric_id);
CREATE TABLE IF NOT EXISTS consumed_batches (
  snapshot_id TEXT PRIMARY KEY, output_sha256 TEXT NOT NULL,
  consumed_at TEXT NOT NULL, case_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
  decision_id TEXT PRIMARY KEY, policy_id TEXT NOT NULL, version INTEGER NOT NULL,
  state TEXT NOT NULL, receipt_json TEXT NOT NULL, plan_json TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT NOT NULL,
  new_batches INTEGER NOT NULL, changed_cases INTEGER NOT NULL,
  affected_policies INTEGER NOT NULL, planner_calls INTEGER NOT NULL,
  llm_calls INTEGER NOT NULL, elapsed_ms INTEGER NOT NULL
);
"""


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamp must be UTC")
    return parsed


def _db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    db.executescript(SCHEMA)
    return db


def _dependencies(store: Store, need: Need) -> list[tuple[str, str, str]]:
    latest = store.latest("justlend_markets_v1")
    if latest is None:
        raise ValueError("no JustLend market snapshot for policy binding")
    markets = [item for item in store.markets_for(latest["id"])
               if item["underlying_symbol"] == need.asset and item["status"] == "active"]
    if len(markets) != 1:
        raise ValueError("policy asset does not bind to one active JustLend market")
    address = markets[0]["market_address"]
    return sorted({
        ("justlend_markets_v1", address, "supply_apy"),
        ("justlend_markets_v1", address, "available_cash"),
        ("justlend_usdd_rewards_v1", address, "usdd_reward_apy"),
        ("usdd_earn_apy", "usdd:tron", "usdd_tron_earn_apy"),
    })


def register_policy(store: Store, runtime_dir: Path, payload: dict,
                    *, now: datetime | None = None) -> dict:
    """Register a read-only demo policy. User approval is never inferred here."""
    if not isinstance(payload, dict) or set(payload) != {
        "policy_id", "node_id", "network", "consent", "mode", "need"
    }:
        raise ValueError("exact policy_id/node_id/network/consent/mode/need required")
    for key in ("policy_id", "node_id"):
        value = payload[key]
        if not isinstance(value, str) or not 1 <= len(value) <= 64 or not all(
                char.isalnum() or char in "-_" for char in value):
            raise ValueError(f"invalid {key}")
    if (payload["network"], payload["consent"], payload["mode"]) != (
            "tron-mainnet-read", "DEMO_SCENARIO", "READ_ONLY"):
        raise ValueError("v1 only accepts explicitly marked, read-only demo policies")
    need = Need.from_json(payload["need"])
    normalized = {**payload, "need": {
        "asset": need.asset, "amount": canonical_decimal(need.amount),
        "liquid_reserve": canonical_decimal(need.liquid_reserve),
        "horizon_days": need.horizon_days, "risk": need.risk,
    }}
    deps = _dependencies(store, need)
    at = now or datetime.now(timezone.utc)
    _utc(at.isoformat())
    db = _db(Path(runtime_dir) / "runner.sqlite3")
    try:
        with db:
            old = db.execute("SELECT version FROM active_policies WHERE policy_id=?",
                             (payload["policy_id"],)).fetchone()
            if old:
                previous = db.execute("SELECT policy_json,policy_hash FROM policies "
                                      "WHERE policy_id=? AND version=?",
                                      (payload["policy_id"], old["version"])).fetchone()
                if previous["policy_json"] == _canonical(normalized):
                    return {"policy_id": payload["policy_id"], "version": old["version"],
                            "policy_hash": previous["policy_hash"], "dependencies": deps,
                            "execution_permitted": False, "idempotent": True}
            version = old["version"] + 1 if old else 1
            if old:
                db.execute("UPDATE policies SET state='SUPERSEDED' WHERE policy_id=? AND version=?",
                           (payload["policy_id"], old["version"]))
            policy_hash = _sha(_canonical({"version": version, "policy": normalized}).encode())
            db.execute("INSERT INTO policies VALUES (?,?,?,?,?,?)",
                       (payload["policy_id"], version, policy_hash,
                        _canonical(normalized), at.isoformat(), "ACTIVE"))
            db.execute("INSERT INTO active_policies VALUES (?,?,NULL) "
                       "ON CONFLICT(policy_id) DO UPDATE SET version=excluded.version,"
                       "current_decision_id=NULL", (payload["policy_id"], version))
            db.executemany("INSERT INTO dependencies VALUES (?,?,?,?,?)", [
                (payload["policy_id"], version, *dep) for dep in deps])
    finally:
        db.close()
    return {"policy_id": payload["policy_id"], "version": version,
            "policy_hash": policy_hash, "dependencies": deps,
            "execution_permitted": False}


def _source_health(store: Store, at: datetime) -> tuple[list[str], dict]:
    blockers = []
    refs = {}
    for source_id, max_age in REQUIRED_SOURCES.items():
        row = store.latest(source_id)
        if row is None:
            blockers.append("MISSING_SOURCE:" + source_id)
            continue
        age = (at - _utc(row["fetched_at"])).total_seconds()
        if age < -30 or age > max_age:
            blockers.append("STALE_SOURCE:" + source_id)
        try:
            store.payload_for(row)
        except (OSError, UnicodeError, ValueError):
            blockers.append("RAW_INTEGRITY:" + source_id)
        refs[source_id] = {"snapshot_id": row["id"], "raw_sha256": row["sha256"],
                           "fetched_at": row["fetched_at"]}
    return blockers, refs


def _dependency_health(store: Store, db: sqlite3.Connection,
                       policy_id: str, version: int) -> list[str]:
    """A missing/invalid watched fact stays blocked until a valid observation arrives."""
    blockers = []
    for dep in db.execute("SELECT source_id,subject_id,metric_id FROM dependencies "
                          "WHERE policy_id=? AND version=?", (policy_id, version)):
        snapshot = store.latest(dep["source_id"])
        if snapshot is None:
            blockers.append("MISSING_WATCHED_SOURCE:" + dep["source_id"])
            continue
        fact = next((item for item in store.facts_for(snapshot["id"])
                     if item["subject_id"] == dep["subject_id"]
                     and item["metric_id"] == dep["metric_id"]), None)
        if fact is None or fact["quality"] not in {"VALID", "VALID_ZERO"}:
            blockers.append("INVALID_WATCHED_FACT:" + ":".join(dep))
        elif fact is not None:
            try:
                store.verify_fact(store.payload_for(snapshot), fact)
            except (OSError, UnicodeError, ValueError):
                blockers.append("WATCHED_FACT_RECONCILIATION:" + ":".join(dep))
    return blockers


def _read_batches(case_dir: Path, consumed: set[str]) -> list[tuple[sqlite3.Row, list[dict]]]:
    index = sqlite3.connect(case_dir / "index.sqlite3")
    index.row_factory = sqlite3.Row
    try:
        rows = index.execute("SELECT snapshot_id,fetched_at,source_id,output_sha256,case_count "
                             "FROM processed ORDER BY fetched_at,snapshot_id").fetchall()
    finally:
        index.close()
    result = []
    for row in rows:
        if row["snapshot_id"] in consumed:
            continue
        batch = case_dir / "batches" / (row["snapshot_id"] + ".jsonl")
        raw = batch.read_bytes()
        if _sha(raw) != row["output_sha256"]:
            raise ValueError("case batch hash mismatch: " + row["snapshot_id"])
        cases = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if len(cases) != row["case_count"] or any(
                case.get("source_id") != row["source_id"]
                or not case.get("witnesses")
                or case["witnesses"][-1].get("snapshot_id") != row["snapshot_id"]
                for case in cases):
            raise ValueError("case batch identity/count mismatch: " + row["snapshot_id"])
        result.append((row, cases))
    return result


def run_once(store: Store, case_dir: Path, runtime_dir: Path,
             *, now: datetime | None = None,
             planner: Callable = compare) -> dict:
    """Consume new immutable batches; coalesce changes to one decision per policy."""
    start = time.monotonic()
    at = now or datetime.now(timezone.utc)
    _utc(at.isoformat())
    runtime_dir = Path(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    with (runtime_dir / "runner.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = _db(runtime_dir / "runner.sqlite3")
        try:
            existing = {row["snapshot_id"]: row["output_sha256"] for row in
                        db.execute("SELECT snapshot_id,output_sha256 FROM consumed_batches")}
            index = sqlite3.connect(Path(case_dir) / "index.sqlite3")
            try:
                for snapshot_id, digest in existing.items():
                    indexed = index.execute("SELECT output_sha256 FROM processed WHERE snapshot_id=?",
                                            (snapshot_id,)).fetchone()
                    if indexed is None or indexed[0] != digest:
                        raise ValueError("previously consumed case batch changed")
            finally:
                index.close()
            batches = _read_batches(Path(case_dir), set(existing))
            active = db.execute("SELECT p.*,a.current_decision_id FROM policies p "
                                "JOIN active_policies a ON a.policy_id=p.policy_id "
                                "AND a.version=p.version ORDER BY p.policy_id").fetchall()
            changes = {row["policy_id"]: [] for row in active}
            registrations = {row["policy_id"]: _utc(row["registered_at"]) for row in active}
            total_cases = 0
            for batch, cases in batches:
                total_cases += len(cases)
                fetched = _utc(batch["fetched_at"])
                for case in cases:
                    targets = db.execute(
                        "SELECT d.policy_id FROM dependencies d "
                        "JOIN active_policies a ON a.policy_id=d.policy_id AND a.version=d.version "
                        "WHERE d.source_id=? AND d.subject_id=? AND d.metric_id=?",
                        (case["source_id"], case["subject_id"], case["metric_id"])).fetchall()
                    for target in targets:
                        policy_id = target["policy_id"]
                        if fetched > registrations[policy_id]:
                            changes[policy_id].append(case)
            blockers, refs = _source_health(store, at)
            emitted = []
            planner_calls = 0
            with db:
                for row in active:
                    policy_id = row["policy_id"]
                    relevant = changes[policy_id]
                    previous = (db.execute("SELECT state,receipt_json FROM decisions WHERE decision_id=?",
                                           (row["current_decision_id"],)).fetchone()
                                if row["current_decision_id"] else None)
                    previous_state = previous["state"] if previous else None
                    previous_receipt = json.loads(previous["receipt_json"]) if previous else None
                    dependency_blockers = _dependency_health(
                        store, db, policy_id, row["version"])
                    must_pause = bool(blockers or dependency_blockers)
                    if must_pause:
                        if previous_state == "PAUSED" and not relevant:
                            continue
                        state = "PAUSED"
                        plan = None
                        reasons = sorted(set(blockers + dependency_blockers))
                    else:
                        if previous_state is not None and not relevant and previous_state != "PAUSED":
                            continue
                        if (previous_state == "PAUSED" and not relevant
                                and previous_receipt["state_refs"] == refs
                                and not any(reason.startswith("STALE_SOURCE:")
                                            for reason in previous_receipt["reason_codes"])):
                            continue
                        need = Need.from_json(json.loads(row["policy_json"])["need"])
                        planner_calls += 1
                        try:
                            plan = planner(store, need, now=at)
                            state = "REPLAN_RESEARCH_ONLY"
                            reasons = ["INITIAL_OR_POLICY_REVISION" if previous_state is None
                                       else "SOURCE_CHANGED" if relevant else "SOURCE_RECOVERED"]
                        except PlanUnavailable as exc:
                            plan = None
                            state = "PAUSED"
                            reasons = ["PLANNER_UNAVAILABLE:" + str(exc)]
                    identity = {"runner_version": VERSION, "node_id": json.loads(row["policy_json"])["node_id"],
                                "policy_id": policy_id, "policy_version": row["version"],
                                "policy_hash": row["policy_hash"], "state": state,
                                "reason_codes": reasons, "case_ids": sorted({c["case_id"] for c in relevant}),
                                "state_refs": refs, "plan_id": plan["plan_id"] if plan else None,
                                "execution_permitted": False,
                                "signature_status": "UNSIGNED_LOCAL",
                                "chain_status": "NOT_SUBMITTED"}
                    decision_id = _sha(_canonical(identity).encode())
                    receipt = {**identity, "decision_id": decision_id,
                               "created_at": at.isoformat(),
                               "source_time_kind": "collector_completion"}
                    db.execute("INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?,?,?)",
                               (decision_id, policy_id, row["version"], state,
                                _canonical(receipt), _canonical(plan) if plan else None,
                                at.isoformat()))
                    db.execute("UPDATE active_policies SET current_decision_id=? WHERE policy_id=?",
                               (decision_id, policy_id))
                    emitted.append(receipt)
                for batch, cases in batches:
                    db.execute("INSERT INTO consumed_batches VALUES (?,?,?,?)",
                               (batch["snapshot_id"], batch["output_sha256"], at.isoformat(), len(cases)))
                elapsed = round((time.monotonic() - start) * 1000)
                run_id = _sha((at.isoformat() + str(time.monotonic_ns())).encode())
                db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
                           (run_id, at.isoformat(), datetime.now(timezone.utc).isoformat(),
                            len(batches), total_cases,
                            sum(bool(c) for c in changes.values()), planner_calls, 0, elapsed))
            return {"run_id": run_id, "node_mode": "BYON_READ_ONLY_DEMO",
                    "new_batches": len(batches), "changed_cases": total_cases,
                    "affected_policies": sum(bool(c) for c in changes.values()),
                    "planner_calls": planner_calls, "llm_calls": 0,
                    "elapsed_ms": elapsed, "decisions": emitted}
        finally:
            db.close()


def status(runtime_dir: Path) -> dict:
    db = _db(Path(runtime_dir) / "runner.sqlite3")
    try:
        policies = []
        for row in db.execute("SELECT a.policy_id,a.version,a.current_decision_id,p.policy_hash "
                              "FROM active_policies a JOIN policies p ON p.policy_id=a.policy_id "
                              "AND p.version=a.version ORDER BY a.policy_id"):
            decision = db.execute("SELECT state,receipt_json FROM decisions WHERE decision_id=?",
                                  (row["current_decision_id"],)).fetchone() if row["current_decision_id"] else None
            policies.append({"policy_id": row["policy_id"], "version": row["version"],
                             "policy_hash": row["policy_hash"],
                             "state": decision["state"] if decision else "AWAITING_FIRST_RUN",
                             "receipt": json.loads(decision["receipt_json"]) if decision else None})
        last = db.execute("SELECT * FROM runs ORDER BY completed_at DESC LIMIT 1").fetchone()
        return {"runner_version": VERSION, "node_host": socket.gethostname(),
                "logical_cpus": os.cpu_count(), "mode": "BYON_READ_ONLY_DEMO",
                "policies": policies, "last_run": dict(last) if last else None,
                "execution_permitted": False}
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="BYON TRON B research runner (read only)")
    parser.add_argument("command", choices=("register", "run-once", "status"))
    parser.add_argument("--data-dir", type=Path, default=Path("data/live-read"))
    parser.add_argument("--case-dir", type=Path, default=Path("data/auto-cases"))
    parser.add_argument("--runtime-dir", type=Path, default=Path("data/fs1-runner-demo"))
    parser.add_argument("--policy-file", type=Path)
    args = parser.parse_args()
    if args.command == "register":
        if args.policy_file is None:
            parser.error("register requires --policy-file")
        answer = register_policy(Store(args.data_dir), args.runtime_dir,
                                 json.loads(args.policy_file.read_text()))
    elif args.command == "run-once":
        answer = run_once(Store(args.data_dir), args.case_dir, args.runtime_dir)
    else:
        answer = status(args.runtime_dir)
    print(json.dumps(answer, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
