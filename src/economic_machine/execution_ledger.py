"""Hash-chained graph progress for non-atomic TRON execution."""

import re
from copy import deepcopy

from .position_reconciliation import verify_reconciliation
from .preflight import _verify_graph
from .tron_execution import verify_execution_result
from .values import MachineError, digest, ident, require_keys, utc


VERSION = "economic-graph-execution-ledger-1"
EXCEPTION_VERSION = "economic-graph-execution-exception-1"
RESERVATION_VERSION = "economic-capital-reservation-evidence-1"


def _ledger(raw):
    if not isinstance(raw, dict) or raw.get("schema_version") != VERSION:
        raise MachineError("GraphExecutionLedgerV1 required")
    claimed = raw.get("ledger_hash")
    body = deepcopy(raw)
    body.pop("ledger_hash", None)
    if claimed != digest({"domain": VERSION, "ledger": body}):
        raise MachineError("graph execution ledger commitment mismatch")
    if not isinstance(raw.get("history"), list) or len(raw["history"]) > 128:
        raise MachineError("graph execution history is not bounded")
    return raw


def _commit(raw):
    raw.pop("ledger_hash", None)
    raw["ledger_hash"] = digest({"domain": VERSION, "ledger": raw})
    return raw


def _reservation(raw, graph, steps, started_at):
    require_keys(raw, {"schema_version", "reservation_id", "scope", "graph_hash",
        "step_hashes", "status", "locked_at", "expires_at", "source_hash",
        "execution_authority", "evidence_hash"}, "CapitalReservationEvidenceV1")
    if raw["schema_version"] != RESERVATION_VERSION:
        raise MachineError("unsupported capital reservation evidence")
    claimed = raw["evidence_hash"]
    body = deepcopy(raw)
    body.pop("evidence_hash")
    if claimed != digest({"domain": RESERVATION_VERSION, "evidence": body}):
        raise MachineError("capital reservation evidence commitment mismatch")
    expected = sorted(item["step_hash"] for item in steps
                      if item["kind"] == "OFFCHAIN_RESERVATION")
    if (raw["scope"] != graph["scope"] or raw["graph_hash"] != graph["graph_hash"]
            or raw["step_hashes"] != expected or raw["status"] != "EXECUTION_LOCKED"
            or raw["execution_authority"] != "NONE"):
        raise MachineError("capital reservation does not bind this graph")
    locked, expires, started = utc(raw["locked_at"]), utc(raw["expires_at"]), utc(started_at)
    if not locked <= started < expires:
        raise MachineError("capital reservation is not active at ledger start")
    if not isinstance(raw["source_hash"], str) or re.fullmatch(
            r"[0-9a-f]{64}", raw["source_hash"]) is None:
        raise MachineError("capital reservation source hash required")
    return {**deepcopy(raw), "reservation_id": ident(
        raw["reservation_id"], "graph reservation"), "locked_at": locked,
        "expires_at": expires}


def start_execution_ledger(graph, reservation_evidence, *, started_at):
    steps = _verify_graph(graph)
    reservation = _reservation(reservation_evidence, graph, steps, started_at)
    states = [{"step_id": item["step_id"], "operation": item["operation"],
        "step_hash": item["step_hash"],
        "depends_on": list(item["depends_on"]),
        "status": "RESERVED" if item["kind"] == "OFFCHAIN_RESERVATION" else "PENDING",
        "txid": None, "receipt_record_hash": None, "block_number": None,
        "block_id": None, "execution_result_hash": None,
        "reconciliation_hash": None}
        for item in steps]
    return _commit({"schema_version": VERSION, "status": "ACTIVE",
        "scope": deepcopy(graph["scope"]), "plan_hash": graph["plan_hash"],
        "graph_hash": graph["graph_hash"], "reservation_id": reservation["reservation_id"],
        "reservation_evidence_hash": reservation["evidence_hash"],
        "execution_semantics": "ORDERED_MULTI_TRANSACTION_NON_ATOMIC",
        "started_at": utc(started_at), "updated_at": utc(started_at),
        "steps": states, "history": [{"kind": "CAPITAL_RESERVATION",
            "reservation_id": reservation["reservation_id"],
            "evidence_hash": reservation["evidence_hash"],
            "at": reservation["locked_at"]}], "capital_status": "LOCKED",
        "safe_next_action": "EXECUTE_NEXT_READY_STEP", "execution_authority": "NONE"})


