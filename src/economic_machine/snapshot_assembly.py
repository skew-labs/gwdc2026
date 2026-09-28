"""Replay source observations into product facts without inventing execution truth."""

from copy import deepcopy
from datetime import datetime, timezone

from .mandate import normalize_scope
from .state import normalize_state, state_root
from .tron_products import discover_products, registry_config
from .tron_rpc_snapshot import replay_rpc
from .tron_sources import SOURCES, address_hex, make_capture, validate_capture
from .values import MachineError, decimal, decstr, digest, utc


VERSION = "economic-tron-snapshot-1"
PUBLIC = ("justlend_contracts", "justlend_markets_v1", "justlend_usdd_rewards_v1",
          "justlend_strx_v1", "usdd_tron_collateral", "usdd_vault_config", "usdd_earn_apy", "tron_chain_parameters")
PRIVATE = ("justlend_account_v1", "justlend_strx_account_v1", "justlend_rental_account_v1")
MARKET_FIELDS = (("supplyRate", "supply_apy", "annual_fraction"), ("borrowRate", "borrow_apy", "annual_fraction"),
                 ("exchangeRate", "exchange_rate", "underlying_per_share"), ("cash", "available_cash", "ASSET"),
                 ("totalBorrows", "total_borrows", "ASSET"), ("totalSupply", "total_shares", "jtoken"))
VAULT_FIELDS = (("debt", "type_debt", "USDD"), ("mintedUSDD", "type_minted", "USDD"),
                ("lockedValue", "type_collateral_value", "USD"), ("line", "type_debt_ceiling", "USDD"),
                ("stabilityFee", "stability_fee", "annual_fraction"),
                ("minCollateralRatio", "liquidation_ratio", "multiple"),
                ("collateralRatio", "type_collateral_ratio", "multiple"))


def _time(value):
    return datetime.fromisoformat(utc(value))


def _data(payload):
    if type(payload.get("code")) is not int or payload["code"] != 0 or not isinstance(payload.get("data"), dict):
        raise MachineError("V1 source business error or schema mismatch")
    return payload["data"]


def _list(data, key):
    rows = data.get(key)
    if not isinstance(rows, list) or len(rows) > 1000 or any(not isinstance(x, dict) for x in rows):
        raise MachineError("bounded source rows missing")
    return rows


def _number(value):
    if type(value) is int:
        value = str(value)
    return decstr(decimal(value))


