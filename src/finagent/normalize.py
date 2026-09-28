"""Source-specific normalization with field semantics and zero preservation."""

from decimal import Decimal
from typing import Any

from .contracts import ContractError, canonical_decimal, decimal_string


def _fact(subject: str, metric: str, raw: Any, unit: str, path: str,
          *, null_quality: str = "MISSING") -> dict:
    if raw is None:
        return dict(subject_id=subject, metric_id=metric, raw_value=None,
                    canonical_value=None, unit=unit, quality=null_quality, json_path=path)
    # Published APIs mix decimal strings, JSON decimals and integer zero.
    # The collector parses JSON decimals as strings; integer conversion is exact.
    stored_raw = str(raw) if isinstance(raw, int) and not isinstance(raw, bool) else raw
    value = decimal_string(stored_raw)
    return dict(subject_id=subject, metric_id=metric, raw_value=stored_raw,
                canonical_value=canonical_decimal(value), unit=unit,
                quality="VALID_ZERO" if value == 0 else "VALID", json_path=path)


def _data(payload: dict, *, v2: bool = False) -> Any:
    if not isinstance(payload, dict) or payload.get("code") != (200 if v2 else 0):
        raise ContractError("source business error or unexpected envelope")
    data = payload.get("data")
    if data is None:
        raise ContractError("source has no data")
    return data


def contract_status(payload: dict) -> dict[str, str]:
    try:
        entries = payload["networks"]["mainnet"]["jtokens"]
    except (KeyError, TypeError) as exc:
        raise ContractError("invalid JustLend contract registry") from exc
    result: dict[str, str] = {}
    for record in entries.values():
        try:
            address = record["delegator"]["address"]["base58"]
            status = record["status"]
        except (KeyError, TypeError) as exc:
            raise ContractError("invalid jToken registry entry") from exc
        if status not in {"active", "legacy"}:
            raise ContractError("unknown market status")
        result[address] = status
    return result


def normalize(kind: str, payload: dict,
              statuses: dict[str, str] | None = None) -> tuple[list[dict], list[dict]]:
    markets: list[dict] = []
    facts: list[dict] = []
    if kind == "justlend_contracts":
        contract_status(payload)
        return markets, facts
    if kind == "justlend_markets_v1":
        data = _data(payload)
        tokens = data.get("tokenList") if isinstance(data, dict) else None
        if not isinstance(tokens, list):
            raise ContractError("JustLend tokenList is missing")
        for index, token in enumerate(tokens):
            if not isinstance(token, dict):
                raise ContractError("invalid market row")
            address = token["address"]
            market = dict(market_address=address, network="tron_mainnet",
                          jtoken_symbol=token["symbol"],
                          underlying_symbol=token["underlyingSymbol"],
                          underlying_address=token["underlyingAddress"],
                          underlying_decimals=int(token["underlyingDecimal"]),
                          status=(statuses or {}).get(address, "unverified"))
            if market["underlying_decimals"] < 0 or market["underlying_decimals"] > 36:
                raise ContractError("invalid token decimals")
            markets.append(market)
            path = f"/data/tokenList/{index}"
            for field, metric, unit in (
                ("supplyRate", "supply_apy", "annual_fraction"),
                ("borrowRate", "borrow_apy", "annual_fraction"),
                ("cash", "available_cash", "underlying_tokens"),
                ("totalBorrows", "total_borrows", "underlying_tokens"),
                ("reserves", "reserves", "underlying_tokens"),
                ("totalSupply", "jtoken_supply", "jtoken_tokens"),
                ("exchangeRate", "exchange_rate", "underlying_per_jtoken"),
                ("collateralFactor", "collateral_factor", "fraction"),
                ("reserveFactor", "reserve_factor", "fraction"),
                ("underlyingPriceInTrx", "underlying_price_in_trx", "TRX_per_underlying"),
            ):
                facts.append(_fact(address, metric, token.get(field), unit, path + "/" + field))
        return markets, facts
    if kind == "justlend_rewards_v1":
        data = _data(payload)
        if not isinstance(data, dict):
            raise ContractError("invalid reward map")
        for address, rewards in data.items():
            if not isinstance(rewards, dict):
                raise ContractError("invalid reward row")
            facts.append(_fact(address, "usdd_reward_apy", rewards.get("USDD"),
                               "annual_fraction", f"/data/{address}/USDD"))
        return markets, facts
    if kind == "usdd_earn_apy":
        data = _data(payload)
        if not isinstance(data, dict):
            raise ContractError("invalid USDD APY response")
        facts.append(_fact("usdd:tron", "usdd_tron_earn_apy",
                           data.get("tronApy"), "annual_fraction", "/data/tronApy"))
        return markets, facts
    if kind == "usdd_tron_collateral":
        data = _data(payload)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ContractError("invalid USDD TRON collateral response")
        for field, metric, unit in (
            ("usddTotalSupply", "usdd_tron_total_supply", "USDD"),
            ("totalCollateralValue", "usdd_tron_collateral_value", "USD"),
            ("earnTvl", "usdd_tron_earn_tvl", "USD"),
            ("apy", "usdd_tron_collateral_apy", "annual_fraction"),
        ):
            facts.append(_fact("usdd:tron", metric, data.get(field), unit,
                               "/data/" + field))
        seen: set[str] = set()
        for index, vault in enumerate(data["items"]):
            if not isinstance(vault, dict) or vault.get("chain") != "tron":
                raise ContractError("invalid TRON vault row")
            address = vault.get("contractAddress")
            if not isinstance(address, str) or not address.startswith("T") or len(address) != 34:
                raise ContractError("invalid TRON vault address")
            if address in seen or vault.get("collateralType") not in {1, 2, 3}:
                raise ContractError("duplicate vault or unknown collateral type")
            seen.add(address)
            path = f"/data/items/{index}"
            for field, metric, unit in (
                ("mintedUSDD", "vault_minted_usdd", "USDD"),
                ("debt", "vault_debt", "USDD"),
                ("lockedValue", "vault_locked_value", "USD"),
                ("collateralRatio", "vault_collateral_ratio", "multiple"),
                ("minCollateralRatio", "vault_min_collateral_ratio", "multiple"),
                ("stabilityFee", "vault_stability_fee", "annual_fraction"),
                ("line", "vault_debt_ceiling", "USDD"),
                ("apy", "vault_apy", "annual_fraction"),
                ("estimatedAnnualEarnings", "vault_estimated_annual_earnings", "USD"),
                ("psmFee", "vault_psm_fee", "annual_fraction"),
            ):
                not_applicable = (field in {"apy", "estimatedAnnualEarnings"}
                                  and vault["collateralType"] == 1) or (
                                      field == "psmFee" and vault["collateralType"] != 2)
                facts.append(_fact(address, metric, vault.get(field), unit,
                                   path + "/" + field,
                                   null_quality="NOT_APPLICABLE" if not_applicable else "MISSING"))
        return markets, facts
    if kind == "usdd_overview":
        _data(payload)
        return markets, facts
    raise ContractError(f"unsupported source kind: {kind}")
