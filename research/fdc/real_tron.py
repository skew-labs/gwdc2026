"""Bounded, read-only TRON observation release for research.

Historical API points are provider-reported event times, not proof that those
values were available to an agent on those dates. Raw responses are archived.
No row in this release is a reviewed allocation or execution label.
"""

import argparse
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from finagent.store import Store


HISTORY_URL = "https://openapi.usdd.io/api/v1/data-platform/collateral-history?chain=tron&interval=ANNUAL"
EVENT_BASE = "https://api.trongrid.io/v1/contracts"
MAX_BYTES = 1_000_000
DECIMAL = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")
INTEGER = re.compile(r"^(0|[1-9][0-9]*)$")
HISTORY_FIELDS = {
    "collateralValue": "USD", "debt": "USDD", "mintedUSDD": "USDD",
    "usddTotalSupply": "USDD", "totalSupplyValue": "USD",
    "earnTvl": "USD", "earnApy": "annual_fraction",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_ms(value: object) -> str:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("event timestamp must be epoch milliseconds")
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()


def exact(value: object, field: str) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError(f"{field}: decimal string or integer required")
    raw = str(value)
    if not DECIMAL.fullmatch(raw) or len(raw) > 100:
        raise ValueError(f"{field}: nonnegative plain decimal required")
    parsed = Decimal(raw)
    if not parsed.is_finite():
        raise ValueError(f"{field}: finite value required")
    return format(parsed, "f")


def exact_uint(value: object, field: str) -> int:
    if not isinstance(value, str) or not INTEGER.fullmatch(value) or len(value) > 100:
        raise ValueError(f"{field}: uint string required")
    return int(value)


def get_json(url: str) -> tuple[bytes, dict, str]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("unsafe source URL")
    if not (url == HISTORY_URL or
            (parsed.netloc == "api.trongrid.io" and
             parsed.path.startswith("/v1/contracts/") and parsed.path.endswith("/events"))):
        raise ValueError("source URL not allowlisted")
    request = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": "GWDC-FinAgent-Research/0.1"})
    if parsed.netloc == "api.trongrid.io" and os.environ.get("TRON_PRO_API_KEY"):
        request.add_header("TRON-PRO-API-KEY", os.environ["TRON_PRO_API_KEY"])
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        raw = response.read(MAX_BYTES + 1)
    fetched_at = now_utc().isoformat()
    if len(raw) > MAX_BYTES:
        raise ValueError("source response exceeds size cap")
    payload = json.loads(raw.decode("utf-8-sig"), parse_float=str)
    if not isinstance(payload, dict):
        raise ValueError("expected JSON object")
    return raw, payload, fetched_at


def _base(source_kind: str, source_url: str, raw_sha: str, fetched_at: str,
          pointer: str, event_time: str, product_id: str) -> dict:
    record_id = hashlib.sha256(f"{raw_sha}|{pointer}|{product_id}".encode()).hexdigest()
    return {
        "schema_version": "1.0.0", "record_id": record_id,
        "network": "tron_mainnet", "source_kind": source_kind,
        "source_url": source_url, "raw_sha256": raw_sha,
        "source_available_at": fetched_at, "event_time": event_time,
        "json_pointer": pointer, "product_id": product_id,
        "review_status": "unreviewed", "rights_status": "unknown",
        "decision_label": None,
    }


def history_rows(payload: dict, *, raw_sha: str, fetched_at: str) -> list[dict]:
    if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise ValueError("USDD history business error")
    entries = payload["data"].get("items")
    if not isinstance(entries, list) or not entries:
        raise ValueError("USDD history empty")
    fetched = datetime.fromisoformat(fetched_at)
    seen = set()
    rows = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError("invalid USDD history point")
        event_time = iso_ms(entry.get("statisticTime"))
        at = datetime.fromisoformat(event_time)
        if at > fetched:
            # The current-day record can carry a future period marker. It is
            # not available as a historical observation yet.
            continue
        if event_time in seen:
            raise ValueError("duplicate USDD history time")
        seen.add(event_time)
        point = _base("usdd_tron_daily", HISTORY_URL, raw_sha, fetched_at,
                      f"/data/items/{index}", event_time, "usdd:tron:protocol")
        point["measurements"] = {
            name: {"value": exact(entry.get(name), name), "unit": unit,
                   "json_pointer": f"/data/items/{index}/{name}"}
            for name, unit in HISTORY_FIELDS.items()
        }
        point["metadata"] = {"reported_time_text": entry.get("time"),
                             "source_time_kind": "provider_statisticTime"}
        rows.append(point)
    if not rows or [row["event_time"] for row in rows] != sorted(seen):
        raise ValueError("USDD history is empty or not chronological")
    return rows


def event_rows(payload: dict, *, source_url: str, raw_sha: str, fetched_at: str,
               market: dict) -> list[dict]:
    entries = payload.get("data")
    if not isinstance(entries, list):
        raise ValueError("TronGrid events missing")
    fetched = datetime.fromisoformat(fetched_at)
    rows = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("event_name") != "JTokenStatus":
            raise ValueError("unexpected event type")
        if entry.get("contract_address") != market["market_address"]:
            raise ValueError("event contract does not match registry")
        at = datetime.fromisoformat(iso_ms(entry.get("block_timestamp")))
        if at > fetched:
            raise ValueError("future chain event")
        result = entry.get("result")
        if not isinstance(result, dict):
            raise ValueError("event result missing")
        cash_raw = exact_uint(result.get("totalCash"), "totalCash")
        decimals = market["underlying_decimals"]
        if not isinstance(decimals, int) or not 0 <= decimals <= 36:
            raise ValueError("invalid underlying decimals")
        txid = entry.get("transaction_id")
        event_index = entry.get("event_index")
        block = entry.get("block_number")
        if not isinstance(txid, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", txid):
            raise ValueError("invalid transaction id")
        if not isinstance(event_index, int) or event_index < 0 or not isinstance(block, int) or block < 0:
            raise ValueError("invalid event position")
        point = _base("justlend_jtoken_status", source_url, raw_sha, fetched_at,
                      f"/data/{index}", at.isoformat(), market["market_address"])
        point["measurements"] = {"available_cash": {
            "value": format(Decimal(cash_raw) / (Decimal(10) ** decimals), "f"),
            "unit": market["underlying_symbol"],
            "json_pointer": f"/data/{index}/result/totalCash"}}
        point["metadata"] = {
            "transaction_id": txid, "event_index": event_index,
            "block_number": block, "underlying_address": market["underlying_address"],
            "underlying_decimals": decimals, "raw_total_cash": str(cash_raw),
            "confirmation_filter": "only_confirmed=true",
        }
        rows.append(point)
    return rows


def _markets(store: Store) -> tuple[list[dict], dict]:
    registry = store.latest("justlend_contracts")
    snapshot = store.latest("justlend_markets_v1")
    if registry is None or snapshot is None:
        raise ValueError("JustLend source snapshots missing")
    if now_utc() - datetime.fromisoformat(registry["fetched_at"]) > timedelta(days=1):
        raise ValueError("JustLend contract registry stale")
    if now_utc() - datetime.fromisoformat(snapshot["fetched_at"]) > timedelta(minutes=30):
        raise ValueError("JustLend market registry stale")
    store.payload_for(registry)
    store.payload_for(snapshot)
    markets = store.markets_for(snapshot["id"])
    selected = []
    for symbol in ("USDT", "USDD"):
        matches = [m for m in markets if m["network"] == "tron_mainnet"
                   and m["status"] == "active" and m["underlying_symbol"] == symbol]
        if len(matches) != 1:
            raise ValueError(f"unique active JustLend {symbol} market unavailable")
        selected.append(matches[0])
    return selected, {"contracts": {"snapshot_id": registry["id"], "sha256": registry["sha256"]},
                      "markets": {"snapshot_id": snapshot["id"], "sha256": snapshot["sha256"]}}


def collect(store: Store, output_root: Path, *, pages: int = 2, lookback_days: int = 30) -> dict:
    if not 1 <= pages <= 5 or not 1 <= lookback_days <= 90:
        raise ValueError("bounded pages and lookback required")
    markets, registry = _markets(store)
    start = now_utc()
    responses = []
    source_errors = []
    raw, payload, fetched_at = get_json(HISTORY_URL)
    raw_sha = hashlib.sha256(raw).hexdigest()
    rows = history_rows(payload, raw_sha=raw_sha, fetched_at=fetched_at)
    responses.append(("usdd_tron_annual", raw_sha, raw, fetched_at, HISTORY_URL))
    for market in markets:
        if source_errors:
            break
        base = f"{EVENT_BASE}/{market['market_address']}/events"
        params = {"event_name": "JTokenStatus", "only_confirmed": "true",
                  "min_timestamp": str(int((start - timedelta(days=lookback_days)).timestamp() * 1000)),
                  "max_timestamp": str(int(start.timestamp() * 1000)),
                  "order_by": "block_timestamp,desc", "limit": "200"}
        fingerprint = None
        for page in range(pages):
            query = dict(params)
            if fingerprint:
                query["fingerprint"] = fingerprint
            url = base + "?" + urllib.parse.urlencode(query)
            try:
                raw, payload, fetched_at = get_json(url)
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    raise
                source_errors.append({"source": "trongrid_confirmed_events",
                                      "status": "RATE_LIMITED_429",
                                      "market": market["market_address"],
                                      "attempted_at": now_utc().isoformat()})
                break
            raw_sha = hashlib.sha256(raw).hexdigest()
            page_rows = event_rows(payload, source_url=url, raw_sha=raw_sha,
                                   fetched_at=fetched_at, market=market)
            rows.extend(page_rows)
            responses.append((f"{market['underlying_symbol'].lower()}_events_{page}",
                              raw_sha, raw, fetched_at, url))
            meta = payload.get("meta") or {}
            fingerprint = meta.get("fingerprint")
            if not page_rows or not isinstance(fingerprint, str) or not fingerprint:
                break
    identities = [(row["product_id"], row["metadata"].get("transaction_id"),
                   row["metadata"].get("event_index"))
                  for row in rows if row["source_kind"] == "justlend_jtoken_status"]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate on-chain events across pages")
    release_id = hashlib.sha256(json.dumps(
        {"raw": [item[1] for item in responses], "registry": registry},
        sort_keys=True).encode()).hexdigest()
    release_dir = output_root / release_id
    if release_dir.exists():
        existing = json.loads((release_dir / "manifest.json").read_text(encoding="utf-8"))
        if existing.get("release_id") != release_id:
            raise ValueError("release path collision")
        return {"path": str(release_dir), **existing}
    staging = output_root / (".staging-" + release_id)
    staging.mkdir(parents=True, exist_ok=False)
    try:
        (staging / "raw").mkdir()
        manifest_sources = []
        for name, sha, body, fetched, url in responses:
            filename = f"raw/{name}-{sha}.json"
            (staging / filename).write_bytes(body)
            manifest_sources.append({"url": url, "fetched_at": fetched,
                                     "sha256": sha, "path": filename})
        with (staging / "observations.jsonl").open("x", encoding="utf-8") as output:
            for row in rows:
                output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        counts = {kind: sum(row["source_kind"] == kind for row in rows)
                  for kind in ("usdd_tron_daily", "justlend_jtoken_status")}
        manifest = {"schema_version": "1.0.0", "release_id": release_id,
                    "created_at": now_utc().isoformat(), "network": "tron_mainnet",
                    "sources": manifest_sources, "justlend_registry": registry,
                    "counts": counts, "rows": len(rows),
                    "review_status": "unreviewed", "rights_status": "unknown",
                    "decision_labels": 0,
                    "source_errors": source_errors,
                    "note": "Historical API series was first fetched at source_available_at; no past point-in-time availability is claimed."}
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        staging.rename(release_dir)
    except Exception:
        for path in sorted(staging.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        staging.rmdir()
        raise
    return {"path": str(release_dir), **manifest}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--lookback-days", type=int, default=30)
    args = parser.parse_args()
    print(json.dumps(collect(Store(args.data_dir), args.output_root,
                             pages=args.pages, lookback_days=args.lookback_days),
                     ensure_ascii=False, indent=2))
