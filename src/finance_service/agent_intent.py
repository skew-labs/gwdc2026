"""Authenticated first-message extraction for the agent workspace.

The model may extract typed user conditions. It cannot choose an allocation,
confirm a mandate, request a wallet transaction, or grant execution authority.
"""

import json
import re

from economic_machine.values import MachineError, digest, utc
from finagent.contracts import ContractError
from finagent.qwen import intent_messages, parse_intent_answer

from .context import AuthenticatedContext
from .model_provider import ModelProviderError

QUESTIONS = {
    "capital": "How much capital and which asset should the plan use?",
    "base_asset": "Which asset should be used to value returns and limits?",
    "risk_profile": "Choose a cautious, balanced or growth risk profile.",
    "horizon_seconds": "How long do you want to allocate the capital?",
    "immediate_cash": "How much should remain immediately available as cash?",
    "withdrawals": "Do you need scheduled withdrawals? If so, when and how much?",
    "borrowing_consent": "Should collateralized borrowing be permitted?",
    "max_debt": "What is the maximum permitted debt amount and asset?",
}


class AgentIntentService:
    def __init__(self, provider, usage_store, clock):
        self.provider = provider
        self.usage = usage_store
        self.clock = clock

    @staticmethod
    def _usage(context, at, request_hash, metadata, *, outcome, error_code=None):
        energy = metadata.get("energy") or {
            "kind": "UNMEASURED", "joules": None, "measurement_source": None}
        return {"schema_version": "model-usage-1", "occurred_at": at,
            "trace_id": context.trace_id, "flow": "initial_mandate_intent",
            "provider": metadata.get("provider") or "kiln",
            "model_id": metadata.get("model_id") or "qwen3-32b",
            "model_revision": metadata.get("model_revision"),
            "provider_request_id": metadata.get("request_id"),
            "request_hash": request_hash, "policy_hash": "0" * 64,
            "response_hash": metadata.get("response_sha256"),
            "input_tokens": metadata.get("input_tokens"),
            "output_tokens": metadata.get("output_tokens"),
            "latency_ms": metadata.get("latency_ms", 0),
            "attempts": metadata.get("attempts", 0), "outcome": outcome,
            "error_code": error_code, "cache": "MISS", "energy": energy}

    def propose(self, context: AuthenticatedContext, message: str, current_terms: dict | None = None) -> dict:
        if not isinstance(context, AuthenticatedContext):
            raise MachineError("authenticated service context required")
        if not isinstance(message, str) or not 1 <= len(message) <= 4000:
            raise MachineError("intent message length must be 1 to 4000 characters")
        at = utc(self.clock())
        scope = context.authorize(at)
        request_hash = digest({"domain": "initial-financial-intent-request-1",
            "scope": scope, "message": message, "current_terms": current_terms or {}})
        try:
            response = self.provider.complete(intent_messages(message, current_terms or {}))
            try:
                content = response["content"].strip()
                if content.startswith("```json\n") and content.endswith("\n```"):
                    content = content[8:-4]
                raw = json.loads(content)
                if not isinstance(raw, dict) or not isinstance(raw.get("patch"), dict) or not isinstance(raw.get("evidence"), dict):
                    raise ContractError("intent response must contain patch and evidence objects")
                # Risk appetite and a generic "yes" are not borrowing consent.
                # Preserve other extractable fields, but require a source quote
                # that actually names borrowing before proposing permission.
                patch = raw.get('patch', {})
                quote = raw.get('evidence', {}).get('borrowing_consent', '')
                if patch.get('borrowing_consent') is True and not re.search(
                        r'borrow|debt|loan|leverage|차입|대출|빌리|빌려|레버리지|빚', quote, re.I):
                    patch.pop('borrowing_consent', None)
                    raw.get('evidence', {}).pop('borrowing_consent', None)
                    raw['reason_codes'] = list(dict.fromkeys(raw.get('reason_codes', []) + ['EXPLICIT_BORROWING_REQUIRED']))
                    raw['missing'] = list(dict.fromkeys(raw.get('missing', []) + ['borrowing_consent']))
                answer = parse_intent_answer(raw, message)
            except (ValueError, TypeError, ContractError) as exc:
                event = self.usage.append(self._usage(context, at, request_hash, response,
                    outcome="FAILED", error_code="INVALID_MODEL_OUTPUT"), scope=scope)
                return self._failed("MODEL_OUTPUT_REJECTED", "INVALID_MODEL_OUTPUT",
                                    event["event_id"])
        except ModelProviderError as exc:
            event = self.usage.append(self._usage(context, at, request_hash, exc.metadata,
                outcome="FAILED", error_code=exc.code), scope=scope)
            return self._failed("MODEL_UNAVAILABLE", exc.code, event["event_id"])
        event = self.usage.append(self._usage(context, at, request_hash, response,
            outcome="SUCCEEDED"), scope=scope)
        missing = [field for field in answer["missing"] if not (
            field == "max_debt" and answer["patch"].get("borrowing_consent") is False)]
        questions = [{"field": field, "question": QUESTIONS[field]} for field in missing]
        status = "NEEDS_INFORMATION" if questions else (
            "DRAFT_READY" if answer["patch"] else "NO_CHANGE")
        return {"schema_version": "agent-intent-result-1", "status": status,
            "intent": answer["intent"], "patch": answer["patch"],
            "evidence": answer["evidence"], "reason_codes": answer["reason_codes"],
            "questions": questions, "request_hash": request_hash,
            "usage_event_id": event["event_id"], "execution_authority": "NONE",
            "signature_status": "NOT_REQUESTED", "chain_status": "NOT_SUBMITTED"}

    @staticmethod
    def _failed(status, code, event_id):
        return {"schema_version": "agent-intent-result-1", "status": status,
            "intent": None, "patch": {}, "evidence": {}, "reason_codes": [code],
            "questions": [], "request_hash": None, "usage_event_id": event_id,
            "execution_authority": "NONE", "signature_status": "NOT_REQUESTED",
            "chain_status": "NOT_SUBMITTED"}
