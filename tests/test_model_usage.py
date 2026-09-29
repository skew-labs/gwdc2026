import unittest
import tempfile
from pathlib import Path

from economic_machine.values import MachineError
from finance_service.model_usage import (InMemoryModelUsageStore,
    OperationalModelUsageStore, normalize_usage_event)
from finance_service.operational_repository import OperationalRepository


def event(**changes):
    value = {"schema_version": "model-usage-1", "occurred_at": "2026-09-28T12:00:00Z",
        "trace_id": "trace-one", "flow": "mandate_intent", "provider": "kiln",
        "model_id": "qwen3-32b", "model_revision": None,
        "provider_request_id": None, "request_hash": "a" * 64,
        "policy_hash": "b" * 64, "response_hash": None, "input_tokens": None,
        "output_tokens": None, "latency_ms": 12, "attempts": 1,
        "outcome": "SUCCEEDED", "error_code": None, "cache": "MISS",
        "energy": {"kind": "UNMEASURED", "joules": None,
                   "measurement_source": None}}
    value.update(changes)
    return value


class ModelUsageTests(unittest.TestCase):
    def test_missing_usage_stays_null_and_store_is_idempotent(self):
        store = InMemoryModelUsageStore()
        first = store.append(event())
        second = store.append(event())
        self.assertEqual(first, second)
        self.assertIsNone(first["input_tokens"])
        self.assertIsNone(first["output_tokens"])
        self.assertEqual(len(store.for_trace("trace-one")), 1)

    def test_cache_hit_cannot_claim_tokens_or_provider_attempt(self):
        cached = event(outcome="CACHE_HIT", cache="HIT", attempts=0, latency_ms=0)
        self.assertEqual(normalize_usage_event(cached)["outcome"], "CACHE_HIT")
        with self.assertRaises(MachineError):
            normalize_usage_event({**cached, "input_tokens": 0})
        with self.assertRaises(MachineError):
            normalize_usage_event(event(outcome="FAILED", error_code=None))

    def test_operational_store_commits_usage_inside_authenticated_scope(self):
        scope = {"tenant_id": "tenant-one", "owner_id": "owner-one",
            "wallet": "41" + "11" * 20, "network": "tron-nile"}
        with tempfile.TemporaryDirectory() as temp:
            repository = OperationalRepository(Path(temp) / "usage.sqlite3")
            stored = OperationalModelUsageStore(repository).append(event(), scope=scope)
            record = repository.get_record(scope, "MODEL_USAGE", "usage-" + stored["event_id"])
            self.assertEqual(record["body"], stored)
            self.assertTrue(repository.verify_journal())
            with self.assertRaisesRegex(MachineError, "authenticated scope"):
                OperationalModelUsageStore(repository).append(event())


if __name__ == "__main__":
    unittest.main()
