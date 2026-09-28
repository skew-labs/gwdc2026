"""Server-owned, bounded TRON read port and replayable request/response transcript.

No private keys, transaction creation, customer RPC URLs or broadcast methods.
Solid-state reads are bracketed, not historical RPC reads or cryptographic proofs.
Full-node resource/fee observations retain an unknown observation block.
"""

import hashlib
import json
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from .mandate import integer, normalize_scope
from .tron_products import registry_config
from .tron_registry_read import _NoRedirect, _block, _word
from .tron_constant import read_constant
from .tron_multicall import read_public_batch
from .tron_sources import MAX_BYTES, address_hex, parse_raw
from .values import MachineError, digest, require_keys, utc


VERSION = "economic-tron-rpc-capture-2"
BASE_URL = "https://api.trongrid.io"
PATHS = {"/walletsolidity/getnowblock", "/walletsolidity/triggerconstantcontract",
         "/walletsolidity/getaccount", "/wallet/getaccountresource", "/wallet/getchainparameters",
         "/walletsolidity/getblockbynum"}


def http_reader(store):
    last_request = 0.0
    def request(path, payload):
        nonlocal last_request
        if path not in PATHS:
            raise MachineError("RPC method not in read allowlist")
        req = Request(BASE_URL + path, data=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Content-Type": "application/json", "User-Agent": "GWDC-Snapshot/1"})
        for attempt in range(2):
            time.sleep(max(0, 0.35 - (time.monotonic() - last_request)))
            store.claim_request(datetime.now(timezone.utc).date().isoformat(), 1000)
            last_request = time.monotonic()
            try:
                with build_opener(_NoRedirect()).open(req, timeout=12) as response:
                    if response.status != 200:
                        raise MachineError("RPC HTTP failure")
                    raw = response.read(MAX_BYTES + 1)
                parse_raw(raw)
                return raw
            except HTTPError as exc:
                if attempt or exc.code not in {429, 502, 503, 504}:
                    raise
                # Bounded retry for transient transport failures only. No VM retry.
                time.sleep(1)
    return request


def _address_word(value):
    return _word(int(address_hex(value)[2:], 16))


def _from_word(value, *, nullable=False):
    if not 0 <= value < 1 << 160 or (value == 0 and not nullable):
        raise MachineError("noncanonical/empty ABI address")
    return None if value == 0 else "41" + f"{value:040x}"


def _scaled(value, places):
    # Integer formatting avoids Decimal's ambient 28-digit rounding context.
    text = str(value).zfill(places + 1)
    if places == 0:
        return text
    return (text[:-places] + "." + text[-places:]).rstrip("0").rstrip(".") or "0"


def _read(config, products, scope, vault_ids, request, *, atomic_public=False, wait=None):
    """The request sequence is code-defined and replayed from raw bytes."""
    observations, unavailable = {}, []
    # POST avoids a cached GET head being older than the constant-call state.
    before = _block(request("/walletsolidity/getnowblock", {}))
    # A public constant call still requires an owner; no wallet secret is used.
    caller = scope["wallet"] if scope else products["justlend.v1.jUSDT"]["capability"]["contract"]
    batch, coherent_block = read_public_batch(request, products, caller) if atomic_public else ({}, None)

    def call(contract, selector, parameter="", words=1):
        if atomic_public:
            if parameter or words != 1 or (contract, selector) not in batch:
                raise MachineError("getter not present in atomic public batch")
            return [batch[(contract, selector)]]
        return read_constant(request, contract, caller, selector, parameter, words)

    def fact(path, value, unit, *, solid=True):
        observations[path] = {"value": value, "unit": unit, "solid_view": solid}

    for name, product in products.items():
        if not name.startswith("justlend.v1."):
            continue
        cap, shares = product["capability"], product["share_decimals"]
        contract, decimals = cap["contract"], cap["token"]["decimals"]
        for selector, metric, scale, unit in (
            ("getCash()", "available_cash", decimals, cap["token"]["asset"]),
            ("exchangeRateStored()", "exchange_rate", 18 + decimals - shares, "underlying_per_share"),
        ):
            fact(name + "." + metric, _scaled(call(contract, selector)[0], scale), unit)
        if scope:
            parameter = _address_word(scope["wallet"])
            fact("wallet." + name + ".shares", _scaled(call(contract, "balanceOf(address)", parameter)[0], shares), "jtoken")
            fact("wallet." + name + ".borrow_stored", _scaled(call(contract, "borrowBalanceStored(address)", parameter)[0], decimals), cap["token"]["asset"])
    strx = products["justlend.strx"]["capability"]["contract"]
    fact("justlend.strx.exchange_rate", _scaled(call(strx, "exchangeRate()")[0], 18), "TRX_per_sTRX")
    fact("justlend.strx.total_underlying", _scaled(call(strx, "totalUnderlying()")[0], 6), "TRX")
    if scope:
        parameter = _address_word(scope["wallet"])
        fact("wallet.justlend.strx.balance", _scaled(call(strx, "balanceOf(address)", parameter)[0], 18), "sTRX")
        account = request("/walletsolidity/getaccount", {"address": scope["wallet"], "visible": False})
        if not account or address_hex(account.get("address")) != scope["wallet"]:
            raise MachineError("account absent or wallet mismatch; absence is not zero")
        # Protobuf scalar defaults are only valid inside an identified account.
        fact("wallet.TRX.balance", _scaled(integer(account.get("balance", 0), "sun balance", 0, (1 << 63)-1), 6), "TRX")
        for field, metric in (("delegated_frozenV2_balance_for_bandwidth", "bandwidth_delegated"),
                              ("acquired_delegated_frozenV2_balance_for_bandwidth", "bandwidth_received")):
            fact("wallet.native_stake." + metric, _scaled(integer(account.get(field, 0), field, 0, (1 << 63)-1), 6), "TRX")
        energy = account.get("account_resource", {})
        if not isinstance(energy, dict):
            raise MachineError("invalid account_resource")
        for field, metric in (("delegated_frozenV2_balance_for_energy", "energy_delegated"),
                              ("acquired_delegated_frozenV2_balance_for_energy", "energy_received")):
            fact("wallet.native_stake." + metric, _scaled(integer(energy.get(field, 0), field, 0, (1 << 63)-1), 6), "TRX")
        seen = set()
        for entry in account.get("frozenV2", []):
            kind = entry.get("type", "BANDWIDTH")
            if kind not in {"BANDWIDTH", "ENERGY", "TRON_POWER"} or kind in seen:
                raise MachineError("invalid/duplicate native stake resource")
            seen.add(kind)
            fact("wallet.native_stake." + kind, _scaled(integer(entry.get("amount", 0), "stake sun", 0, (1 << 63)-1), 6), "TRX")
        for kind in {"BANDWIDTH", "ENERGY", "TRON_POWER"} - seen:
            fact("wallet.native_stake." + kind, "0", "TRX")
        unfreezes = account.get("unfrozenV2", [])
        if not isinstance(unfreezes, list) or len(unfreezes) > 32:
            raise MachineError("unbounded native unstake schedule")
        for index, entry in enumerate(unfreezes):
            kind = entry.get("type", "BANDWIDTH")
            if kind not in {"BANDWIDTH", "ENERGY", "TRON_POWER"}:
                raise MachineError("invalid unstake resource")
            path = f"wallet.native_unstake.{index}.{kind}"
            fact(path + ".amount", _scaled(integer(entry.get("unfreeze_amount"), "unstake sun", 0, (1 << 63)-1), 6), "TRX")
            fact(path + ".expires_ms", str(integer(entry.get("unfreeze_expire_time"), "unlock time", 0, (1 << 63)-1)), "epoch_ms")
        resource = request("/wallet/getaccountresource", {"address": scope["wallet"], "visible": False})
        if not resource:
            unavailable.append("RESOURCE_RESPONSE_EMPTY")
        else:
            for field in ("EnergyLimit", "EnergyUsed", "NetLimit", "NetUsed", "freeNetLimit", "freeNetUsed"):
                fact("wallet.resource." + field, str(integer(resource.get(field, 0), field, 0, (1 << 63)-1)),
                     "energy" if field.startswith("Energy") else "bytes", solid=False)
    parameters = request("/wallet/getchainparameters", None).get("chainParameter")
    if not isinstance(parameters, list):
        raise MachineError("chain parameters missing")
    fees = {}
    for entry in parameters:
        key = entry.get("key")
        if not isinstance(key, str) or key in fees:
            raise MachineError("duplicate/invalid chain parameter")
        fees[key] = integer(entry.get("value", 0), "chain parameter", -(1 << 63), (1 << 63)-1)
    for key, unit in (("getEnergyFee", "sun_per_energy"), ("getTransactionFee", "sun_per_byte"),
                      ("getUnfreezeDelayDays", "days")):
        if key in fees and fees[key] < 0:
            raise MachineError("negative fee/unfreeze parameter")
        fact("tron.chain." + key, str(fees[key]) if key in fees else None, unit, solid=False)

    ownership = []
    if vault_ids:
        contracts = config["usdd_contracts"]
        manager, vat, registry = (address_hex(contracts[key]) for key in ("manager", "vat", "proxy_registry"))
        if _from_word(call(manager, "vat()")[0]) != vat:
            raise MachineError("USDD manager/Vat binding mismatch")
        proxy = _from_word(call(registry, "proxies(address)", _address_word(scope["wallet"]))[0], nullable=True)
        if proxy and _from_word(call(proxy, "owner()")[0]) != scope["wallet"]:
            raise MachineError("USDD proxy owner mismatch")
        for vault_id in vault_ids:
            parameter = _word(vault_id)
            owner = _from_word(call(manager, "owns(uint256)", parameter)[0])
            if owner not in {scope["wallet"], proxy}:
                raise MachineError("USDD position belongs to another wallet/proxy")
            urn = _from_word(call(manager, "urns(uint256)", parameter)[0])
            ilk_word = call(manager, "ilks(uint256)", parameter)[0]
            encoded = ilk_word.to_bytes(32, "big")
            try:
                ilk = encoded.rstrip(b"\0").decode("ascii")
            except UnicodeError as exc:
                raise MachineError("invalid USDD collateral type") from exc
            if ilk not in config["usdd_joins"] or encoded != ilk.encode().ljust(32, b"\0"):
                raise MachineError("unsupported USDD collateral type")
            ink, art = call(vat, "urns(bytes32,address)", _word(ilk_word) + _address_word(urn), 2)
            total_art, rate, spot, line, dust = call(vat, "ilks(bytes32)", _word(ilk_word), 5)
            if rate == 0:
                raise MachineError("uninitialized USDD rate")
            path = "wallet.usdd.vault." + str(vault_id)
            for metric, value, places, unit in (("collateral", ink, 18, ilk.split("-")[0]),
                ("normalized_debt", art, 18, "USDD"), ("stored_accrued_debt", art * rate, 45, "USDD"),
                ("stored_rate", rate, 27, "multiple"), ("liquidation_adjusted_price", spot, 27, "USDD_per_collateral"),
                ("type_debt_ceiling", line, 45, "USDD"), ("minimum_debt", dust, 45, "USDD")):
                fact(path + "." + metric, _scaled(value, places), unit)
            ownership.append({"vault_id": vault_id, "owner": owner, "urn": urn, "ilk": ilk,
                              "wallet": scope["wallet"], "proxy": proxy})
        unavailable.extend(["VAULT_ENUMERATION_PARTIAL", "VAULT_DEBT_SINCE_LAST_RATE_UPDATE_UNKNOWN"])
    elif scope:
        unavailable.append("VAULT_IDS_NOT_PROVIDED")
    after = _block(request("/walletsolidity/getnowblock", {}))
    # Public RPC backends can report a solid head slightly behind the backend
    # serving the atomic call. Preserve the call and wait for confirmation;
    # never relabel its block or re-read values into the same commitment.
    for _ in range(6):
        if not coherent_block or (coherent_block["number"] <= after["number"]
                                  and coherent_block["timestamp_ms"] <= after["timestamp_ms"]):
            break
        if wait is not None:
            wait(1)
        after = _block(request("/walletsolidity/getnowblock", {}))
    if coherent_block and (coherent_block["number"] > after["number"] or coherent_block["timestamp_ms"] > after["timestamp_ms"]):
        raise MachineError("atomic read is newer than solidified head")
    return {"block_before": before, "block_after": after, "observations": observations,
            "coherent_block": coherent_block, "vault_ownership": ownership, "unavailable": sorted(unavailable)}


def capture_rpc(config, products, *, scope=None, vault_ids=(), mode="LIVE_READ", transport=None, store=None, atomic_public=True):
    config = registry_config(config)
    scope = normalize_scope(scope) if scope is not None else None
    if scope is not None and scope["network"] != "tron-mainnet":
        raise MachineError("TRON RPC network mismatch")
    if mode not in {"LIVE_READ", "FIXTURE"} or len(vault_ids) > 8 or len(set(vault_ids)) != len(vault_ids):
        raise MachineError("invalid RPC mode/vault budget")
    ids = sorted(integer(x, "vault id", 1, (1 << 64)-1) for x in vault_ids)
    if ids and scope is None:
        raise MachineError("vault reads require wallet scope")
    if type(atomic_public) is not bool:
        raise MachineError("atomic public flag must be boolean")
    strategy = "ATOMIC_PUBLIC" if scope is None and atomic_public else "SEQUENTIAL_SCOPED"
    records = []
    reader = transport or http_reader(store)

    pending_path = None
    def request(path, payload):
        nonlocal pending_path
        pending_path = path
        raw = reader(path, payload)
        parsed = parse_raw(raw)
        records.append({"path": path, "payload": payload, "raw_text": raw.decode("utf-8"),
                        "raw_sha256": hashlib.sha256(raw).hexdigest()})
        return parsed
    error, failure = None, None
    try:
        _read(config, products, scope, ids, request, atomic_public=strategy == "ATOMIC_PUBLIC",
              wait=time.sleep if transport is None else None)
    except Exception as exc:
        # An interrupted batch is deliberately not projected as partial balances.
        error = "INCOMPLETE_RPC_READ"
        failure = {"record_index": len(records), "path": pending_path,
                   "error_type": "HTTP_" + str(exc.code) if isinstance(exc, HTTPError) else type(exc).__name__}
    capture = {"schema_version": VERSION, "url": BASE_URL, "provider_group": "trongrid",
               "network": "tron-mainnet", "scope": scope, "vault_ids": ids, "mode": mode, "strategy": strategy,
               "received_at": datetime.now(timezone.utc).isoformat(), "records": records,
               "error": error, "failure": failure, "registry_hash": digest(config), "products_hash": digest(products)}
    capture["capture_hash"] = digest(capture)
    return capture


def replay_rpc(capture, config, products):
    require_keys(capture, {"schema_version", "url", "provider_group", "network", "scope", "vault_ids", "mode", "strategy",
                          "received_at", "records", "error", "failure", "registry_hash", "products_hash", "capture_hash"}, "RPC capture")
    body = {k: v for k, v in capture.items() if k != "capture_hash"}
    if (digest(body) != capture["capture_hash"] or capture["schema_version"] != VERSION
        or capture["url"] != BASE_URL or capture["provider_group"] != "trongrid" or capture["network"] != "tron-mainnet"
        or capture["mode"] not in {"LIVE_READ", "FIXTURE"} or capture["registry_hash"] != digest(registry_config(config))
        or capture["products_hash"] != digest(products)):
        raise MachineError("RPC capture commitment/config mismatch")
    utc(capture["received_at"])
    scope = normalize_scope(capture["scope"]) if capture["scope"] is not None else None
    if scope and scope["network"] != "tron-mainnet":
        raise MachineError("RPC scope network mismatch")
    if capture["strategy"] not in {"ATOMIC_PUBLIC", "SEQUENTIAL_SCOPED"} or (scope and capture["strategy"] == "ATOMIC_PUBLIC"):
        raise MachineError("RPC strategy/scope mismatch")
    ids = capture["vault_ids"]
    if (not isinstance(ids, list) or len(ids) > 8 or any(type(x) is not int or not 0 < x < 1 << 64 for x in ids)
        or ids != sorted(set(ids)) or (ids and scope is None)):
        raise MachineError("invalid replay vault scope")
    records = capture["records"]
    if not isinstance(records, list) or len(records) > 100:
        raise MachineError("unbounded RPC transcript")
    for record in records:
        require_keys(record, {"path", "payload", "raw_text", "raw_sha256"}, "RPC record")
        if not isinstance(record["raw_text"], str) or record["path"] not in PATHS:
            raise MachineError("invalid RPC record")
        raw = record["raw_text"].encode()
        if hashlib.sha256(raw).hexdigest() != record["raw_sha256"]:
            raise MachineError("RPC raw hash mismatch")
        parse_raw(raw)
    if capture["error"] == "INCOMPLETE_RPC_READ":
        require_keys(capture["failure"], {"record_index", "path", "error_type"}, "RPC failure")
        if (capture["failure"]["record_index"] != len(records) or capture["failure"]["path"] not in PATHS
            or not isinstance(capture["failure"]["error_type"], str)):
            raise MachineError("invalid RPC failure boundary")
        return None
    if capture["error"] is not None or capture["failure"] is not None:
        raise MachineError("unsupported RPC error")
    cursor = 0

    def request(path, payload):
        nonlocal cursor
        if cursor >= len(records):
            raise MachineError("truncated RPC transcript")
        record = records[cursor]
        cursor += 1
        if record["path"] != path or record["payload"] != payload:
            raise MachineError("RPC transcript request/scope mismatch")
        return parse_raw(record["raw_text"].encode())
    result = _read(config, products, scope, ids, request, atomic_public=capture["strategy"] == "ATOMIC_PUBLIC")
    if cursor != len(records):
        raise MachineError("RPC transcript has trailing requests")
    return result
