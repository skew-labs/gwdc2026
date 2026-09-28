"""Build the PR10 customer replay from a verified historical TRON snapshot.

Both condition runs use the Economic Machine.  Qwen is deliberately not called,
and the output keeps token, latency and energy measurements null.
"""

import argparse
import json
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from economic_machine.mandate import draft_hash, normalize_mandate, policy_hash
from economic_machine.plan_compiler import (
    REQUEST_VERSION,
    compare_plans,
    verify_comparison,
)
from economic_machine.snapshot_assembly import SnapshotAssembler
from economic_machine.values import canonical, decstr, digest
from finance_service.product_workspace import VERSION, normalize_product_story


def quote(name):
    value = {"product_id": name, "principal": "1000",
        "prices_base": {"USDT": "1", "USDD": "0.99", "TRX": "0.25"},
        "costs_base": {"entry": "1", "exit": "1", "conversion": "0", "network": "1"},
        "redemption_seconds": 30, "reward_claim_seconds": 0,
        "reward_haircut_bps": 2500, "exit_tranches": None, "native": None,
        "vault": None, "resources": None}
    if name.startswith("usdd.vault."):
        value["principal"] = "8000"
        value["vault"] = {"deployed_usdd": "1000", "stored_debt_usdd": "1000",
            "fee_age_seconds": 0, "fee_convention": "EFFECTIVE_APY",
            "destination_id": "justlend.v1.jUSDD", "destination_redemption_seconds": 30,
            "collateral_release_seconds": 10,
            "shocks": [{"name": "market", "collateral_change_bps": -100,
                         "usdd_change_bps": 100, "deployment_loss_bps": 500}]}
    return value


def template(name, daily, stress):
    return {"product_id": name, "max_bps": 4000, "current_bps": 0,
        "daily_loss_bps": daily, "stress_loss_bps": {"market": stress},
        "quote": quote(name)}


def confirmed_record(root, snapshot, immediate_cash_bps):
    raw = json.loads((root / "cases/economic_mandate_demo.json").read_text())
    raw["scope"]["network"] = snapshot["network"]
    raw["revision"] = 1 if immediate_cash_bps == 3000 else 2
    terms = raw["terms"]
    start = datetime.fromisoformat(snapshot["as_of"])
    terms.update(effective_at=snapshot["as_of"],
        expires_at=(start + timedelta(hours=1)).isoformat(), withdrawals=[],
        immediate_cash={"kind": "BPS", "value": immediate_cash_bps},
        price_exposure_caps_bps={"TRX": 5000, "USDD": 5000},
        protocol_caps_bps={"justlend": 7000, "usdd": 5000, "tron-native": 5000})
    terms["borrowing"].update(consent=False,
                               max_debt={"asset": "USDT", "amount": "0"})
    proof = ("SYNTHETIC PR10 REPLAY CONDITION: immediate cash "
             + str(immediate_cash_bps) + " bps; borrowing not consented. "
             + json.dumps(terms, sort_keys=True))
    raw["source_messages"] = [{"message_id": "pr10-condition-" + str(immediate_cash_bps),
                               "text": proof}]
    raw["source_refs"] = {key: [{"message_id": raw["source_messages"][0]["message_id"],
                                 "quote": proof}] for key in terms}
    return {"mandate": normalize_mandate(raw), "status": "CONFIRMED",
            "draft_hash": draft_hash(raw), "policy_hash": policy_hash(raw)}


def projection(plan, capital):
    return {"name": plan["name"], "plan_hash": plan["plan_hash"],
        "cash_amount": plan["cash_amount"], "net_income_base": plan["net_income_base"],
        "total_cost_base": plan["total_cost_base"],
        "worst_stress_loss_base": plan["worst_stress_loss_base"],
        "weights": [{"product_id": product_id, "bps": bps,
                     "amount_base": decstr(capital * Decimal(bps) / 10000)}
                    for product_id, bps in sorted(plan["weights_bps"].items()) if bps],
        "exit_summary": "30초 환매 가정을 사용한 historical replay; 실행 전 재조회 필요",
        "vault_summary": ("USDD Vault 배분 포함" if plan["weights_bps"].get(
            "usdd.vault.TRX-A") else "차입 미동의로 USDD Vault 부채 경로 제외")}


def input_fact(comparison, product_id, suffix):
    for plan in comparison["plans"]:
        for cashflow in plan["cashflows"]:
            if cashflow["product_id"] != product_id:
                continue
            for key, value in cashflow["inputs"].items():
                if key.endswith(suffix):
                    return value
    return None


