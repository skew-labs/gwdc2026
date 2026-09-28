"""Exercise idempotence, exact changes, and fail-closed source proof."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from finagent.store import Store
from research.fdc.auto_cases import run_once


class AutoCaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / "source")
        self.output = self.root / "cases"

    def _snapshot(self, at: str, value: str) -> str:
        source = {"id": "justlend_markets_v1", "url": "https://example.test/market"}
        raw = json.dumps({"data": {"value": value}}).encode()
        from finagent.contracts import canonical_decimal, decimal_string
        fact = {"subject_id": "market-a", "metric_id": "supply_apy",
                "raw_value": value, "canonical_value": canonical_decimal(decimal_string(value)),
                "unit": "annual_fraction",
                "quality": "VALID", "json_path": "/data/value"}
        return self.store.save_snapshot(source, at, raw, json.loads(raw), [], [fact])

    def test_first_unchanged_changed_and_replay(self):
        self._snapshot("2026-09-24T00:00:00+00:00", "0.10")
        self._snapshot("2026-09-24T00:10:00+00:00", "0.10")
        self._snapshot("2026-09-24T00:20:00+00:00", "0.11")
        result = run_once(self.store.root, self.output)
        self.assertEqual((result["new_snapshots"], result["new_cases"]), (3, 2))
        self.assertFalse(result["training_enabled"])
        cases = [json.loads(line) for path in (self.output / "batches").glob("*.jsonl")
                 for line in path.read_text().splitlines() if line]
        change = next(row for row in cases if row["kind"] == "OBSERVED_VALUE_CHANGE")
        self.assertEqual((change["previous_value"], change["observed_value"], change["direction"]),
                         ("0.10", "0.11", "INCREASE"))
        self.assertEqual(len(change["witnesses"]), 2)
        self.assertEqual(run_once(self.store.root, self.output)["new_cases"], 0)

    def test_corrupted_raw_blocks_new_cases(self):
        snapshot_id = self._snapshot("2026-09-24T00:00:00+00:00", "0.10")
        with self.store.connect() as db:
            path = Path(db.execute("SELECT raw_path FROM snapshots WHERE id=?",
                                   (snapshot_id,)).fetchone()[0])
        path.write_text("{}")
        with self.assertRaisesRegex(ValueError, "snapshot hash mismatch"):
            run_once(self.store.root, self.output)

    def test_modified_batch_blocks_replay(self):
        snapshot_id = self._snapshot("2026-09-24T00:00:00+00:00", "0.10")
        run_once(self.store.root, self.output)
        (self.output / "batches" / f"{snapshot_id}.jsonl").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "batch missing or modified"):
            run_once(self.store.root, self.output)

    def test_uncommitted_temporary_batch_recovers(self):
        snapshot_id = self._snapshot("2026-09-24T00:00:00+00:00", "0.10")
        batches = self.output / "batches"
        batches.mkdir(parents=True)
        (batches / f"{snapshot_id}.jsonl.tmp").write_text("partial write")
        self.assertEqual(run_once(self.store.root, self.output)["new_cases"], 1)

    def test_unknown_parser_blocks_silent_skip(self):
        snapshot_id = self._snapshot("2026-09-24T00:00:00+00:00", "0.10")
        with self.store.connect() as db:
            db.execute("UPDATE snapshots SET parser_version='0.4.0' WHERE id=?", (snapshot_id,))
        with self.assertRaisesRegex(ValueError, "unknown parser version"):
            run_once(self.store.root, self.output)

    def test_historical_identity_dedup_and_revision(self):
        release_root = self.root / "history"
        release_root.mkdir()
        for index, (value, pointer) in enumerate(
                [("100", "/data/0"), ("100", "/data/1"), ("101", "/data/0")], 1):
            directory = release_root / f"release-{index}"
            directory.mkdir()
            (directory / "manifest.json").write_text(json.dumps({
                "created_at": f"2026-09-24T0{index}:00:00+00:00"}))
            row = {"source_kind": "usdd_tron_daily", "product_id": "usdd:tron:protocol",
                   "event_time": "2026-09-20T00:00:00+00:00",
                   "source_available_at": f"2026-09-24T0{index}:00:00+00:00",
                   "record_id": f"record-{index}", "raw_sha256": "a" * 64,
                   "source_url": "https://example.test/history", "json_pointer": pointer,
                   "measurements": {"debt": {"value": value, "unit": "USDD",
                                             "json_pointer": pointer}}, "metadata": {}}
            (directory / "observations.jsonl").write_text(json.dumps(row) + "\n")
        with patch("research.fdc.validate_real_tron.validate_release",
                   side_effect=lambda _store, path: {"release_id": path.name}):
            result = run_once(self.store.root, self.output, release_root)
        self.assertEqual((result["new_historical_releases"],
                          result["new_historical_cases"], result["new_source_revisions"]),
                         (3, 2, 1))
        self.assertEqual(run_once(self.store.root, self.output, release_root)
                         ["new_historical_cases"], 0)


if __name__ == "__main__":
    unittest.main()
