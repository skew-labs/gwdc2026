"""Typed money and user-need contracts shared by the collector and planner."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Any


class ContractError(ValueError):
    pass


def decimal_string(value: Any, *, nonnegative: bool = True) -> Decimal:
    """Accept exact decimal strings only. JSON floating point has already lost scale."""
    if not isinstance(value, str) or len(value) > 100 or not re.fullmatch(
        r"(0|[1-9][0-9]*)(\.[0-9]+)?", value
    ):
        raise ContractError("decimal must be a plain nonnegative string of at most 100 characters")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ContractError("invalid decimal string") from exc
    if not parsed.is_finite() or (nonnegative and parsed < 0):
        raise ContractError("decimal must be finite and nonnegative")
    return parsed


@dataclass(frozen=True)
class Need:
    asset: str
    amount: Decimal
    liquid_reserve: Decimal
    horizon_days: int
    risk: str

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "Need":
        if not isinstance(payload, dict):
            raise ContractError("need must be an object")
        asset = payload.get("asset")
        if asset not in {"USDT", "USDD"}:
            raise ContractError("currently supported assets: USDT, USDD")
        amount = decimal_string(payload.get("amount"))
        reserve = decimal_string(payload.get("liquid_reserve"))
        horizon = payload.get("horizon_days")
        risk = payload.get("risk")
        if amount <= 0 or reserve > amount:
            raise ContractError("amount must be positive and reserve cannot exceed it")
        if type(horizon) is not int or not 1 <= horizon <= 365:
            raise ContractError("horizon_days must be an integer from 1 to 365")
        if risk not in {"cautious", "balanced", "growth"}:
            raise ContractError("risk must be cautious, balanced, or growth")
        return cls(asset, amount, reserve, horizon, risk)


def canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ContractError("non-finite calculation")
    return format(value, "f")
