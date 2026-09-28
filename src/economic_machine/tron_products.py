"""Products bind symbols/decimals to the official directory and explicit actions."""

from .capabilities import capability_hash, normalize_capability, product_id
from .mandate import integer
from .tron_sources import address_hex, validate_capture
from .values import MachineError, digest, require_keys


def registry_config(raw: dict) -> dict:
    require_keys(raw, {"schema_version", "network", "justlend_directory_source", "market_api_version",
                       "jtoken_symbols", "usdd_deployment_source", "usdd_joins", "usdd_contracts",
                       "max_age_seconds", "max_registry_age_seconds", "max_source_skew_seconds",
                       "execution_enabled"}, "TRON product registry")
    if (raw["schema_version"] != "economic-tron-product-registry-1" or raw["network"] != "tron-mainnet"
            or raw["justlend_directory_source"] != "justlend_contracts" or raw["market_api_version"] != "v1"
            or raw["execution_enabled"] is not False):
        raise MachineError("unsupported product registry policy")
    if raw["jtoken_symbols"] != ["jUSDT", "jUSDD", "jUSDDOLD"]:
        raise MachineError("explicit active/legacy comparison set required")
    if raw["usdd_deployment_source"] != "https://docs.usdd.io/developers/deployment-addresses":
        raise MachineError("unexpected deployment reference")
    require_keys(raw["usdd_joins"], {"TRX-A", "TRX-B", "TRX-C", "USDT-A"}, "USDD joins")
    require_keys(raw["usdd_contracts"], {"manager", "vat", "proxy_registry"}, "USDD core")
    for key in ("max_age_seconds", "max_registry_age_seconds", "max_source_skew_seconds"):
        integer(raw[key], key, 1, 86400)
    for key in ("usdd_joins", "usdd_contracts"):
        if not isinstance(raw[key], dict) or not raw[key]:
            raise MachineError("USDD address mapping required")
        for address in raw[key].values():
            address_hex(address)
    return raw


def _directory_address(record: dict, network: str) -> str:
    if record.get("network") != network:
        raise MachineError("contract directory network mismatch")
    address = record["address"]
    value = address_hex(address["base58"])
    if value != address_hex(address["hex_tron"]) or value[2:] != address["hex_evm"].removeprefix("0x").lower():
        raise MachineError("contract directory address representations disagree")
    return value


def _product(name: str, contract: str | None, asset: str, token: str | None, decimals: int,
             protocol: str, version: str, action: str, evidence: str, **extra) -> dict:
    capability = normalize_capability({"schema_version": "economic-product-capability-1",
        "chain": "TRON", "network": "tron-mainnet", "contract": contract,
        "protocol": protocol, "protocol_version": version, "action": action,
        "token": {"asset": asset, "address": token, "decimals": decimals},
        "stages": {stage: {"status": "UNKNOWN" if stage == "read" else "UNSUPPORTED",
                           "evidence_hash": evidence if stage == "read" else None}
                   for stage in ("read", "quote", "simulate", "execute", "reconcile")}})
    return {"product_id": name, "identity_hash": product_id(capability),
            "capability_hash": capability_hash(capability), "capability": capability, **extra}


def discover_products(config: dict, directory: dict) -> dict[str, dict]:
    config = registry_config(config)
    if directory["source_id"] != "justlend_contracts":
        raise MachineError("official contract directory capture required")
    payload = validate_capture(directory)
    if payload is None:
        raise MachineError("contract directory unavailable")
    try:
        root = payload["networks"]["mainnet"]
        entries = root["jtokens"]
        products = {}
        for symbol in config["jtoken_symbols"]:
            row = entries[symbol]
            status = row["status"]
            if status not in {"active", "legacy"} or row["symbol"] != symbol:
                raise MachineError("invalid market status/symbol")
            expected_asset = {"jUSDT": "USDT", "jUSDD": "USDD", "jUSDDOLD": "USDDOLD"}[symbol]
            if row["underlying_symbol"] != expected_asset:
                raise MachineError("directory underlying symbol mismatch")
            contract = _directory_address(row["delegator"], "mainnet")
            token = _directory_address(row["underlying"], "mainnet")
            decimals = integer(row["underlying_decimals"], "underlying decimals", 0, 36)
            shares = integer(row["decimals"], "share decimals", 0, 36)
            if shares > 18 + decimals:
                raise MachineError("unsupported negative exchange rate scale")
            name = "justlend.v1." + symbol
            products[name] = _product(name, contract, expected_asset, token, decimals, "justlend", "v1",
                "SUPPLY", directory["raw_sha256"], share_decimals=shares,
                market_status=status, new_supply_allowed=status == "active")
        strx = _directory_address(root["strx"]["staked_trx"], "mainnet")
        strx_market = entries["jsTRX"]
        if _directory_address(strx_market["underlying"], "mainnet") != strx:
            raise MachineError("sTRX staking token and lending underlying disagree")
        strx_decimals = integer(strx_market["underlying_decimals"], "sTRX decimals", 0, 36)
        name = "justlend.strx"
        products[name] = _product(name, strx, "sTRX", strx, strx_decimals,
            "justlend", "strx-v1", "STAKE", directory["raw_sha256"],
            market_status="active", new_supply_allowed=True, underlying_asset="TRX", underlying_decimals=6)
        name = "tron.native.stake"
        products[name] = _product(name, None, "TRX", None, 6, "tron-native", "stake-v2", "STAKE",
            directory["raw_sha256"], market_status="active", new_supply_allowed=True)
    except (KeyError, TypeError, AttributeError) as exc:
        raise MachineError("contract directory schema incomplete") from exc
    contracts = [p["capability"]["contract"] for p in products.values() if p["capability"]["contract"]]
    if len(contracts) != len(set(contracts)):
        raise MachineError("directory product contracts must be distinct")
    # Deployment address bindings are versioned configuration; RPC/code checks
    # remain separate. Unknown collateral families are not silently added.
    for ilk, contract in config["usdd_joins"].items():
        name = "usdd.vault." + ilk
        usdd = products["justlend.v1.jUSDD"]["capability"]["token"]
        products[name] = _product(name, address_hex(contract), "USDD", usdd["address"], usdd["decimals"],
            "usdd", "vault-v2", "MINT_USDD", digest(config),
            market_status="configured", new_supply_allowed=False, ilk=ilk, economic_role="COLLATERAL_DEBT")
    return products
