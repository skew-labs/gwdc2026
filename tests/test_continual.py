"""Guard the daily learner's revision and future-label boundaries."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import torch

from research.fdc.continual import (
    LEDGER_SCHEMA, ingest_release, unreviewed_mature_revision_count,
)
from research.fdc.train_real_temporal import (
    INPUT_METRICS, TARGET_METRICS, TemporalForecaster, load_daily,
    score_unseen_day,
)


class ContinualTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _release(self, name: str, value: str, pointer: str) -> Path:
        path = self.root / name
        path.mkdir()
        (path / "manifest.json").write_text(json.dumps({"created_at": "2026-09-24T05:00:00+00:00"}))
        row = {"source_kind": "usdd_tron_daily", "product_id": "usdd:tron:protocol",
               "event_time": "2026-09-20T00:00:00+00:00",
               "source_available_at": "2026-09-24T05:00:00+00:00",
               "measurements": {"debt": {"value": value, "unit": "USDD",
                                         "json_pointer": pointer}}, "metadata": {}}
        (path / "observations.jsonl").write_text(json.dumps(row) + "\n")
        return path

    def test_revision_is_value_change_not_json_position(self):
        db = sqlite3.connect(":memory:")
        db.executescript(LEDGER_SCHEMA)
        first = self._release("first", "100", "/data/items/1/debt")
        shifted = self._release("shifted", "100", "/data/items/0/debt")
        revised = self._release("revised", "101", "/data/items/0/debt")
        with patch("research.fdc.continual.validate_release", side_effect=lambda _s, p: {"release_id": p.name}):
            self.assertEqual(ingest_release(db, None, first)["new_daily"], 1)
            self.assertEqual(ingest_release(db, None, shifted)["mature_revisions"], 0)
            self.assertEqual(ingest_release(db, None, revised)["mature_revisions"], 1)
            self.assertTrue(ingest_release(db, None, revised)["already_ingested"])
        self.assertEqual(db.execute("SELECT count(*) FROM revisions").fetchone()[0], 1)
        self.assertEqual(unreviewed_mature_revision_count(db), 1)

    def test_maturity_excludes_recent_provider_rows(self):
        path = self.root / "release"
        path.mkdir()
        at = datetime(2026, 9, 24, 5, tzinfo=timezone.utc)
        rows = []
        for day in range(105):
            event_time = at - timedelta(days=104-day)
            rows.append({"source_kind": "usdd_tron_daily",
                         "event_time": event_time.isoformat(),
                         "source_available_at": at.isoformat()})
        (path / "observations.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
        mature = load_daily(path)
        self.assertEqual(len(mature), 103)
        self.assertEqual(mature[-1]["event_time"], (at - timedelta(days=2)).isoformat())

    def test_forward_score_rejects_seen_target(self):
        model = TemporalForecaster()
        keys = ("core.history_projection.", "core.temporal_attention.",
                "core.temporal_norm.", "core.temporal_null")
        checkpoint = {"scope": "real_tron_self_supervised_temporal_only",
                      "maturity_hours": 48, "input_metrics": INPUT_METRICS,
                      "target_metrics": TARGET_METRICS,
                      "dataset_through_event_time": "2026-09-14T00:00:00+00:00",
                      "release_id": "earlier",
                      "temporal_state": {k: v for k, v in model.state_dict().items()
                                         if k.startswith(keys)},
                      "head_state": model.head.state_dict(),
                      "input_mean": torch.zeros(len(INPUT_METRICS)),
                      "input_scale": torch.ones(len(INPUT_METRICS)),
                      "target_mean": torch.zeros(len(TARGET_METRICS)),
                      "target_scale": torch.ones(len(TARGET_METRICS))}
        checkpoint_path = self.root / "temporal.pt"
        torch.save(checkpoint, checkpoint_path)
        rows = []
        for day in range(1, 16):
            event_time = datetime(2026, 9, day, tzinfo=timezone.utc).isoformat()
            rows.append({"event_time": event_time,
                         "source_available_at": "2026-09-20T00:00:00+00:00",
                         "measurements": {name: {"value": "100", "unit": "USD"}
                                          for name in INPUT_METRICS}})
        score = score_unseen_day(checkpoint_path, rows[:-1], rows[-1])
        self.assertEqual(score["event_time"], rows[-1]["event_time"])
        checkpoint["dataset_through_event_time"] = rows[-1]["event_time"]
        torch.save(checkpoint, checkpoint_path)
        with self.assertRaisesRegex(ValueError, "has seen target"):
            score_unseen_day(checkpoint_path, rows[:-1], rows[-1])


if __name__ == "__main__":
    unittest.main()
