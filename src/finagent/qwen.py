"""Opt-in external Qwen adapter. Model output is a draft, not approval."""

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

from .contracts import ContractError, Need, decimal_string


class ModelUnavailable(RuntimeError):
    def __init__(self, message: str, event: dict | None = None):
        super().__init__(message)
        self.event = event


def configured() -> bool:
    return all(os.environ.get(key) for key in (
        "GWDC_QWEN_BASE_URL", "GWDC_QWEN_MODEL_ID", "GWDC_QWEN_API_KEY"
    ))


FIELDS = ("asset", "amount", "liquid_reserve", "horizon_days", "risk")


def parse_model_answer(answer: dict) -> dict:
    if not isinstance(answer, dict) or set(answer) != {
        "intent", *FIELDS, "missing"
    }:
        raise ContractError("unexpected model response shape")
    if answer["intent"] not in {"PLAN", "REVISE", "REVIEW", "DISCOVER", "UNCLEAR"}:
        raise ContractError("unexpected intent")
    if not isinstance(answer["missing"], list) or any(
        item not in FIELDS for item in answer["missing"]
    ):
        raise ContractError("invalid missing fields")
    if len(answer["missing"]) != len(set(answer["missing"])):
        raise ContractError("duplicate missing fields")
    if set(answer["missing"]) != {key for key in FIELDS if answer[key] is None}:
        raise ContractError("missing fields do not match null values")
    if answer["asset"] is not None and answer["asset"] not in {"USDT", "USDD"}:
        raise ContractError("unsupported asset")
    for field in ("amount", "liquid_reserve"):
        if answer[field] is not None:
            decimal_string(answer[field])
    if answer["horizon_days"] is not None and (
        type(answer["horizon_days"]) is not int
        or not 1 <= answer["horizon_days"] <= 365
    ):
        raise ContractError("invalid horizon")
    if answer["risk"] is not None and answer["risk"] not in {
        "cautious", "balanced", "growth"
    }:
        raise ContractError("invalid risk")
    if not answer["missing"]:
        Need.from_json(answer)
    return answer


def interpret(message: str) -> tuple[dict, dict]:
    if not configured():
        raise ModelUnavailable("Qwen 32B endpoint is not configured")
    if not isinstance(message, str) or not 1 <= len(message) <= 2000:
        raise ContractError("message length must be 1 to 2000 characters")
    base = os.environ["GWDC_QWEN_BASE_URL"].rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ModelUnavailable("Qwen base URL must be HTTPS")
    model = os.environ["GWDC_QWEN_MODEL_ID"]
    system = (
        "You interpret a Korean user's TRON allocation request. Reply with exactly one JSON object "
        "with keys intent, asset, amount, liquid_reserve, horizon_days, risk, missing. "
        "intent: PLAN, REVISE, REVIEW, DISCOVER, or UNCLEAR. asset: USDT or USDD or null. "
        "amount and liquid_reserve: exact decimal strings or null. horizon_days: integer 1..365 or null. "
        "risk: cautious, balanced, growth, or null. missing: array of missing field names. "
        "Never invent absent values, rates, holdings, permissions, or approval. "
        "Return no markdown or other text."
    )
    payload = {"model": model, "temperature": 0, "max_tokens": 600,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": message}]}
    raw_prompt = json.dumps(payload["messages"], ensure_ascii=False, sort_keys=True)
    request = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + os.environ["GWDC_QWEN_API_KEY"],
                 "Content-Type": "application/json"}, method="POST",
    )
    start = time.monotonic()
    happened = datetime.now(timezone.utc).isoformat()
    response_body = None
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            raw = response.read(128_001)
            if len(raw) > 128_000:
                raise ModelUnavailable("model response too large")
            response_body = json.loads(raw.decode("utf-8"))
        content = response_body["choices"][0]["message"]["content"]
        answer = parse_model_answer(json.loads(content))
        return answer, _event(happened, model, raw_prompt, response_body,
                              start, "DRAFT_PARSED")
    except (urllib.error.URLError, KeyError, IndexError, TypeError, ValueError,
            ModelUnavailable) as exc:
        event = _event(happened, model, raw_prompt, response_body,
                       start, "ERROR:" + type(exc).__name__)
        raise ModelUnavailable(type(exc).__name__, event) from exc


def _event(happened, model, raw_prompt, response_body, start, outcome):
    usage = response_body.get("usage") if isinstance(response_body, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    prompt_hash = hashlib.sha256(raw_prompt.encode("utf-8")).hexdigest()
    return {
        "event_id": hashlib.sha256((happened + prompt_hash).encode()).hexdigest(),
        "occurred_at": happened, "flow": "needs_interpretation",
        "provider": "configured_qwen_endpoint", "model_id": model,
        "prompt_sha256": prompt_hash,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "latency_ms": round((time.monotonic() - start) * 1000),
        "outcome": outcome,
    }