def artifact(root, name, path, status="VERIFIED_REPLAY"):
    raw = (root / path).read_bytes()
    return {"name": name, "sha256": __import__("hashlib").sha256(raw).hexdigest(),
            "status": status}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--story", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = args.snapshot.resolve()
    if not source.is_file() or source.stat().st_size > 16_000_000:
        raise SystemExit("bounded verified snapshot required")
    assembler = SnapshotAssembler(json.loads(
        (root / "config/tron_product_registry.json").read_text()))
    snapshot = assembler.verify(json.loads(source.read_text()))
    request = {"schema_version": REQUEST_VERSION, "snapshot_hash": snapshot["snapshot_hash"],
        "grid_step_bps": 2000, "min_plan_distance_bps": 2000,
        "max_plan_age_seconds": 300, "scenarios": ["market"],
        "product_templates": [template("justlend.v1.jUSDT", 25, 100),
                              template("justlend.v1.jUSDD", 100, 400),
                              template("usdd.vault.TRX-A", 200, 1000)]}
    records = [confirmed_record(root, snapshot, value) for value in (3000, 7000)]
    comparisons = [compare_plans(record, snapshot, request, assembler=assembler,
                                 at=snapshot["as_of"]) for record in records]
    if any(item["status"] != "COMPARISON_READY" or not verify_comparison(
            item, record, snapshot, request, assembler=assembler, at=snapshot["as_of"])
           for item, record in zip(comparisons, records, strict=True)):
        raise SystemExit("condition run did not produce two replayable plans")
    if comparisons[0]["comparison_hash"] == comparisons[1]["comparison_hash"]:
        raise SystemExit("condition change did not alter comparison")
    selected = comparisons[0]["plans"][1]
    capital = Decimal(records[0]["mandate"]["terms"]["capital"][0]["amount"])
    captures = {item["source_id"]: item for item in snapshot["captures"]}
    products = []
    descriptions = {
        "justlend.v1.jUSDT": ("JustLend USDT Supply", "LENDING_SUPPLY", "justlend",
            "즉시 유동성 조건 아래 공급 후보", "30초 환매 가정; 실제 quote 필요",
            "금리 변동, 시장 현금, 스마트컨트랙트 위험"),
        "justlend.v1.jUSDD": ("JustLend USDD Supply", "LENDING_SUPPLY", "justlend",
            "보상 haircut을 적용한 성장안 후보", "30초 환매 가정; 실제 quote 필요",
            "인센티브 의존, USDD 가격, 시장 현금 위험"),
        "usdd.vault.TRX-A": ("USDD Vault TRX-A", "COLLATERAL_DEBT", "usdd",
            "차입 미동의이므로 담보·부채 경로 제외", "부채 상환 뒤 담보 해제 가정",
            "stability fee, 청산, 담보 가격, 발행 USDD 운용 위험")}
    for product_id, detail in descriptions.items():
        fact = input_fact(comparisons[0], product_id, ".supply_apy")
        source_id = fact["source_id"] if fact else next((key for key in captures
            if "vault" in key.lower()), "justlend_contracts")
        capture = captures[source_id]
        included = any(plan["weights_bps"].get(product_id, 0)
                       for plan in comparisons[0]["plans"])
        products.append({"product_id": product_id, "name": detail[0], "kind": detail[1],
            "protocol": detail[2], "decision": "INCLUDED" if included else "EXCLUDED",
            "rationale": detail[3],
            "rate": {"value": None if fact is None else fact["value"],
                     "unit": "annual_fraction", "status": "UNAVAILABLE" if fact is None
                     else "OBSERVED"},
            "liquidity": detail[4], "risk": detail[5],
            "source": {"source_id": source_id,
                       "observed_at": None if fact is None else fact["observed_at"],
                       "capture_hash": capture["capture_hash"],
                       "state_eligible": False if fact is None else fact["state_eligible"]}})
    usage = {"provider": "furiosa-kiln", "model_id": "qwen3-32b", "status": "NOT_RUN",
             "prompt_tokens": None, "completion_tokens": None, "latency_ms": None,
             "energy_joules": None}
    artifacts = [
        artifact(root, "PR04 deterministic comparison", "artifacts/pr04/replay-summary.json"),
        artifact(root, "PR05 Qwen boundary", "artifacts/pr05/verification-summary.json",
                 "LIVE_PROVIDER_NOT_MET"),
        artifact(root, "PR07 approval boundary", "artifacts/pr07/verification-summary.json"),
        artifact(root, "PR08 reconciliation boundary", "artifacts/pr08/verification-summary.json"),
        artifact(root, "PR09 hosted service", "artifacts/pr09/verification-summary.json"),
    ]
    start = datetime.fromisoformat(snapshot["as_of"])
    story = {"schema_version": VERSION, "story_id": "pr10-replay-demo",
        "scope": records[0]["mandate"]["scope"], "revision": 1,
        "mode": "HISTORICAL_REPLAY", "status": "READY",
        "created_at": snapshot["as_of"], "valid_until": (start + timedelta(minutes=4)).isoformat(),
        "headline": "10,000 USDT를 조건에 맞춰 비교한 기록",
        "mandate": {"asset": "USDT", "amount": "10000", "liquid_reserve": "3000",
            "horizon_days": 30, "risk_profile": "cautious", "borrowing_consent": False,
            "withdrawal_summary": "즉시 30% 보유; 조건 B는 70%로 변경",
            "policy_hash": records[0]["policy_hash"]},
        "runs": [{"run_id": name, "condition": condition,
            "mandate_policy_hash": record["policy_hash"],
            "comparison_hash": comparison["comparison_hash"],
            "plan_hashes": [plan["plan_hash"] for plan in comparison["plans"]],
            "selected_plan_hash": comparison["plans"][1]["plan_hash"],
            "model_usage": deepcopy(usage), "calculation_mode": "DETERMINISTIC_REPLAY"}
            for name, condition, record, comparison in zip(
                ("A", "B"),
                ("즉시 현금 30%, 차입 미동의", "즉시 현금 70%, 차입 미동의"),
                records, comparisons, strict=True)],
        "products": products,
        "plans": [projection(plan, capital) for plan in comparisons[0]["plans"]],
        "approval": {"status": "UNAVAILABLE", "plan_hash": selected["plan_hash"],
            "approval_hash": None, "prepared_at": None, "expires_at": None,
            "execution_path": None, "wallet": records[0]["mandate"]["scope"]["wallet"],
            "network": snapshot["network"], "amount": selected["principal_base"],
            "asset": "USDT", "fee_limit_sun": None, "target": None,
            "recipient": records[0]["mandate"]["scope"]["wallet"],
            "signature_status": "NOT_REQUESTED", "chain_status": "NOT_SUBMITTED",
            "txid": None, "reason_codes": ["HISTORICAL_REPLAY", "LIVE_QUOTE_REQUIRED",
                "WALLET_SESSION_REQUIRED", "LIVE_CAPABILITY_REQUIRED"]},
        "positions": [],
        "performance": {"status": "NOT_STARTED",
            "expected_net_income": selected["net_income_base"], "actual_net_income": None,
            "fees": None, "external_net_flows": None, "measurement_status": "NOT_MEASURED"},
        "employees": [
            {"role": "ALPHA", "name": "ALPHA", "status": "PAUSED",
             "responsibility": "조건과 배분안 갱신", "next_action": "실제 Qwen 연결 필요"},
            {"role": "VAULT", "name": "VAULT", "status": "HELD",
             "responsibility": "승인과 자본 상태 관리", "next_action": "live quote와 지갑 세션 필요"},
            {"role": "WATCH", "name": "WATCH", "status": "PAUSED",
             "responsibility": "시장·포지션·TTL 감시", "next_action": "historical snapshot은 감시하지 않음"}],
        "evidence": {"snapshot_hash": snapshot["snapshot_hash"],
            "snapshot_as_of": snapshot["as_of"], "artifacts": artifacts,
            "tron_b_status": "PARTIAL_NO_LIVE_EXECUTION",
            "furiosa_a_status": "NOT_MET_NO_ACTUAL_QWEN_TRACE",
            "live_execution_status": "NOT_RUN"},
        "blockers": ["ACTUAL_QWEN_TRACE_MISSING", "LIVE_QUOTE_MISSING",
            "WALLET_SESSION_MISSING", "TESTNET_TRANSACTION_MISSING",
            "USDD_VAULT_EXECUTION_CAPABILITY_UNVERIFIED"],
        "execution_authority": "NONE"}
    story["story_hash"] = digest({"domain": VERSION, "story": story})
    story = normalize_product_story(story)
    evidence = {"schema_version": "dual-track-evidence-manifest-1",
        "story_hash": story["story_hash"], "snapshot_hash": snapshot["snapshot_hash"],
        "run_a_comparison_hash": comparisons[0]["comparison_hash"],
        "run_b_comparison_hash": comparisons[1]["comparison_hash"],
        "condition_change_altered_comparison": True,
        "qwen_actual_calls": 0, "qwen_token_usage": None, "qwen_latency_ms": None,
        "energy_joules": None, "customer_signatures": 0, "broadcasts": 0,
        "actual_transactions": 0, "tron_b_acceptance": "NOT_MET",
        "furiosa_a_acceptance": "NOT_MET", "ui_evidence": "PENDING_BROWSER_VERIFICATION",
        "artifacts": artifacts}
    args.story.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.story.write_bytes(canonical(story))
    args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"story_hash": story["story_hash"],
        "run_a": comparisons[0]["comparison_hash"], "run_b": comparisons[1]["comparison_hash"],
        "plans": [item["name"] for item in story["plans"]]}, indent=2))


if __name__ == "__main__":
    main()
