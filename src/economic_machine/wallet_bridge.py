"""Wallet bridge interface. Implementations display and return a signed payload."""

from typing import Protocol

from .values import MachineError


class WalletBridge(Protocol):
    def request_signature(self, request: dict) -> dict:
        """Ask the user wallet to sign one exact request; never broadcast it."""


def request_wallet_signature(bridge: WalletBridge, request: dict) -> dict:
    if not isinstance(request, dict) or request.get("status") != "AWAITING_WALLET_SIGNATURE":
        raise MachineError("approved wallet signature request required")
    response = bridge.request_signature(request)
    if not isinstance(response, dict):
        raise MachineError("wallet bridge returned no signed transaction")
    return response
