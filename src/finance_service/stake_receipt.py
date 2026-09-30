"""Solid node membership and exact native bytes, independent of TVM receipts."""

import hashlib
from datetime import datetime
from economic_machine.tron_registry_read import _block
from economic_machine.values import MachineError, digest
from .native_execution import READER
from .stake_codec import decode


def observe(adapter, context, transaction):
    txid = transaction["txID"]
    raw = transaction["raw_data_hex"]
    expected = decode(bytes.fromhex(raw))
    if (
        hashlib.sha256(bytes.fromhex(raw)).hexdigest() != txid
        or expected["value"]["owner_address"] != context.wallet
    ):
        raise MachineError("Recorded staking transaction identity mismatch.")
    captured = []

    def get(path, body=None):
        value = adapter.rpc(context, path, body)
        captured.append({"path": path, "request": body, "response": value})
        return value

    head = _block(get("walletsolidity/getnowblock"))
    anchor = _block(
        get("walletsolidity/getblockbynum", {"num": READER["network_anchor_height"]})
    )
    if (
        context.network != "tron-nile"
        or anchor["block_id"] != READER["network_anchor_block_id"]
    ):
        raise MachineError("Native staking network anchor mismatch.")
    now = int(datetime.fromisoformat(adapter.clock()).timestamp() * 1000)
    if not -5000 <= now - head["timestamp_ms"] <= 300000:
        raise MachineError("Solidified native chain head is stale.")
    body = get("walletsolidity/gettransactionbyid", {"value": txid})
    info = get("walletsolidity/gettransactioninfobyid", {"value": txid})
    result = {"status": "PENDING", "reads": captured, "head": head, "txid": txid}
    if body and info:
        if (
            body.get("txID") != txid
            or body.get("raw_data_hex") != raw
            or info.get("id") != txid
        ):
            raise MachineError("Native receipt and exact transaction bytes differ.")
        height = info.get("blockNumber")
        if type(height) is not int or not 0 <= height <= head["number"]:
            raise MachineError("Native receipt block is not solid.")
        b = get("walletsolidity/getblockbynum", {"num": height})
        block = _block(b)
        if (
            block["timestamp_ms"] != info.get("blockTimeStamp")
            or block["number"] != height
        ):
            raise MachineError("Native receipt block timestamp mismatch.")
        matches = [t for t in b.get("transactions", []) if t.get("txID") == txid]
        if len(matches) != 1 or matches[0].get("raw_data_hex") != raw:
            raise MachineError("Native transaction membership mismatch.")
        ret = body.get("ret")
        fee = info.get("fee", 0)
        if (
            not isinstance(ret, list)
            or len(ret) != 1
            or not isinstance(ret[0], dict)
            or not isinstance(info.get("receipt", {}), dict)
            or type(fee) is not int
            or fee < 0
        ):
            raise MachineError("Native receipt result malformed.")
        code = ret[0].get("contractRet")
        if code not in {
            "SUCCESS",
            "REVERT",
            "BAD_JUMP_DESTINATION",
            "OUT_OF_MEMORY",
            "PRECOMPILED_CONTRACT",
            "STACK_TOO_SMALL",
            "STACK_TOO_LARGE",
            "ILLEGAL_OPERATION",
            "STACK_OVERFLOW",
            "OUT_OF_ENERGY",
            "OUT_OF_TIME",
            "JVM_STACK_OVER_FLOW",
            "TRANSFER_FAILED",
            "INVALID_CODE",
        }:
            raise MachineError("Native receipt result is unresolved.")
        ok = code == "SUCCESS"
        if info.get("result") not in (
            (None, "SUCESS", "SUCCESS") if ok else (None, "FAILED")
        ):
            raise MachineError("Native receipt results contradict each other.")
        if info.get("receipt", {}).get("energy_usage_total", 0):
            raise MachineError("Unexpected TVM energy in native receipt.")
        result.update(
            status="SUCCESS" if ok else "FAILED", fee_sun=fee, block=block, receipt=info
        )
    elif (
        not body
        and not info
        and now > expected["expiration"] + 60000
        and head["timestamp_ms"] > expected["expiration"] + 60000
    ):
        full = get("wallet/gettransactionbyid", {"value": txid})
        receipt = get("wallet/gettransactioninfobyid", {"value": txid})
        if not full and not receipt:
            result["status"] = "EXPIRED_NOT_OBSERVED"
    tail = _block(get("walletsolidity/getnowblock"))
    if tail["number"] < head["number"] or (
        tail["number"] == head["number"] and tail["block_id"] != head["block_id"]
    ):
        raise MachineError("Native solid chain regressed.")
    result["hash"] = digest(result)
    return result
