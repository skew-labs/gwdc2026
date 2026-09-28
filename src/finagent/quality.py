"""Read-only release quality gate. PASS here is not financial truth or gold."""

import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .release import build_manifest
from .store import Store


def build_quality_report(store: Store, specs: list[dict], *, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    findings = []
    source_rows = {}
    source_quality = {}
    for spec in specs:
        source_id = spec["id"]
        snapshot = store.latest(source_id)
        if snapshot is None:
            findings.append(f"MISSING_SOURCE:{source_id}")
            continue
        source_rows[source_id] = snapshot
        age = (now - datetime.fromisoformat(snapshot["fetched_at"])).total_seconds()
        if age < 0 or age > spec["max_age_seconds"]:
            findings.append(f"STALE_SOURCE:{source_id}")
        facts = store.facts_for(snapshot["id"])
        try:
            payload = store.payload_for(snapshot)  # verifies raw SHA-256
            for fact in facts:
                store.verify_fact(payload, fact)
        except (OSError, UnicodeError, ValueError) as exc:
            findings.append(f"RAW_INTEGRITY_ERROR:{source_id}:{type(exc).__name__}")
        source_quality[source_id] = {
            "snapshot_id": snapshot["id"], "sha256": snapshot["sha256"],
            "fetched_at": snapshot["fetched_at"], "age_seconds": round(age, 3),
            "facts": len(facts), "quality_counts": dict(Counter(
                fact["quality"] for fact in facts)),
            "source_observed_at": None, "source_time_unknown": True,
        }
    if "justlend_markets_v1" in source_rows:
        market_snapshot = source_rows["justlend_markets_v1"]
        markets = store.markets_for(market_snapshot["id"])
        facts = store.facts_for(market_snapshot["id"])
        for asset in ("USDT", "USDD"):
            matches = [market for market in markets
                       if market["underlying_symbol"] == asset and market["status"] == "active"]
            if len(matches) != 1:
                findings.append(f"ACTIVE_MARKET_COUNT:{asset}:{len(matches)}")
                continue
            address = matches[0]["market_address"]
            required = ["supply_apy", "available_cash"]
            if market_snapshot["parser_version"] == "0.3.0":
                required += ["total_borrows", "reserves", "jtoken_supply",
                             "exchange_rate", "collateral_factor", "reserve_factor",
                             "underlying_price_in_trx"]
            for metric in required:
                values = [fact for fact in facts if fact["subject_id"] == address
                          and fact["metric_id"] == metric]
                if len(values) != 1 or values[0]["quality"] not in {"VALID", "VALID_ZERO"}:
                    findings.append(f"MISSING_MARKET_METRIC:{asset}:{metric}")
            if market_snapshot["parser_version"] == "0.3.0":
                usable = {fact["metric_id"]: Decimal(fact["canonical_value"])
                          for fact in facts if fact["subject_id"] == address
                          and fact["metric_id"] in required
                          and fact["quality"] in {"VALID", "VALID_ZERO"}}
                if all(key in usable for key in ("collateral_factor", "reserve_factor")):
                    if any(usable[key] > 1 for key in ("collateral_factor", "reserve_factor")):
                        findings.append(f"INVALID_MARKET_FACTOR:{asset}")
                if all(key in usable for key in ("available_cash", "total_borrows", "reserves")):
                    if usable["available_cash"] + usable["total_borrows"] <= usable["reserves"]:
                        findings.append(f"INVALID_MARKET_LIQUIDITY:{asset}")
                if "exchange_rate" in usable and usable["exchange_rate"] <= 0:
                    findings.append(f"INVALID_EXCHANGE_RATE:{asset}")
    if "usdd_earn_apy" in source_rows:
        facts = store.facts_for(source_rows["usdd_earn_apy"]["id"])
        values = [fact for fact in facts if fact["metric_id"] == "usdd_tron_earn_apy"]
        if len(values) != 1 or values[0]["quality"] not in {"VALID", "VALID_ZERO"}:
            findings.append("MISSING_USDD_APY_CONTEXT")
    if "usdd_tron_collateral" in source_rows:
        facts = store.facts_for(source_rows["usdd_tron_collateral"]["id"])
        by_key = {(fact["subject_id"], fact["metric_id"]): fact for fact in facts}
        for metric in ("usdd_tron_total_supply", "usdd_tron_collateral_value",
                       "usdd_tron_earn_tvl", "usdd_tron_collateral_apy"):
            fact = by_key.get(("usdd:tron", metric))
            if fact is None or fact["quality"] not in {"VALID", "VALID_ZERO"}:
                findings.append(f"MISSING_USDD_COLLATERAL_METRIC:{metric}")
        addresses = {fact["subject_id"] for fact in facts if fact["subject_id"] != "usdd:tron"}
        if not addresses:
            findings.append("MISSING_USDD_TRON_VAULTS")
        for address in addresses:
            for metric in ("vault_minted_usdd", "vault_debt", "vault_locked_value",
                           "vault_collateral_ratio", "vault_min_collateral_ratio",
                           "vault_stability_fee", "vault_debt_ceiling"):
                fact = by_key.get((address, metric))
                if fact is None or fact["quality"] not in {"VALID", "VALID_ZERO"}:
                    findings.append(f"MISSING_VAULT_METRIC:{address}:{metric}")
            debt = by_key.get((address, "vault_debt"))
            ratio = by_key.get((address, "vault_collateral_ratio"))
            minimum = by_key.get((address, "vault_min_collateral_ratio"))
            if all(fact is not None and fact["quality"] in {"VALID", "VALID_ZERO"}
                   for fact in (debt, ratio, minimum)):
                if Decimal(minimum["canonical_value"]) <= 0 or (
                    Decimal(debt["canonical_value"]) > 0
                    and Decimal(ratio["canonical_value"]) <= 0
                ):
                    findings.append(f"INVALID_VAULT_RATIO:{address}")
    release = (build_manifest(store, [spec["id"] for spec in specs])
               if len(source_rows) == len(specs) else None)
    return {"checked_at": now.isoformat(),
            "status": "PASS_FOR_RESEARCH_READ" if not findings else "BLOCKED_FOR_RESEARCH_READ",
            "release_id": release["release_id"] if release else None,
            "findings": findings, "sources": source_quality,
            "limits": ["Source timestamps are not supplied by these APIs",
                       "Raw hash verifies bytes, not external truth",
                       "Rights, execution route, fees, and wallet state are not verified",
                       "This is not an adjudicated training release"]}


def write_quality_report(store: Store, report: dict) -> Path:
    root = store.root / "quality"
    root.mkdir(exist_ok=True)
    stamp = report["checked_at"].replace("+00:00", "Z").replace("-", "").replace(":", "")
    key = (report["release_id"] or "missing-sources") + "-" + stamp
    path = root / f"{key}.json"
    with path.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return path