class SnapshotAssembler:
    def __init__(self, config):
        self.config = deepcopy(registry_config(config))

    def assemble(self, captures, *, as_of, scope=None, rpc=None):
        as_of = utc(as_of)
        scope = normalize_scope(scope) if scope is not None else None
        if scope and scope["network"] != "tron-mainnet":
            raise MachineError("snapshot only supports mainnet")
        if not isinstance(captures, list) or len(captures) > len(SOURCES):
            raise MachineError("bounded capture list required")
        by_source = {}
        modes = set()
        for capture in captures:
            validate_capture(capture)
            source = capture["source_id"]
            if source in by_source:
                raise MachineError("duplicate source capture")
            if source in PRIVATE and (scope is None or capture["scope"] != scope):
                raise MachineError("wallet capture scope differs from authenticated scope")
            if source == "justlend_markets_v2":
                raise MachineError("V2 markets require a separate identity/parser; no V1 fallback")
            by_source[source] = capture
            modes.add(capture["mode"])
        if rpc:
            modes.add(rpc["mode"])
            if rpc["scope"] != scope:
                raise MachineError("RPC scope mismatch")
        if len(modes) != 1:
            raise MachineError("cannot mix live observations and fixtures")
        mode = next(iter(modes))
        for source in PUBLIC + (PRIVATE if scope else ()):
            if source not in by_source:
                by_source[source] = make_capture(source, None, received_at=as_of, mode=mode,
                    scope=scope if source in PRIVATE else None, error="NOT_COLLECTED")
        products = discover_products(self.config, by_source["justlend_contracts"])
        facts, statuses, unavailable = {}, {}, []
        registry_status = self._freshness(by_source["justlend_contracts"], as_of, registry=True)

        def add(capture, path, row, field, unit, pointer, *, role="MARKET"):
            value = row.get(field)
            quality = "ERROR" if capture["error"] and capture["error"] != "NOT_COLLECTED" else "MISSING" if value is None else "VALID"
            value = _number(value) if quality == "VALID" else None
            if value == "0":
                quality = "VALID_ZERO"
            if path in facts:
                raise MachineError("duplicate semantic field")
            facts[path] = {"value": value, "quality": quality, "unit": unit, "economic_role": role,
                "source_id": capture["source_id"], "provider_group": capture["provider_group"],
                "raw_sha256": capture["raw_sha256"], "capture_hash": capture["capture_hash"],
                "json_pointer": pointer + "/" + field, "received_at": capture["received_at"],
                "observed_at": None, "block": None, "availability": statuses[capture["source_id"]],
                "state_eligible": False, "withheld_reasons": ["SOURCE_TIME_UNKNOWN", "SOURCE_BLOCK_UNKNOWN"]}

        # Unavailable APIs are represented as explicit missing/error fields,
        # while identity/semantic violations reject the entire assembly.
        for source, capture in sorted(by_source.items()):
            statuses[source] = self._freshness(capture, as_of, registry=source == "justlend_contracts")
            payload = validate_capture(capture)
            data = _data(payload) if payload is not None and source not in {"justlend_contracts", "tron_chain_parameters"} else {}
            if source == "justlend_markets_v1":
                rows = _list(data, "tokenList") if payload else []
                indexed = {}
                for index, row in enumerate(rows):
                    contract = address_hex(row.get("address"))
                    if contract in indexed:
                        raise MachineError("duplicate market contract")
                    indexed[contract] = (index, row)
                for name, product in products.items():
                    if not name.startswith("justlend.v1."):
                        continue
                    cap = product["capability"]
                    index, row = indexed.get(cap["contract"], (None, {}))
                    if row and (row.get("symbol") != name.split(".")[-1]
                        or row.get("underlyingSymbol") != cap["token"]["asset"]
                        or address_hex(row.get("underlyingAddress")) != cap["token"]["address"]
                        or type(row.get("underlyingDecimal")) is not int
                        or row["underlyingDecimal"] != cap["token"]["decimals"]):
                        raise MachineError("market symbol/address/decimals differ from directory")
                    for field, metric, unit in MARKET_FIELDS:
                        add(capture, name + "." + metric, row, field,
                            cap["token"]["asset"] if unit == "ASSET" else unit,
                            f"/data/tokenList/{index}" if index is not None else "/missing-market")
            elif source == "justlend_usdd_rewards_v1":
                for name, product in products.items():
                    if not name.startswith("justlend.v1."):
                        continue
                    entries = [(key, row) for key, row in data.items() if address_hex(key) == product["capability"]["contract"]]
                    if len(entries) > 1:
                        raise MachineError("duplicate reward address representation")
                    key, row = entries[0] if entries else ("missing-market", {})
                    if not isinstance(row, dict):
                        raise MachineError("invalid reward map")
                    add(capture, name + ".reward_apy", row, "USDD", "annual_fraction", "/data/" + key)
            elif source == "justlend_strx_v1":
                row, rental = data.get("stakeInfo", {}), data.get("rentInfo", {})
                if not isinstance(row, dict) or not isinstance(rental, dict):
                    raise MachineError("invalid sTRX data")
                if row and (address_hex(row.get("strxAddress")) != products["justlend.strx"]["capability"]["contract"]
                    or row.get("decimal") != "18" or row.get("underlyingDecimal") != "6"):
                    raise MachineError("sTRX address/decimal mismatch")
                for field, metric, unit in (("exchangeRate", "exchange_rate", "TRX_per_sTRX"),
                    ("supplyRate", "aggregate_apy", "annual_fraction"), ("totalUnderlying", "total_underlying", "TRX"),
                    ("totalSupply", "total_shares", "sTRX"), ("reserves", "reserves", "TRX")):
                    add(capture, "justlend.strx." + metric, row, field, unit, "/data/stakeInfo", role="STAKING_AND_RENTAL_AGGREGATE")
                for field, metric, unit in (("priceFor10KEnergByRent", "price_per_10000_energy", "TRX_per_10000_energy"),
                    ("totalDelegatedEnergyTrx", "delegated_trx", "TRX")):
                    add(capture, "tron.energy_rental." + metric, rental, field, unit, "/data/rentInfo")
            elif source == "usdd_tron_collateral":
                rows = _list(data, "items") if payload else []
                indexed = {}
                for index, row in enumerate(rows):
                    if row.get("chain") != "tron":
                        raise MachineError("USDD chain mismatch")
                    ilk = row.get("vaultType")
                    if not isinstance(ilk, str) or ilk in indexed:
                        raise MachineError("duplicate/invalid USDD collateral type")
                    indexed[ilk] = (index, row)
                for name, product in products.items():
                    if not name.startswith("usdd.vault."):
                        continue
                    index, row = indexed.get(product["ilk"], (None, {}))
                    if row and (type(row.get("collateralType")) is not int or row["collateralType"] != 1
                                or address_hex(row.get("contractAddress")) != product["capability"]["contract"]):
                        raise MachineError("USDD join/type mismatch")
                    for field, metric, unit in VAULT_FIELDS:
                        add(capture, name + "." + metric, row, field, unit,
                            f"/data/items/{index}" if index is not None else "/missing-ilk", role="COLLATERAL_TYPE_AGGREGATE")
            elif source == "usdd_vault_config":
                rows = _list(data, "items") if payload else []
                seen = set()
                for index, row in enumerate(rows):
                    ilk = row.get("ilk")
                    if not isinstance(ilk, str) or ilk in seen:
                        raise MachineError("duplicate/invalid vault config")
                    seen.add(ilk)
                    if ilk not in self.config["usdd_joins"]:
                        continue
                    for field, metric, unit in (("dust", "minimum_debt", "USDD"), ("maxMinted", "debt_ceiling", "USDD"),
                        ("minCollateralRatio", "liquidation_ratio", "multiple"), ("stabilityFee", "stability_fee", "annual_fraction")):
                        add(capture, "usdd.config." + ilk + "." + metric, row, field, unit,
                            f"/data/items/{index}", role="COLLATERAL_TYPE_CONFIG")
                for ilk in self.config["usdd_joins"].keys() - seen:
                    for field, metric, unit in (("dust", "minimum_debt", "USDD"), ("maxMinted", "debt_ceiling", "USDD"),
                        ("minCollateralRatio", "liquidation_ratio", "multiple"), ("stabilityFee", "stability_fee", "annual_fraction")):
                        add(capture, "usdd.config." + ilk + "." + metric, {}, field, unit, "/missing-ilk")
            elif source == "usdd_earn_apy":
                add(capture, "usdd.earn.apy", data, "tronApy", "annual_fraction", "/data", role="READ_ONLY_UNSUPPORTED_ROUTE")
            elif source == "tron_chain_parameters":
                rows = _list(payload, "chainParameter") if payload else []
                indexed = {}
                for index, row in enumerate(rows):
                    key = row.get("key")
                    if not isinstance(key, str) or key in indexed:
                        raise MachineError("duplicate/invalid chain fee parameter")
                    if "value" in row and type(row["value"]) is not int:
                        raise MachineError("chain parameter must be a JSON integer")
                    indexed[key] = (index, {"value": row.get("value", 0)})
                for key, unit in (("getEnergyFee", "sun_per_energy"), ("getTransactionFee", "sun_per_byte"),
                                  ("getUnfreezeDelayDays", "days")):
                    index, row = indexed.get(key, (None, {}))
                    add(capture, "tron.chain." + key, row, "value", unit,
                        f"/chainParameter/{index}" if index is not None else "/missing-parameter")
                    if index is not None and "value" not in rows[index]:
                        facts["tron.chain." + key]["normalization"] = "PROTOBUF_SCALAR_DEFAULT_ZERO"
            elif source in PRIVATE:
                self._wallet(source, capture, data, payload, scope, products, add, unavailable)

        rpc_result = replay_rpc(rpc, self.config, products) if rpc else None
        comparisons = []
        if rpc_result:
            before, after = rpc_result["block_before"], rpc_result["block_after"]
            # _block returns canonical height/hash/timestamp_ms fields.
            observed = datetime.fromtimestamp(before["timestamp_ms"] / 1000, timezone.utc).isoformat()
            reasons = []
            if before != after:
                reasons.append("SOLID_BLOCK_CHANGED")
            if not 0 <= (_time(as_of) - _time(observed)).total_seconds() <= self.config["max_age_seconds"]:
                reasons.append("BLOCK_TIME_STALE_OR_FUTURE")
            if not 0 <= (_time(as_of) - _time(rpc["received_at"])).total_seconds() <= self.config["max_age_seconds"]:
                reasons.append("RPC_RECEIPT_STALE_OR_FUTURE")
            if registry_status != "AVAILABLE":
                reasons.append("REGISTRY_UNAVAILABLE")
            if mode == "FIXTURE":
                reasons.append("FIXTURE_ONLY")
            for path, item in rpc_result["observations"].items():
                local_reasons = reasons + ([] if item["solid_view"] else ["FULLNODE_BLOCK_UNKNOWN"])
                value = item["value"]
                fact = {"value": value, "quality": "MISSING" if value is None else "VALID_ZERO" if value == "0" else "VALID",
                    "unit": item["unit"], "economic_role": "WALLET" if path.startswith("wallet.") else "MARKET",
                    "source_id": "trongrid", "provider_group": "trongrid", "raw_sha256": None,
                    "capture_hash": rpc["capture_hash"], "json_pointer": None, "received_at": rpc["received_at"],
                    "observed_at": observed if item["solid_view"] else None,
                    "block": before if item["solid_view"] and before == after else None,
                    "availability": "AVAILABLE", "state_eligible": not local_reasons and value is not None,
                    "withheld_reasons": sorted(local_reasons)}
                if path in facts and facts[path]["value"] is not None and value is not None:
                    api = facts[path]
                    comparisons.append({"path": path, "status": "AGREE" if api["value"] == value else "DISAGREE",
                        "api_value": api["value"], "rpc_value": value, "unit": item["unit"],
                        "aligned_source_block": False, "meaning": "OBSERVATIONS_ONLY_NO_ORACLE_QUORUM"})
                    if api["unit"] != item["unit"]:
                        raise MachineError("API/RPC unit mismatch")
                    if api["value"] != value:
                        fact["state_eligible"] = False
                        fact["withheld_reasons"].append("SOURCE_DISAGREEMENT")
                    # Preserve both observations. No preference is a truth proof.
                facts["rpc." + path] = fact
            unavailable.extend(rpc_result["unavailable"])
        else:
            unavailable.append("RPC_NOT_COLLECTED" if rpc is None else "RPC_READ_INCOMPLETE")
        if scope is None:
            unavailable.append("WALLET_NOT_PROVIDED")
        received = [_time(c["received_at"]) for s, c in by_source.items() if not c["error"] and s != "justlend_contracts"]
        compatible = not received or (max(received) - min(received)).total_seconds() <= self.config["max_source_skew_seconds"]
        if not compatible:
            unavailable.append("SOURCE_RECEIPT_SKEW")
        for fact in facts.values():
            if not compatible:
                fact["state_eligible"] = False
                fact["withheld_reasons"].append("SOURCE_RECEIPT_SKEW")
        snapshot = {"schema_version": VERSION, "as_of": as_of, "network": "tron-mainnet", "scope": scope,
            "mode": mode, "registry_hash": digest(self.config), "products": products, "facts": facts,
            "source_status": statuses, "comparisons": comparisons,
            "provider_groups": sorted({c["provider_group"] for c in by_source.values() if not c["error"]}
                                      | ({"trongrid"} if rpc_result else set())),
            "oracle_quorum": None, "execution_enabled": False,
            "vault_ownership": rpc_result["vault_ownership"] if rpc_result else [],
            "unavailable": sorted(set(unavailable)), "captures": sorted(by_source.values(), key=lambda c: c["source_id"]),
            "rpc_capture": rpc}
        snapshot["snapshot_hash"] = digest(snapshot)
        return snapshot

    def _freshness(self, capture, as_of, *, registry=False):
        if capture["error"]:
            return capture["error"]
        age = (_time(as_of) - _time(capture["received_at"])).total_seconds()
        if age < 0:
            return "FUTURE_RECEIPT"
        limit = self.config["max_registry_age_seconds" if registry else "max_age_seconds"]
        return "AVAILABLE" if age <= limit else "STALE_RECEIPT"

    @staticmethod
    def _wallet(source, capture, data, payload, scope, products, add, unavailable):
        rows = _list(data, "list") if payload else []
        if payload:
            if (type(data.get("totalPage")) is not int or type(data.get("totalCount")) is not int
                or not 0 <= data["totalPage"] <= 1 or data["totalCount"] != len(rows)):
                raise MachineError("wallet API pagination incomplete")
        if source != "justlend_rental_account_v1" and len(rows) > 1:
            raise MachineError("duplicate wallet account")
        for row in rows:
            address = row.get("renter") if source == "justlend_rental_account_v1" else row.get("address")
            if address_hex(address) != scope["wallet"]:
                raise MachineError("wallet API returned another wallet")
        if not rows:
            unavailable.append(source + ":WALLET_ROW_MISSING")
        if source == "justlend_account_v1":
            tokens = _list(rows[0], "tokens") if rows else []
            indexed = {}
            for index, row in enumerate(tokens):
                contract = address_hex(row.get("address"))
                if contract in indexed:
                    raise MachineError("duplicate wallet market")
                indexed[contract] = (index, row)
            for name, product in products.items():
                if not name.startswith("justlend.v1."):
                    continue
                index, row = indexed.get(product["capability"]["contract"], (None, {}))
                asset = product["capability"]["token"]["asset"]
                if row and row.get("underlyingSymbol") != asset:
                    raise MachineError("wallet underlying symbol mismatch")
                for field, metric, unit in (("supplyBalanceUnderlying", "supply", asset),
                    ("borrowBalanceUnderlying", "borrow", asset), ("supplyBalanceJtoken", "shares", "jtoken")):
                    add(capture, "wallet." + name + "." + metric, row, field, unit,
                        f"/data/list/0/tokens/{index}" if index is not None else "/missing-wallet-market", role="WALLET")
        elif source == "justlend_strx_account_v1":
            for field, metric, unit in (("availableWithdrawAmount", "claimable", "TRX"),
                ("unstakingAmount", "unstaking", "TRX"), ("sTRXBalance", "balance", "sTRX")):
                add(capture, "wallet.justlend.strx." + metric, rows[0] if rows else {}, field, unit, "/data/list/0", role="WALLET")
            unavailable.append("STRX_PER_REQUEST_UNLOCK_TIMES_UNKNOWN")
        else:
            for index, row in enumerate(rows):
                if row.get("rentType") != "Energy":
                    raise MachineError("unsupported rental resource")
                address_hex(row.get("receiver"))
                for field, metric, unit in (("delegatedAmount", "delegated_trx", "TRX"),
                    ("rentRemainAmount", "deposit", "TRX"), ("rentAmountPerDay", "daily_cost", "TRX_per_day")):
                    add(capture, f"wallet.energy_rental.{index}." + metric, row, field, unit,
                        f"/data/list/{index}", role="WALLET_RENTAL_LIABILITY")

    def verify(self, snapshot):
        expected = self.assemble(snapshot["captures"], as_of=snapshot["as_of"], scope=snapshot["scope"], rpc=snapshot["rpc_capture"])
        if snapshot != expected:
            raise MachineError("snapshot differs from raw replay")
        return expected

    def state_delta(self, snapshot, previous, *, state_scope=None):
        snapshot = self.verify(snapshot)
        previous = normalize_state(previous)
        if snapshot["scope"] is not None and (state_scope is None or normalize_scope(state_scope) != snapshot["scope"]):
            raise MachineError("wallet state projection requires matching tenant/owner/wallet scope")
        if (previous["network"] != snapshot["network"] or (snapshot["scope"] is not None
            and previous["owner_id"] != snapshot["scope"]["owner_id"])):
            raise MachineError("snapshot/state scope mismatch")
        if _time(snapshot["as_of"]) < _time(previous["as_of"]):
            raise MachineError("snapshot cannot roll state backwards")
        updates = {}
        prefix = "tron.input."
        for path, fact in snapshot["facts"].items():
            eligible = fact["state_eligible"]
            old = previous["facts"].get(prefix + path)
            if eligible and old and _time(fact["observed_at"]) < _time(old["observed_at"]):
                eligible = False
            updates[prefix + path] = {"value": fact["value"] if eligible else None, "unit": fact["unit"],
                "quality": fact["quality"] if eligible else "ERROR" if fact["quality"] == "ERROR" else "MISSING",
                "source_id": fact["source_id"], "source_hash": fact["capture_hash"],
                # For invalidation this is the time of the invalidation event,
                # not a claimed observation time for a financial value.
                "observed_at": fact["observed_at"] if eligible else snapshot["as_of"]}
        for path, old in previous["facts"].items():
            if path.startswith(prefix) and path not in updates:
                updates[path] = {"value": None, "unit": old["unit"], "quality": "MISSING",
                    "source_id": "snapshot_assembly", "source_hash": snapshot["snapshot_hash"], "observed_at": snapshot["as_of"]}
        return {"schema_version": "econ-state-delta-1", "owner_id": previous["owner_id"], "network": previous["network"],
                "sequence": previous["sequence"] + 1, "previous_root": state_root(previous), "as_of": snapshot["as_of"],
                "evidence_hash": snapshot["snapshot_hash"], "fact_updates": updates, "quote_updates": {},
                "account_updates": {"balances": {}, "exposures": {}, "daily_losses": {}}}
