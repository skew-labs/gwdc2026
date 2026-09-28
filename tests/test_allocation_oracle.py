"""Allocation oracle checks model proposals, not the episode target label."""

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from jsonschema import Draft202012Validator

from finagent.normalize import normalize
from finagent.store import Store
from research.fdc.allocation_oracle import judge, judge_counterfactual_pair
from research.fdc.mine_oracle_cases import _variants, mine
from research.fdc.synthetic_v3 import generate


class AllocationOracleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dataset = self.root / "synthetic.jsonl"
        generate(self.dataset, families=21, seed=33)
        self.rows = [json.loads(line) for line in self.dataset.read_text().splitlines()]
        self.propose = next(row for row in self.rows if row["target"]["mode"] == "propose")

    def test_independent_positive_and_hard_negatives(self):
        proposal = {"mode": "propose", "plans": deepcopy(self.propose["target"]["plans"])}
        input_only = {key: value for key, value in self.propose.items() if key != "target"}
        positive = judge(input_only, proposal)
        verdict_schema = json.loads((Path(__file__).resolve().parents[1]
                                     / "contracts/allocation_oracle_v1.schema.json").read_text())
        Draft202012Validator(verdict_schema).validate(positive)
        self.assertEqual(positive["verdict"], "CONSTRAINTS_PASS")
        self.assertFalse(positive["human_gold"])
        self.assertFalse(positive["optimality_proven"])
        changed_target = deepcopy(self.propose)
        changed_target["target"] = {"fabricated": "ignored"}
        self.assertEqual(judge(changed_target, proposal), positive)
        for mutation, episode, changed, expected, code in _variants(self.propose):
            result = judge(episode, changed)
            self.assertEqual(result["verdict"], expected, mutation)
            if code:
                self.assertIn(code, {item["code"] for item in result["hard_failures"]})
        numeric = deepcopy(proposal)
        numeric["plans"][0]["hold"] = 2.5
        self.assertEqual(judge(input_only, numeric)["hard_failures"][0]["code"],
                         "PROPOSAL_SCHEMA_INVALID")
        missing_dependency = deepcopy(proposal)
        missing_dependency["plans"][0]["dependency_ids"].remove("budget")
        self.assertIn("MISSING_REQUIRED_DEPENDENCY",
                      {item["code"] for item in
                       judge(input_only, missing_dependency)["hard_failures"]})
        duplicate_plan = deepcopy(proposal)
        duplicate_plan["plans"].append(deepcopy(duplicate_plan["plans"][0]))
        self.assertIn("DUPLICATE_PLAN_ID",
                      {item["code"] for item in
                       judge(input_only, duplicate_plan)["hard_failures"]})

    def test_real_fact_reconciliation_and_unknown_terms(self):
        address = "T" + "a" * 33
        unit = self.propose["need"]["budget"]["unit"]
        token = {"address": address, "symbol": "j" + unit,
                 "underlyingSymbol": unit, "underlyingAddress": "T" + "b" * 33,
                 "underlyingDecimal": 6, "supplyRate": "0.04", "cash": "10000"}
        raw = json.dumps({"code": 0, "data": {"tokenList": [token]}}).encode()
        payload = json.loads(raw)
        markets, facts = normalize("justlend_markets_v1", payload, {address: "active"})
        store = Store(self.root / "store")
        as_of = datetime.fromisoformat(self.propose["as_of"])
        snapshot_id = store.save_snapshot({"id": "justlend_markets_v1",
                                          "url": "https://openapi.just.network/lend/jtoken"},
                                         (as_of - timedelta(minutes=1)).isoformat(),
                                         raw, payload, markets, facts)
        by_metric = {fact["metric_id"]: fact for fact in facts}
        episode = {key: deepcopy(value) for key, value in self.propose.items()
                   if key != "target"}
        episode.update(source_mode="api_observation", rights_status="unknown",
                       review_status="unreviewed", split="excluded",
                       need_origin="authored_example", source_release="fixture-release")
        candidate = {"id": address, "asset": unit, "status": "active",
                     "network": "tron_mainnet", "protocol_version": "justlend_v1",
                     "underlying_address": token["underlyingAddress"],
                     "underlying_decimals": 6, "supply_apy": "0.04",
                     "available_cash": {"value": "10000", "unit": unit},
                     "lock_days": None, "notice_days": None,
                     "observed_at": None, "valid_until": None,
                     "risk_groups": ["protocol:justlend-v1", "token:" + unit.lower()],
                     "history": [], "source_ref": snapshot_id,
                     "evidence_ids": ["rate", "cash"]}
        episode["candidates"] = [candidate]
        episode["evidence"] = [
            {"id": eid, "text": metric, "source_ref": snapshot_id,
             "locator": by_metric[metric]["json_path"]}
            for eid, metric in (("rate", "supply_apy"), ("cash", "available_cash"))]
        budget = Decimal(episode["need"]["budget"]["value"])
        size = budget / 10
        proposal = {"mode": "propose", "plans": [{
            "plan_id": "candidate", "allocations": {address: format(size, "f")},
            "hold": format(budget - size, "f"), "evidence_ids": ["rate", "cash"],
            "dependency_ids": ["budget", "source_freshness", "withdrawal_deadline",
                               "withdrawal_notice", "market_cash"]}]}
        result = judge(episode, proposal, store=store)
        verdict_schema = json.loads((Path(__file__).resolve().parents[1]
                                     / "contracts/allocation_oracle_v1.schema.json").read_text())
        Draft202012Validator(verdict_schema).validate(result)
        self.assertEqual(result["verdict"], "NEEDS_EVIDENCE")
        self.assertEqual(result["training_scope"], "excluded_unreviewed")
        self.assertEqual(result["source_witnesses"][0]["snapshot_id"], snapshot_id)
        self.assertEqual(result["source_witnesses"][0]["raw_sha256"],
                         store.latest("justlend_markets_v1")["sha256"])
        self.assertNotIn("SOURCE_FACT_MISMATCH",
                         {item["code"] for item in result["hard_failures"]})
        episode["candidates"][0]["supply_apy"] = "0.05"
        bad = judge(episode, proposal, store=store)
        self.assertEqual(bad["verdict"], "REJECTED")
        self.assertIn("SOURCE_FACT_MISMATCH",
                      {item["code"] for item in bad["hard_failures"]})
        episode["candidates"][0]["supply_apy"] = "0.04"
        episode["as_of"] = (as_of + timedelta(hours=1)).isoformat()
        stale = judge(episode, proposal, store=store)
        self.assertIn("SOURCE_SNAPSHOT_OUT_OF_TIME",
                      {item["code"] for item in stale["hard_failures"]})

    def test_counterfactual_reused_plan_fails_new_risk_cap(self):
        base = {key: deepcopy(value) for key, value in self.propose.items()
                if key != "target"}
        changed = deepcopy(base)
        changed["episode_id"] += "-tight-risk"
        changed["need"]["risk"]["shared_group_fraction"] = "0.01"
        proposal = {"mode": "propose", "plans": deepcopy(self.propose["target"]["plans"])}
        result = judge_counterfactual_pair(base, changed, proposal, proposal)
        pair_schema = json.loads((Path(__file__).resolve().parents[1]
                                  / "contracts/allocation_pair_verdict_v1.schema.json").read_text())
        Draft202012Validator(pair_schema).validate(result)
        self.assertEqual(result["changed_field"], "need.risk.shared_group_fraction")
        self.assertEqual(result["pair_verdict"], "CHANGED_PROPOSAL_REJECTED")
        self.assertIn("SHARED_RISK_CAP_EXCEEDED", result["changed_failure_codes"])
        self.assertFalse(result["proposal_changed"])
        with self.assertRaisesRegex(ValueError, "exactly one"):
            judge_counterfactual_pair(base, base, proposal, proposal)

    def test_mined_cases_preserve_splits_and_strip_targets(self):
        output = self.root / "oracle-cases.jsonl"
        manifest = mine(self.dataset, output,
                        Path(__file__).resolve().parents[1] / "contracts/episode_v3.schema.json")
        records = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual(len(records), manifest["case_count"])
        self.assertGreater(manifest["mutations"]["budget_plus_one"], 0)
        self.assertGreater(manifest["mutations"].get("shared_group_over_cap", 0), 0)
        self.assertTrue(all("target" not in item["input"] and not item["human_gold"]
                            and item["label_scope"] == "synthetic_constraint_only"
                            for item in records))
        self.assertEqual(len({(item["scenario_family"], item["split"])
                              for item in records}),
                         len({item["scenario_family"] for item in records}))
        self.assertEqual(len({item["scenario_family"] for item in records}),
                         manifest["case_family_count"])
        self.assertGreaterEqual(manifest["source_family_count"],
                                manifest["case_family_count"])


if __name__ == "__main__":
    unittest.main()
