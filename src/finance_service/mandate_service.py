"""Atomic mandate lifecycle, unsigned review invalidation and conservative holds.

This service exposes no signing/submission/release path. A hold records that
capital must not be reused while a future execution adapter reconciles a
possible external effect. It cannot authorize that effect. Scope comes from
the trusted session adapter and time from the server's clock.
"""

from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Callable

from economic_machine.application import compile_review_projections, confirmed_mandate, single_asset_budget
from economic_machine.mandate import (assert_effective, draft_hash, hash32, money,
                                     normalize_mandate, policy_hash)
from economic_machine.values import MachineError, decimal, digest, ident, require_keys, utc

from .context import AuthenticatedContext
from .repository import MandateRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record(mandate: dict, at: str, session_id: str) -> dict:
    return {"mandate": mandate, "status": "DRAFT", "policy_hash": policy_hash(mandate),
            "draft_hash": draft_hash(mandate), "created_at": at, "created_session": session_id,
            "confirmed_at": None, "confirmed_session": None, "retired_at": None}


def _invalidate_unsigned(aggregate: dict, reason: str) -> None:
    for review in aggregate["reviews"].values():
        if review["status"] == "UNSIGNED":
            review["status"] = "INVALIDATED"
            review["invalidation_reason"] = reason


REVIEW_COMMITTED_FIELDS = {"review_id", "mandate_id", "revision", "policy_hash", "draft_hash",
                          "scope", "session_id", "trace_id", "plan_hash", "state_root",
                          "amount", "fee", "created_at", "expires_at"}


def _review_hash(review: dict) -> str:
    if not REVIEW_COMMITTED_FIELDS.issubset(review):
        raise MachineError("incomplete review commitment")
    return digest({"domain": "mandate-review-binding-1",
                   "review": {key: review[key] for key in REVIEW_COMMITTED_FIELDS}})


