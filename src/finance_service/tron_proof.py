"""Verify a minimal Nile wallet round trip from independent node reads."""

import json
import re
import urllib.request

from economic_machine.mandate import tron_address
from economic_machine.tron_registry_read import _NoRedirect
from economic_machine.values import MachineError, digest, utc

from .context import AuthenticatedContext


NILE = "https://nile.trongrid.io"
MAX_BYTES = 1_048_576


class TronProofService:
    def __init__(self, repository, clock, opener=None):
        self.repository = repository
        self.clock = clock
        self._open = opener or urllib.request.build_opener(_NoRedirect()).open

    def _post(self, path, body):
        request = urllib.request.Request(NILE + path,
            data=json.dumps(body, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST")
        try:
            with self._open(request, timeout=12) as response:
                raw = response.read(MAX_BYTES + 1)
        except Exception as exc:
            raise MachineError("Nile proof reader unavailable") from exc
        if len(raw) > MAX_BYTES:
            raise MachineError("Nile proof response exceeds bound")
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MachineError("Nile proof response is malformed") from exc
        if not isinstance(result, dict):
            raise MachineError("Nile proof response must be an object")
        return result

    def verify(self, context: AuthenticatedContext, txid: str) -> dict:
        if not isinstance(context, AuthenticatedContext):
            raise MachineError("authenticated service context required")
        at = utc(self.clock())
        scope = context.authorize(at)
        if scope["network"] != "tron-nile":
            raise MachineError("wallet proof transaction is Nile-only")
        if not isinstance(txid, str) or re.fullmatch(r"[0-9a-fA-F]{64}", txid) is None:
            raise MachineError("invalid TRON proof txid")
        txid = txid.lower()
        transaction = self._post("/wallet/gettransactionbyid", {"value": txid})
        if not transaction:
            return self._result(scope, txid, at, "PENDING_NODE_OBSERVATION", None, None)
        if transaction.get("txID", "").lower() != txid:
            raise MachineError("Nile node returned a different transaction")
        contracts = transaction.get("raw_data", {}).get("contract")
        if not isinstance(contracts, list) or len(contracts) != 1:
            raise MachineError("wallet proof requires one transfer contract")
        contract = contracts[0]
        if contract.get("type") != "TransferContract":
            raise MachineError("wallet proof transaction is not a TRX transfer")
        value = contract.get("parameter", {}).get("value")
        if not isinstance(value, dict):
            raise MachineError("wallet proof transfer value is missing")
        try:
            owner = tron_address(value["owner_address"])
            recipient = tron_address(value["to_address"])
        except (KeyError, TypeError) as exc:
            raise MachineError("wallet proof addresses are missing") from exc
        if owner != scope["wallet"] or recipient != scope["wallet"] or value.get("amount") != 1:
            raise MachineError("wallet proof must transfer exactly one SUN to the same wallet")
        signatures = transaction.get("signature")
        if (not isinstance(signatures, list) or len(signatures) != 1
                or re.fullmatch(r"[0-9a-fA-F]{130}", signatures[0]) is None):
            raise MachineError("wallet proof needs one transaction signature")
        info = self._post("/walletsolidity/gettransactioninfobyid", {"value": txid})
        if not info or info.get("id", "").lower() != txid:
            return self._result(scope, txid, at, "PENDING_SOLIDITY", transaction, None)
        receipt = info.get("receipt")
        result = info.get("result") or (receipt.get("result") if isinstance(receipt, dict) else None)
        if result not in {"SUCCESS", None}:
            return self._result(scope, txid, at, "SOLID_EXECUTION_FAILED", transaction, info)
        if type(info.get("blockNumber")) is not int:
            return self._result(scope, txid, at, "PENDING_SOLIDITY", transaction, info)
        return self._result(scope, txid, at, "SOLID_EXECUTED", transaction, info)

    @staticmethod
    def _result(scope, txid, at, status, transaction, info):
        evidence = {"schema_version": "tron-wallet-proof-1", "txid": txid,
            "network": scope["network"], "wallet": scope["wallet"], "observed_at": at,
            "status": status,
            "transaction_hash": None if transaction is None else digest(transaction),
            "receipt_hash": None if info is None else digest(info),
            "block_number": None if info is None else info.get("blockNumber"),
            "amount_sun": 1, "direction": "SELF_TRANSFER",
            "execution_authority": "NONE"}
        evidence["evidence_hash"] = digest(evidence)
        return evidence
