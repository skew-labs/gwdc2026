"""Independent, exact allocation audit. It never reads an episode's target label.

The oracle proves constraint violations and preserves missing evidence. A pass
on synthetic rules is not an optimal allocation, a real gold label, or trade
authority. Real-source drafts remain excluded until source rights and the
required evidence for each claimed label are verified.
"""

import hashlib
import json
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from finagent.store import Store

from .validate_v3 import exact, instant, money


ORACLE_VERSION = "allocation-constraints-1.0.0"
REAL_MARKET_MAX_AGE = timedelta(seconds=900)


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode()).hexdigest()


def _item(code: str, plan_id: str | None = None, subject: str | None = None,
          **details: str) -> dict:
    return {"code": code, "plan_id": plan_id, "subject": subject,
            "details": details}


@lru_cache(maxsize=1)
def _proposal_validator():
    from jsonschema import Draft202012Validator, FormatChecker
    path = Path(__file__).resolve().parents[2] / "contracts/allocation_proposal_v1.schema.json"
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")),
                                  format_checker=FormatChecker())


@lru_cache(maxsize=1)
def _verdict_validator():
    from jsonschema import Draft202012Validator
    path = Path(__file__).resolve().parents[2] / "contracts/allocation_oracle_v1.schema.json"
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def validate_verdict(result: dict) -> None:
    _verdict_validator().validate(result)


def _real_source_check(store: Store | None, candidate: dict, evidence: dict,
                       as_of) -> tuple[list[dict], dict | None]:
    """Check the current market facts against their archived raw response."""
    cid = candidate["id"]
    if store is None:
        return [_item("SOURCE_PROOF_NOT_LOADED", subject=cid)], None
    with store.connect() as db:
        snapshot = db.execute("SELECT * FROM snapshots WHERE id=?",
                              (candidate["source_ref"],)).fetchone()
    if snapshot is None or snapshot["source_id"] != "justlend_markets_v1":
        return [_item("SOURCE_SNAPSHOT_NOT_FOUND", subject=cid)], None
    fetched = instant(snapshot["fetched_at"], "snapshot fetched_at")
    if fetched > as_of or as_of - fetched > REAL_MARKET_MAX_AGE:
        return ([_item("SOURCE_SNAPSHOT_OUT_OF_TIME", subject=cid,
                       fetched_at=snapshot["fetched_at"])], None)
    try:
        payload = store.payload_for(snapshot)
        markets = [row for row in store.markets_for(snapshot["id"])
                   if row["market_address"] == cid]
        if len(markets) != 1:
            raise ValueError("market identity missing")
        market = markets[0]
        for key in ("status", "underlying_address", "underlying_decimals"):
            if candidate.get(key) != market[key]:
                raise ValueError("market identity changed")
        facts = {(fact["subject_id"], fact["metric_id"]): fact
                 for fact in store.facts_for(snapshot["id"])}
        source_evidence = [item for item in evidence.values()
                           if item["id"] in candidate["evidence_ids"]]
        locators = {}
        for metric, field in (("supply_apy", candidate["supply_apy"]),
                              ("available_cash", candidate["available_cash"]["value"])):
            fact = facts.get((cid, metric))
            if fact is None or fact["quality"] not in {"VALID", "VALID_ZERO"}:
                raise ValueError("market fact unavailable")
            store.verify_fact(payload, fact)
            if fact["canonical_value"] != field or not any(
                item["source_ref"] == snapshot["id"]
                and item["locator"] == fact["json_path"] for item in source_evidence
            ):
                raise ValueError("market fact or evidence changed")
            locators[metric] = fact["json_path"]
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return [_item("SOURCE_FACT_MISMATCH", subject=cid,
                      error_type=type(exc).__name__)], None
    return [], {"candidate_id": cid, "snapshot_id": snapshot["id"],
                "raw_sha256": snapshot["sha256"],
                "fetched_at": snapshot["fetched_at"], "fact_locators": locators}


