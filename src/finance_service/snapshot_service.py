"""Authenticated read-only boundary. Capture inputs stay server-side."""

from economic_machine.values import MachineError


class SnapshotService:
    def __init__(self, assembler, capture_repository, clock):
        self.assembler, self.repository, self.clock = assembler, capture_repository, clock

    def read(self, context):
        at = self.clock()
        scope = context.authorize(at)
        # Repository must select by all four scope components, not wallet alone.
        captures, rpc = self.repository.read_scope(scope)
        snapshot = self.assembler.assemble(captures, as_of=at, scope=scope, rpc=rpc)
        if snapshot["mode"] != "LIVE_READ":
            raise MachineError("fixtures cannot serve a live customer snapshot")
        return snapshot
