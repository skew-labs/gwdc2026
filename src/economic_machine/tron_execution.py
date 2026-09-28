"""Durable-value records for exact TRON submission and solidified observation.

The functions in this module do not own a private key and do not broadcast.
They make an already verified signed payload idempotent by txid, preserve every
attempt outcome, and refuse to infer absence or success from transport output.
"""

import hashlib
import re
from copy import deepcopy
from datetime import datetime

from .approval import REQUEST_VERSION
from .mandate import tron_address
from .signed_tx_validation import (VERSION as VALIDATION_VERSION, decode_trigger_raw)
from .tron_consumption_read import (LEGACY_VERSION as LEGACY_OBSERVATION_VERSION,
                                    VERSION as OBSERVATION_VERSION)
from .tron_crypto import recover_tron_address
from .values import MachineError, digest, ident, require_keys, utc


VERSION = "economic-tron-submission-record-1"
ATTEMPT_VERSION = "economic-tron-submission-attempt-1"
EVIDENCE_VERSION = "economic-tron-execution-observation-evidence-1"
EXECUTION_VERSION = "economic-tron-execution-result-1"
MAX_ATTEMPTS = 8


def _hash(value, label):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise MachineError("invalid " + label)
    return value


def _block_commitment(raw, label):
    if not isinstance(raw, dict) or set(raw) != {"number", "block_id", "timestamp_ms"}:
        raise MachineError(label + " block commitment required")
    number, block_id, timestamp = raw["number"], raw["block_id"], raw["timestamp_ms"]
    if (type(number) is not int or number < 0 or type(timestamp) is not int
            or timestamp < 0 or not isinstance(block_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", block_id) is None
            or int(block_id[:16], 16) != number):
        raise MachineError(label + " block commitment is invalid")
    return raw


def _resource_receipt(raw):
    required = {"total_fee_sun", "energy_fee_sun", "bandwidth_fee_sun",
                "energy_usage_total", "bandwidth_usage"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise MachineError("v2 solidified observation omits resource receipt")
    for value in raw.values():
        if value is not None and (not isinstance(value, str)
                or re.fullmatch(r"0|[1-9][0-9]*", value) is None
                or len(value) > 78):
            raise MachineError("v2 solidified resource value is invalid")
    return raw


def _verified_validation(raw):
    if not isinstance(raw, dict) or raw.get("schema_version") != VALIDATION_VERSION:
        raise MachineError("ValidatedSignedTronTransactionV1 required")
    claimed = raw.get("validation_hash")
    body = deepcopy(raw)
    body.pop("validation_hash", None)
    if claimed != digest({"domain": VALIDATION_VERSION, "validation": body}):
        raise MachineError("signed transaction validation commitment mismatch")
    signed = raw.get("signed_transaction")
    request = raw.get("wallet_request")
    if not isinstance(request, dict) or request.get("schema_version") != REQUEST_VERSION:
        raise MachineError("validated transaction omits original wallet request")
    request_claim = request.get("request_hash")
    request_body = deepcopy(request)
    request_body.pop("request_hash", None)
    if request_claim != digest({"domain": REQUEST_VERSION, "request": request_body}):
        raise MachineError("wallet request commitment mismatch at submission")
    if (raw.get("status") != "SIGNED_PAYLOAD_VERIFIED"
            or raw.get("signature_status") != "VERIFIED"
            or raw.get("chain_status") != "NOT_SUBMITTED"
            or raw.get("execution_authority") != "EXACT_SIGNED_PAYLOAD_ONLY"
            or not isinstance(signed, dict)
            or signed.get("txID") != raw.get("txid")):
        raise MachineError("signed transaction is not eligible for submission")
    if (set(signed) != {"visible", "txID", "raw_data_hex", "signature"}
            or not isinstance(signed.get("raw_data_hex"), str)
            or re.fullmatch(r"[0-9a-f]+", signed["raw_data_hex"]) is None
            or not isinstance(signed.get("signature"), list)
            or len(signed["signature"]) != 1
            or not isinstance(signed["signature"][0], str)
            or re.fullmatch(r"[0-9a-f]{130}", signed["signature"][0]) is None):
        raise MachineError("canonical single-signature transaction required at submission")
    try:
        raw_bytes = bytes.fromhex(signed["raw_data_hex"])
        signature = bytes.fromhex(signed["signature"][0])
        request_fee = int(request.get("fee_limit_sun", "-1"))
    except (KeyError, TypeError, ValueError, IndexError):
        raise MachineError("signed transaction bytes missing at submission") from None
    txid = hashlib.sha256(raw_bytes).hexdigest()
    decoded = decode_trigger_raw(raw_bytes)
    signer = recover_tron_address(bytes.fromhex(txid), signature)
    expected_fields = {"plan_hash": "plan_hash", "graph_hash": "graph_hash",
        "step_hash": "step_hash", "action_hash": "action_hash",
        "approval_hash": "approval_hash", "request_hash": "request_hash"}
    if (txid != raw["txid"] or decoded != raw.get("decoded_transaction")
            or signer != raw.get("signer_address")
            or signer != tron_address(raw["scope"]["wallet"])
            or signed.get("visible") is not False
            or signed.get("txID") != txid
            or any(raw.get(left) != request.get(right)
                   for left, right in expected_fields.items())
            or request.get("owner_address") != signer
            or request.get("contract_address") != decoded["contract_address"]
            or request.get("data_hex") != decoded["data_hex"]
            or request_fee != decoded["fee_limit_sun"]
            or request.get("scope") != raw.get("scope")):
        raise MachineError("signed payload, signer and wallet request no longer agree")
    return raw


def _record(raw):
    if not isinstance(raw, dict) or raw.get("schema_version") != VERSION:
        raise MachineError("TronSubmissionRecordV1 required")
    claimed = raw.get("record_hash")
    body = deepcopy(raw)
    body.pop("record_hash", None)
    if claimed != digest({"domain": VERSION, "record": body}):
        raise MachineError("submission record commitment mismatch")
    if not isinstance(raw.get("attempts"), list) or len(raw["attempts"]) > MAX_ATTEMPTS:
        raise MachineError("submission attempts are not bounded")
    return raw


def prepare_submission(validation, *, at):
    validation = _verified_validation(validation)
    at = utc(at)
    moment = datetime.fromisoformat(at)
    if (moment < datetime.fromisoformat(utc(validation["validated_at"]))
            or int(moment.timestamp() * 1000) >= validation["decoded_transaction"][
                "expiration_ms"]):
        raise MachineError("signed payload submission window expired")
    signed = deepcopy(validation["signed_transaction"])
    payload_hash = digest(signed)
    result = {"schema_version": VERSION, "status": "READY_TO_SUBMIT",
        "scope": deepcopy(validation["scope"]), "plan_hash": validation["plan_hash"],
        "graph_hash": validation["graph_hash"], "step_hash": validation["step_hash"],
        "action_hash": validation["action_hash"], "approval_hash": validation[
            "approval_hash"], "request_hash": validation["request_hash"],
        "validation_hash": validation["validation_hash"], "txid": validation["txid"],
        "signed_payload_hash": payload_hash, "signed_transaction": signed,
        "contract_address": validation["decoded_transaction"]["contract_address"],
        "expiration_ms": validation["decoded_transaction"]["expiration_ms"],
        "prepared_at": at, "attempts": [], "last_attempt_at": None,
        "safe_next_action": "BROADCAST_EXACT_PAYLOAD", "capital_status": "LOCKED",
        "chain_status": "NOT_SUBMITTED", "rebuild_allowed": False,
        "execution_authority": "EXACT_SIGNED_PAYLOAD_ONLY"}
    result["record_hash"] = digest({"domain": VERSION, "record": result})
    return result


def prepare_exact_broadcast(record, *, at):
    record = _record(record)
    at = utc(at)
    moment_ms = int(datetime.fromisoformat(at).timestamp() * 1000)
    if (moment_ms >= record["expiration_ms"]
            or record["status"] in {"SOLID_EXECUTED_PENDING_POST_STATE",
                                    "SOLID_EXECUTION_FAILED"}):
        raise MachineError("exact payload is no longer broadcast eligible")
    if record["signed_payload_hash"] != digest(record["signed_transaction"]):
        raise MachineError("signed payload changed after validation")
    return {"txid": record["txid"], "idempotency_key": record["txid"],
        "signed_payload_hash": record["signed_payload_hash"],
        "signed_transaction": deepcopy(record["signed_transaction"]),
        "prepared_at": at, "mutation_allowed": False,
        "replacement_transaction_allowed": False}


def record_broadcast_attempt(record, attempt):
    record = _record(record)
    require_keys(attempt, {"schema_version", "attempt_id", "txid",
        "signed_payload_hash", "attempted_at", "transport_status", "node_result",
        "response_hash", "error_code"}, "TronSubmissionAttemptV1")
    if attempt["schema_version"] != ATTEMPT_VERSION:
        raise MachineError("unsupported submission attempt version")
    attempt_id = ident(attempt["attempt_id"], "submission attempt")
    attempted = utc(attempt["attempted_at"])
    if (attempt["txid"] != record["txid"]
            or attempt["signed_payload_hash"] != record["signed_payload_hash"]):
        raise MachineError("submission attempt changed txid or signed payload")
    _hash(attempt["response_hash"], "submission response hash")
    transport = attempt["transport_status"]
    node_result = attempt["node_result"]
    allowed = {"BEFORE_SEND_FAILURE": {"NOT_SENT"},
               "AFTER_SEND_UNKNOWN": {"UNKNOWN"},
               "NODE_RESPONSE": {"ACCEPTED", "REJECTED"}}
    if transport not in allowed or node_result not in allowed[transport]:
        raise MachineError("submission transport/result combination is impossible")
    error = attempt["error_code"]
    if error is not None:
        error = ident(error, "submission error code")
    if transport != "NODE_RESPONSE" and error is None:
        raise MachineError("transport failure requires an error code")
    moment_ms = int(datetime.fromisoformat(attempted).timestamp() * 1000)
    if (datetime.fromisoformat(attempted) < datetime.fromisoformat(record["prepared_at"])
            or moment_ms >= record["expiration_ms"]):
        raise MachineError("submission attempt outside signed payload lifetime")
    attempts = deepcopy(record["attempts"])
    normalized = {**attempt, "attempt_id": attempt_id, "attempted_at": attempted,
                  "error_code": error}
    same = next((item for item in attempts if item["attempt_id"] == attempt_id), None)
    if same is not None:
        if same != normalized:
            raise MachineError("conflicting result for submission attempt")
        return record
    if len(attempts) >= MAX_ATTEMPTS:
        raise MachineError("submission attempt limit reached")
    if attempts and datetime.fromisoformat(attempted) < datetime.fromisoformat(
            attempts[-1]["attempted_at"]):
        raise MachineError("submission attempts must be chronological")
    attempts.append(normalized)
    if transport == "BEFORE_SEND_FAILURE":
        status, chain, next_action = ("NOT_SENT_RETRYABLE", "NOT_SUBMITTED",
                                      "BROADCAST_EXACT_PAYLOAD")
    elif transport == "AFTER_SEND_UNKNOWN":
        status, chain, next_action = ("SUBMISSION_UNKNOWN", "UNKNOWN",
                                      "QUERY_ORIGINAL_TXID")
    elif node_result == "ACCEPTED":
        status, chain, next_action = ("NODE_ACCEPTED_UNCONFIRMED", "UNCONFIRMED",
                                      "QUERY_ORIGINAL_TXID")
    else:
        status, chain, next_action = ("NODE_REJECTED_UNCONFIRMED_ABSENCE", "UNKNOWN",
                                      "QUERY_ORIGINAL_TXID")
    result = deepcopy(record)
    result.update(status=status, attempts=attempts, last_attempt_at=attempted,
                  safe_next_action=next_action, chain_status=chain)
    result.pop("record_hash")
    result["record_hash"] = digest({"domain": VERSION, "record": result})
    return result


def _observation(raw):
    if not isinstance(raw, dict) or raw.get("schema_version") not in {
            OBSERVATION_VERSION, LEGACY_OBSERVATION_VERSION}:
        raise MachineError("TronTransactionObservationV1 required")
    claimed = raw.get("observation_hash")
    body = deepcopy(raw)
    body.pop("observation_hash", None)
    if claimed != digest(body):
        raise MachineError("TRON transaction observation commitment mismatch")
    require_keys(raw, {"schema_version", "txid", "status", "network_anchor",
        "solid_tip_before", "solid_tip_after", "observed_at_ms", "endpoint_fingerprint",
        "details", "source_trust", "settlement_status", "execution_authority",
        "observation_hash"}, "TronTransactionObservationV1")
    _hash(raw.get("txid"), "observation txid")
    _hash(raw.get("endpoint_fingerprint"), "observation endpoint fingerprint")
    anchor = _block_commitment(raw.get("network_anchor"), "network anchor")
    before = _block_commitment(raw.get("solid_tip_before"), "solid tip before")
    after = _block_commitment(raw.get("solid_tip_after"), "solid tip after")
    observed = raw.get("observed_at_ms")
    if (after["number"] < before["number"] or anchor["number"] > before["number"]
            or type(observed) is not int or observed < 0):
        raise MachineError("observation block sequence is invalid")
    if (raw.get("source_trust") != "NODE_RESPONSE_ONLY"
            or raw.get("settlement_status") != "NOT_VERIFIED"
            or raw.get("execution_authority") != "NONE"):
        raise MachineError("TRON transaction observation overclaims authority")
    return raw


def assess_execution_observation(record, evidence):
    record = _record(record)
    require_keys(evidence, {"schema_version", "network", "reader_config_hash",
        "source_id", "observation"}, "TronExecutionObservationEvidenceV1")
    if evidence["schema_version"] != EVIDENCE_VERSION:
        raise MachineError("unsupported execution observation evidence version")
    if evidence["network"] != record["scope"]["network"]:
        raise MachineError("execution observation network mismatch")
    _hash(evidence["reader_config_hash"], "reader config hash")
    ident(evidence["source_id"], "execution observation source")
    observation = _observation(evidence["observation"])
    if observation["txid"] != record["txid"]:
        raise MachineError("execution observation txid mismatch")
    status = observation["status"]
    details = observation["details"]
    receipt_hash = None
    block = None
    resource_receipt = None
    if status == "NOT_OBSERVED":
        result_status, next_action = "SUBMISSION_UNKNOWN", "KEEP_LOCK_AND_QUERY"
    elif status == "SOLID_BODY_RECEIPT_MISSING":
        result_status, next_action = "SOLID_BODY_RECEIPT_PENDING", "KEEP_LOCK_AND_QUERY"
    elif status in {"SOLID_EXECUTED", "SOLID_EXECUTION_FAILED"}:
        possible_attempts = [item for item in record["attempts"]
                             if item["transport_status"] != "BEFORE_SEND_FAILURE"]
        if not possible_attempts:
            raise MachineError("solidified execution has no possible submission attempt")
        expected_target = record["contract_address"]
        if expected_target.startswith("41"):
            expected_target = expected_target[2:]
        if not isinstance(details, dict) or details.get(
                "call_target_address") != expected_target:
            raise MachineError("solidified execution target mismatch")
        receipt_hash = _hash(details.get("receipt_record_hash"), "receipt record hash")
        _hash(details.get("body_record_hash"), "body record hash")
        resource_receipt = deepcopy(details.get("resource_receipt"))
        if observation["schema_version"] == OBSERVATION_VERSION:
            _resource_receipt(resource_receipt)
        block = _block_commitment(details.get("block"), "solidified execution")
        first_possible_ms = int(datetime.fromisoformat(
            possible_attempts[0]["attempted_at"]).timestamp() * 1000)
        if not first_possible_ms <= block["timestamp_ms"] < record["expiration_ms"]:
            raise MachineError("solidified execution is outside signed submission window")
        if status == "SOLID_EXECUTED" and details.get("execution_success") is True:
            result_status, next_action = ("SOLID_EXECUTED_PENDING_POST_STATE",
                                          "READ_FRESH_ACCOUNT_STATE")
        elif status == "SOLID_EXECUTION_FAILED" and details.get(
                "execution_success") is False:
            result_status, next_action = "SOLID_EXECUTION_FAILED", "KEEP_LOCK_AND_RECOVER"
        else:
            raise MachineError("solidified status contradicts execution result")
    else:
        raise MachineError("unsupported TRON observation status")
    result = {"schema_version": EXECUTION_VERSION, "status": result_status,
        "scope": deepcopy(record["scope"]), "plan_hash": record["plan_hash"],
        "graph_hash": record["graph_hash"], "step_hash": record["step_hash"],
        "action_hash": record["action_hash"], "approval_hash": record["approval_hash"],
        "validation_hash": record["validation_hash"], "submission_record_hash": record[
            "record_hash"], "txid": record["txid"], "signed_payload_hash": record[
            "signed_payload_hash"], "observation_hash": observation["observation_hash"],
        "observation_evidence_hash": digest(evidence), "receipt_record_hash": receipt_hash,
        "block": deepcopy(block), "resource_receipt": resource_receipt,
        "safe_next_action": next_action,
        "capital_status": "LOCKED", "position_status": "NOT_RECONCILED",
        "replacement_transaction_allowed": False, "execution_authority": "NONE"}
    result["execution_result_hash"] = digest({"domain": EXECUTION_VERSION,
                                               "execution": result})
    return result


def verify_execution_result(raw):
    if not isinstance(raw, dict) or raw.get("schema_version") != EXECUTION_VERSION:
        return False
    claimed = raw.get("execution_result_hash")
    body = deepcopy(raw)
    body.pop("execution_result_hash", None)
    return claimed == digest({"domain": EXECUTION_VERSION, "execution": body})