class MandateService:
    def __init__(self, repository: MandateRepository, clock: Callable[[], str] = _now):
        self.repository, self.clock = repository, clock

    def _authorize(self, context: AuthenticatedContext, claimed_scope: dict | None = None) -> tuple[dict, str]:
        if not isinstance(context, AuthenticatedContext):
            raise MachineError("authenticated service context required")
        at = utc(self.clock())
        return context.authorize(at, claimed_scope), at

    @staticmethod
    def _active(aggregate: dict | None, at: str) -> tuple[dict, dict]:
        if aggregate is None:
            raise MachineError("mandate not found in authenticated scope")
        if datetime.fromisoformat(at) < datetime.fromisoformat(aggregate["updated_at"]):
            raise MachineError("service clock moved backwards")
        record = aggregate["revisions"][-1]
        return record, confirmed_mandate(record, at)

    def get(self, context: AuthenticatedContext, mandate_id: str) -> dict:
        scope, _ = self._authorize(context)
        return self.repository.read(scope, mandate_id)

    def create(self, context: AuthenticatedContext, raw: dict) -> dict:
        mandate = normalize_mandate(raw)
        scope, at = self._authorize(context, mandate["scope"])
        if mandate["revision"] != 1 or mandate["trace_id"] != context.trace_id:
            raise MachineError("initial revision or request trace mismatch")

        def mutate(current):
            if current is not None:
                raise MachineError("mandate already exists")
            return {"scope": scope, "mandate_id": mandate["mandate_id"], "updated_at": at,
                    "revisions": [_record(mandate, at, context.session_id)],
                    "reviews": {}, "holds": {}}

        return self.repository.transact(scope, mandate["mandate_id"], 0, mutate)

    def confirm(self, context: AuthenticatedContext, mandate_id: str, *,
                expected_version: int, expected_draft_hash: str) -> dict:
        scope, at = self._authorize(context)
        hash32(expected_draft_hash, "confirmed draft hash")

        def mutate(current):
            if current is None or current["revisions"][-1]["status"] != "DRAFT":
                raise MachineError("only a draft may be confirmed")
            if datetime.fromisoformat(at) < datetime.fromisoformat(current["updated_at"]):
                raise MachineError("service clock moved backwards")
            record = current["revisions"][-1]
            if record["draft_hash"] != expected_draft_hash or draft_hash(record["mandate"]) != expected_draft_hash:
                raise MachineError("confirmation does not match exact draft")
            if record["policy_hash"] != policy_hash(record["mandate"]):
                raise MachineError("stored policy commitment mismatch")
            assert_effective(record["mandate"], at)
            record.update(status="CONFIRMED", confirmed_at=at, confirmed_session=context.session_id)
            current["updated_at"] = at
            return current

        return self.repository.transact(scope, mandate_id, expected_version, mutate)

    def revise(self, context: AuthenticatedContext, raw: dict, *, expected_version: int) -> dict:
        mandate = normalize_mandate(raw)
        scope, at = self._authorize(context, mandate["scope"])
        if mandate["trace_id"] != context.trace_id:
            raise MachineError("request trace mismatch")

        def mutate(current):
            if current is None:
                raise MachineError("mandate not found in authenticated scope")
            prior = current["revisions"][-1]
            if datetime.fromisoformat(at) < datetime.fromisoformat(current["updated_at"]):
                raise MachineError("service clock moved backwards")
            if prior["status"] == "REVOKED":
                raise MachineError("revoked mandate cannot be resurrected")
            if mandate["revision"] != prior["mandate"]["revision"] + 1:
                raise MachineError("revision must advance by one")
            if current["holds"] and mandate["terms"]["base_asset"] != prior["mandate"]["terms"]["base_asset"]:
                raise MachineError("cannot change base asset with unresolved holds")
            prior.update(status="SUPERSEDED", retired_at=at)
            current["revisions"].append(_record(mandate, at, context.session_id))
            _invalidate_unsigned(current, "MANDATE_REVISED")
            current["updated_at"] = at
            return current

        return self.repository.transact(scope, mandate["mandate_id"], expected_version, mutate)

    def revoke(self, context: AuthenticatedContext, mandate_id: str, *, expected_version: int) -> dict:
        scope, at = self._authorize(context)

        def mutate(current):
            if current is None:
                raise MachineError("mandate not found in authenticated scope")
            if datetime.fromisoformat(at) < datetime.fromisoformat(current["updated_at"]):
                raise MachineError("service clock moved backwards")
            record = current["revisions"][-1]
            if record["status"] == "REVOKED":
                raise MachineError("mandate already revoked")
            record.update(status="REVOKED", retired_at=at)
            _invalidate_unsigned(current, "MANDATE_REVOKED")
            current["updated_at"] = at
            return current

        return self.repository.transact(scope, mandate_id, expected_version, mutate)

    def project(self, context: AuthenticatedContext, mandate_id: str, **parameters) -> dict:
        scope, at = self._authorize(context)
        aggregate = self.repository.read(scope, mandate_id)
        record, _ = self._active(aggregate, at)
        return compile_review_projections(record, at=at, **parameters)

    @staticmethod
    def _budget(aggregate: dict, mandate: dict, amount: dict, fee: dict) -> None:
        asset, capital, reserve = single_asset_budget(mandate)
        value, cost = money(amount, asset=asset), money(fee, asset=asset)
        limits = mandate["terms"]["limits"]
        with localcontext() as ctx:
            ctx.prec = 256
            principal, fee_value = decimal(value["amount"]), decimal(cost["amount"])
            if principal <= 0 or principal > decimal(limits["single_amount"]["amount"]):
                raise MachineError("review exceeds single amount limit")
            if fee_value > decimal(limits["fee_amount"]["amount"]):
                raise MachineError("review exceeds fee limit")
            held_amount = sum((decimal(money(item["amount"], asset=asset)["amount"])
                               for item in aggregate["holds"].values()), Decimal(0))
            held_fees = sum((decimal(money(item["fee"], asset=asset)["amount"])
                             for item in aggregate["holds"].values()), Decimal(0))
            if held_amount + principal > decimal(limits["cumulative_amount"]["amount"]):
                raise MachineError("review exceeds cumulative amount limit including holds")
            if held_amount + held_fees + principal + fee_value > decimal(capital) - decimal(reserve):
                raise MachineError("review and holds would consume required cash")

    def prepare_review(self, context: AuthenticatedContext, mandate_id: str, raw: dict,
                       *, expected_version: int) -> dict:
        """Bind an unsigned review placeholder; not a verified plan or transaction."""
        require_keys(raw, {"scope", "review_id", "policy_hash", "plan_hash", "state_root",
                           "amount", "fee", "expires_at"}, "review binding")
        scope, at = self._authorize(context, raw["scope"])
        review_id = ident(raw["review_id"], "review id")
        for key in ("policy_hash", "plan_hash", "state_root"):
            hash32(raw[key], key)
        until = utc(raw["expires_at"])

        def mutate(current):
            record, mandate = self._active(current, at)
            if raw["policy_hash"] != record["policy_hash"]:
                raise MachineError("review policy commitment mismatch")
            if review_id in current["reviews"]:
                raise MachineError("review id already used")
            if not datetime.fromisoformat(at) < datetime.fromisoformat(until) <= min(
                    datetime.fromisoformat(mandate["terms"]["expires_at"]),
                    datetime.fromisoformat(utc(context.expires_at))):
                raise MachineError("review outside mandate/session validity")
            self._budget(current, mandate, raw["amount"], raw["fee"])
            review = {"review_id": review_id, "mandate_id": mandate_id,
                      "revision": mandate["revision"], "policy_hash": record["policy_hash"],
                      "draft_hash": record["draft_hash"], "scope": scope,
                      "session_id": context.session_id, "trace_id": context.trace_id,
                      "plan_hash": raw["plan_hash"], "state_root": raw["state_root"],
                      "amount": money(raw["amount"]), "fee": money(raw["fee"]),
                      "created_at": at, "expires_at": until}
            review["review_hash"] = _review_hash(review)
            review.update(status="UNSIGNED", invalidation_reason=None,
                          execution_authority="NONE", chain_status="NOT_SUBMITTED",
                          blockers=["PR04_PLAN_REPLAY_REQUIRED", "PR06_TRANSACTION_GRAPH_REQUIRED",
                                    "PR07_WALLET_SIGNATURE_REQUIRED"])
            current["reviews"][review_id] = review
            current["updated_at"] = at
            return current

        return self.repository.transact(scope, mandate_id, expected_version, mutate)

    def hold_review(self, context: AuthenticatedContext, mandate_id: str, review_id: str,
                    *, expected_version: int, expected_review_hash: str) -> dict:
        """Conservative reservation only; no signature or external call is made.

        This internal hook is for the later wallet handoff boundary. It only
        reduces available capital. There is deliberately no release hook before
        independently verified reconciliation is implemented in PR 08.
        """
        scope, at = self._authorize(context)
        hash32(expected_review_hash, "review hash")

        def mutate(current):
            record, mandate = self._active(current, at)
            review = current["reviews"].get(review_id)
            if review is None or review["status"] != "UNSIGNED":
                raise MachineError("unsigned review required")
            if review["session_id"] != context.session_id:
                raise MachineError("review belongs to a different authenticated session")
            if review["review_hash"] != expected_review_hash or _review_hash(review) != expected_review_hash:
                raise MachineError("review commitment mismatch")
            if datetime.fromisoformat(at) >= datetime.fromisoformat(review["expires_at"]):
                raise MachineError("review expired")
            if (review["revision"] != mandate["revision"] or review["policy_hash"] != record["policy_hash"]
                    or review["draft_hash"] != record["draft_hash"] or review["scope"] != scope):
                raise MachineError("review belongs to a superseded revision")
            self._budget(current, mandate, review["amount"], review["fee"])
            current["holds"][review_id] = {"review_hash": expected_review_hash,
                "revision": review["revision"], "amount": dict(review["amount"]),
                "fee": dict(review["fee"]), "created_at": at, "status": "RECONCILIATION_REQUIRED"}
            review["status"] = "HELD"
            current["updated_at"] = at
            return current

        return self.repository.transact(scope, mandate_id, expected_version, mutate)
