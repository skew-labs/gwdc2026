"""Privacy-minimized model usage receipts.

Records contain hashes and provider counters, never prompts, completions, API
keys, or hidden reasoning. Missing provider counters remain null.
"""

from copy import deepcopy
from threading import RLock

from economic_machine.mandate import hash32, integer
from economic_machine.values import MachineError, decimal, digest, ident, require_keys, utc


VERSION = "model-usage-1"
OUTCOMES = {"SUCCEEDED", "FAILED", "CACHE_HIT"}
CACHE = {"MISS", "HIT"}


def _nullable_counter(value, label):
    if value is None:
        return None
    return integer(value, label, 0, 2_147_483_647)


def _nullable_provider_text(value, label):
    if value is None:
        return None
    if (not isinstance(value, str) or not 1 <= len(value) <= 256
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise MachineError("invalid " + label)
    return value


def normalize_usage_event(raw: dict) -> dict:
    require_keys(raw, {"schema_version", "occurred_at", "trace_id", "flow",
        "provider", "model_id", "model_revision", "provider_request_id",
        "request_hash", "policy_hash", "response_hash", "input_tokens",
        "output_tokens", "latency_ms", "attempts", "outcome", "error_code",
        "cache", "energy"}, "ModelUsageV1")
    if raw["schema_version"] != VERSION:
        raise MachineError("unsupported model usage version")
    outcome, cache = raw["outcome"], raw["cache"]
    if outcome not in OUTCOMES or cache not in CACHE:
        raise MachineError("invalid model usage outcome")
    for key in ("trace_id", "flow", "provider", "model_id"):
        ident(raw[key], key)
    for key in ("request_hash", "policy_hash"):
        hash32(raw[key], key)
    response = raw["response_hash"]
    if response is not None:
        hash32(response, "response hash")
    for key in ("model_revision", "provider_request_id"):
        _nullable_provider_text(raw[key], key)
    if raw["error_code"] is not None:
        ident(raw["error_code"], "error code")
    energy = require_keys(raw["energy"], {"kind", "joules", "measurement_source"},
                          "energy measurement")
    if energy["kind"] not in {"MEASURED", "ESTIMATED", "UNMEASURED"}:
        raise MachineError("invalid energy measurement kind")
    if energy["kind"] == "UNMEASURED":
        if energy["joules"] is not None or energy["measurement_source"] is not None:
            raise MachineError("unmeasured energy cannot contain a value")
    else:
        if energy["measurement_source"] is None:
            raise MachineError("measured or estimated energy needs value and source")
        _nullable_provider_text(energy["measurement_source"], "energy source")
        if decimal(energy["joules"]) <= 0:
            raise MachineError("energy value must be positive")
    result = {**raw, "occurred_at": utc(raw["occurred_at"]),
              "input_tokens": _nullable_counter(raw["input_tokens"], "input tokens"),
              "output_tokens": _nullable_counter(raw["output_tokens"], "output tokens"),
              "latency_ms": integer(raw["latency_ms"], "latency", 0, 86_400_000),
              "attempts": integer(raw["attempts"], "attempts", 0, 2)}
    if outcome == "CACHE_HIT" and (cache != "HIT" or result["attempts"] != 0
            or result["input_tokens"] is not None or result["output_tokens"] is not None):
        raise MachineError("cache hit cannot claim a provider call or token usage")
    if outcome == "SUCCEEDED" and cache != "MISS":
        raise MachineError("provider success must be a cache miss")
    if outcome == "FAILED" and not raw["error_code"]:
        raise MachineError("failed model usage needs an error code")
    result["event_id"] = digest({"domain": VERSION, "event": result})
    return result


class InMemoryModelUsageStore:
    """Reference adapter; durable multi-instance storage belongs to PR09."""

    def __init__(self):
        self._events = {}
        self._lock = RLock()

    def append(self, raw: dict, *, scope=None) -> dict:
        event = normalize_usage_event(raw)
        with self._lock:
            existing = self._events.get(event["event_id"])
            if existing is not None and existing != event:
                raise MachineError("model usage event collision")
            self._events[event["event_id"]] = deepcopy(event)
        return deepcopy(event)

    def for_trace(self, trace_id: str) -> list[dict]:
        ident(trace_id, "trace id")
        with self._lock:
            return [deepcopy(item) for item in self._events.values()
                    if item["trace_id"] == trace_id]


class OperationalModelUsageStore:
    """Persist privacy-minimized receipts in the scoped operational journal."""

    def __init__(self, repository):
        self.repository = repository

    def append(self, raw: dict, *, scope=None) -> dict:
        if scope is None:
            raise MachineError("model usage persistence requires authenticated scope")
        event = normalize_usage_event(raw)
        record = self.repository.put_record(scope, "MODEL_USAGE", "usage-" + event["event_id"], event,
            expected_version=0, at=event["occurred_at"])
        if record["body"] != event:
            raise MachineError("stored model usage commitment mismatch")
        return deepcopy(event)
