import unittest

from economic_machine.values import MachineError
from finance_service.model_usage import InMemoryModelUsageStore, normalize_usage_event


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


if __name__ == "__main__":
    unittest.main()