def _step(ledger, step_id):
    step = next((item for item in ledger["steps"] if item["step_id"] == step_id), None)
    if step is None:
        raise MachineError("execution evidence references unknown graph step")
    return step


def _step_hash(ledger, step_hash):
    step = next((item for item in ledger["steps"] if item["step_hash"] == step_hash), None)
    if step is None:
        raise MachineError("execution result references unknown graph step hash")
    return step


def _dependencies_ready(ledger, step):
    states = {item["step_id"]: item["status"] for item in ledger["steps"]}
    return all(states.get(dep) in {"RESERVED", "RECONCILED"} for dep in step["depends_on"])


def _halt_remaining(ledger, current):
    for item in ledger["steps"]:
        if item["step_id"] != current and item["status"] in {
                "PENDING", "SUBMISSION_UNCERTAIN", "EXECUTED_PENDING_RECONCILIATION"}:
            item["status"] = "BLOCKED_BY_PRIOR_STEP"


def _invalidate_descendants(ledger, current):
    frontier = [current]
    while frontier:
        parent = frontier.pop()
        for item in ledger["steps"]:
            if parent in item["depends_on"] and item["status"] != "RESERVED":
                item["status"] = "INVALIDATED_BY_PRIOR_STEP_REORG"
                frontier.append(item["step_id"])


def apply_execution_result(ledger, execution, *, at):
    ledger = deepcopy(_ledger(ledger))
    if not verify_execution_result(execution):
        raise MachineError("committed TRON execution result required")
    if (execution["scope"] != ledger["scope"]
            or execution["graph_hash"] != ledger["graph_hash"]):
        raise MachineError("execution result belongs to another graph")
    step = _step_hash(ledger, execution["step_hash"])
    existing = next((item for item in ledger["history"]
                     if item.get("execution_result_hash") == execution[
                         "execution_result_hash"]), None)
    if existing is not None:
        return ledger
    if ledger["status"] == "HALTED":
        raise MachineError("halted graph cannot accept more execution results")
    if not _dependencies_ready(ledger, step):
        raise MachineError("graph step dependencies are not reconciled")
    if step["txid"] is not None and step["txid"] != execution["txid"]:
        raise MachineError("graph step already binds another transaction")
    if step["status"] not in {"PENDING", "SUBMISSION_UNCERTAIN"}:
        raise MachineError("graph step cannot accept this execution transition")
    at = utc(at)
    if at < ledger["updated_at"]:
        raise MachineError("graph execution evidence moved backward in time")
    state = execution["status"]
    if state in {"SUBMISSION_UNKNOWN", "SOLID_BODY_RECEIPT_PENDING"}:
        step["status"] = "SUBMISSION_UNCERTAIN"
        ledger["status"] = "WAITING"
        ledger["safe_next_action"] = "QUERY_ORIGINAL_TXID"
    elif state == "SOLID_EXECUTED_PENDING_POST_STATE":
        step["status"] = "EXECUTED_PENDING_RECONCILIATION"
        ledger["status"] = "WAITING"
        ledger["safe_next_action"] = "READ_FRESH_ACCOUNT_STATE"
    elif state == "SOLID_EXECUTION_FAILED":
        step["status"] = "FAILED"
        ledger["status"] = "HALTED"
        ledger["safe_next_action"] = "RECOVER_WITHOUT_REBUILDING_TX"
        _halt_remaining(ledger, step["step_id"])
    else:
        raise MachineError("unsupported execution result state")
    step["txid"] = execution["txid"]
    step["receipt_record_hash"] = execution["receipt_record_hash"]
    if execution["block"] is not None:
        step["block_number"] = execution["block"]["number"]
        step["block_id"] = execution["block"]["block_id"]
    step["execution_result_hash"] = execution["execution_result_hash"]
    ledger["history"].append({"kind": "EXECUTION_RESULT", "step_id": step["step_id"],
        "txid": execution["txid"], "status": state,
        "execution_result_hash": execution["execution_result_hash"], "at": at})
    ledger["updated_at"] = at
    return _commit(ledger)


