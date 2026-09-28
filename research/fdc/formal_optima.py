"""Continuously mine exact allocation optima under a declared, narrow objective.

Real JustLend market observations supply rate/cash/status witnesses. User needs
are generated templates. These labels prove a mathematical gross-rate optimum
for that scenario, not the best real investment, executable liquidity, or a
licensed training set.
"""

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from finagent.normalize import contract_status
from finagent.store import Store


VERSION = "formal-gross-proxy-1.0.0"
SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_market_snapshots (
 snapshot_id TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, output_sha256 TEXT NOT NULL,
 scenario_count INTEGER NOT NULL, pair_count INTEGER NOT NULL,
 blocked_reason TEXT
);
CREATE TABLE IF NOT EXISTS seen_states (
 asset TEXT NOT NULL, state_sha256 TEXT NOT NULL, first_snapshot_id TEXT NOT NULL,
 PRIMARY KEY(asset,state_sha256)
);
"""
BUDGETS = ("100", "1000", "10000")
RESERVE_FRACTIONS = ("0", "0.25", "0.5")
TOTAL_RISK_FRACTIONS = ("0.25", "0.5", "1")
SHARED_RISK_FRACTIONS = ("0.5", "1")
HORIZONS = (30, 365)


def _bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode()


def _hash(value: dict | bytes) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else _bytes(value)).hexdigest()


def _fraction(value: str) -> Fraction:
    if not isinstance(value, str) or not value or "e" in value.lower():
        raise ValueError("plain decimal string required")
    parsed = Decimal(value)
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("nonnegative finite decimal required")
    return Fraction(parsed)


def _atoms(value: str, decimals: int) -> int:
    scaled = _fraction(value) * 10 ** decimals
    if scaled.denominator != 1:
        raise ValueError("amount exceeds token precision")
    return scaled.numerator


def _cash_atoms(value: str, decimals: int) -> int:
    return int(_fraction(value) * 10 ** decimals)  # conservative floor


def _cap(scenario: dict) -> int:
    budget = int(scenario["budget_atoms"])
    reserve = int(scenario["min_immediate_atoms"])
    if budget <= 0 or not 0 <= reserve <= budget:
        raise ValueError("invalid budget or reserve")
    fractions = [_fraction(scenario[key]) for key in
                 ("total_risk_fraction", "shared_risk_fraction")]
    if any(value > 1 for value in fractions):
        raise ValueError("risk cap outside [0,1]")
    return min(budget - reserve, *(int(budget * value) for value in fractions))


def solve(scenario: dict) -> dict:
    """Fractional-knapsack primal solution in exact token atoms."""
    remaining = _cap(scenario)
    allocations = {market["id"]: 0 for market in scenario["markets"]}
    for market in sorted(scenario["markets"],
                         key=lambda row: (-_fraction(row["supply_rate"]), row["id"])):
        if _fraction(market["supply_rate"]) <= 0:
            continue
        amount = min(remaining, int(market["cash_proxy_atoms"]))
        allocations[market["id"]] = amount
        remaining -= amount
    hold = int(scenario["budget_atoms"]) - sum(allocations.values())
    score = sum((Fraction(amount) * _fraction(market["supply_rate"])
                 for market in scenario["markets"]
                 for amount in [allocations[market["id"]]]), Fraction(0))
    score *= Fraction(scenario["horizon_days"], 365)
    return {"allocations_atoms": {key: str(value) for key, value in sorted(allocations.items())},
            "hold_atoms": str(hold), "gross_simple_rate_score_atoms":
                {"numerator": str(score.numerator), "denominator": str(score.denominator)},
            "global_invest_cap_atoms": str(_cap(scenario))}


def verify(scenario: dict, target: dict) -> None:
    """Independent exchange-argument certificate for the common-cap problem."""
    markets = scenario["markets"]
    ids = [market["id"] for market in markets]
    if not markets or len(ids) != len(set(ids)):
        raise ValueError("market identities invalid")
    if set(target["allocations_atoms"]) != set(ids):
        raise ValueError("allocation keys differ from candidates")
    amounts = {key: int(value) for key, value in target["allocations_atoms"].items()}
    if any(value < 0 for value in amounts.values()):
        raise ValueError("negative allocation")
    budget = int(scenario["budget_atoms"])
    invested = sum(amounts.values())
    cap = _cap(scenario)
    if (invested > cap or invested + int(target["hold_atoms"]) != budget
            or target["global_invest_cap_atoms"] != str(cap)):
        raise ValueError("budget or global cap violated")
    ordered = sorted(markets, key=lambda row: (-_fraction(row["supply_rate"]), row["id"]))
    for market in ordered:
        amount = amounts[market["id"]]
        cash = int(market["cash_proxy_atoms"])
        rate = _fraction(market["supply_rate"])
        if amount > cash or (rate == 0 and amount != 0):
            raise ValueError("market capacity or nonpositive rate violated")
        # If a better market has room while a worse one is funded, shifting
        # one atom strictly improves the objective. ID breaks equal-rate ties.
        if rate > 0 and amount < cash:
            lower_funded = any(amounts[other["id"]] > 0 for other in ordered
                               if (-_fraction(other["supply_rate"]), other["id"])
                               > (-rate, market["id"]))
            if lower_funded:
                raise ValueError("allocation is dominated by an earlier market")
            if invested < cap:
                raise ValueError("positive-rate capacity left unused")
    score = sum((Fraction(amounts[row["id"]]) * _fraction(row["supply_rate"])
                 for row in markets), Fraction(0))
    score *= Fraction(scenario["horizon_days"], 365)
    recorded = target["gross_simple_rate_score_atoms"]
    if Fraction(int(recorded["numerator"]), int(recorded["denominator"])) != score:
        raise ValueError("objective score differs from exact arithmetic")


def _source_markets(store: Store, snapshot: sqlite3.Row) -> tuple[dict, dict]:
    if snapshot["source_url"] != "https://openapi.just.network/lend/jtoken":
        raise ValueError("market source URL is not registered JustLend API")
    at = datetime.fromisoformat(snapshot["fetched_at"])
    if at.tzinfo is None or at.utcoffset() != timedelta(0):
        raise ValueError("market snapshot has no UTC completion time")
    contracts = store.snapshots_for("justlend_contracts", snapshot["fetched_at"])
    contracts = [row for row in contracts if row["parser_version"] == "0.3.0"]
    if not contracts:
        raise ValueError("matching contract registry missing")
    registry = contracts[-1]
    if registry["source_url"] != "https://docs.justlend.org/developers/contracts.json":
        raise ValueError("contract source URL is not registered JustLend registry")
    if at - datetime.fromisoformat(registry["fetched_at"]) > timedelta(days=1):
        raise ValueError("contract registry stale at market snapshot")
    statuses = contract_status(store.payload_for(registry))
    payload = store.payload_for(snapshot)
    facts = {(row["subject_id"], row["metric_id"]): row
             for row in store.facts_for(snapshot["id"])}
    grouped = {"USDT": [], "USDD": []}
    for market in store.markets_for(snapshot["id"]):
        asset = market["underlying_symbol"]
        if asset not in grouped or market["status"] != "active":
            continue
        if statuses.get(market["market_address"]) != "active":
            raise ValueError("market status differs from contract registry")
        selected = {}
        for metric in ("supply_apy", "available_cash"):
            fact = facts.get((market["market_address"], metric))
            if fact is None or fact["quality"] not in {"VALID", "VALID_ZERO"}:
                raise ValueError("market fact missing for formal scenario")
            store.verify_fact(payload, fact)
            selected[metric] = fact
        rate = selected["supply_apy"]["canonical_value"]
        if _fraction(rate) > 1:
            raise ValueError("supply rate outside reviewed range")
        decimals = market["underlying_decimals"]
        rate_path = selected["supply_apy"]["json_path"].split("/")
        cash_path = selected["available_cash"]["json_path"].split("/")
        if (len(rate_path) != 5 or len(cash_path) != 5
                or rate_path[:4] != cash_path[:4]
                or rate_path[1:3] != ["data", "tokenList"]
                or rate_path[4] != "supplyRate" or cash_path[4] != "cash"):
            raise ValueError("rate and cash do not identify one raw market row")
        token = payload["data"]["tokenList"][int(rate_path[3])]
        if (token["address"] != market["market_address"]
                or token["symbol"] != market["jtoken_symbol"]
                or token["underlyingSymbol"] != asset
                or token["underlyingAddress"] != market["underlying_address"]
                or int(token["underlyingDecimal"]) != decimals):
            raise ValueError("normalized market identity differs from raw row")
        grouped[asset].append({"id": market["market_address"],
                               "underlying_address": market["underlying_address"],
                               "decimals": decimals, "supply_rate": rate,
                               "cash_proxy_atoms": str(_cash_atoms(
                                   selected["available_cash"]["canonical_value"], decimals)),
                               "source_locators": {key: value["json_path"]
                                                   for key, value in selected.items()}})
    witness = {"market_snapshot_id": snapshot["id"],
               "market_raw_sha256": snapshot["sha256"],
               "market_fetched_at": snapshot["fetched_at"],
               "contract_snapshot_id": registry["id"],
               "contract_raw_sha256": registry["sha256"],
               "contract_fetched_at": registry["fetched_at"]}
    return grouped, witness


def _scenario(market_state: dict, witness: dict, asset: str, budget: str,
              reserve: str, total: str, shared: str, horizon: int) -> dict:
    decimals = market_state["decimals"]
    budget_atoms = _atoms(budget, decimals)
    reserve_atoms = int(Fraction(budget_atoms) * _fraction(reserve))
    body = {"schema_version": "1.0.0", "objective_version": VERSION,
            "asset": asset, "decimals": decimals,
            "budget_atoms": str(budget_atoms),
            "min_immediate_atoms": str(reserve_atoms),
            "total_risk_fraction": total, "shared_risk_fraction": shared,
            "horizon_days": horizon, "markets": market_state["markets"],
            "source_witness": witness,
            "assumptions": ["static_observed_base_supply_rate",
                            "simple_rate_score_not_realized_return",
                            "market_cash_as_conservative_proxy_cap",
                            "no_fees_or_rewards_in_objective",
                            "future_withdrawal_terms_not_modeled"]}
    return body


def _records_for_asset(market_state: dict, witness: dict, asset: str) -> list[dict]:
    scenarios = {}
    records = []
    for budget in BUDGETS:
        for reserve in RESERVE_FRACTIONS:
            for total in TOTAL_RISK_FRACTIONS:
                for shared in SHARED_RISK_FRACTIONS:
                    for horizon in HORIZONS:
                        key = (budget, reserve, total, shared, horizon)
                        scenario = _scenario(market_state, witness, asset, *key)
                        target = solve(scenario)
                        verify(scenario, target)
                        case_id = _hash({"input": scenario})
                        scenarios[key] = (case_id, scenario, target)
                        records.append({"record_type": "FORMAL_OPTIMUM", "case_id": case_id,
                                        "input": scenario, "target": target,
                                        "formal_optimality_verified": True,
                                        "rights_status": "unknown",
                                        "training_scope": "excluded_rights_unknown",
                                        "real_customer_optimum": False,
                                        "product_actionable": False})
    # One-field changes stay in the same source family. An unchanged optimum
    # is a valid outcome, not a forced positive counterfactual label.
    fields = [(1, "min_immediate_atoms"), (2, "total_risk_fraction"),
              (3, "shared_risk_fraction"), (4, "horizon_days")]
    for key, (case_id, scenario, target) in scenarios.items():
        for index, field in fields:
            options = {other[index] for other in scenarios if all(
                other[position] == key[position] for position in range(5)
                if position != index) and other[index] > key[index]}
            if not options:
                continue
            changed_key = list(key)
            changed_key[index] = min(options)
            changed_id, changed_scenario, changed_target = scenarios[tuple(changed_key)]
            pair_id = _hash({"base": case_id, "changed": changed_id, "field": field})
            records.append({"record_type": "COUNTERFACTUAL_PAIR", "pair_id": pair_id,
                            "base_case_id": case_id, "changed_case_id": changed_id,
                            "changed_field": field,
                            "allocation_changed": target["allocations_atoms"] !=
                                changed_target["allocations_atoms"],
                            "source_family": witness["market_snapshot_id"] + ":" + asset,
                            "rights_status": "unknown",
                            "training_scope": "excluded_rights_unknown",
                            "product_actionable": False})
    return records


def _write_immutable(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError("formal batch replay differs")
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    with temporary.open("xb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)


def run_once(data_dir: Path, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "formal.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = Store(data_dir)
        state = sqlite3.connect(output_dir / "formal.sqlite3")
        state.row_factory = sqlite3.Row
        state.executescript(SCHEMA)
        for row in state.execute("SELECT snapshot_id,output_sha256 FROM processed_market_snapshots"):
            path = output_dir / "batches" / f"{row['snapshot_id']}.jsonl"
            if not path.is_file() or _hash(path.read_bytes()) != row["output_sha256"]:
                raise ValueError("formal batch missing or modified")
        with store.connect() as db:
            snapshots = db.execute("SELECT * FROM snapshots WHERE source_id='justlend_markets_v1' "
                                   "AND parser_version='0.3.0' ORDER BY fetched_at,id").fetchall()
        new_snapshots = scenarios = pairs = blocked = 0
        for snapshot in snapshots:
            if state.execute("SELECT 1 FROM processed_market_snapshots WHERE snapshot_id=?",
                             (snapshot["id"],)).fetchone():
                continue
            records = []
            new_states = []
            try:
                grouped, witness = _source_markets(store, snapshot)
                for asset, markets in grouped.items():
                    if not markets:
                        continue
                    if len({(row["underlying_address"], row["decimals"])
                            for row in markets}) != 1:
                        raise ValueError("same-asset markets disagree on token identity")
                    markets = sorted(markets, key=lambda item: item["id"])
                    state_hash = _hash({"asset": asset, "markets": [
                        {key: value for key, value in market.items()
                         if key != "source_locators"} for market in markets]})
                    if state.execute("SELECT 1 FROM seen_states WHERE asset=? AND state_sha256=?",
                                     (asset, state_hash)).fetchone():
                        continue
                    market_state = {"markets": markets, "decimals": markets[0]["decimals"]}
                    records.extend(_records_for_asset(market_state, witness, asset))
                    new_states.append((asset, state_hash, snapshot["id"]))
                reason = None if records else "NO_NEW_ELIGIBLE_MARKET_STATE"
            except ValueError as exc:
                # Integrity failures must stop the service; missing historical
                # point-in-time registry is an explicit non-label, not a guess.
                if str(exc) != "matching contract registry missing":
                    raise
                reason = str(exc)
                blocked += 1
            content = b"".join(_bytes(record) + b"\n" for record in records)
            batch = output_dir / "batches" / f"{snapshot['id']}.jsonl"
            batch.parent.mkdir(parents=True, exist_ok=True)
            _write_immutable(batch, content)
            count = sum(row["record_type"] == "FORMAL_OPTIMUM" for row in records)
            pair_count = len(records) - count
            with state:
                state.executemany("INSERT INTO seen_states VALUES (?,?,?)", new_states)
                state.execute("INSERT INTO processed_market_snapshots VALUES (?,?,?,?,?,?)",
                              (snapshot["id"], snapshot["fetched_at"], _hash(content),
                               count, pair_count, reason))
            new_snapshots += 1
            scenarios += count
            pairs += pair_count
        totals = state.execute("SELECT count(*) AS snapshots,coalesce(sum(scenario_count),0) AS scenarios,"
                               "coalesce(sum(pair_count),0) AS pairs FROM processed_market_snapshots").fetchone()
        unique_states = state.execute("SELECT count(*) FROM seen_states").fetchone()[0]
        result = {"status": "FORMAL_OPTIMA_UNDER_ASSUMPTIONS", "new_snapshots": new_snapshots,
                  "new_optima": scenarios, "new_counterfactual_pairs": pairs,
                  "blocked_snapshots": blocked, "total_snapshots": totals["snapshots"],
                  "unique_market_states": unique_states,
                  "total_optima": totals["scenarios"], "total_pairs": totals["pairs"],
                  "objective_version": VERSION, "rights_status": "unknown",
                  "training_enabled": False, "real_customer_optimum_count": 0,
                  "product_actionable": False}
        temporary = output_dir / "latest.json.tmp"
        temporary.write_bytes(_bytes(result) + b"\n")
        temporary.replace(output_dir / "latest.json")
        state.close()
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run_once(args.data_dir, args.output_dir), ensure_ascii=False, indent=2))
