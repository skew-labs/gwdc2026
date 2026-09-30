"""Canonical, source-linked user conditions; confirmation is not trade authority.

Money, ratios and durations have distinct transport types. Hashes commit to
normalized conditions and account scope, never to incidental JSON formatting.
An evidence quote proves a textual reference, not the correctness of an LLM's
interpretation. The user must confirm the exact draft commitment separately.
"""

import json
import re
from datetime import datetime
from decimal import Decimal, localcontext

from .values import MachineError, canonical, decimal, decstr, digest, ident, require_keys, utc


VERSION = "economic-mandate-1"
NETWORKS = {"tron-mainnet", "tron-nile", "tron-shasta"}
SCOPE_KEYS = {"tenant_id", "owner_id", "wallet", "network"}
ACTIONS = {"HOLD", "SWAP", "SUPPLY", "REDEEM", "STAKE", "UNSTAKE", "CLAIM",
           "DELEGATE_ENERGY", "REVOKE_DELEGATION", "VOTE", "OPEN_VAULT", "ADD_COLLATERAL",
           "MINT_USDD", "BORROW", "REPAY", "WITHDRAW_COLLATERAL"}
BORROW_ACTIONS = {"OPEN_VAULT", "MINT_USDD", "BORROW"}
TERM_KEYS = {"capital", "base_asset", "risk_profile", "horizon_seconds", "immediate_cash",
             "withdrawals", "price_exposure_caps_bps", "protocol_caps_bps",
             "borrowing", "limits", "allowed_actions", "effective_at", "expires_at"}


