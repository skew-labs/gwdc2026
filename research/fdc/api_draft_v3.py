"""Map current public API facts to an excluded typed episode input draft.

Unknown product withdrawal terms and source observation time force abstention.
This is neither a human label nor a recommendation.
"""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from finagent.normalize import contract_status
from finagent.release import build_manifest
from finagent.store import Store

from .validate_v3 import check_episode, exact, money


SOURCE_IDS = ["justlend_contracts", "justlend_markets_v1",
              "justlend_usdd_rewards_v1", "usdd_earn_apy", "usdd_overview"]


def draft(store: Store, need: dict, *, text: str,
          need_origin: str = "authored_example", now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("as_of timezone required")
    unit = need["budget"]["unit"]
    if unit not in {"USDT", "USDD"}:
        raise ValueError("unsupported unit")
    amount = money(need["budget"], unit, "budget")
    immediate = money(need["withdrawal"]["min_immediate"], unit, "immediate")
    if not 0 <= immediate <= amount or amount <= 0:
        raise ValueError("invalid budget or immediate reserve")
    if need_origin not in {"authored_example", "user_confirmed"}:
        raise ValueError("need origin must be explicit")
    for field in ("total_fraction", "shared_group_fraction"):
        if exact(need["risk"][field], field) > 1:
            raise ValueError("risk fraction exceeds one")
    source_rows = {}
    payloads = {}
    for source_id, age_limit in (("justlend_contracts", 86400),
                                 ("justlend_markets_v1", 900)):
        row = store.latest(source_id)
        if row is None:
            raise ValueError(f"missing source: {source_id}")
        age = (now - datetime.fromisoformat(row["fetched_at"])).total_seconds()
        if not 0 <= age <= age_limit:
            raise ValueError(f"stale or future source: {source_id}")
        source_rows[source_id] = row
        payloads[source_id] = store.payload_for(row)
    statuses = contract_status(payloads["justlend_contracts"])
    market_row = source_rows["justlend_markets_v1"]
    matches = [market for market in store.markets_for(market_row["id"])
               if market["underlying_symbol"] == unit and market["status"] == "active"
               and statuses.get(market["market_address"]) == "active"]
    if len(matches) != 1:
        raise ValueError("unique active market unavailable")
    market = matches[0]
    facts = [fact for fact in store.facts_for(market_row["id"])
             if fact["subject_id"] == market["market_address"]]
    selected = {}
    for metric in ("supply_apy", "available_cash"):
        rows = [fact for fact in facts if fact["metric_id"] == metric]
        if len(rows) != 1 or rows[0]["quality"] not in {"VALID", "VALID_ZERO"}:
            raise ValueError(f"unusable source metric: {metric}")
        store.verify_fact(payloads["justlend_markets_v1"], rows[0])
        selected[metric] = rows[0]
    release = build_manifest(store, SOURCE_IDS)
    if any(release["sources"][key]["snapshot_id"] != row["id"]
           for key, row in source_rows.items()):
        raise ValueError("source changed during draft construction")
    identity = json.dumps({"source_release": release["release_id"],
                           "need": need, "text": text}, sort_keys=True,
                          ensure_ascii=False)
    digest = hashlib.sha256(identity.encode()).hexdigest()
    evidence = [
        {"id": "e-base-rate", "text": "JustLend public API base supply rate; fee and rewards excluded",
         "source_ref": market_row["id"], "locator": selected["supply_apy"]["json_path"]},
        {"id": "e-cash", "text": "JustLend public API market cash; not future withdrawal proof",
         "source_ref": market_row["id"], "locator": selected["available_cash"]["json_path"]},
    ]
    row = {
        "schema_version": "3.0.0", "episode_id": "api-draft-" + digest,
        "scenario_family": "api-draft-" + digest,
        "parent_episode_id": None, "changed_field": None,
        "expected_pair_effect": None,
        "as_of": now.isoformat(), "split": "excluded",
        "source_mode": "api_observation", "review_status": "unreviewed",
        "rights_status": "unknown", "need_origin": need_origin,
        "source_release": release["release_id"], "text": text,
        "need": need,
        "candidates": [{"id": market["market_address"], "asset": unit,
                        "network": "tron_mainnet", "protocol_version": "justlend_v1",
                        "underlying_address": market["underlying_address"],
                        "underlying_decimals": market["underlying_decimals"],
                        "status": market["status"],
                        "supply_apy": selected["supply_apy"]["canonical_value"],
                        "available_cash": {"value": selected["available_cash"]["canonical_value"],
                                           "unit": unit},
                        "lock_days": None, "notice_days": None,
                        "observed_at": None, "valid_until": None,
                        "risk_groups": ["protocol:justlend-v1", "token:" + unit.lower()],
                        "history": [], "source_ref": market_row["id"],
                        "evidence_ids": [item["id"] for item in evidence]}],
        "evidence": evidence,
        "target": {"mode": "abstain", "question_fields": [],
                   "abstain_reasons": ["WITHDRAWAL_TERMS_AND_SOURCE_TIME_UNVERIFIED"],
                   "plans": [], "oracle_kind": "unreviewed_draft"},
    }
    from jsonschema import Draft202012Validator, FormatChecker
    schema_path = Path(__file__).resolve().parents[2] / "contracts/episode_v3.schema.json"
    validator = Draft202012Validator(json.loads(schema_path.read_text()),
                                      format_checker=FormatChecker())
    errors = list(validator.iter_errors(row))
    if errors:
        raise ValueError("V3 schema: " + errors[0].message)
    check_episode(row, 1)
    return row


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    need_input = parser.add_mutually_exclusive_group(required=True)
    need_input.add_argument("--need-json")
    need_input.add_argument("--need-file", type=Path)
    parser.add_argument("--text", required=True)
    parser.add_argument("--need-origin", choices=("authored_example", "user_confirmed"),
                        default="authored_example")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    need = json.loads(arguments.need_json if arguments.need_json is not None
                      else arguments.need_file.read_text())
    result = draft(Store(arguments.data_dir), need,
                   text=arguments.text, need_origin=arguments.need_origin)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    with arguments.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({"output": str(arguments.output), "episode_id": result["episode_id"],
                      "split": result["split"], "review_status": result["review_status"],
                      "rights_status": result["rights_status"], "mode": result["target"]["mode"]},
                     ensure_ascii=False))
