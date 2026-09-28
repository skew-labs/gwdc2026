"""Reconcile every normalized TRON observation to archived source bytes."""

import argparse
import hashlib
import json
import urllib.parse
from collections import Counter
from pathlib import Path

from finagent.store import Store

from .real_tron import HISTORY_URL, event_rows, history_rows


def validate_release(store: Store, release_dir: Path) -> dict:
    release_dir = Path(release_dir)
    manifest = json.loads((release_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest["network"] != "tron_mainnet" or manifest["decision_labels"] != 0:
        raise ValueError("invalid release scope")
    if manifest["review_status"] != "unreviewed" or manifest["rights_status"] != "unknown":
        raise ValueError("real data promoted without review")
    registry = manifest["justlend_registry"]
    with store.connect() as db:
        for key in ("contracts", "markets"):
            row = db.execute("SELECT * FROM snapshots WHERE id=?",
                             (registry[key]["snapshot_id"],)).fetchone()
            if row is None or row["sha256"] != registry[key]["sha256"]:
                raise ValueError("JustLend registry snapshot mismatch")
            store.payload_for(row)
    markets = {market["market_address"]: market
               for market in store.markets_for(registry["markets"]["snapshot_id"])}
    source_hashes = [source["sha256"] for source in manifest["sources"]]
    expected_release = hashlib.sha256(json.dumps(
        {"raw": source_hashes, "registry": registry}, sort_keys=True).encode()).hexdigest()
    if expected_release != manifest["release_id"] or release_dir.name != expected_release:
        raise ValueError("release identity mismatch")
    expected = []
    for source in manifest["sources"]:
        path = (release_dir / source["path"]).resolve()
        if not path.is_relative_to(release_dir.resolve()):
            raise ValueError("source path escapes release")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != source["sha256"]:
            raise ValueError("raw source hash mismatch")
        payload = json.loads(raw.decode("utf-8-sig"), parse_float=str)
        url = source["url"]
        if url == HISTORY_URL:
            expected.extend(history_rows(payload, raw_sha=source["sha256"],
                                         fetched_at=source["fetched_at"]))
            continue
        parsed = urllib.parse.urlsplit(url)
        parts = parsed.path.split("/")
        query = urllib.parse.parse_qs(parsed.query)
        if (parsed.scheme != "https" or parsed.netloc != "api.trongrid.io"
                or len(parts) != 5 or parts[:3] != ["", "v1", "contracts"]
                or parts[4] != "events" or parts[3] not in markets
                or query.get("event_name") != ["JTokenStatus"]
                or query.get("only_confirmed") != ["true"]):
            raise ValueError("unexpected on-chain source URL")
        expected.extend(event_rows(payload, source_url=url, raw_sha=source["sha256"],
                                   fetched_at=source["fetched_at"], market=markets[parts[3]]))
    actual = [json.loads(line) for line in
              (release_dir / "observations.jsonl").read_text(encoding="utf-8").splitlines()
              if line.strip()]
    from jsonschema import Draft202012Validator, FormatChecker
    schema_path = Path(__file__).resolve().parents[2] / "contracts/tron_observation_v1.schema.json"
    validator = Draft202012Validator(json.loads(schema_path.read_text()),
                                      format_checker=FormatChecker())
    for index, row in enumerate(actual, 1):
        errors = list(validator.iter_errors(row))
        if errors:
            raise ValueError(f"observation {index} violates schema: {errors[0].message}")
    if actual != expected or len({row["record_id"] for row in actual}) != len(actual):
        raise ValueError("observations do not reconcile to archived raw responses")
    observed = Counter(row["source_kind"] for row in actual)
    counts = {kind: observed[kind] for kind in
              ("usdd_tron_daily", "justlend_jtoken_status")}
    if counts != manifest["counts"] or len(actual) != manifest["rows"]:
        raise ValueError("manifest counts mismatch")
    return {"release_id": expected_release, "rows": len(actual), "counts": counts,
            "earliest_event_time": min(row["event_time"] for row in actual),
            "latest_event_time": max(row["event_time"] for row in actual),
            "status": "RAW_RECONCILED_UNREVIEWED_OBSERVATIONS"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("release_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(validate_release(Store(args.data_dir), args.release_dir),
                     ensure_ascii=False, indent=2))
