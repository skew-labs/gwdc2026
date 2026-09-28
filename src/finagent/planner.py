"""Exact, conservative single-asset comparison. Never emits a transaction."""

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

from .contracts import Need, canonical_decimal
from .store import Store


class PlanUnavailable(ValueError):
    pass


def _age_seconds(timestamp: str, now: datetime) -> float:
    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None:
        raise PlanUnavailable("source has no timezone")
    return (now - parsed).total_seconds()


def _metric(facts: list[dict], metric_id: str) -> dict:
    rows = [row for row in facts if row["metric_id"] == metric_id]
    if len(rows) != 1 or rows[0]["quality"] not in {"VALID", "VALID_ZERO"}:
        raise PlanUnavailable(f"metric unavailable: {metric_id}")
    return rows[0]


def compare(store: Store, need: Need, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    sources = {}
    payloads = {}
    for source_id, max_age in (
        ("justlend_contracts", 86400), ("justlend_markets_v1", 900),
        ("justlend_usdd_rewards_v1", 900), ("usdd_earn_apy", 900),
    ):
        row = store.latest(source_id)
        if row is None:
            raise PlanUnavailable(f"no collected snapshot: {source_id}")
        age = _age_seconds(row["fetched_at"], now)
        if age < -30 or age > max_age:
            raise PlanUnavailable(f"stale or future source: {source_id}")
        try:
            payloads[source_id] = store.payload_for(row)
        except (OSError, UnicodeError, ValueError) as exc:
            raise PlanUnavailable(f"raw source integrity failed: {source_id}") from exc
        sources[source_id] = row
    markets = store.markets_for(sources["justlend_markets_v1"]["id"])
    matches = [row for row in markets if row["underlying_symbol"] == need.asset
               and row["status"] == "active"]
    if len(matches) != 1:
        raise PlanUnavailable("no unique active market for this asset")
    market = matches[0]
    address = market["market_address"]
    market_facts = [row for row in store.facts_for(sources["justlend_markets_v1"]["id"])
                    if row["subject_id"] == address]
    apy_fact = _metric(market_facts, "supply_apy")
    cash_fact = _metric(market_facts, "available_cash")
    try:
        store.verify_fact(payloads["justlend_markets_v1"], apy_fact)
        store.verify_fact(payloads["justlend_markets_v1"], cash_fact)
    except ValueError as exc:
        raise PlanUnavailable("normalized market fact failed raw reconciliation") from exc
    apy = Decimal(apy_fact["canonical_value"])
    cash = Decimal(cash_fact["canonical_value"])
    if not Decimal(0) <= apy <= Decimal(1):
        raise PlanUnavailable("supply APY outside review range")
    if cash < 0:
        raise PlanUnavailable("invalid market cash")
    reward_facts = [row for row in store.facts_for(sources["justlend_usdd_rewards_v1"]["id"])
                    if row["subject_id"] == address]
    reward = next((row for row in reward_facts if row["metric_id"] == "usdd_reward_apy"), None)
    usdd_facts = store.facts_for(sources["usdd_earn_apy"]["id"])
    usdd_context = next((row for row in usdd_facts if row["metric_id"] == "usdd_tron_earn_apy"), None)
    risk_ceiling = {"cautious": Decimal("0.50"),
                    "balanced": Decimal("0.75"),
                    "growth": Decimal("1.00")}[need.risk]
    maximum = min(need.amount - need.liquid_reserve,
                  need.amount * risk_ceiling, cash)
    conservative = min(maximum, need.amount / Decimal(4))
    sizes = list(dict.fromkeys((conservative, maximum)))
    plans = []
    for number, amount in enumerate(sizes, start=1):
        if amount <= 0:
            continue
        # Market cash is a conservative exit-liquidity signal, not proof
        # of future withdrawability or an official deposit limit.
        reserve = need.amount - amount
        legs = [
            {"kind": "HOLD", "asset": need.asset, "amount": canonical_decimal(reserve)},
            {"kind": "SUPPLY_RESEARCH_ONLY", "asset": need.asset,
             "market_address": address, "amount": canonical_decimal(amount)},
        ]
        record = {
            "name": "유동성 우선" if number == 1 else "공급 비중 우선",
            "legs": legs, "gross_base_estimate": None,
            "estimate_asset": need.asset, "estimate_method": "unavailable_rate_convention_unknown",
            "fee_estimate": None, "net_estimate": None, "executable": False,
            "blockers": ["현재 지갑 잔액 미확인", "거래 수수료 견적 없음",
                         "기간 수익 계산 방식 미검증", "승인 대상 및 테스트넷 실행 경로 미검증"],
        }
        plans.append(record)
    snapshot_refs = {key: {"id": value["id"], "sha256": value["sha256"],
                           "fetched_at": value["fetched_at"],
                           "observed_at": None, "source_time_unknown": True}
                     for key, value in sources.items()}
    body = {
        "schema_version": "0.1.0", "calculation_version": "0.2.0",
        "as_of": now.isoformat(),
        "needs": {"asset": need.asset, "amount": canonical_decimal(need.amount),
                  "liquid_reserve": canonical_decimal(need.liquid_reserve),
                  "horizon_days": need.horizon_days, "risk": need.risk},
        "market": market, "supply_apy": apy_fact["canonical_value"],
        "supply_rate_convention": "UNKNOWN",
        "risk_ceiling_fraction": canonical_decimal(risk_ceiling),
        "risk_policy_status": "PROVISIONAL_ALLOCATION_CAP_NOT_A_RISK_SCORE",
        "supply_apy_evidence": {"snapshot_id": apy_fact["snapshot_id"],
                                "json_path": apy_fact["json_path"]},
        "available_cash_evidence": {"snapshot_id": cash_fact["snapshot_id"],
                                    "json_path": cash_fact["json_path"]},
        "usdd_mining_apy": reward["canonical_value"] if reward and reward["quality"] in {"VALID", "VALID_ZERO"} else None,
        "usdd_mining_included_in_estimate": False,
        "usdd_tron_earn_apy_context": usdd_context["canonical_value"] if usdd_context and usdd_context["quality"] in {"VALID", "VALID_ZERO"} else None,
        "usdd_direct_earn_route_verified": False,
        "available_market_cash": cash_fact["canonical_value"],
        "sources": snapshot_refs, "plans": plans,
        "state": "RESEARCH_COMPARISON_ONLY",
    }
    identity = {key: body[key] for key in (
        "schema_version", "calculation_version", "needs", "market", "supply_apy",
        "supply_rate_convention", "risk_ceiling_fraction", "available_market_cash",
        "sources", "plans", "state",
    )}
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    body["plan_id"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return body