def judge(episode: dict, proposal: dict, *, store: Store | None = None) -> dict:
    """Judge proposed allocations without reading episode['target'].

    Only synthetically generated cases may provide constraint-training labels.
    Real API cases remain excluded from training under the current contract;
    machine-proven constraints do not establish an optimal allocation label.
    """
    if episode.get("schema_version") != "3.0.0":
        raise ValueError("V3 episode required")
    need = episode["need"]
    as_of = instant(episode["as_of"], "as_of")
    unit = need["budget"]["unit"]
    budget = money(need["budget"], unit, "budget")
    immediate = money(need["withdrawal"]["min_immediate"], unit, "immediate")
    total_fraction = exact(need["risk"]["total_fraction"], "total risk")
    shared_fraction = exact(need["risk"]["shared_group_fraction"], "shared risk")
    if (budget <= 0 or immediate > budget or total_fraction > 1
            or shared_fraction > 1):
        raise ValueError("invalid episode need")
    candidates = episode["candidates"]
    by_id = {item["id"]: item for item in candidates}
    evidence = {item["id"]: item for item in episode["evidence"]}
    if len(by_id) != len(candidates) or len(evidence) != len(episode["evidence"]):
        raise ValueError("duplicate episode identity")
    real = episode["source_mode"] == "api_observation"
    if not real and episode["source_mode"] != "synthetic_fixture":
        raise ValueError("unknown episode source mode")
    if real and episode["rights_status"] != "unknown":
        raise ValueError("unexpected real source rights status")
    if not real and (episode["rights_status"] != "synthetic_generated"
                     or any(item["status"] not in {"synthetic", "legacy"}
                            or not item["source_ref"].startswith("fixture:")
                            for item in candidates)):
        raise ValueError("synthetic episode provenance mismatch")
    failures: list[dict] = []
    unknowns: list[dict] = []
    source_witnesses: dict[str, dict] = {}
    plan_results = []
    errors = sorted(_proposal_validator().iter_errors(proposal), key=lambda e: str(e.path))
    if errors:
        failures.append(_item("PROPOSAL_SCHEMA_INVALID", detail=errors[0].message[:200]))
    else:
        plan_ids = [item["plan_id"] for item in proposal["plans"]]
        if len(set(plan_ids)) != len(plan_ids):
            failures.append(_item("DUPLICATE_PLAN_ID"))
        if real:
            unknowns.extend((
                _item("REAL_DECISION_GOLD_UNREVIEWED"),
                _item("SOURCE_RIGHTS_UNVERIFIED"),
                _item("WITHDRAWAL_TERMS_UNVERIFIED"),
                _item("COST_AND_EXECUTION_UNVERIFIED"),
                _item("RISK_GRAPH_UNVERIFIED"),
            ))
            if episode.get("need_origin") != "user_confirmed":
                unknowns.append(_item("USER_NEED_UNCONFIRMED"))
        for plan in proposal["plans"]:
            pid = plan["plan_id"]
            start_fail, start_unknown = len(failures), len(unknowns)
            allocations = {cid: exact(value, "allocation")
                           for cid, value in plan["allocations"].items()}
            hold = exact(plan["hold"], "hold")
            invested = sum(allocations.values(), Decimal(0))
            if invested <= 0:
                failures.append(_item("EMPTY_ALLOCATION", pid))
            if invested + hold != budget:
                failures.append(_item("BUDGET_NOT_CONSERVED", pid,
                                      observed=format(invested + hold, "f"),
                                      required=format(budget, "f")))
            if hold < immediate:
                failures.append(_item("IMMEDIATE_RESERVE_SHORTFALL", pid,
                                      observed=format(hold, "f"),
                                      required=format(immediate, "f")))
            total_cap = budget * total_fraction
            if invested > total_cap:
                failures.append(_item("TOTAL_RISK_CAP_EXCEEDED", pid,
                                      observed=format(invested, "f"),
                                      limit=format(total_cap, "f")))
            if len(set(plan["evidence_ids"])) != len(plan["evidence_ids"]):
                failures.append(_item("DUPLICATE_PLAN_EVIDENCE", pid))
            if len(set(plan["dependency_ids"])) != len(plan["dependency_ids"]):
                failures.append(_item("DUPLICATE_DEPENDENCY", pid))
            required_dependencies = {"budget", "source_freshness",
                                     "withdrawal_deadline", "withdrawal_notice"}
            for dependency in sorted(required_dependencies - set(plan["dependency_ids"])):
                failures.append(_item("MISSING_REQUIRED_DEPENDENCY", pid, dependency))
            for eid in plan["evidence_ids"]:
                if eid not in evidence:
                    failures.append(_item("UNKNOWN_PLAN_EVIDENCE", pid, eid))
            exposure = defaultdict(Decimal)
            for cid, amount in allocations.items():
                candidate = by_id.get(cid)
                if candidate is None:
                    failures.append(_item("UNKNOWN_CANDIDATE", pid, cid))
                    continue
                if amount == 0:
                    continue
                if candidate["asset"] != unit:
                    failures.append(_item("ASSET_UNIT_MISMATCH", pid, cid))
                if candidate["status"] not in {"active", "synthetic"}:
                    failures.append(_item("MARKET_NOT_ACTIVE", pid, cid))
                cash = money(candidate["available_cash"], candidate["asset"], "market cash")
                if amount > cash:
                    failures.append(_item("MARKET_CASH_EXCEEDED", pid, cid,
                                          observed=format(amount, "f"),
                                          limit=format(cash, "f")))
                if not set(candidate["evidence_ids"]).issubset(plan["evidence_ids"]):
                    failures.append(_item("CANDIDATE_EVIDENCE_MISSING", pid, cid))
                for eid in candidate["evidence_ids"]:
                    item = evidence.get(eid)
                    if item is None or item["source_ref"] != candidate["source_ref"]:
                        failures.append(_item("CANDIDATE_EVIDENCE_BAD_REF", pid, cid))
                observed = candidate["observed_at"]
                valid_until = candidate["valid_until"]
                if observed is None or valid_until is None:
                    unknowns.append(_item("SOURCE_TIME_UNKNOWN", pid, cid))
                elif instant(observed, "observed_at") > as_of or instant(valid_until, "valid_until") < as_of:
                    failures.append(_item("SOURCE_OUT_OF_TIME", pid, cid))
                deadline = need["withdrawal"]["latest_day"]
                notice = need["withdrawal"]["max_notice_days"]
                lock_days = candidate["lock_days"]
                notice_days = candidate["notice_days"]
                if None in (deadline, notice, lock_days, notice_days):
                    unknowns.append(_item("WITHDRAWAL_SCHEDULE_UNKNOWN", pid, cid))
                elif lock_days > deadline or notice_days > notice:
                    failures.append(_item("WITHDRAWAL_SCHEDULE_VIOLATION", pid, cid))
                for group in candidate["risk_groups"]:
                    exposure[group] += amount
                if real:
                    source_issues, witness = _real_source_check(store, candidate, evidence, as_of)
                    failures.extend(_item(item["code"], pid, item["subject"],
                                          **item["details"])
                                    for item in source_issues
                                    if item["code"] != "SOURCE_PROOF_NOT_LOADED")
                    if witness is not None:
                        source_witnesses[cid] = witness
                    if any(item["code"] == "SOURCE_PROOF_NOT_LOADED" for item in source_issues):
                        unknowns.append(_item("SOURCE_PROOF_NOT_LOADED", pid, cid))
            group_cap = budget * shared_fraction
            for group, amount in sorted(exposure.items()):
                if amount > group_cap:
                    failures.append(_item("SHARED_RISK_CAP_EXCEEDED", pid, group,
                                          observed=format(amount, "f"),
                                          limit=format(group_cap, "f")))
            plan_results.append({"plan_id": pid, "invested": format(invested, "f"),
                                 "hold": format(hold, "f"),
                                 "failure_codes": [item["code"] for item in failures[start_fail:]],
                                 "unknown_codes": [item["code"] for item in unknowns[start_unknown:]]})
    verdict = ("REJECTED" if failures else "NEEDS_EVIDENCE" if unknowns
               else "CONSTRAINTS_PASS")
    return {"schema_version": "1.0.0", "oracle_version": ORACLE_VERSION,
            "episode_id": episode["episode_id"],
            "episode_sha256": _digest({key: value for key, value in episode.items()
                                       if key != "target"}),
            "proposal_sha256": _digest(proposal), "verdict": verdict,
            "hard_failures": failures, "unknowns": unknowns,
            "plan_results": plan_results,
            "source_witnesses": [source_witnesses[key] for key in sorted(source_witnesses)],
            "training_scope": "excluded_unreviewed" if real else "synthetic_constraint_only",
            "human_gold": False, "optimality_proven": False,
            "economic_net_estimate": None, "product_actionable": False}


