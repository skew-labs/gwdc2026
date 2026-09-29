"""Bounded read captures with exact raw bytes, request scope and provider lineage.

These are observations, not oracle signatures. HTTP receipt time never becomes
source observation time. Only fixed read endpoints are permitted by this port.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from finagent.normalize import normalize
from .mandate import normalize_scope, tron_address
from .tron_registry_read import _NoRedirect
from .values import MachineError, digest, require_keys, utc


VERSION = "economic-tron-capture-1"
MAX_BYTES = 1048576
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
# Source families are provenance labels, never independent-oracle counts.
SOURCES = {
    "justlend_contracts": ("justlend", "directory", "https://docs.justlend.org/developers/contracts.json", False),
    "justlend_markets_v1": ("justlend", "v1", "https://openapi.just.network/lend/jtoken", False),
    "justlend_usdd_rewards_v1": ("justlend", "v1", "https://openapi.just.network/mining/apy", False),
    "justlend_strx_v1": ("justlend", "v1", "https://openapi.just.network/lend/strx", False),
    "justlend_account_v1": ("justlend", "v1", "https://openapi.just.network/lend/account", True),
    "justlend_strx_account_v1": ("justlend", "v1", "https://openapi.just.network/lend/strxStake/account", True),
    "justlend_rental_account_v1": ("justlend", "v1", "https://openapi.just.network/lend/rentResource/account", True),
    "justlend_markets_v2": ("justlend", "v2", "https://openapi.just.network/v2/index/market/list", False),
    "usdd_tron_collateral": ("usdd", "v1", "https://openapi.usdd.io/api/v1/data-platform/latest-collateral?chain=tron", False),
    "usdd_vault_config": ("usdd", "v1", "https://openapi.usdd.io/api/v1/vault/collaterals", False),
    "usdd_earn_apy": ("usdd", "v1", "https://openapi.usdd.io/api/v1/external/earn-apy", False),
    "usdd_overview": ("usdd", "v1", "https://openapi.usdd.io/api/v1/market-site/overview", False),
    "tron_chain_parameters": ("trongrid", "native", "https://api.trongrid.io/wallet/getchainparameters", False),
}


def address_hex(value: str) -> str:
    if isinstance(value, str) and value.startswith("0x41"):
        value = value[2:]
    if isinstance(value, str) and re.fullmatch(r"41[0-9a-fA-F]{40}", value):
        return tron_address(value)
    if not isinstance(value, str) or len(value) != 34 or not value.startswith("T"):
        raise MachineError("invalid TRON Base58 address")
    number = 0
    for char in value:
        if char not in BASE58:
            raise MachineError("invalid Base58 character")
        number = number * 58 + BASE58.index(char)
    if number >= 1 << 200:
        raise MachineError("invalid TRON address width")
    decoded = number.to_bytes(25, "big")
    checksum = hashlib.sha256(hashlib.sha256(decoded[:21]).digest()).digest()[:4]
    if decoded[-4:] != checksum:
        raise MachineError("TRON address checksum mismatch")
    return tron_address(decoded[:21].hex())


def address_base58(value: str) -> str:
    raw = bytes.fromhex(address_hex(value))
    payload = raw + hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    number, result = int.from_bytes(payload, "big"), ""
    while number:
        number, index = divmod(number, 58)
        result = BASE58[index] + result
    return result


def parse_raw(raw: bytes, *, max_bytes=MAX_BYTES) -> dict:
    if max_bytes not in (MAX_BYTES, 8*MAX_BYTES):
        raise MachineError("unsupported response size bound")
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= max_bytes:
        raise MachineError("source body exceeds bounded response size")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise MachineError("duplicate JSON source key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise MachineError("nonfinite JSON source number")

    try:
        result = json.loads(raw.decode("utf-8-sig"), parse_float=str,
                            parse_constant=invalid_constant, object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise MachineError("source body is not bounded valid JSON") from exc
    if not isinstance(result, dict):
        raise MachineError("source body must be an object")
    return result


def source_url(source_id: str, scope: dict | None) -> str:
    if not isinstance(source_id, str) or source_id not in SOURCES:
        raise MachineError("unknown read source")
    _, _, url, private = SOURCES[source_id]
    if private:
        if scope is None:
            raise MachineError("wallet source requires authenticated scope")
        scope = normalize_scope(scope)
        if scope["network"] != "tron-mainnet":
            raise MachineError("public protocol API only serves mainnet")
        return url + "?" + urlencode({"addresses": address_base58(scope["wallet"]),
                                      "pageNo": 1, "pageSize": 1000})
    if scope is not None:
        raise MachineError("public market captures must not contain private scope")
    return url


def make_capture(source_id: str, raw: bytes | None, *, received_at: str,
                 scope: dict | None = None, mode: str = "LIVE_READ", error: str | None = None) -> dict:
    url = source_url(source_id, scope)
    if not isinstance(mode, str) or mode not in {"LIVE_READ", "FIXTURE"}:
        raise MachineError("unsupported capture mode")
    if (raw is None) != (error is not None):
        raise MachineError("capture requires either body or error")
    if error is not None and (not isinstance(error, str) or error not in {"HTTP_ERROR", "TRANSPORT_ERROR", "NOT_COLLECTED", "INVALID_RESPONSE"}):
        raise MachineError("invalid capture error code")
    if raw is not None:
        parse_raw(raw)
    provider, version, _, _ = SOURCES[source_id]
    capture = {"schema_version": VERSION, "source_id": source_id, "provider_group": provider,
               "api_version": version, "network": "tron-mainnet", "url": url,
               "scope": normalize_scope(scope) if scope is not None else None,
               "mode": mode, "received_at": utc(received_at), "observed_at": None,
               "source_time_status": "UNKNOWN", "block": None, "error": error,
               "raw_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
               "raw_text": raw.decode("utf-8-sig") if raw is not None else None}
    # Decode/re-encode excludes a UTF-8 BOM so preserve the exact bytes separately.
    if raw is not None and raw.startswith(b"\xef\xbb\xbf"):
        capture["raw_text"] = "\ufeff" + capture["raw_text"]
    capture["capture_hash"] = digest(capture)
    return capture


def validate_capture(raw: dict) -> dict | None:
    keys = {"schema_version", "source_id", "provider_group", "api_version", "network", "url",
            "scope", "mode", "received_at", "observed_at", "source_time_status", "block",
            "error", "raw_sha256", "raw_text", "capture_hash"}
    require_keys(raw, keys, "TRON source capture")
    text = raw["raw_text"]
    if text is not None and not isinstance(text, str):
        raise MachineError("source raw text must be text or null")
    body = text.encode("utf-8") if text is not None else None
    expected = make_capture(raw["source_id"], body, received_at=raw["received_at"],
                            scope=raw["scope"], mode=raw["mode"], error=raw["error"])
    if raw != expected:
        raise MachineError("capture metadata/raw commitment mismatch")
    return parse_raw(body) if body is not None else None


def capture_from_store(store, source_id: str) -> dict:
    """Replay normalized rows, not just their freely editable JSON pointers."""
    row = store.latest(source_id)
    if row is None:
        raise MachineError("no stored source snapshot")
    if row["source_url"] != source_url(source_id, None):
        raise MachineError("stored source endpoint mismatch")
    from pathlib import Path
    body = Path(row["raw_path"]).read_bytes()
    if hashlib.sha256(body).hexdigest() != row["sha256"]:
        raise MachineError("stored raw hash mismatch")
    payload = parse_raw(body)
    kinds = {"justlend_usdd_rewards_v1": "justlend_rewards_v1"}
    kind = kinds.get(source_id, source_id)
    _, expected = normalize(kind, payload)
    actual = [{key: value for key, value in item.items() if key != "snapshot_id"}
              for item in store.facts_for(row["id"])]
    key = lambda item: (item["subject_id"], item["metric_id"])
    if sorted(actual, key=key) != sorted(expected, key=key):
        raise MachineError("stored metric semantics differ from raw replay")
    return make_capture(source_id, body, received_at=row["fetched_at"])


def fetch_capture(source_id: str, store, *, scope: dict | None = None) -> dict:
    """One bounded GET; no retries or background collection is started."""
    url = source_url(source_id, scope)
    store.claim_request(datetime.now(timezone.utc).date().isoformat(), 1000)
    body, error = None, None
    try:
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "GWDC-Snapshot/1"})
        with build_opener(_NoRedirect()).open(request, timeout=12) as response:
            if response.status != 200:
                raise MachineError("HTTP source failure")
            body = response.read(MAX_BYTES + 1)
        parse_raw(body)
    except MachineError:
        body, error = None, "INVALID_RESPONSE"
    except Exception:
        body, error = None, "TRANSPORT_ERROR"
    return make_capture(source_id, body, received_at=datetime.now(timezone.utc).isoformat(),
                        scope=scope, error=error)