def apply_reconciliation(ledger, reconciliation, *, at):
    ledger = deepcopy(_ledger(ledger))
    if not verify_reconciliation(reconciliation):
        raise MachineError("committed position reconciliation required")
    if (reconciliation["scope"] != ledger["scope"]
            or reconciliation["graph_hash"] != ledger["graph_hash"]):
        raise MachineError("reconciliation belongs to another graph")
    step = _step(ledger, reconciliation["step_id"])
    existing = next((item for item in ledger["history"]
                     if item.get("reconciliation_hash") == reconciliation[
                         "reconciliation_hash"]), None)
    if existing is not None:
        return ledger
    if (step["status"] != "EXECUTED_PENDING_RECONCILIATION"
            or step["execution_result_hash"] != reconciliation["execution_result_hash"]
            or step["txid"] != reconciliation["txid"]):
        raise MachineError("reconciliation does not follow this step execution")
    at = utc(at)
    if at < ledger["updated_at"] or at < reconciliation["reconciled_at"]:
        raise MachineError("reconciliation moved backward in time")
    step["reconciliation_hash"] = reconciliation["reconciliation_hash"]
    if reconciliation["status"] == "RECONCILED":
        step["status"] = "RECONCILED"
        complete = all(item["status"] in {"RESERVED", "RECONCILED"}
                       for item in ledger["steps"])
        ledger["status"] = "COMPLETED" if complete else "ACTIVE"
        ledger["capital_status"] = "RELEASE_ELIGIBLE" if complete else "LOCKED"
        ledger["safe_next_action"] = ("RELEASE_CAPITAL_LOCK" if complete else
                                      "EXECUTE_NEXT_READY_STEP")
    elif reconciliation["status"] == "DISPUTED":
        step["status"] = "DISPUTED"
        ledger["status"] = "HALTED"
        ledger["capital_status"] = "LOCKED"
        ledger["safe_next_action"] = "INVESTIGATE_POST_STATE_MISMATCH"
        _halt_remaining(ledger, step["step_id"])
    else:
        raise MachineError("unsupported reconciliation state")
    ledger["history"].append({"kind": "RECONCILIATION", "step_id": step["step_id"],
        "txid": reconciliation["txid"], "status": reconciliation["status"],
        "reconciliation_hash": reconciliation["reconciliation_hash"], "at": at})
    ledger["updated_at"] = at
    return _commit(ledger)


def record_reorg(ledger, evidence):
    ledger = deepcopy(_ledger(ledger))
    require_keys(evidence, {"schema_version", "step_id", "txid", "receipt_record_hash",
        "reconciliation_hash", "observed_at", "prior_block_number", "prior_block_id",
        "source_hash"}, "GraphExecutionExceptionV1")
    if evidence["schema_version"] != EXCEPTION_VERSION:
        raise MachineError("unsupported graph execution exception")
    step = _step(ledger, evidence["step_id"])
    if (step["status"] != "RECONCILED" or step["txid"] != evidence["txid"]
            or step["receipt_record_hash"] != evidence["receipt_record_hash"]
            or step["reconciliation_hash"] != evidence["reconciliation_hash"]
            or step["block_number"] != evidence["prior_block_number"]
            or step["block_id"] != evidence["prior_block_id"]):
        raise MachineError("reorg does not bind a reconciled graph step")
    for key in ("receipt_record_hash", "reconciliation_hash", "source_hash"):
        if not isinstance(evidence[key], str) or re.fullmatch(r"[0-9a-f]{64}", evidence[key]) is None:
            raise MachineError("invalid reorg evidence hash")
    height, block_id = evidence["prior_block_number"], evidence["prior_block_id"]
    if (type(height) is not int or height < 0 or not isinstance(block_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", block_id) is None
            or int(block_id[:16], 16) != height):
        raise MachineError("invalid reorg block commitment")
    observed = utc(evidence["observed_at"])
    if observed < ledger["updated_at"]:
        raise MachineError("reorg evidence moved backward in time")
    evidence_hash = digest(evidence)
    existing = next((item for item in ledger["history"]
                     if item.get("exception_hash") == evidence_hash), None)
    if existing is not None:
        return ledger
    step["status"] = "REORGED"
    _invalidate_descendants(ledger, step["step_id"])
    ledger["status"] = "HALTED"
    ledger["capital_status"] = "LOCKED"
    ledger["safe_next_action"] = "REOBSERVE_ORIGINAL_TXID_AND_ACCOUNT_STATE"
    ledger["history"].append({"kind": "REORG", "step_id": step["step_id"],
        "txid": step["txid"], "exception_hash": evidence_hash, "at": observed})
    ledger["updated_at"] = observed
    return _commit(ledger)
