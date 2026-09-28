"""Decode and verify one signed TRON TriggerSmartContract transaction.

The JSON projection is not trusted.  This module parses protobuf raw_data_hex,
recomputes txID, recovers the secp256k1 signer, and compares every approved
field.  It never calls broadcasttransaction.
"""

import hashlib
import re
from copy import deepcopy
from datetime import datetime

from .approval import REQUEST_VERSION, normalize_wallet_session
from .mandate import tron_address
from .tron_crypto import recover_tron_address
from .values import MachineError, digest, require_keys, utc


VERSION = "economic-validated-signed-tron-transaction-1"
TRIGGER_SMART_CONTRACT = 31
TYPE_URL = b"type.googleapis.com/protocol.TriggerSmartContract"


def _varint(data, offset):
    value, shift, start = 0, 0, offset
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            if data[start:offset] != _encode_varint(value):
                raise MachineError("noncanonical protobuf varint")
            return value, offset
        shift += 7
    raise MachineError("truncated or oversized protobuf varint")


def _encode_varint(value):
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        result.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(result)


def _fields(data):
    if not isinstance(data, bytes) or len(data) > 131072:
        raise MachineError("bounded protobuf bytes required")
    result, offset = [], 0
    while offset < len(data):
        tag, offset = _varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise MachineError("protobuf field zero is invalid")
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire == 1:
            if offset + 8 > len(data):
                raise MachineError("truncated fixed64")
            value, offset = data[offset:offset + 8], offset + 8
        elif wire == 2:
            size, offset = _varint(data, offset)
            if size > 131072 or offset + size > len(data):
                raise MachineError("truncated length-delimited field")
            value, offset = data[offset:offset + size], offset + size
        elif wire == 5:
            if offset + 4 > len(data):
                raise MachineError("truncated fixed32")
            value, offset = data[offset:offset + 4], offset + 4
        else:
            raise MachineError("unsupported protobuf wire type")
        result.append((number, wire, value))
    return result


def _book(data, allowed, label):
    result = {}
    for number, wire, value in _fields(data):
        if number not in allowed or number in result:
            raise MachineError(label + " has unknown or duplicate field")
        expected = allowed[number]
        if wire != expected:
            raise MachineError(label + " has wrong wire type")
        result[number] = value
    return result


def decode_trigger_raw(raw):
    fields = _book(raw, {1: 2, 4: 2, 8: 0, 11: 2, 14: 0, 18: 0}, "transaction raw")
    if set(fields) != {1, 4, 8, 11, 14, 18}:
        raise MachineError("transaction raw omits required TAPOS, time, fee or contract")
    if len(fields[1]) != 2 or len(fields[4]) != 8:
        raise MachineError("invalid TAPOS byte lengths")
    contract = _book(fields[11], {1: 0, 2: 2, 3: 0}, "transaction contract")
    if contract.get(1) != TRIGGER_SMART_CONTRACT or 2 not in contract:
        raise MachineError("one TriggerSmartContract is required")
    permission = contract.get(3, 0)
    any_value = _book(contract[2], {1: 2, 2: 2}, "protobuf Any")
    if set(any_value) != {1, 2} or any_value[1] != TYPE_URL:
        raise MachineError("unexpected smart-contract Any type")
    trigger = _book(any_value[2], {1: 2, 2: 2, 3: 0, 4: 2, 5: 0, 6: 0},
                    "TriggerSmartContract")
    if not {1, 2, 4}.issubset(trigger) or len(trigger[1]) != 21 or len(trigger[2]) != 21:
        raise MachineError("trigger owner, contract or data missing")
    if trigger.get(5, 0) != 0 or trigger.get(6, 0) != 0:
        raise MachineError("TRC10 value is outside approved request")
    return {"ref_block_bytes": fields[1].hex(), "ref_block_hash": fields[4].hex(),
        "expiration_ms": fields[8], "timestamp_ms": fields[14],
        "fee_limit_sun": fields[18], "permission_id": permission,
        "owner_address": trigger[1].hex(), "contract_address": trigger[2].hex(),
        "call_value_sun": trigger.get(3, 0), "data_hex": trigger[4].hex()}


