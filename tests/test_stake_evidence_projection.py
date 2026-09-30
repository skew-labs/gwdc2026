"""The public combined root is a projection of retained compiler evidence."""
import copy
import unittest
from finance_service.stake_comparison import project_evidence


class EvidenceProjectionTests(unittest.TestCase):
    def state(self):
        return {
            "workspace": {
                "snapshots": [{"id": "wallet", "root": "component"}],
                "comparison": {
                    "id": "comparison", "snapshot_root": "combined",
                    "network": "nile", "expires_at": "2026-09-29T12:03:00+00:00",
                },
            },
            "comparison_core": {
                "kind": "NATIVE_STAKE",
                "comparison": {"comparison_hash": "comparison"},
                "snapshot": {"snapshot_hash": "combined"},
                "market": {"observed_at": "2026-09-29T12:00:00+00:00", "block": 100},
            },
        }

    def test_original_root_and_expiry_are_preserved_without_duplication(self):
        state = self.state()
        core = copy.deepcopy(state["comparison_core"])
        project_evidence(state)
        project_evidence(state)
        snaps = state["workspace"]["snapshots"]
        self.assertEqual(len(snaps), 2)
        self.assertEqual(snaps[0], {"id": "wallet", "root": "component"})
        self.assertEqual(snaps[1]["root"], "combined")
        self.assertEqual(snaps[1]["expires_at"], "2026-09-29T12:03:00+00:00")
        self.assertEqual(state["comparison_core"], core)

    def test_unrelated_root_or_comparison_is_not_promoted(self):
        for key in ("id", "snapshot_root"):
            state = self.state()
            state["workspace"]["comparison"][key] = "different"
            before = copy.deepcopy(state)
            project_evidence(state)
            self.assertEqual(state, before)
