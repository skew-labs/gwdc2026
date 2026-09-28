"""Cherry-only historical snapshot replay for PR04 plan comparison.

The source observations are real saved public responses. The mandate, prices,
fees, exits and stress losses are synthetic assumptions. Nothing is signed,
submitted, scheduled or treated as customer guidance.
"""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from economic_machine.mandate import draft_hash, normalize_mandate, policy_hash
from economic_machine.plan_compiler import (
    REQUEST_VERSION, compare_plans, compile_plan_intent,
    verify_comparison, verify_plan_intent,
)
from economic_machine.snapshot_assembly import SnapshotAssembler
from economic_machine.tron_cashflow import rate
from economic_machine.values import canonical


def _quote(name):
    value = {"product_id": name, "principal": "1000",
             "prices_base": {"USDT": "1", "USDD": "0.99", "TRX": "0.25"},
             "costs_base": {"entry": "1", "exit": "1", "conversion": "0",
                            "network": "1"},
             "redemption_seconds": 30, "reward_claim_seconds": 0,
             "reward_haircut_bps": 2500, "exit_tranches": None,
             "native": None, "vault": None, "resources": None}
    if name == "justlend.strx":
        value["exit_tranches"] = [{"amount": "1000", "after_seconds": None}]
    elif name == "tron.native.stake":
        value["native"] = {"voting_rate": rate("0.04", "SIMPLE_APR"),
                           "lock_remaining_seconds": 3600,
                           "resource_recovery_seconds": 0,
                           "window": None, "rental": None}
    elif name.startswith("usdd.vault."):
        value["principal"] = "8000"
        value["vault"] = {"deployed_usdd": "1000", "stored_debt_usdd": "1000",
                          "fee_age_seconds": 0, "fee_convention": "EFFECTIVE_APY",
                          "destination_id": "justlend.v1.jUSDD",
                          "destination_redemption_seconds": 30,
                          "collateral_release_seconds": 10,
                          "shocks": [{"name": "market", "collateral_change_bps": -100,
                                      "usdd_change_bps": 100,
                                      "deployment_loss_bps": 500}]}
    return value


def _template(name, daily, stress):
    return {"product_id": name, "max_bps": 4000, "current_bps": 0,
            "daily_loss_bps": daily, "stress_loss_bps": {"market": stress},
            "quote": _quote(name)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if root != Path("/srv/skew/gwdc-financial-agent-20260924"):
        raise SystemExit("Run verification only in the authorized Cherry project")
    source, output = args.snapshot.resolve(), args.output.resolve()
    if not source.is_relative_to(root / "data/verification") or source.stat().st_size > 16000000:
        raise SystemExit("Bounded prior project snapshot required")
    if not output.is_relative_to(root / "data/verification") or output.exists():
        raise SystemExit("Use a new project verification directory")
    assembler = SnapshotAssembler(json.loads(
        (root / "config/tron_product_registry.json").read_text()))
    snapshot = assembler.verify(json.loads(source.read_text()))
    at = snapshot["as_of"]
    start = datetime.fromisoformat(at)

    raw = json.loads((root / "cases/economic_mandate_demo.json").read_text())
    raw["scope"]["network"] = snapshot["network"]
    terms = raw["terms"]
    terms.update(effective_at=at, expires_at=(start + timedelta(hours=1)).isoformat(),
                 withdrawals=[], price_exposure_caps_bps={"TRX": 5000, "USDD": 5000},
                 protocol_caps_bps={"justlend": 7000, "usdd": 5000,
                                    "tron-native": 5000})
    terms["borrowing"].update(consent=False,
                              max_debt={"asset": "USDT", "amount": "0"})
    terms["allowed_actions"] += ["STAKE"]
    proof = "SYNTHETIC_PLAN_VERIFICATION " + json.dumps(terms, sort_keys=True)
    raw["source_messages"] = [{"message_id": "synthetic-plan-verification",
                               "text": proof}]
    raw["source_refs"] = {key: [{"message_id": "synthetic-plan-verification",
                                 "quote": proof}] for key in terms}
    record = {"mandate": normalize_mandate(raw), "status": "CONFIRMED",
              "draft_hash": draft_hash(raw), "policy_hash": policy_hash(raw)}
    request = {"schema_version": REQUEST_VERSION,
               "snapshot_hash": snapshot["snapshot_hash"], "grid_step_bps": 2000,
               "min_plan_distance_bps": 2000, "max_plan_age_seconds": 300,
               "scenarios": ["market"], "product_templates": [
                   _template("justlend.v1.jUSDT", 25, 100),
                   _template("justlend.v1.jUSDD", 100, 400),
                   _template("usdd.vault.TRX-A", 200, 1000),
               ]}
    comparison = compare_plans(record, snapshot, request, assembler=assembler, at=at)
    if comparison["status"] != "COMPARISON_READY" or not verify_comparison(
            comparison, record, snapshot, request, assembler=assembler, at=at):
        raise SystemExit("comparison did not produce two replayable plans")
    valid_until = (start + timedelta(minutes=4)).isoformat()
    intent = compile_plan_intent(
        comparison, record, snapshot, request, selected_plan="GROWTH",
        assembler=assembler, at=at, valid_until=valid_until)
    if not verify_plan_intent(intent, comparison, record, snapshot, request,
                              assembler=assembler, at=at):
        raise SystemExit("intent replay failed")

    output.mkdir(parents=True)
    for name, value in (("conditions.json", record), ("request.json", request),
                        ("comparison.json", comparison), ("intent.json", intent)):
        (output / name).write_bytes(canonical(value))
    summary = {"verified_at": datetime.now(timezone.utc).isoformat(),
               "source_snapshot_as_of": at,
               "source_snapshot_hash": snapshot["snapshot_hash"],
               "evidence_kind": "HISTORICAL_PUBLIC_CAPTURE_REPLAY_WITH_SYNTHETIC_CONDITIONS",
               "comparison_hash": comparison["comparison_hash"],
               "intent_hash": intent["intent_hash"],
               "enumerated_candidates": comparison["enumerated_candidates"],
               "eligible_candidates": comparison["eligible_candidates"],
               "complete_enumeration": comparison["complete_enumeration"],
               "vault_exclusion_reasons": comparison["product_exclusions"]["usdd.vault.TRX-A"],
               "plans": [{key: plan[key] for key in
                          ("name", "weights_bps", "cash_amount", "net_income_base",
                           "daily_loss_base", "worst_stress_loss_base", "plan_hash")}
                         for plan in comparison["plans"]],
               "intent_status": intent["status"], "intent_blockers": intent["blockers"],
               "execution_authority": intent["execution_authority"],
               "signature_status": intent["signature_status"],
               "chain_status": intent["chain_status"]}
    (output / "replay-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