def _need_leaves(value: dict, prefix: str = "need") -> dict:
    result = {}
    for key, item in value.items():
        path = prefix + "." + key
        if isinstance(item, dict):
            result.update(_need_leaves(item, path))
        else:
            result[path] = item
    return result


def judge_counterfactual_pair(base_episode: dict, changed_episode: dict,
                              base_proposal: dict, changed_proposal: dict,
                              *, store: Store | None = None) -> dict:
    """Audit one-field need changes without assuming every change needs a new plan."""
    if (base_episode["scenario_family"] != changed_episode["scenario_family"]
            or any(base_episode[key] != changed_episode[key]
                   for key in ("as_of", "candidates", "evidence", "source_mode", "rights_status"))):
        raise ValueError("counterfactual pair changes market context")
    before = _need_leaves(base_episode["need"])
    after = _need_leaves(changed_episode["need"])
    if before.keys() != after.keys():
        raise ValueError("counterfactual need shape changed")
    changed = [key for key in before if before[key] != after[key]]
    if len(changed) != 1:
        raise ValueError("counterfactual must change exactly one need field")
    first = judge(base_episode, base_proposal, store=store)
    second = judge(changed_episode, changed_proposal, store=store)
    if first["verdict"] == "REJECTED":
        verdict = "BASE_PROPOSAL_REJECTED"
    elif second["verdict"] == "REJECTED":
        verdict = "CHANGED_PROPOSAL_REJECTED"
    elif "NEEDS_EVIDENCE" in {first["verdict"], second["verdict"]}:
        verdict = "NEEDS_EVIDENCE"
    else:
        verdict = "BOTH_CONSTRAINTS_PASS"
    return {"schema_version": "1.0.0", "oracle_version": ORACLE_VERSION,
            "scenario_family": base_episode["scenario_family"],
            "changed_field": changed[0], "proposal_changed":
                first["proposal_sha256"] != second["proposal_sha256"],
            "base_episode_sha256": first["episode_sha256"],
            "changed_episode_sha256": second["episode_sha256"],
            "base_proposal_sha256": first["proposal_sha256"],
            "changed_proposal_sha256": second["proposal_sha256"],
            "base_verdict": first["verdict"],
            "changed_verdict": second["verdict"],
            "pair_verdict": verdict,
            "changed_failure_codes": [item["code"] for item in second["hard_failures"]],
            "optimality_proven": False, "human_gold": False,
            "product_actionable": False}
