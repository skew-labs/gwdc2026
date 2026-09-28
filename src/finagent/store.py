"""Append-only source snapshots and typed facts. Run this on the approved host."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .contracts import canonical_decimal, decimal_string


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS snapshots (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  source_url TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  raw_path TEXT NOT NULL,
  http_status INTEGER NOT NULL,
  parser_version TEXT NOT NULL,
  CHECK(http_status = 200)
);
CREATE INDEX IF NOT EXISTS snapshot_latest ON snapshots(source_id, fetched_at DESC);
CREATE TABLE IF NOT EXISTS markets (
  snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
  market_address TEXT NOT NULL,
  network TEXT NOT NULL,
  jtoken_symbol TEXT NOT NULL,
  underlying_symbol TEXT NOT NULL,
  underlying_address TEXT NOT NULL,
  underlying_decimals INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active','legacy','unverified')),
  PRIMARY KEY(snapshot_id, market_address)
);
CREATE TABLE IF NOT EXISTS facts (
  snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
  subject_id TEXT NOT NULL,
  metric_id TEXT NOT NULL,
  raw_value TEXT,
  canonical_value TEXT,
  unit TEXT NOT NULL,
  quality TEXT NOT NULL CHECK(quality IN ('VALID','VALID_ZERO','MISSING','ERROR','NOT_APPLICABLE')),
  json_path TEXT NOT NULL,
  PRIMARY KEY(snapshot_id, subject_id, metric_id)
);
CREATE INDEX IF NOT EXISTS fact_lookup ON facts(subject_id, metric_id, snapshot_id);
CREATE TABLE IF NOT EXISTS collection_errors (
  event_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  error_type TEXT NOT NULL,
  detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request_budget (
  utc_day TEXT PRIMARY KEY,
  attempts INTEGER NOT NULL CHECK(attempts >= 0)
);
CREATE TABLE IF NOT EXISTS inference_events (
  event_id TEXT PRIMARY KEY,
  occurred_at TEXT NOT NULL,
  flow TEXT NOT NULL,
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  latency_ms INTEGER NOT NULL,
  outcome TEXT NOT NULL
);
"""

PARSER_VERSION = "0.3.0"


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.raw_root = self.root / "raw"
        self.db_path = self.root / "canonical.sqlite3"
        self.root.mkdir(parents=True, exist_ok=True)
        self.raw_root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def save_snapshot(self, source: dict[str, Any], fetched_at: str,
                      raw: bytes, parsed: Any, markets: list[dict],
                      facts: list[dict]) -> str:
        digest = hashlib.sha256(raw).hexdigest()
        snapshot_id = hashlib.sha256(
            (source["id"] + fetched_at + digest).encode("utf-8")
        ).hexdigest()
        destination = self.raw_root / source["id"] / f"{snapshot_id}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
        try:
            with self.connect() as db:
                db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?)",
                           (snapshot_id, source["id"], fetched_at, source["url"],
                            digest, str(destination), 200, PARSER_VERSION))
                db.executemany("INSERT INTO markets VALUES (?,?,?,?,?,?,?,?)", [
                    (snapshot_id, row["market_address"], row["network"],
                     row["jtoken_symbol"], row["underlying_symbol"],
                     row["underlying_address"], row["underlying_decimals"],
                     row["status"]) for row in markets
                ])
                db.executemany("INSERT INTO facts VALUES (?,?,?,?,?,?,?,?)", [
                    (snapshot_id, row["subject_id"], row["metric_id"],
                     row["raw_value"], row["canonical_value"], row["unit"],
                     row["quality"], row["json_path"]) for row in facts
                ])
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return snapshot_id

    def record_error(self, source_id: str, at: str, exc: Exception) -> None:
        event_id = hashlib.sha256((source_id + at + repr(exc)).encode()).hexdigest()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO collection_errors VALUES (?,?,?,?,?)",
                       (event_id, source_id, at, type(exc).__name__, str(exc)[:500]))

    def claim_request(self, utc_day: str, daily_limit: int) -> None:
        if daily_limit <= 0:
            raise ValueError("daily limit must be positive")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT attempts FROM request_budget WHERE utc_day=?",
                             (utc_day,)).fetchone()
            used = row["attempts"] if row else 0
            if used >= daily_limit:
                raise RuntimeError("public source daily request budget exhausted")
            db.execute("INSERT INTO request_budget(utc_day, attempts) VALUES (?, 1) "
                       "ON CONFLICT(utc_day) DO UPDATE SET attempts=attempts+1",
                       (utc_day,))

    def record_inference(self, event: dict) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO inference_events VALUES (?,?,?,?,?,?,?,?,?,?)", (
                event["event_id"], event["occurred_at"], event["flow"],
                event["provider"], event["model_id"], event["prompt_sha256"],
                event.get("input_tokens"), event.get("output_tokens"),
                event["latency_ms"], event["outcome"],
            ))

    def latest(self, source_id: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM snapshots WHERE source_id=? ORDER BY fetched_at DESC LIMIT 1",
                (source_id,),
            ).fetchone()

    def snapshots_for(self, source_id: str, through: str) -> list[sqlite3.Row]:
        """Return only snapshots fetched by a recorded UTC cutoff."""
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM snapshots WHERE source_id=? AND fetched_at<=? "
                "ORDER BY fetched_at, id", (source_id, through),
            ).fetchall()

    def facts_for(self, snapshot_id: str) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM facts WHERE snapshot_id=?", (snapshot_id,),
            )]

    def markets_for(self, snapshot_id: str) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM markets WHERE snapshot_id=?", (snapshot_id,),
            )]

    def payload_for(self, snapshot: sqlite3.Row) -> Any:
        raw = Path(snapshot["raw_path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != snapshot["sha256"]:
            raise ValueError("snapshot hash mismatch")
        return json.loads(raw.decode("utf-8"), parse_float=str)

    @staticmethod
    def verify_fact(payload: Any, fact: dict) -> None:
        """Reconcile one normalized value against its JSON Pointer in raw data."""
        if fact["quality"] not in {"VALID", "VALID_ZERO", "MISSING", "NOT_APPLICABLE"}:
            raise ValueError("unknown fact quality")
        value = payload
        absent = False
        try:
            for segment in fact["json_path"].split("/")[1:]:
                key = segment.replace("~1", "/").replace("~0", "~")
                value = value[int(key)] if isinstance(value, list) else value[key]
        except (KeyError, IndexError):
            absent = True
        except (TypeError, ValueError) as exc:
            raise ValueError("fact locator cannot be reconciled") from exc
        if fact["quality"] in {"MISSING", "NOT_APPLICABLE"}:
            if (fact["raw_value"] is not None or fact["canonical_value"] is not None
                    or (fact["quality"] == "NOT_APPLICABLE" and absent)
                    or (not absent and value is not None)):
                raise ValueError("null or missing fact differs from raw response")
            return
        if absent:
            raise ValueError("valid fact locator is absent")
        try:
            stored_raw = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
            canonical = canonical_decimal(decimal_string(stored_raw))
        except ValueError as exc:
            raise ValueError("fact value cannot be reconciled") from exc
        if fact["canonical_value"] != canonical or fact["raw_value"] != stored_raw:
            raise ValueError("normalized fact differs from raw response")
