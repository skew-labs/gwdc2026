"""Allowlisted, bounded read-only collection for remote execution."""

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .normalize import contract_status, normalize
from .store import Store


MAX_RESPONSE_BYTES = 1_000_000
MAX_DAILY_REQUESTS = 1000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def source_registry(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    sources = payload["sources"]
    ids = {source["id"] for source in sources}
    if len(ids) != len(sources):
        raise ValueError("duplicate source ID")
    for source in sources:
        if source["url"] not in {
            "https://openapi.just.network/lend/jtoken",
            "https://openapi.just.network/mining/apy",
            "https://docs.justlend.org/developers/contracts.json",
            "https://openapi.usdd.io/api/v1/external/earn-apy",
            "https://openapi.usdd.io/api/v1/market-site/overview",
            "https://openapi.usdd.io/api/v1/data-platform/latest-collateral?chain=tron",
        }:
            raise ValueError("source URL is not allowlisted")
    return sources


def fetch_json(url: str, store: Store) -> tuple[bytes, Any]:
    request = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": "GWDC-FinAgent-Research/0.1"
    })
    for attempt in range(3):
        store.claim_request(datetime.now(timezone.utc).date().isoformat(),
                            MAX_DAILY_REQUESTS)
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError("response exceeds size limit")
                return raw, json.loads(raw.decode("utf-8-sig"), parse_float=str)
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
            wait = exc.headers.get("Retry-After")
            time.sleep(min(15, int(wait)) if wait and wait.isdigit() else 2 ** attempt)
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable retry exit")


def collect_all(store: Store, registry_path: Path) -> dict[str, str]:
    sources = source_registry(registry_path)
    ordered = sorted(sources, key=lambda item: 0 if item["id"] == "justlend_contracts" else 1)
    result: dict[str, str] = {}
    statuses: dict[str, str] | None = None
    for index, source in enumerate(ordered):
        if index:
            time.sleep(1)
        attempted_at = utc_now()
        try:
            raw, payload = fetch_json(source["url"], store)
            # A fact cannot be available to a historical decision before the
            # complete HTTP body has arrived and parsed successfully.
            fetched_at = utc_now()
            if source["kind"] == "justlend_contracts":
                statuses = contract_status(payload)
            markets, facts = normalize(source["kind"], payload, statuses)
            result[source["id"]] = store.save_snapshot(
                source, fetched_at, raw, payload, markets, facts,
            )
        except Exception as exc:
            store.record_error(source["id"], attempted_at, exc)
            result[source["id"]] = "ERROR:" + type(exc).__name__
    return result
