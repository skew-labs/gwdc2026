"""Daily, fail-closed TRON observation and research-learning job.

It appends raw-validated observations, records source revisions, scores the
previous frozen checkpoint on newly matured days, and trains a new *research*
candidate. It never promotes a planner, calls Qwen, or submits transactions.
"""

import argparse
import fcntl
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from finagent.collect import source_registry
from finagent.quality import build_quality_report
from finagent.store import PARSER_VERSION, Store

from .real_tron import collect
from .train_real_temporal import MATURITY_HOURS, load_daily, score_unseen_day, train
from .validate_real_tron import validate_release


LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingested_releases (
 release_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
 source_kind TEXT NOT NULL, identity TEXT NOT NULL,
 event_time TEXT NOT NULL, first_seen_at TEXT NOT NULL,
 first_release TEXT NOT NULL, first_digest TEXT NOT NULL,
 latest_release TEXT NOT NULL, latest_digest TEXT NOT NULL,
 PRIMARY KEY (source_kind, identity)
);
CREATE TABLE IF NOT EXISTS revisions (
 source_kind TEXT NOT NULL, identity TEXT NOT NULL,
 release_id TEXT NOT NULL, previous_digest TEXT NOT NULL,
 current_digest TEXT NOT NULL, detected_at TEXT NOT NULL,
 mature_when_detected INTEGER NOT NULL,
 PRIMARY KEY (source_kind, identity, release_id)
);
CREATE TABLE IF NOT EXISTS forward_scores (
 event_time TEXT NOT NULL, checkpoint_release TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 PRIMARY KEY (event_time, checkpoint_release)
);
"""


def _identity(row: dict) -> str:
    if row["source_kind"] == "usdd_tron_daily":
        return row["product_id"] + ":" + row["event_time"]
    meta = row["metadata"]
    return f"{row['product_id']}:{meta['transaction_id']}:{meta['event_index']}"


def _digest(row: dict) -> str:
    # A sliding annual API window may change JSON array positions. Those are
    # provenance locators, not a change in the underlying economic values.
    facts = {key: {"value": value["value"], "unit": value["unit"]}
             for key, value in row["measurements"].items()}
    return hashlib.sha256(json.dumps(facts, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def ingest_release(db: sqlite3.Connection, store: Store, release_dir: Path) -> dict:
    audit = validate_release(store, release_dir)
    manifest = json.loads((release_dir / "manifest.json").read_text(encoding="utf-8"))
    release_id = audit["release_id"]
    if db.execute("SELECT 1 FROM ingested_releases WHERE release_id=?", (release_id,)).fetchone():
        return {"release_id": release_id, "already_ingested": True,
                "new_daily": 0, "new_events": 0, "mature_revisions": 0,
                "provisional_revisions": 0}
    counts = {"new_daily": 0, "new_events": 0,
              "mature_revisions": 0, "provisional_revisions": 0}
    rows = [json.loads(line) for line in
            (release_dir / "observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    with db:
        for row in rows:
            kind = row["source_kind"]
            identity = _identity(row)
            digest = _digest(row)
            old = db.execute("SELECT latest_digest FROM observations WHERE source_kind=? AND identity=?",
                             (kind, identity)).fetchone()
            if old is None:
                db.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?)",
                           (kind, identity, row["event_time"], row["source_available_at"],
                            release_id, digest, release_id, digest))
                counts["new_daily" if kind == "usdd_tron_daily" else "new_events"] += 1
            elif old[0] != digest:
                mature = (datetime.fromisoformat(row["source_available_at"])
                          - datetime.fromisoformat(row["event_time"])
                          >= timedelta(hours=MATURITY_HOURS))
                db.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?,?)",
                           (kind, identity, release_id, old[0], digest,
                            row["source_available_at"], int(mature)))
                db.execute("UPDATE observations SET latest_release=?, latest_digest=? "
                           "WHERE source_kind=? AND identity=?",
                           (release_id, digest, kind, identity))
                counts["mature_revisions" if mature else "provisional_revisions"] += 1
        db.execute("INSERT INTO ingested_releases VALUES (?,?,?)",
                   (release_id, manifest["created_at"], str(release_dir)))
    return {"release_id": release_id, "already_ingested": False, **counts}


def _release_dirs(root: Path) -> list[Path]:
    directories = [path for path in root.iterdir()
                   if path.is_dir() and (path / "manifest.json").is_file()]
    return sorted(directories, key=lambda path: json.loads(
        (path / "manifest.json").read_text(encoding="utf-8"))["created_at"])


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": "1.0.0", "dataset_through_event_time": None,
                "last_run_dir": None, "last_release_id": None}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema_version") != "1.0.0":
        raise ValueError("unknown continual state version")
    return state


def unreviewed_mature_revision_count(db: sqlite3.Connection) -> int:
    return db.execute("SELECT COUNT(*) FROM revisions WHERE mature_when_detected=1").fetchone()[0]


def _save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_once(data_dir: Path, registry_path: Path, release_root: Path,
             run_root: Path, state_dir: Path, *, pages: int = 2) -> dict:
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = (state_dir / "continual.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    store = Store(data_dir)
    specs = source_registry(registry_path)
    quality = build_quality_report(store, specs)
    market = store.latest("justlend_markets_v1")
    if quality["status"] != "PASS_FOR_RESEARCH_READ" or market is None or market["parser_version"] != PARSER_VERSION:
        raise ValueError("current TRON source quality or parser version blocked")
    release_root.mkdir(parents=True, exist_ok=True)
    run_root.mkdir(parents=True, exist_ok=True)
    ledger = sqlite3.connect(state_dir / "observation_index.sqlite3")
    ledger.executescript(LEDGER_SCHEMA)
    prior = []
    for path in _release_dirs(release_root):
        prior.append(ingest_release(ledger, store, path))
    release = collect(store, release_root, pages=pages, lookback_days=30)
    release_dir = Path(release["path"])
    current_ingest = ingest_release(ledger, store, release_dir)
    source_errors = release.get("source_errors", [])
    state_path = state_dir / "state.json"
    state = _load_state(state_path)
    daily = load_daily(release_dir)
    newest = daily[-1]["event_time"]
    # A detected mature rewrite remains quarantined across later idempotent
    # collection runs. A one-run failure would silently train on revised data.
    mature_revisions = unreviewed_mature_revision_count(ledger)
    if mature_revisions:
        result = {"status": "BLOCKED_MATURE_SOURCE_REVISION", "release_id": release["release_id"],
                  "newest_mature_event_time": newest, "ingest": current_ingest,
                  "unreviewed_mature_revisions": mature_revisions,
                  "source_errors": source_errors,
                  "model_promoted": False, "decision_gold_count": 0}
        _save_json(state_dir / "latest_attempt.json", result)
        return result
    previous_day = state["dataset_through_event_time"]
    if previous_day is not None and newest <= previous_day:
        result = {"status": "COLLECTED_NO_NEW_MATURE_DAY", "release_id": release["release_id"],
                  "newest_mature_event_time": newest, "ingest": current_ingest,
                  "source_errors": source_errors,
                  "model_promoted": False, "decision_gold_count": 0}
        _save_json(state_dir / "latest_attempt.json", result)
        return result
    prospective = []
    if previous_day is not None and state["last_run_dir"] is not None:
        checkpoint = Path(state["last_run_dir"]) / "temporal.pt"
        for index, row in enumerate(daily):
            if row["event_time"] <= previous_day:
                continue
            score = score_unseen_day(checkpoint, daily[:index], row)
            score["source_release"] = release["release_id"]
            prospective.append(score)
        with ledger:
            for score in prospective:
                ledger.execute("INSERT OR IGNORE INTO forward_scores VALUES (?,?,?)",
                               (score["event_time"], score["checkpoint_release"],
                                json.dumps(score, ensure_ascii=False, sort_keys=True)))
    run_dir = run_root / release["release_id"]
    if run_dir.exists():
        report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        if not (run_dir / "temporal.pt").exists() or report["source_release"] != release["release_id"]:
            raise ValueError("incomplete existing training run")
    else:
        report = train(store, release_dir, run_dir)
    model = report["holdout_relative_mae"]
    baseline = report["persistence_relative_mae"]
    beats_baseline = all(model[key] < baseline[key] for key in model)
    state = {"schema_version": "1.0.0", "dataset_through_event_time": newest,
             "last_run_dir": str(run_dir), "last_release_id": release["release_id"]}
    _save_json(state_path, state)
    result = {"status": "RESEARCH_CANDIDATE_ONLY" if beats_baseline else "REJECTED_BASELINE",
              "release_id": release["release_id"], "newest_mature_event_time": newest,
              "ingest": current_ingest, "prospective_scores": prospective,
              "source_errors": source_errors,
              "retrospective_model_beats_persistence": beats_baseline,
              "run_dir": str(run_dir), "model_promoted": False,
              "decision_gold_count": 0}
    _save_json(state_dir / "latest_attempt.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--pages", type=int, default=2)
    args = parser.parse_args()
    try:
        result = run_once(args.data_dir, args.registry, args.release_root,
                          args.run_root, args.state_dir, pages=args.pages)
    except Exception as exc:
        _save_json(args.state_dir / "latest_attempt.json", {
            "status": "RUN_FAILED", "checked_at": datetime.now(timezone.utc).isoformat(),
            "error_type": type(exc).__name__, "error": str(exc)[:300],
            "model_promoted": False, "decision_gold_count": 0,
        })
        raise
    print(json.dumps(result, ensure_ascii=False, indent=2))
