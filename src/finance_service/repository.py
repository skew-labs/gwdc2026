"""Atomic aggregate repository port and isolated in-memory reference adapter.

The reference adapter is intentionally not a production persistence layer.
PostgreSQL adapters must provide the same atomic version comparison, rollback,
tenant isolation and reservation preservation (PR 09).
"""

from copy import deepcopy
from threading import RLock
from typing import Callable, Protocol

from economic_machine.mandate import integer, normalize_scope
from economic_machine.values import MachineError, ident


class VersionConflict(MachineError):
    pass


Mutation = Callable[[dict | None], dict]


class MandateRepository(Protocol):
    def read(self, scope: dict, mandate_id: str) -> dict:
        ...

    def transact(self, scope: dict, mandate_id: str, expected_version: int,
                 mutation: Mutation) -> dict:
        """Commit all revision, review invalidation and lock changes together."""
        ...


class InMemoryMandateRepository:
    def __init__(self):
        self._records: dict[tuple, dict] = {}
        self._lock = RLock()

    @staticmethod
    def _key(scope: dict, mandate_id: str) -> tuple:
        scope = normalize_scope(scope)
        return tuple(scope[key] for key in ("tenant_id", "owner_id", "wallet", "network")) + (ident(mandate_id, "mandate id"),)

    def read(self, scope: dict, mandate_id: str) -> dict:
        with self._lock:
            value = self._records.get(self._key(scope, mandate_id))
            if value is None:
                raise MachineError("mandate not found in authenticated scope")
            return deepcopy(value)

    def transact(self, scope: dict, mandate_id: str, expected_version: int,
                 mutation: Mutation) -> dict:
        key = self._key(scope, mandate_id)
        integer(expected_version, "aggregate version", 0, 2147483646)
        with self._lock:
            current = self._records.get(key)
            version = current["version"] if current is not None else 0
            if version != expected_version:
                raise VersionConflict("mandate aggregate changed; reload before retry")
            candidate = mutation(deepcopy(current))
            if (candidate["scope"] != normalize_scope(scope)
                    or candidate["mandate_id"] != mandate_id):
                raise MachineError("repository mutation changed account identity")
            candidate["version"] = version + 1
            self._records[key] = deepcopy(candidate)
            return deepcopy(candidate)