def integer(value: object, label: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise MachineError(label + " outside integer range")
    return value


def hash32(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise MachineError(label + " must be a SHA-256 hex digest")
    return value


def tron_address(value: object) -> str:
    # Canonical external TRON hex, including network-independent 0x41 prefix.
    # Base58 conversion and ownership verification belong to the auth adapter.
    if (not isinstance(value, str) or re.fullmatch(r"41[0-9a-fA-F]{40}", value) is None
            or value[2:] == "0" * 40):
        raise MachineError("wallet/contract must be a nonzero TRON hex address")
    return value.lower()


def normalize_scope(raw: dict) -> dict:
    require_keys(raw, SCOPE_KEYS, "account scope")
    if not isinstance(raw["network"], str) or raw["network"] not in NETWORKS:
        raise MachineError("unsupported TRON network")
    return {"tenant_id": ident(raw["tenant_id"], "tenant"),
            "owner_id": ident(raw["owner_id"], "owner"),
            "wallet": tron_address(raw["wallet"]), "network": raw["network"]}


def money(raw: dict, *, asset: str | None = None) -> dict:
    require_keys(raw, {"asset", "amount"}, "money")
    symbol = ident(raw["asset"], "asset")
    if asset is not None and symbol != asset:
        raise MachineError("money asset mismatch")
    return {"asset": symbol, "amount": decstr(decimal(raw["amount"]))}


def reserve(raw: dict, asset: str) -> dict:
    if not isinstance(raw, dict):
        raise MachineError("reserve must be typed AMOUNT or BPS")
    if raw.get("kind") == "AMOUNT":
        require_keys(raw, {"kind", "asset", "amount"}, "amount reserve")
        return {"kind": "AMOUNT", **money({k: raw[k] for k in ("asset", "amount")}, asset=asset)}
    if raw.get("kind") == "BPS":
        require_keys(raw, {"kind", "value"}, "ratio reserve")
        return {"kind": "BPS", "value": integer(raw["value"], "reserve bps", 0, 10000)}
    raise MachineError("reserve must be typed AMOUNT or BPS")


def reserve_amount(value: dict, capital: str) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 256
        if value["kind"] == "BPS":
            return decimal(capital) * Decimal(value["value"]) / 10000
        return decimal(value["amount"])


def _caps(raw: dict, label: str) -> dict:
    if not isinstance(raw, dict) or not 1 <= len(raw) <= 32:
        raise MachineError(label + " requires explicit bounded caps")
    return {ident(k, label): integer(v, label, 0, 10000) for k, v in sorted(raw.items())}


def normalize_terms(raw: dict) -> dict:
    require_keys(raw, TERM_KEYS, "mandate terms")
    asset = ident(raw["base_asset"], "base asset")
    if not isinstance(raw["risk_profile"], str) or raw["risk_profile"] not in {"cautious", "balanced", "growth"}:
        raise MachineError("unknown risk profile")
    capital = raw["capital"]
    if not isinstance(capital, list) or not 1 <= len(capital) <= 16:
        raise MachineError("capital must be a bounded money list")
    capital = sorted((money(item) for item in capital), key=lambda item: item["asset"])
    if (len({item["asset"] for item in capital}) != len(capital)
            or any(decimal(item["amount"]) <= 0 for item in capital)):
        raise MachineError("capital assets must be unique and amounts positive")
    horizon = integer(raw["horizon_seconds"], "horizon seconds", 1, 365 * 86400)
    immediate = reserve(raw["immediate_cash"], asset)
    withdrawals = raw["withdrawals"]
    if not isinstance(withdrawals, list) or len(withdrawals) > 32:
        raise MachineError("withdrawal schedule must be bounded")
    schedule = []
    for item in withdrawals:
        require_keys(item, {"after_seconds", "minimum"}, "withdrawal requirement")
        schedule.append({"after_seconds": integer(item["after_seconds"], "withdrawal seconds", 1, horizon),
                         "minimum": reserve(item["minimum"], asset)})
    schedule.sort(key=lambda item: item["after_seconds"])
    if len({item["after_seconds"] for item in schedule}) != len(schedule):
        raise MachineError("duplicate withdrawal deadline")
    borrowing = require_keys(raw["borrowing"], {"consent", "max_debt",
                              "min_collateral_ratio_bps", "liquidation_buffer_bps"}, "borrowing")
    if borrowing["consent"] is not None and type(borrowing["consent"]) is not bool:
        raise MachineError("borrowing consent must be boolean or unknown")
    debt = money(borrowing["max_debt"], asset=asset)
    if borrowing["consent"] is not True and decimal(debt["amount"]) != 0:
        raise MachineError("debt cap requires explicit borrowing consent")
    borrowing = {"consent": borrowing["consent"], "max_debt": debt,
                 "min_collateral_ratio_bps": integer(borrowing["min_collateral_ratio_bps"], "collateral ratio", 10000, 1000000),
                 "liquidation_buffer_bps": integer(borrowing["liquidation_buffer_bps"], "liquidation buffer", 0, 1000000)}
    require_keys(raw["limits"], {"single_amount", "cumulative_amount", "fee_amount",
                                "daily_loss", "stress_loss"}, "mandate limits")
    limits = {key: money(value, asset=asset) for key, value in raw["limits"].items()}
    if decimal(limits["single_amount"]["amount"]) > decimal(limits["cumulative_amount"]["amount"]):
        raise MachineError("single amount exceeds cumulative amount")
    actions = raw["allowed_actions"]
    if (not isinstance(actions, list) or not actions or len(actions) > len(ACTIONS)
            or any(not isinstance(item, str) or item not in ACTIONS for item in actions)
            or len(set(actions)) != len(actions)):
        raise MachineError("allowed actions must be unique supported actions")
    if borrowing["consent"] is not True and set(actions) & BORROW_ACTIONS:
        raise MachineError("borrowing actions require explicit consent")
    start, end = utc(raw["effective_at"]), utc(raw["expires_at"])
    if datetime.fromisoformat(start) >= datetime.fromisoformat(end):
        raise MachineError("mandate expiry must follow effective time")
    # No currency conversion is invented. Mixed capital is retained for later
    # valued snapshots, but cannot use the single-asset compatibility bridge.
    if len(capital) == 1 and capital[0]["asset"] == asset:
        amount = capital[0]["amount"]
        floor = reserve_amount(immediate, amount)
        if floor > decimal(amount):
            raise MachineError("immediate cash exceeds capital")
        for item in schedule:
            required = reserve_amount(item["minimum"], amount)
            if not floor <= required <= decimal(amount):
                raise MachineError("cumulative withdrawal requirements must be nondecreasing and bounded")
            floor = required
    return {"base_asset": asset, "risk_profile": raw["risk_profile"], "capital": capital, "horizon_seconds": horizon,
            "immediate_cash": immediate, "withdrawals": schedule,
            "price_exposure_caps_bps": _caps(raw["price_exposure_caps_bps"], "price exposure"),
            "protocol_caps_bps": _caps(raw["protocol_caps_bps"], "protocol cap"),
            "borrowing": borrowing, "limits": limits, "allowed_actions": sorted(actions),
            "effective_at": start, "expires_at": end}


def normalize_mandate(raw: dict) -> dict:
    require_keys(raw, {"schema_version", "mandate_id", "revision", "scope", "trace_id",
                       "terms", "source_messages", "source_refs"}, "MandateV1")
    if raw["schema_version"] != VERSION:
        raise MachineError("unsupported mandate version")
    if len(canonical(raw)) > 65536:
        raise MachineError("mandate exceeds size limit")
    messages = raw["source_messages"]
    if not isinstance(messages, list) or not 1 <= len(messages) <= 32:
        raise MachineError("source messages must be bounded")
    book = {}
    for item in messages:
        require_keys(item, {"message_id", "text"}, "source message")
        key = ident(item["message_id"], "source message id")
        if key in book or not isinstance(item["text"], str) or not 1 <= len(item["text"]) <= 16384:
            raise MachineError("invalid or duplicate source message")
        book[key] = item["text"]
    refs = require_keys(raw["source_refs"], TERM_KEYS, "source references")
    normalized_refs = {}
    for field, items in refs.items():
        if not isinstance(items, list) or not 1 <= len(items) <= 8:
            raise MachineError("every mandate field needs a source reference")
        for item in items:
            require_keys(item, {"message_id", "quote"}, "source reference")
            message = ident(item["message_id"], "source reference id")
            if (message not in book or not isinstance(item["quote"], str)
                    or not item["quote"] or item["quote"] not in book[message]):
                raise MachineError("source quote does not match its message")
        normalized_refs[field] = sorted(items, key=lambda item: (item["message_id"], item["quote"]))
    result = {"schema_version": VERSION, "mandate_id": ident(raw["mandate_id"], "mandate id"),
              "revision": integer(raw["revision"], "revision", 1, 2147483647),
              "scope": normalize_scope(raw["scope"]), "trace_id": ident(raw["trace_id"], "trace id"),
              "terms": normalize_terms(raw["terms"]),
              "source_messages": [{"message_id": key, "text": value} for key, value in sorted(book.items())],
              "source_refs": normalized_refs}
    return json.loads(canonical(result))


def policy_hash(mandate: dict) -> str:
    item = normalize_mandate(mandate)
    return digest({"domain": VERSION + "/policy", "scope": item["scope"], "terms": item["terms"]})


def draft_hash(mandate: dict) -> str:
    return digest({"domain": VERSION + "/draft", "mandate": normalize_mandate(mandate)})


def assert_effective(mandate: dict, at: str) -> None:
    terms = mandate["terms"]
    if not datetime.fromisoformat(terms["effective_at"]) <= datetime.fromisoformat(utc(at)) < datetime.fromisoformat(terms["expires_at"]):
        raise MachineError("mandate not effective or expired")
    if terms["borrowing"]["consent"] is None:
        raise MachineError("borrowing consent is unresolved")
