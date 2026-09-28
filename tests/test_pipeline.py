"""Run on the user-selected Cherry host; these tests never touch live funds."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from finagent.contracts import ContractError, Need
from finagent.episode import draft_episode
from finagent.history import market_observations
from finagent.normalize import normalize
from finagent.planner import PlanUnavailable, compare
from finagent.quality import build_quality_report
from finagent.store import Store


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.now = datetime.now(timezone.utc)
        self.at = self.now.isoformat()
        self.address = "T" + "a" * 33
        self.usdd_address = "T" + "c" * 33

    def save(self, source_id, kind, payload, *, at=None, statuses=None):
        raw = json.dumps(payload, separators=(",", ":")).encode()
        markets, facts = normalize(kind, payload, statuses)
        return self.store.save_snapshot(
            {"id": source_id, "url": "https://example.invalid/fixture"},
            at or self.at, raw, payload, markets, facts)

    def seed(self, *, apy="0.04", cash="5000", at=None):
        self.save("justlend_contracts", "justlend_contracts", {
            "networks": {"mainnet": {"jtokens": {
                "jUSDT": {"delegator": {"address": {"base58": self.address}},
                          "status": "active"},
                "jUSDD": {"delegator": {"address": {"base58": self.usdd_address}},
                          "status": "active"}}}}
        }, at=at)
        self.save("justlend_markets_v1", "justlend_markets_v1", {
            "code": 0, "data": {"tokenList": [{
                "address": self.address, "symbol": "jUSDT",
                "underlyingSymbol": "USDT", "underlyingAddress": "T" + "b" * 33,
                "underlyingDecimal": 6, "supplyRate": apy,
                "borrowRate": "0", "cash": cash,
                "totalBorrows": "200", "reserves": "10", "totalSupply": "600000",
                "exchangeRate": "0.01", "collateralFactor": "0.75",
                "reserveFactor": "0.1", "underlyingPriceInTrx": "3",
            }, {"address": self.usdd_address, "symbol": "jUSDD",
                "underlyingSymbol": "USDD", "underlyingAddress": "T" + "d" * 33,
                "underlyingDecimal": 18, "supplyRate": "0.02",
                "borrowRate": "0", "cash": "7000",
                "totalBorrows": "100", "reserves": "5", "totalSupply": "700000",
                "exchangeRate": "0.01", "collateralFactor": "0.85",
                "reserveFactor": "0.05", "underlyingPriceInTrx": "3"}]}
        }, at=at, statuses={self.address: "active", self.usdd_address: "active"})
        self.save("justlend_usdd_rewards_v1", "justlend_rewards_v1",
                  {"code": 0, "data": {self.address: {"USDD": "0"}}}, at=at)
        self.save("usdd_earn_apy", "usdd_earn_apy",
                  {"code": 0, "data": {"tronApy": "0.03"}}, at=at)
        self.save("usdd_overview", "usdd_overview",
                  {"code": 0, "data": {}}, at=at)

    def test_zero_and_missing_remain_distinct(self):
        _, facts = normalize("justlend_rewards_v1", {"code": 0, "data": {
            "zero": {"USDD": "0"}, "missing": {}
        }})
        self.assertEqual({row["subject_id"]: row["quality"] for row in facts},
                         {"zero": "VALID_ZERO", "missing": "MISSING"})

    def test_usdd_tron_collateral_numeric_zero_and_vault_risk(self):
        # The official API mixes integer zero with decimal strings and JSON decimals.
        raw = ('{"code":0,"data":{"usddTotalSupply":1250.5,'
               '"totalCollateralValue":2200.25,"earnTvl":0,"apy":0.04,'
               '"items":[{"chain":"tron","collateralType":1,'
               '"contractAddress":"' + self.address + '","mintedUSDD":100.5,'
               '"debt":101.25,"lockedValue":180.0,"collateralRatio":1.7778,'
               '"minCollateralRatio":"1.2","stabilityFee":"0.005",'
               '"line":"400","apy":null,"estimatedAnnualEarnings":null,'
               '"psmFee":null}]}}').encode()
        payload = json.loads(raw, parse_float=str)
        markets, facts = normalize("usdd_tron_collateral", payload)
        self.assertEqual(markets, [])
        self.assertEqual(len(facts), 14)
        by_metric = {row["metric_id"]: row for row in facts}
        self.assertEqual(by_metric["usdd_tron_earn_tvl"]["quality"], "VALID_ZERO")
        self.assertEqual(by_metric["vault_apy"]["quality"], "NOT_APPLICABLE")
        self.assertEqual(by_metric["vault_collateral_ratio"]["unit"], "multiple")
        source = {"id": "usdd_tron_collateral", "url": "https://example.invalid/fixture"}
        self.store.save_snapshot(source, self.at, raw, payload, markets, facts)
        report = build_quality_report(self.store, [
            {"id": "usdd_tron_collateral", "max_age_seconds": 900}], now=self.now)
        self.assertEqual(report["status"], "PASS_FOR_RESEARCH_READ")
        with self.store.connect() as db:
            db.execute("UPDATE facts SET raw_value='0' WHERE metric_id='vault_apy'")
        corrupted = build_quality_report(self.store, [
            {"id": "usdd_tron_collateral", "max_age_seconds": 900}], now=self.now)
        self.assertEqual(corrupted["status"], "BLOCKED_FOR_RESEARCH_READ")
        with self.assertRaises(ContractError):
            normalize("usdd_tron_collateral", {"code": 0, "data": {
                **payload["data"], "items": [{**payload["data"]["items"][0],
                                                "chain": "eth"}]}})

    def test_exact_budget_and_blocked_execution(self):
        self.seed()
        need = Need.from_json({"asset": "USDT", "amount": "1000",
                               "liquid_reserve": "250", "horizon_days": 30,
                               "risk": "balanced"})
        result = compare(self.store, need, now=self.now)
        self.assertEqual(result["plan_id"], compare(
            self.store, need, now=self.now + timedelta(seconds=1))["plan_id"])
        self.assertEqual(len(result["plans"]), 2)
        self.assertEqual(result["usdd_mining_apy"], "0")
        self.assertFalse(result["usdd_mining_included_in_estimate"])
        self.assertTrue(all(not plan["executable"] and plan["net_estimate"] is None
                            for plan in result["plans"]))
        for plan in result["plans"]:
            self.assertEqual(sum((Decimal(leg["amount"]) for leg in plan["legs"]), Decimal(0)),
                             need.amount)
        draft = draft_episode(self.store, need)
        self.assertEqual((draft["split"], draft["review_status"]),
                         ("excluded", "unreviewed"))
        self.assertEqual(draft["candidates"][0]["history"], [])
        self.assertEqual(draft["evidence"][1]["locator"],
                         result["available_cash_evidence"]["json_path"])

    def test_stale_source_fails_closed(self):
        self.seed(at=(self.now - timedelta(days=2)).isoformat())
        need = Need.from_json({"asset": "USDT", "amount": "1000",
                               "liquid_reserve": "0", "horizon_days": 30,
                               "risk": "balanced"})
        with self.assertRaises(PlanUnavailable):
            compare(self.store, need, now=self.now)

    def test_numeric_json_amount_is_rejected(self):
        with self.assertRaises(ContractError):
            Need.from_json({"asset": "USDT", "amount": 1000.0,
                            "liquid_reserve": "0", "horizon_days": 30,
                            "risk": "balanced"})

    def test_daily_public_request_budget(self):
        self.store.claim_request("2026-09-24", 2)
        self.store.claim_request("2026-09-24", 2)
        with self.assertRaises(RuntimeError):
            self.store.claim_request("2026-09-24", 2)

    def test_quality_gate_detects_raw_tampering(self):
        self.seed()
        specs = [{"id": source_id, "max_age_seconds": 900}
                 for source_id in ("justlend_contracts", "justlend_markets_v1",
                                   "justlend_usdd_rewards_v1", "usdd_earn_apy",
                                   "usdd_overview")]
        self.assertEqual(build_quality_report(self.store, specs, now=self.now)["status"],
                         "PASS_FOR_RESEARCH_READ")
        row = self.store.latest("justlend_markets_v1")
        Path(row["raw_path"]).write_text("{}")  # temp fixture only
        report = build_quality_report(self.store, specs, now=self.now)
        self.assertEqual(report["status"], "BLOCKED_FOR_RESEARCH_READ")
        self.assertTrue(any(item.startswith("RAW_INTEGRITY_ERROR")
                            for item in report["findings"]))
        need = Need.from_json({"asset": "USDT", "amount": "1000",
                               "liquid_reserve": "200", "horizon_days": 30,
                               "risk": "balanced"})
        with self.assertRaises(PlanUnavailable):
            compare(self.store, need, now=self.now)

    def test_history_cutoff_and_raw_integrity(self):
        self.seed(at=(self.now - timedelta(minutes=2)).isoformat())
        self.seed(apy="0.07", at=(self.now + timedelta(minutes=2)).isoformat())
        rows = market_observations(self.store, as_of=self.now)
        self.assertEqual(len(rows), 2)  # USDT and USDD at the prior fetch only
        self.assertEqual({row["facts"]["supply_apy"]["value"] for row in rows},
                         {"0.04", "0.02"})
        self.assertTrue(all(row["source_time_unknown"] and row["rights_status"] == "unknown"
                            for row in rows))
        old = self.store.snapshots_for("justlend_markets_v1", self.now.isoformat())[0]
        Path(old["raw_path"]).write_text("{}")
        with self.assertRaises(ValueError):
            market_observations(self.store, as_of=self.now)

    def test_normalized_fact_tampering_fails_closed(self):
        self.seed()
        snapshot = self.store.latest("justlend_markets_v1")
        with self.store.connect() as db:
            db.execute("UPDATE facts SET canonical_value='0.99' "
                       "WHERE snapshot_id=? AND subject_id=? AND metric_id='supply_apy'",
                       (snapshot["id"], self.address))
        specs = [{"id": source_id, "max_age_seconds": 900}
                 for source_id in ("justlend_contracts", "justlend_markets_v1",
                                   "justlend_usdd_rewards_v1", "usdd_earn_apy",
                                   "usdd_overview")]
        report = build_quality_report(self.store, specs, now=self.now)
        self.assertEqual(report["status"], "BLOCKED_FOR_RESEARCH_READ")
        need = Need.from_json({"asset": "USDT", "amount": "1000",
                               "liquid_reserve": "200", "horizon_days": 30,
                               "risk": "balanced"})
        with self.assertRaises(PlanUnavailable):
            compare(self.store, need, now=self.now)
        with self.assertRaises(ValueError):
            market_observations(self.store, as_of=self.now)

    def test_typed_api_draft_is_excluded_and_abstains(self):
        from research.fdc.api_draft_v3 import draft
        from research.fdc.validate_v3 import DatasetV3Error, check_episode

        self.seed()
        need = {"budget": {"value": "1000", "unit": "USDT"},
                "horizon_days": 30,
                "withdrawal": {"min_immediate": {"value": "250", "unit": "USDT"},
                               "latest_day": None, "max_notice_days": None},
                "risk": {"total_fraction": "0.75", "shared_group_fraction": "0.45"}}
        row = draft(self.store, need, text="작성 예시: 1000 USDT", now=self.now)
        self.assertEqual((row["split"], row["rights_status"], row["target"]["mode"]),
                         ("excluded", "unknown", "abstain"))
        self.assertIsNone(row["candidates"][0]["lock_days"])
        self.assertIsNone(row["candidates"][0]["observed_at"])
        self.assertEqual(row["candidates"][0]["network"], "tron_mainnet")
        self.assertEqual(row["candidates"][0]["protocol_version"], "justlend_v1")
        self.assertEqual(row["candidates"][0]["underlying_decimals"], 6)
        row["candidates"][0]["network"] = "synthetic"
        with self.assertRaisesRegex(DatasetV3Error, "TRON market identity"):
            check_episode(row, 1)
        row["candidates"][0]["network"] = "tron_mainnet"
        row["split"] = "train"
        with self.assertRaises(DatasetV3Error):
            check_episode(row, 1)


if __name__ == "__main__":
    unittest.main()
