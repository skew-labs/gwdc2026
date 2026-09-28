"""Runner invariants on a node-owned, isolated dataset; run on Cherry."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from finagent.fs1_runner import register_policy, run_once, status
from finagent.store import Store
from research.fdc.auto_cases import run_once as mine_cases


class FS1RunnerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / "live")
        self.cases = self.root / "cases"
        self.runtime = self.root / "runner"
        self.base = datetime.now(timezone.utc).replace(microsecond=0)
        self.usdt = "T" + "a" * 33
        self.usdd = "T" + "b" * 33
        self.planner_calls = []
        self.policy = {"policy_id": "demo-alpha", "node_id": "owner-node-1",
                       "network": "tron-mainnet-read", "consent": "DEMO_SCENARIO",
                       "mode": "READ_ONLY", "need": {
                           "asset": "USDT", "amount": "1000", "liquid_reserve": "500",
                           "horizon_days": 30, "risk": "balanced"}}
        self._aux_sources()
        self._market(self.base, "0.04", "0.02")
        mine_cases(self.store.root, self.cases)

    def _save(self, source_id, payload, facts, at, markets=None):
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return self.store.save_snapshot(
            {"id": source_id, "url": "https://example.invalid/" + source_id},
            at.isoformat(), raw, payload, markets or [], facts)

    def _aux_sources(self):
        self._save("justlend_contracts", {"network": "tron"}, [], self.base)
        self._save("justlend_usdd_rewards_v1", {"reward": "0"}, [{
            "subject_id": self.usdt, "metric_id": "usdd_reward_apy",
            "raw_value": "0", "canonical_value": "0", "unit": "annual_fraction",
            "quality": "VALID_ZERO", "json_path": "/reward"}], self.base)
        self._save("usdd_earn_apy", {"apy": "0.03"}, [{
            "subject_id": "usdd:tron", "metric_id": "usdd_tron_earn_apy",
            "raw_value": "0.03", "canonical_value": "0.03", "unit": "annual_fraction",
            "quality": "VALID", "json_path": "/apy"}], self.base)

    def _market(self, at, usdt_apy, usdd_apy):
        payload = {"usdt": {"apy": usdt_apy, "cash": "5000"},
                   "usdd": {"apy": usdd_apy, "cash": "6000"}}
        markets = [{"market_address": address, "network": "tron-mainnet",
                    "jtoken_symbol": symbol, "underlying_symbol": underlying,
                    "underlying_address": "T" + "c" * 33,
                    "underlying_decimals": 6, "status": "active"}
                   for address, symbol, underlying in
                   ((self.usdt, "jUSDT", "USDT"), (self.usdd, "jUSDD", "USDD"))]
        facts = []
        for address, key, apy in ((self.usdt, "usdt", usdt_apy),
                                  (self.usdd, "usdd", usdd_apy)):
            facts.extend([
                {"subject_id": address, "metric_id": "supply_apy",
                 "raw_value": apy, "canonical_value": apy,
                 "unit": "annual_fraction", "quality": "VALID" if apy is not None else "MISSING",
                 "json_path": f"/{key}/apy"},
                {"subject_id": address, "metric_id": "available_cash",
                 "raw_value": payload[key]["cash"], "canonical_value": payload[key]["cash"],
                 "unit": "underlying_tokens", "quality": "VALID",
                 "json_path": f"/{key}/cash"},
            ])
        self._save("justlend_markets_v1", payload, facts, at, markets)
        mine_cases(self.store.root, self.cases)

    def _planner(self, store, need, *, now):
        self.planner_calls.append((need.asset, now))
        return {"plan_id": "research-plan-" + str(len(self.planner_calls)),
                "state": "RESEARCH_COMPARISON_ONLY",
                "plans": [{"executable": False}, {"executable": False}]}

    def _run(self, seconds):
        return run_once(self.store, self.cases, self.runtime,
                        now=self.base + timedelta(seconds=seconds), planner=self._planner)

    def test_unmodified_and_unrelated_changes_do_not_replan(self):
        register_policy(self.store, self.runtime, self.policy,
                        now=self.base + timedelta(seconds=1))
        initial = self._run(1)
        self.assertEqual((initial["planner_calls"], initial["llm_calls"]), (1, 0))
        self._market(self.base + timedelta(seconds=2), "0.04", "0.02")
        unchanged = self._run(2)
        self.assertEqual((unchanged["changed_cases"], unchanged["planner_calls"]), (0, 0))
        self._market(self.base + timedelta(seconds=3), "0.04", "0.025")
        unrelated = self._run(3)
        self.assertEqual((unrelated["affected_policies"], unrelated["planner_calls"]), (0, 0))
        self._market(self.base + timedelta(seconds=4), "0.05", "0.025")
        related = self._run(4)
        self.assertEqual((related["affected_policies"], related["planner_calls"]), (1, 1))
        self.assertEqual(related["decisions"][0]["state"], "REPLAN_RESEARCH_ONLY")
        self.assertFalse(related["decisions"][0]["execution_permitted"])
        self.assertEqual(self._run(4)["planner_calls"], 0)
        self.assertEqual(len(self.planner_calls), 2)

    def test_quality_failure_pauses_without_repeated_planning_and_recovers(self):
        register_policy(self.store, self.runtime, self.policy,
                        now=self.base + timedelta(seconds=1))
        self._run(1)
        self._market(self.base + timedelta(seconds=2), None, "0.02")
        paused = self._run(2)
        self.assertEqual((paused["planner_calls"], paused["decisions"][0]["state"]),
                         (0, "PAUSED"))
        self.assertEqual(self._run(3)["planner_calls"], 0)
        self._market(self.base + timedelta(seconds=4), "0.06", "0.02")
        recovered = self._run(4)
        self.assertEqual((recovered["planner_calls"], recovered["decisions"][0]["state"]),
                         (1, "REPLAN_RESEARCH_ONLY"))

    def test_policy_revision_invalidates_previous_decision(self):
        first = register_policy(self.store, self.runtime, self.policy,
                                now=self.base + timedelta(seconds=1))
        self._run(1)
        revised = {**self.policy, "need": {**self.policy["need"], "liquid_reserve": "800"}}
        second = register_policy(self.store, self.runtime, revised,
                                 now=self.base + timedelta(seconds=2))
        self.assertNotEqual(first["policy_hash"], second["policy_hash"])
        self.assertEqual(status(self.runtime)["policies"][0]["state"], "AWAITING_FIRST_RUN")
        receipt = self._run(2)["decisions"][0]
        self.assertEqual((receipt["policy_version"], receipt["policy_hash"]),
                         (2, second["policy_hash"]))

    def test_degradation_then_recovery_before_runner_uses_latest_valid_state(self):
        register_policy(self.store, self.runtime, self.policy,
                        now=self.base + timedelta(seconds=1))
        self._run(1)
        self._market(self.base + timedelta(seconds=2), None, "0.02")
        self._market(self.base + timedelta(seconds=3), "0.08", "0.02")
        result = self._run(3)
        self.assertEqual((result["affected_policies"], result["planner_calls"]), (1, 1))
        self.assertEqual(result["decisions"][0]["state"], "REPLAN_RESEARCH_ONLY")
        self.assertEqual(len(result["decisions"][0]["case_ids"]), 2)

    def test_retrying_same_policy_does_not_create_new_version(self):
        first = register_policy(self.store, self.runtime, self.policy,
                                now=self.base + timedelta(seconds=1))
        retry = register_policy(self.store, self.runtime, self.policy,
                                now=self.base + timedelta(seconds=2))
        self.assertEqual(first["policy_hash"], retry["policy_hash"])
        self.assertEqual(retry["version"], 1)
        self.assertTrue(retry["idempotent"])

    def test_batch_tamper_fails_closed(self):
        register_policy(self.store, self.runtime, self.policy,
                        now=self.base + timedelta(seconds=1))
        self._run(1)
        before = set((self.cases / "batches").glob("*.jsonl"))
        self._market(self.base + timedelta(seconds=2), "0.07", "0.02")
        newest, = set((self.cases / "batches").glob("*.jsonl")) - before
        newest.write_text("tampered")
        with self.assertRaisesRegex(ValueError, "case batch hash mismatch"):
            self._run(2)

    def test_non_demo_or_non_read_only_policy_rejected(self):
        with self.assertRaisesRegex(ValueError, "read-only demo"):
            register_policy(self.store, self.runtime,
                            {**self.policy, "mode": "AUTO_TRADE"})


if __name__ == "__main__":
    unittest.main()
