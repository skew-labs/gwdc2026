"""Authenticated, read-only plan comparison and intent preparation boundary."""

from datetime import datetime

from economic_machine.application import confirmed_mandate
from economic_machine.plan_compiler import compare_plans, compile_plan_intent
from economic_machine.values import MachineError, utc

from .context import AuthenticatedContext


class PlanService:
    def __init__(self, mandate_repository, snapshot_service, assembler, clock):
        self.repository = mandate_repository
        self.snapshots = snapshot_service
        self.assembler = assembler
        self.clock = clock

    def _inputs(self, context, mandate_id):
        if not isinstance(context, AuthenticatedContext):
            raise MachineError("authenticated service context required")
        at = utc(self.clock())
        scope = context.authorize(at)
        aggregate = self.repository.read(scope, mandate_id)
        if aggregate["holds"]:
            raise MachineError("pending capital holds require reconciliation before planning")
        record = aggregate["revisions"][-1]
        confirmed_mandate(record, at)
        snapshot = self.snapshots.read(context)
        if snapshot["mode"] != "LIVE_READ" or snapshot["scope"] != scope:
            raise MachineError("customer plan requires an exact scoped live snapshot")
        return at, record, snapshot

    def compare(self, context, mandate_id, request):
        at, record, snapshot = self._inputs(context, mandate_id)
        return compare_plans(record, snapshot, request, assembler=self.assembler, at=at)

    def prepare_intent(self, context, mandate_id, request, *, expected_comparison_hash,
                       selected_plan, valid_until):
        at, record, snapshot = self._inputs(context, mandate_id)
        comparison = compare_plans(record, snapshot, request, assembler=self.assembler, at=at)
        if comparison["comparison_hash"] != expected_comparison_hash:
            raise MachineError("market, conditions or comparison changed; review again")
        if datetime.fromisoformat(utc(valid_until)) > datetime.fromisoformat(utc(context.expires_at)):
            raise MachineError("plan intent exceeds authenticated session validity")
        return compile_plan_intent(comparison, record, snapshot, request,
            selected_plan=selected_plan, assembler=self.assembler, at=at,
            valid_until=valid_until)
