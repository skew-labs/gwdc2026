"""Build an excluded review draft from a fixed official-source comparison.

The planner is not an independent label oracle. These records are deliberately
excluded until a separate adjudication workflow and rights review exist.
"""

import hashlib
import json
from pathlib import Path

from .contracts import Need, canonical_decimal
from .planner import compare
from .release import build_manifest
from .store import Store


SOURCE_IDS = ["justlend_contracts", "justlend_markets_v1",
              "justlend_usdd_rewards_v1", "usdd_earn_apy", "usdd_overview"]


def draft_episode(store: Store, need: Need, utterance: str | None = None) -> dict:
    answer = compare(store, need)
    release = build_manifest(store, SOURCE_IDS)
    for source_id, selected in answer["sources"].items():
        if release["sources"][source_id]["snapshot_id"] != selected["id"]:
            raise ValueError("source changed while constructing episode draft")
    market = answer["market"]
    address = market["market_address"]
    market_ref = answer["sources"]["justlend_markets_v1"]["id"]
    needs = answer["needs"]
    evidence = [
        {"id": "supply-rate", "text": "JustLend 시장 기본 공급 연율. 추가 보상과 비용 제외.",
         "source_ref": market_ref,
         "locator": answer["supply_apy_evidence"]["json_path"]},
        {"id": "market-cash", "text": "조회 당시 시장 가용 수량. 미래 출금 보장은 아님.",
         "source_ref": market_ref,
         "locator": answer["available_cash_evidence"]["json_path"]},
    ]
    candidate = {
        "id": address, "asset": need.asset, "network": "tron_mainnet",
        "status": market["status"], "supply_apy": answer["supply_apy"],
        "available_cash": answer["available_market_cash"], "fee_quote": None,
        # The API omits a source observation time. Fetch time is not a
        # historical observation, so no synthetic age-0 history is emitted.
        "history": [],
        "risk_group": "justlend-v1-" + need.asset,
        "source_ref": market_ref,
    }
    plans = []
    for plan in answer["plans"]:
        supply_leg = next(leg for leg in plan["legs"]
                          if leg["kind"] == "SUPPLY_RESEARCH_ONLY")
        hold_leg = next(leg for leg in plan["legs"] if leg["kind"] == "HOLD")
        plans.append({"allocations": {address: supply_leg["amount"]},
                      "reserve": hold_leg["amount"],
                      "evidence_ids": ["supply-rate", "market-cash"]})
    text = utterance if utterance else (
        f"{canonical_decimal(need.amount)} {need.asset} 중 "
        f"{canonical_decimal(need.liquid_reserve)}는 바로 보유하고 "
        f"{need.horizon_days}일 비교하고 싶다"
    )
    identity = json.dumps({"release": release["release_id"], "needs": needs,
                           "text": text}, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return {
        "schema_version": "2.0.0", "episode_id": "api-draft-" + digest,
        "scenario_family": "api-" + release["release_id"] + "-" + need.asset,
        "as_of": answer["as_of"], "split": "excluded",
        "source_mode": "api_observation", "review_status": "unreviewed",
        "rights_status": "unknown",
        "utterance_origin": "authored_case" if utterance else "synthetic_template",
        "source_release": release["release_id"], "text": text, "needs": needs,
        "candidates": [candidate], "evidence": evidence,
        "target": {"mode": "propose" if plans else "abstain", "plans": plans,
                   "reason": "Deterministic planner draft; no independent label review",
                   "oracle_kind": "deterministic_planner"},
    }


def write_draft(path: Path, episode: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        output.write(json.dumps(episode, ensure_ascii=False, sort_keys=True) + "\n")
    return path
