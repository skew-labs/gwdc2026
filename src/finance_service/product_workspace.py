"""Verified projections for the customer workspace and its review decisions.

The workspace is a view over already committed financial artifacts.  It does
not let the browser invent a plan, approval, signature, receipt, or measured
model usage.  Historical replay remains a separate mode from live execution.
"""

from copy import deepcopy
from datetime import datetime

from economic_machine.mandate import normalize_scope
from economic_machine.values import (
    MachineError,
    decimal,
    digest,
    ident,
    require_keys,
    utc,
)

VERSION = "finance-product-story-1"
MODES = {"HISTORICAL_REPLAY", "SIMULATION", "LIVE"}
ROLES = {"ALPHA", "VAULT", "WATCH"}
_STORY_KEYS = {"schema_version", "story_id", "scope", "revision", "mode", "status",
               "created_at", "valid_until", "headline", "mandate", "runs", "products",
               "plans", "approval", "positions", "performance", "employees", "evidence",
               "blockers", "execution_authority", "story_hash"}


def _exact(value, keys, label):
    require_keys(value, set(keys), label)
    return value


def _text(value, label, *, maximum=500):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise MachineError("invalid " + label)
    return value


def _hash(value, label, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or len(value) != 64 or any(
            char not in "0123456789abcdef" for char in value):
        raise MachineError("invalid " + label)
    return value


def _nullable_measure(value, label):
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise MachineError("invalid " + label)
    return value


def _usage(raw):
    keys = {"provider", "model_id", "status", "prompt_tokens", "completion_tokens",
            "latency_ms", "energy_joules"}
    _exact(raw, keys, "ProductModelUsage")
    status = raw["status"]
    if status not in {"ACTUAL", "NOT_RUN", "UNAVAILABLE"}:
        raise MachineError("invalid product model usage status")
    result = {"provider": raw["provider"], "model_id": raw["model_id"], "status": status}
    if raw["provider"] is not None:
        result["provider"] = ident(raw["provider"], "model provider")
    if raw["model_id"] is not None:
        result["model_id"] = ident(raw["model_id"], "model id")
    for key in ("prompt_tokens", "completion_tokens", "latency_ms", "energy_joules"):
        result[key] = _nullable_measure(raw[key], key)
    measured = [result[key] is not None for key in
                ("prompt_tokens", "completion_tokens", "latency_ms")]
    if status == "ACTUAL" and not all(measured):
        raise MachineError("actual model usage requires token and latency measurements")
    if status != "ACTUAL" and any(measured):
        raise MachineError("unobserved model usage must remain null")
    return result


def _run(raw):
    keys = {"run_id", "condition", "mandate_policy_hash", "comparison_hash",
            "plan_hashes", "selected_plan_hash", "model_usage", "calculation_mode"}
    _exact(raw, keys, "ProductConditionRun")
    hashes = raw["plan_hashes"]
    if (not isinstance(hashes, list) or len(hashes) != 2
            or len(set(hashes)) != 2):
        raise MachineError("condition run requires two distinct plan hashes")
    hashes = [_hash(value, "condition plan hash") for value in hashes]
    selected = _hash(raw["selected_plan_hash"], "selected condition plan hash")
    if selected not in hashes:
        raise MachineError("selected condition plan is absent")
    return {"run_id": ident(raw["run_id"], "condition run"),
        "condition": _text(raw["condition"], "condition summary"),
        "mandate_policy_hash": _hash(raw["mandate_policy_hash"], "mandate policy hash"),
        "comparison_hash": _hash(raw["comparison_hash"], "comparison hash"),
        "plan_hashes": hashes, "selected_plan_hash": selected,
        "model_usage": _usage(raw["model_usage"]),
        "calculation_mode": ident(raw["calculation_mode"], "calculation mode")}


def _product(raw):
    keys = {"product_id", "name", "kind", "protocol", "decision", "rationale",
            "rate", "liquidity", "risk", "source"}
    _exact(raw, keys, "ProductOpportunity")
    decision = raw["decision"]
    if decision not in {"INCLUDED", "EXCLUDED", "AVAILABLE"}:
        raise MachineError("invalid product decision")
    rate = _exact(raw["rate"], {"value", "unit", "status"}, "ProductRate")
    if rate["value"] is not None:
        decimal(rate["value"], signed=True)
    if rate["status"] not in {"OBSERVED", "ASSUMPTION", "UNAVAILABLE"}:
        raise MachineError("invalid product rate status")
    if rate["value"] is None and rate["status"] != "UNAVAILABLE":
        raise MachineError("missing product rate must be unavailable")
    source = _exact(raw["source"], {"source_id", "observed_at", "capture_hash",
                                    "state_eligible"}, "ProductSource")
    if type(source["state_eligible"]) is not bool:
        raise MachineError("invalid product source eligibility")
    return {"product_id": ident(raw["product_id"], "product id"),
        "name": _text(raw["name"], "product name"),
        "kind": ident(raw["kind"], "product kind"),
        "protocol": ident(raw["protocol"], "product protocol"),
        "decision": decision, "rationale": _text(raw["rationale"], "product rationale"),
        "rate": {"value": rate["value"], "unit": ident(rate["unit"], "rate unit"),
                 "status": rate["status"]},
        "liquidity": _text(raw["liquidity"], "product liquidity"),
        "risk": _text(raw["risk"], "product risk"),
        "source": {"source_id": ident(source["source_id"], "product source"),
                   "observed_at": None if source["observed_at"] is None else utc(
                       source["observed_at"]),
                   "capture_hash": _hash(source["capture_hash"], "capture hash"),
                   "state_eligible": source["state_eligible"] is True}}


def _plan(raw):
    keys = {"name", "plan_hash", "cash_amount", "net_income_base", "total_cost_base",
            "worst_stress_loss_base", "weights", "exit_summary", "vault_summary"}
    _exact(raw, keys, "ProductPlan")
    weights = raw["weights"]
    if (not isinstance(weights, list) or not weights or any(not isinstance(row, dict)
            or set(row) != {"product_id", "bps", "amount_base"} for row in weights)):
        raise MachineError("invalid product plan weights")
    seen, total = set(), 0
    normalized = []
    for row in weights:
        product_id = ident(row["product_id"], "plan product")
        if product_id in seen or type(row["bps"]) is not int or not 0 <= row["bps"] <= 10000:
            raise MachineError("invalid or duplicate product weight")
        seen.add(product_id)
        total += row["bps"]
        normalized.append({"product_id": product_id, "bps": row["bps"],
                           "amount_base": str(decimal(row["amount_base"]))})
    if total > 10000:
        raise MachineError("product plan weights exceed total capital")
    result = {"name": ident(raw["name"], "plan name"),
        "plan_hash": _hash(raw["plan_hash"], "plan hash"),
        "weights": normalized,
        "exit_summary": _text(raw["exit_summary"], "plan exit summary"),
        "vault_summary": _text(raw["vault_summary"], "plan vault summary")}
    for key in ("cash_amount", "total_cost_base", "worst_stress_loss_base"):
        result[key] = str(decimal(raw[key]))
    result["net_income_base"] = str(decimal(raw["net_income_base"], signed=True))
    return result


def normalize_product_story(raw):
    _exact(raw, _STORY_KEYS, "ProductWorkspaceStoryV1")
    if raw["schema_version"] != VERSION or raw["mode"] not in MODES:
        raise MachineError("unsupported product workspace story")
    if raw["status"] not in {"READY", "UNAVAILABLE"}:
        raise MachineError("invalid product workspace status")
    created, valid = utc(raw["created_at"]), utc(raw["valid_until"])
    if datetime.fromisoformat(created) >= datetime.fromisoformat(valid):
        raise MachineError("product workspace validity window is empty")
    if type(raw["revision"]) is not int or not 1 <= raw["revision"] < 1 << 31:
        raise MachineError("invalid product workspace revision")
    story_id = ident(raw["story_id"], "product story")
    scope = normalize_scope(raw["scope"])
    mandate = _exact(raw["mandate"], {"asset", "amount", "liquid_reserve",
        "horizon_days", "risk_profile", "borrowing_consent", "withdrawal_summary",
        "policy_hash"}, "ProductMandate")
    if type(mandate["horizon_days"]) is not int or not 1 <= mandate["horizon_days"] <= 3650:
        raise MachineError("invalid product mandate horizon")
    if type(mandate["borrowing_consent"]) is not bool:
        raise MachineError("invalid product borrowing consent")
    amount = decimal(mandate["amount"])
    reserve = decimal(mandate["liquid_reserve"])
    if amount <= 0 or reserve > amount:
        raise MachineError("invalid product mandate capital")
    runs = raw["runs"]
    if not isinstance(runs, list) or [item.get("run_id") for item in runs] != ["A", "B"]:
        raise MachineError("workspace requires ordered condition runs A and B")
    runs = [_run(item) for item in runs]
    products = raw["products"]
    if not isinstance(products, list) or not 1 <= len(products) <= 16:
        raise MachineError("bounded product opportunities required")
    products = [_product(item) for item in products]
    if not any(item["kind"] == "COLLATERAL_DEBT" and "vault" in item["product_id"]
               for item in products):
        raise MachineError("USDD Vault collateral/debt opportunity is required")
    plans = raw["plans"]
    if not isinstance(plans, list) or (raw["status"] == "READY" and len(plans) != 2):
        raise MachineError("ready workspace requires exactly two plans")
    plans = [_plan(item) for item in plans]
    if len({item["plan_hash"] for item in plans}) != len(plans):
        raise MachineError("workspace plan hashes must be distinct")
    product_ids = {item["product_id"] for item in products}
    for plan in plans:
        if any(row["product_id"] not in product_ids for row in plan["weights"]):
            raise MachineError("product plan references an absent opportunity")
        invested = sum((decimal(row["amount_base"]) for row in plan["weights"]),
                       decimal("0"))
        if any(decimal(row["amount_base"]) <= 0 or row["bps"] <= 0
               or decimal(row["amount_base"]) * 10000 != amount * row["bps"]
               for row in plan["weights"]):
            raise MachineError("product plan weight and amount disagree")
        if (decimal(plan["cash_amount"]) + invested
                + decimal(plan["total_cost_base"]) != amount):
            raise MachineError("product plan does not conserve mandate capital")
    approval = _exact(raw["approval"], {"status", "plan_hash", "approval_hash",
        "prepared_at", "expires_at", "execution_path", "wallet", "network", "amount",
        "asset", "fee_limit_sun", "target", "recipient", "signature_status",
        "chain_status", "txid", "reason_codes"}, "ProductApproval")
    if approval["status"] not in {"UNAVAILABLE", "READY"}:
        raise MachineError("invalid approval status")
    if (not isinstance(approval["reason_codes"], list)
            or any(not isinstance(item, str) or not item.strip()
                   for item in approval["reason_codes"])):
        raise MachineError("invalid approval reason codes")
    if approval["approval_hash"] is not None:
        _hash(approval["approval_hash"], "approval hash")
    if (approval["prepared_at"] is None) != (approval["expires_at"] is None):
        raise MachineError("approval validity timestamps must be paired")
    if approval["prepared_at"] is not None:
        prepared, expires = utc(approval["prepared_at"]), utc(approval["expires_at"])
        if datetime.fromisoformat(prepared) >= datetime.fromisoformat(expires):
            raise MachineError("approval validity window is empty")
    if (approval["fee_limit_sun"] is not None
            and (type(approval["fee_limit_sun"]) is not int
                 or approval["fee_limit_sun"] < 0)):
        raise MachineError("invalid approval fee limit")
    approval_amount = decimal(approval["amount"])
    if approval_amount <= 0 or approval_amount > amount:
        raise MachineError("invalid approval amount")
    ident(approval["asset"], "approval asset")
    ident(approval["network"], "approval network")
    if approval["asset"] != mandate["asset"] or approval["network"] != scope["network"]:
        raise MachineError("approval asset or network differs from mandate scope")
    if approval["wallet"] is not None and approval["wallet"] != scope["wallet"]:
        raise MachineError("approval wallet differs from authenticated scope")
    if (approval["signature_status"] not in {"NOT_REQUESTED", "REQUESTED", "SIGNED"}
            or approval["chain_status"] not in {"NOT_SUBMITTED", "SUBMITTED", "SOLID"}):
        raise MachineError("invalid approval lifecycle")
    if raw["mode"] != "LIVE" and (approval["status"] != "UNAVAILABLE"
            or approval["signature_status"] != "NOT_REQUESTED"
            or approval["chain_status"] != "NOT_SUBMITTED" or approval["txid"] is not None):
        raise MachineError("replay/simulation cannot present a live approval or transaction")
    if approval["chain_status"] != "NOT_SUBMITTED" and approval["txid"] is None:
        raise MachineError("submitted approval requires an exact txid")
    if approval["txid"] is not None:
        _hash(approval["txid"], "transaction id")
    if approval["plan_hash"] is not None and approval["plan_hash"] not in {
            item["plan_hash"] for item in plans}:
        raise MachineError("approval plan is absent from workspace")
    if approval["plan_hash"] is not None:
        selected = next(item for item in plans
                        if item["plan_hash"] == approval["plan_hash"])
        principal = sum((decimal(row["amount_base"]) for row in selected["weights"]),
                        decimal("0"))
        if approval_amount != principal:
            raise MachineError("approval amount differs from selected plan")
    if approval["status"] == "READY" and any(approval[key] is None for key in
            ("plan_hash", "approval_hash", "prepared_at", "expires_at",
             "execution_path", "wallet", "target", "recipient")):
        raise MachineError("ready approval is incomplete")
    positions = raw["positions"]
    if not isinstance(positions, list):
        raise MachineError("invalid product positions")
    performance = _exact(raw["performance"], {"status", "expected_net_income",
        "actual_net_income", "fees", "external_net_flows", "measurement_status"},
        "ProductPerformance")
    if performance["status"] not in {"NOT_STARTED", "TRACKING", "RECONCILED"}:
        raise MachineError("invalid product performance status")
    for key in ("expected_net_income", "actual_net_income", "fees", "external_net_flows"):
        if performance[key] is not None:
            decimal(performance[key], signed=True)
    employees = raw["employees"]
    if (not isinstance(employees, list) or {item.get("role") for item in employees} != ROLES
            or any(set(item) != {"role", "name", "status", "responsibility", "next_action"}
                   for item in employees)):
        raise MachineError("workspace requires ALPHA, VAULT and WATCH employees")
    if any(item["status"] not in {"ACTIVE", "PAUSED", "HELD", "FAILED"}
           for item in employees):
        raise MachineError("invalid employee status")
    for item in employees:
        ident(item["role"], "employee role")
        _text(item["name"], "employee name", maximum=80)
        _text(item["responsibility"], "employee responsibility")
        _text(item["next_action"], "employee next action")
    evidence = _exact(raw["evidence"], {"snapshot_hash", "snapshot_as_of", "artifacts",
        "tron_b_status", "furiosa_a_status", "live_execution_status"}, "ProductEvidence")
    _hash(evidence["snapshot_hash"], "workspace snapshot hash")
    utc(evidence["snapshot_as_of"])
    if (not isinstance(evidence["artifacts"], list) or any(not isinstance(item, dict)
            or set(item) != {"name", "sha256", "status"} for item in evidence["artifacts"])):
        raise MachineError("invalid workspace evidence artifacts")
    for item in evidence["artifacts"]:
        _text(item["name"], "artifact name")
        _hash(item["sha256"], "artifact hash")
        ident(item["status"], "artifact status")
    blockers = raw["blockers"]
    if not isinstance(blockers, list) or any(not isinstance(item, str) for item in blockers):
        raise MachineError("invalid workspace blockers")
    if raw["mode"] != "LIVE" and not blockers:
        raise MachineError("non-live workspace must expose its blockers")
    result = deepcopy(raw)
    result.update(story_id=story_id, scope=scope,
                  created_at=created, valid_until=valid,
                  headline=_text(raw["headline"], "workspace headline"),
                  mandate={**mandate, "asset": ident(mandate["asset"], "mandate asset"),
                           "amount": str(amount), "liquid_reserve": str(reserve),
                           "risk_profile": ident(mandate["risk_profile"], "risk profile"),
                           "withdrawal_summary": _text(mandate["withdrawal_summary"],
                                                       "withdrawal summary"),
                           "policy_hash": _hash(mandate["policy_hash"], "policy hash")},
                  runs=runs, products=products, plans=plans)
    if result["execution_authority"] != "NONE":
        raise MachineError("product workspace must not carry execution authority")
    claimed = result.pop("story_hash")
    expected = digest({"domain": VERSION, "story": result})
    if claimed != expected:
        raise MachineError("product workspace story commitment mismatch")
    result["story_hash"] = expected
    return result


class ProductWorkspaceService:
    """Scope-bound persistence and review decisions; never a signing adapter."""

    def __init__(self, repository, clock):
        self.repository = repository
        self.clock = clock

    def publish(self, context, story, *, expected_version):
        at = utc(self.clock())
        scope = context.authorize(at)
        story = normalize_product_story(story)
        if story["scope"] != scope:
            raise MachineError("workspace scope differs from authenticated context")
        return self.repository.put_record(scope, "PRODUCT_STORY", story["story_id"], story,
            expected_version=expected_version, at=at)

    def read(self, context, story_id):
        at = utc(self.clock())
        scope = context.authorize(at)
        record = self.repository.get_record(scope, "PRODUCT_STORY",
                                            ident(story_id, "product story"))
        return normalize_product_story(record["body"])

    def decide(self, context, story_id, request):
        keys = {"decision", "story_revision", "plan_hash", "approval_hash",
                "expected_decision_version"}
        _exact(request, keys, "ProductReviewDecision")
        if type(request["story_revision"]) is not int or request["story_revision"] < 1:
            raise MachineError("invalid workspace decision revision")
        plan_hash = _hash(request["plan_hash"], "workspace decision plan hash")
        approval_hash = _hash(request["approval_hash"], "workspace decision approval hash",
                              optional=True)
        if (type(request["expected_decision_version"]) is not int
                or request["expected_decision_version"] < 0):
            raise MachineError("invalid expected decision version")
        at = utc(self.clock())
        scope = context.authorize(at)
        story = self.read(context, story_id)
        if request["story_revision"] != story["revision"]:
            raise MachineError("workspace revision changed; review the current plan")
        if plan_hash not in {item["plan_hash"] for item in story["plans"]}:
            raise MachineError("review plan is absent from current workspace")
        if approval_hash != story["approval"]["approval_hash"]:
            raise MachineError("workspace approval commitment changed")
        if datetime.fromisoformat(at) >= datetime.fromisoformat(story["valid_until"]):
            raise MachineError("workspace review expired")
        decision = request["decision"]
        if decision == "ACKNOWLEDGE_REPLAY":
            if story["mode"] == "LIVE":
                raise MachineError("live workspace requires an explicit wallet review decision")
            status = "REPLAY_REVIEWED"
        elif decision == "OPEN_WALLET_REVIEW":
            approval = story["approval"]
            if (story["mode"] != "LIVE" or approval["status"] != "READY"
                    or approval_hash != approval["approval_hash"]):
                raise MachineError("wallet review is unavailable for this workspace")
            status = "AWAITING_WALLET"
        elif decision == "REJECT":
            status = "REJECTED"
        else:
            raise MachineError("unsupported product review decision")
        body = {"schema_version": "finance-product-decision-1", "story_id": story["story_id"],
            "story_revision": story["revision"], "story_hash": story["story_hash"],
            "plan_hash": plan_hash, "approval_hash": approval_hash,
            "decision": decision, "status": status, "decided_at": at,
            "session_id": context.session_id, "execution_authority": "NONE",
            "signature_status": "NOT_REQUESTED", "chain_status": "NOT_SUBMITTED"}
        stored = self.repository.put_record(scope, "USER_DECISION",
            story["story_id"] + ":r" + str(story["revision"]), body,
            expected_version=request["expected_decision_version"], at=at)
        return {"decision": stored["body"], "version": stored["version"]}
