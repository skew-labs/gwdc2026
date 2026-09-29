"""Qwen intent proposals bound to authenticated, confirmed mandate state.

The service can produce a source-linked MandateV1 candidate.  It deliberately
does not store, confirm, plan, sign, or submit the candidate.  The existing
MandateService confirmation boundary remains mandatory.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
from threading import RLock

from economic_machine.application import confirmed_mandate
from economic_machine.mandate import draft_hash, normalize_mandate, policy_hash
from economic_machine.values import MachineError, digest, utc
from finagent.contracts import ContractError
from finagent.qwen import intent_messages, parse_intent_answer

from .context import AuthenticatedContext
from .model_provider import ModelProviderError


QUESTIONS = {
    "capital": "운용할 금액과 자산 단위를 알려주세요.",
    "base_asset": "수익과 한도를 계산할 기준 자산을 알려주세요.",
    "risk_profile": "위험 성향을 cautious, balanced, growth 중에서 골라주세요.",
    "horizon_seconds": "운용 기간을 알려주세요.",
    "immediate_cash": "즉시 출금 가능하게 남길 금액 또는 비율을 알려주세요.",
    "withdrawals": "필요한 출금 시점과 각 시점의 최소 회수 가능액을 알려주세요.",
    "borrowing_consent": "담보 대출 경로 사용 여부를 명시해 주세요.",
    "max_debt": "허용할 최대 부채 금액과 단위를 알려주세요.",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


class IntentService:
    def __init__(self, mandate_repository, provider, usage_store, clock=_now):
        self.repository = mandate_repository
        self.provider = provider
        self.usage = usage_store
        self.clock = clock
        self._cache = {}
        self._lock = RLock()

    @staticmethod
    def _usage(context, at, request_hash, policy, metadata, *, outcome,
               cache, error_code=None):
        energy = metadata.get("energy") or {
            "kind": "UNMEASURED", "joules": None, "measurement_source": None}
        return {"schema_version": "model-usage-1", "occurred_at": at,
            "trace_id": context.trace_id, "flow": "mandate_intent",
            "provider": metadata.get("provider") or "kiln",
            "model_id": metadata.get("model_id") or "qwen3-32b",
            "model_revision": metadata.get("model_revision"),
            "provider_request_id": metadata.get("request_id"),
            "request_hash": request_hash, "policy_hash": policy,
            "response_hash": metadata.get("response_sha256"),
            "input_tokens": metadata.get("input_tokens"),
            "output_tokens": metadata.get("output_tokens"),
            "latency_ms": metadata.get("latency_ms", 0),
            "attempts": metadata.get("attempts", 0), "outcome": outcome,
            "error_code": error_code, "cache": cache, "energy": energy}

    @staticmethod
    def _candidate(prior, patch, evidence, message, context, request_hash):
        candidate = deepcopy(prior)
        candidate["revision"] += 1
        candidate["trace_id"] = context.trace_id
        message_id = "user-" + request_hash[:24]
        candidate["source_messages"].append({"message_id": message_id, "text": message})
        terms = candidate["terms"]
        direct = {"capital", "base_asset", "risk_profile", "horizon_seconds",
                  "immediate_cash", "withdrawals"}
        for field in direct & set(patch):
            terms[field] = deepcopy(patch[field])
            candidate["source_refs"][field] = [{"message_id": message_id,
                                                 "quote": evidence[field]}]
        borrowing_quotes = []
        if "borrowing_consent" in patch:
            terms["borrowing"]["consent"] = patch["borrowing_consent"]
            borrowing_quotes.append(evidence["borrowing_consent"])
            if patch["borrowing_consent"] is False and "max_debt" not in patch:
                terms["borrowing"]["max_debt"] = {
                    "asset": terms["base_asset"], "amount": "0"}
        if "max_debt" in patch:
            terms["borrowing"]["max_debt"] = deepcopy(patch["max_debt"])
            borrowing_quotes.append(evidence["max_debt"])
        if borrowing_quotes:
            candidate["source_refs"]["borrowing"] = [
                {"message_id": message_id, "quote": quote}
                for quote in dict.fromkeys(borrowing_quotes)]
        return normalize_mandate(candidate)

    def propose_revision(self, context: AuthenticatedContext, mandate_id: str,
                         message: str) -> dict:
        if not isinstance(context, AuthenticatedContext):
            raise MachineError("authenticated service context required")
        if not isinstance(message, str) or not 1 <= len(message) <= 4000:
            raise MachineError("intent message length must be 1 to 4000 characters")
        at = utc(self.clock())
        scope = context.authorize(at)
        aggregate = self.repository.read(scope, mandate_id)
        record = aggregate["revisions"][-1]
        prior = confirmed_mandate(record, at)
        policy = policy_hash(prior)
        request_hash = digest({"domain": "financial-intent-request-1",
            "scope": scope, "mandate_id": mandate_id, "revision": prior["revision"],
            "policy_hash": policy, "message": message})
        with self._lock:
            cached = deepcopy(self._cache.get(request_hash))
        if cached is not None:
            answer = cached["answer"]
            event = self.usage.append(self._usage(context, at, request_hash, policy, {
                "provider": cached["provider"], "model_id": cached["model_id"],
                "model_revision": cached["model_revision"], "attempts": 0,
                "latency_ms": 0, "input_tokens": None, "output_tokens": None,
                "response_sha256": cached["response_sha256"],
                "energy": {"kind": "UNMEASURED", "joules": None,
                           "measurement_source": None}},
                outcome="CACHE_HIT", cache="HIT"), scope=scope)
            return self._result(context, prior, answer, message, request_hash, event)
        messages = intent_messages(message, prior["terms"])
        try:
            response = self.provider.complete(messages)
            try:
                answer = parse_intent_answer(json.loads(response["content"]), message)
            except (ValueError, TypeError, ContractError) as exc:
                metadata = {**response, "response_sha256": response.get("response_sha256")}
                event = self.usage.append(self._usage(context, at, request_hash, policy,
                    metadata, outcome="FAILED", cache="MISS",
                    error_code="INVALID_MODEL_OUTPUT"), scope=scope)
                return {"schema_version": "intent-service-result-1",
                    "status": "MODEL_OUTPUT_REJECTED", "reason_codes": ["INVALID_MODEL_OUTPUT"],
                    "questions": [], "candidate": None, "candidate_draft_hash": None,
                    "candidate_policy_hash": None, "usage_event_id": event["event_id"],
                    "execution_authority": "NONE", "chain_status": "NOT_SUBMITTED"}
        except ModelProviderError as exc:
            event = self.usage.append(self._usage(context, at, request_hash, policy,
                exc.metadata, outcome="FAILED", cache="MISS", error_code=exc.code),
                scope=scope)
            return {"schema_version": "intent-service-result-1",
                "status": "MODEL_UNAVAILABLE", "reason_codes": [exc.code],
                "questions": [], "candidate": None, "candidate_draft_hash": None,
                "candidate_policy_hash": None, "usage_event_id": event["event_id"],
                "execution_authority": "NONE", "chain_status": "NOT_SUBMITTED"}
        with self._lock:
            self._cache[request_hash] = {"answer": deepcopy(answer),
                "provider": response["provider"], "model_id": response["model_id"],
                "model_revision": response.get("model_revision"),
                "response_sha256": response["response_sha256"]}
        event = self.usage.append(self._usage(context, at, request_hash, policy, response,
            outcome="SUCCEEDED", cache="MISS"), scope=scope)
        return self._result(context, prior, answer, message, request_hash, event)

    def _result(self, context, prior, answer, message, request_hash, event):
        questions = [{"field": field, "question": QUESTIONS[field]}
                     for field in answer["missing"]]
        if questions:
            status, candidate = "NEEDS_INFORMATION", None
        elif not answer["patch"]:
            status, candidate = "NO_CHANGE", None
        else:
            try:
                candidate = self._candidate(prior, answer["patch"], answer["evidence"],
                                            message, context, request_hash)
            except MachineError:
                status, candidate = "CANDIDATE_REJECTED", None
                answer = {**answer, "reason_codes": sorted(set(
                    [*answer["reason_codes"], "CANDIDATE_CONSTRAINT_CONFLICT"]))}
            else:
                status = "DRAFT_READY"
        return {"schema_version": "intent-service-result-1", "status": status,
            "reason_codes": answer["reason_codes"], "questions": questions,
            "candidate": candidate,
            "candidate_draft_hash": draft_hash(candidate) if candidate else None,
            "candidate_policy_hash": policy_hash(candidate) if candidate else None,
            "usage_event_id": event["event_id"], "execution_authority": "NONE",
            "chain_status": "NOT_SUBMITTED"}
