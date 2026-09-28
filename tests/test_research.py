"""Small remote-only neural and dataset integrity smoke checks."""

import json
import tempfile
import unittest
from pathlib import Path

try:
    import torch
except ImportError:
    torch = None


@unittest.skipIf(torch is None, "research optional dependency is not installed")
class ResearchTests(unittest.TestCase):
    def test_synthetic_split_and_forward_backward(self):
        from research.fdc.features import INPUT_KEYS, collate, dimensions, vectorize
        from research.fdc.baseline import FlatMLP
        from research.fdc.model import Config, FinancialDecisionCore
        from research.fdc.projection import inspect_and_project
        from research.fdc.synthetic import generate
        from research.fdc.train import objective
        from research.fdc.validate import DatasetError, validate

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "episodes.jsonl"
            generate(path, families=4, seed=7)
            audit = validate(path)
            self.assertEqual(audit["episodes"], 8)
            row = json.loads(path.read_text().splitlines()[0])
            cfg = Config(width=32, heads=4, layers=1, plans=2, text_width=32)
            batch = collate([vectorize(row, cfg, dimensions([row]))])
            model = FinancialDecisionCore(cfg)
            output = model(**{key: batch[key] for key in INPUT_KEYS})
            self.assertEqual(tuple(output["allocation_logits"].shape), (1, 2, 3))
            loss, _ = objective(output, batch)
            self.assertTrue(torch.isfinite(loss))
            loss.backward()
            self.assertTrue(any(parameter.grad is not None
                                for parameter in model.parameters()))
            baseline = FlatMLP(cfg, dimensions([row]))
            simple_output = baseline(**{key: batch[key] for key in INPUT_KEYS})
            self.assertEqual(tuple(simple_output["allocation_logits"].shape), (1, 2, 3))
            projected = inspect_and_project(row, [0.9, 0.05, 0.05])
            from decimal import Decimal
            total = sum((Decimal(v) for v in projected["corrected_allocations"]),
                        Decimal(projected["corrected_hold"]))
            self.assertEqual(total, Decimal(row["needs"]["amount"]))
            self.assertGreaterEqual(Decimal(projected["corrected_hold"]),
                                    Decimal(row["needs"]["liquid_reserve"]))

            bad = dict(row)
            bad["split"] = "sealed"
            path.write_text(json.dumps(bad) + "\n")
            with self.assertRaises(DatasetError):
                validate(path)


if __name__ == "__main__":
    unittest.main()