def _request(request):
    if not isinstance(request, dict) or request.get("schema_version") != REQUEST_VERSION:
        raise MachineError("WalletSignatureRequestV1 required")
    claimed = request.get("request_hash")
    payload = deepcopy(request)
    payload.pop("request_hash", None)
    if claimed != digest({"domain": REQUEST_VERSION, "request": payload}):
        raise MachineError("wallet signature request commitment mismatch")
    if (request.get("status") != "AWAITING_WALLET_SIGNATURE"
            or request.get("signature_status") != "REQUESTED"
            or request.get("chain_status") != "NOT_SUBMITTED"
            or request.get("execution_authority") != "NONE"):
        raise MachineError("wallet request is not awaiting one signature")
    return request


def validate_signed_transaction(signed, request, session, used_commitments, *, at):
    request = _request(request)
    session = normalize_wallet_session(session)
    require_keys(signed, {"visible", "txID", "raw_data_hex", "signature"},
                 "SignedTronTransaction")
    if signed["visible"] is not False:
        raise MachineError("signed transaction must use canonical hex address mode")
    txid = signed["txID"]
    raw_hex = signed["raw_data_hex"]
    signatures = signed["signature"]
    if (not isinstance(txid, str) or re.fullmatch(r"[0-9a-f]{64}", txid) is None
            or not isinstance(raw_hex, str) or len(raw_hex) > 262144
            or len(raw_hex) % 2 or re.fullmatch(r"[0-9a-f]+", raw_hex) is None
            or not isinstance(signatures, list) or len(signatures) != 1
            or not isinstance(signatures[0], str)
            or re.fullmatch(r"[0-9a-f]{130}", signatures[0]) is None):
        raise MachineError("invalid canonical signed transaction encoding")
    raw = bytes.fromhex(raw_hex)
    actual_txid = hashlib.sha256(raw).hexdigest()
    if txid != actual_txid:
        raise MachineError("txID differs from protobuf raw_data hash")
    decoded = decode_trigger_raw(raw)
    owner = recover_tron_address(bytes.fromhex(txid), bytes.fromhex(signatures[0]))
    expected = {"owner_address": request["owner_address"],
        "contract_address": request["contract_address"], "data_hex": request["data_hex"],
        "call_value_sun": int(request["call_value_sun"]),
        "fee_limit_sun": int(request["fee_limit_sun"]),
        "permission_id": request["permission_id"],
        "timestamp_ms": request["timestamp_ms"], "expiration_ms": request["expiration_ms"],
        "ref_block_bytes": request["ref_block_bytes"],
        "ref_block_hash": request["ref_block_hash"]}
    if decoded != expected:
        raise MachineError("signed protobuf fields differ from approved wallet request")
    if owner != tron_address(request["owner_address"]):
        raise MachineError("transaction signature does not recover approved wallet")
    if session["scope"] != request["scope"] or session["session_id"] != request[
            "session_id"] or session["auth_context_hash"] != request["auth_context_hash"]:
        raise MachineError("wallet session changed before signed-payload validation")
    moment = datetime.fromisoformat(utc(at))
    if not datetime.fromisoformat(request["issued_at"]) <= moment < min(
            datetime.fromisoformat(request["expires_at"]),
            datetime.fromisoformat(session["expires_at"])):
        raise MachineError("signed transaction validation window expired")
    if (not isinstance(used_commitments, list)
            or any(not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{64}", item) is None
                   for item in used_commitments)):
        raise MachineError("used commitment ledger must be SHA-256 hashes")
    replay_keys = {txid, request["request_hash"], request["approval_hash"]}
    if replay_keys & set(used_commitments):
        raise MachineError("signed transaction or approval replay")
    result = {"schema_version": VERSION, "status": "SIGNED_PAYLOAD_VERIFIED",
        "request_hash": request["request_hash"], "approval_hash": request["approval_hash"],
        "scope": request["scope"], "plan_hash": request["plan_hash"],
        "graph_hash": request["graph_hash"], "step_hash": request["step_hash"],
        "action_hash": request["action_hash"], "nonce": request["nonce"],
        "execution_path": request["execution_path"], "txid": txid,
        "raw_data_hash": actual_txid, "signer_address": owner,
        "wallet_request": deepcopy(request),
        "decoded_transaction": decoded,
        "signed_transaction": {"visible": False, "txID": txid,
            "raw_data_hex": raw_hex, "signature": list(signatures)},
        "validated_at": utc(at), "signature_status": "VERIFIED",
        "chain_status": "NOT_SUBMITTED", "execution_authority": "EXACT_SIGNED_PAYLOAD_ONLY"}
    result["validation_hash"] = digest({"domain": VERSION, "validation": result})
    return result
