"""Run typed-finance data and neural checks on the selected Cherry host."""

import json
import tempfile
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

try:
    import torch
except ImportError:
    torch = None


@unittest.skipIf(torch is None, "research dependency unavailable")
class ResearchV3Tests(unittest.TestCase):
    def setUp(self):
        from research.fdc.synthetic_v3 import generate
        from research.fdc.validate_v3 import validate

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "typed.jsonl"
        generate(self.path, families=28, seed=7)
        self.rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.schema = Path(__file__).resolve().parents[1] / "contracts/episode_v3.schema.json"
        self.audit = validate(self.path, self.schema)

    def test_typed_pairs_and_output_heads(self):
        from research.fdc.features_v3 import INPUT_KEYS, collate_pairs, dimensions, vectorize
        from research.fdc.model import Config, FinancialDecisionCore
        from research.fdc.train_v3 import objective

        self.assertEqual(self.audit["episodes"], 56)
        self.assertGreater(self.audit["counterfactual_pairs"].get("changed", 0), 0)
        self.assertGreater(self.audit["counterfactual_pairs"].get("unchanged", 0), 0)
        self.assertGreater(self.audit["modes"].get("ask", 0), 0)
        self.assertGreater(self.audit["modes"].get("abstain", 0), 0)
        pair = self.rows[2:4]
        cfg = Config(width=32, heads=4, layers=1, plans=2, text_width=32)
        dims = dimensions(pair)
        batch = collate_pairs([(vectorize(pair[0], cfg, dims),
                                vectorize(pair[1], cfg, dims))])
        self.assertEqual(tuple(batch["constraints"].shape), (2, 8, 16))
        self.assertEqual(tuple(batch["relations"].shape), (2, 3, 3, 6))
        self.assertEqual(batch["relations"][0, 0, 1, 2].item(), 1)
        model = FinancialDecisionCore(cfg)
        output = model(**{name: batch[name] for name in INPUT_KEYS})
        self.assertEqual(tuple(output["allocation_logits"].shape), (2, 2, 4))
        self.assertEqual(tuple(output["evidence_logits"].shape), (2, 2, 4))
        self.assertEqual(tuple(output["dependency_logits"].shape), (2, 2, 9))
        self.assertEqual(tuple(output["question_field_logits"].shape), (2, 16))
        loss, metrics = objective(output, batch)
        self.assertTrue(torch.isfinite(loss))
        self.assertIn("pair_allocation_l1", metrics)
        for name in ("pair_plan_l1", "pair_evidence_l1", "pair_dependency_l1",
                     "pair_question_l1", "pair_mode_l1"):
            self.assertTrue(0 <= metrics[name] < float("inf"))
        loss.backward()
        self.assertIsNotNone(model.dependency_query.weight.grad)
        self.assertIsNotNone(model.question.weight.grad)

    def test_units_future_evidence_and_shared_risk_fail_closed(self):
        from research.fdc.validate_v3 import DatasetV3Error, check_episode

        base = next(row for row in self.rows if row["target"]["mode"] == "propose"
                    and row["parent_episode_id"] is None
                    and all(c["status"] == "synthetic" for c in row["candidates"]))
        wrong_unit = deepcopy(base)
        wrong_unit["need"]["withdrawal"]["min_immediate"]["unit"] = (
            "USDD" if base["need"]["budget"]["unit"] == "USDT" else "USDT")
        with self.assertRaises(DatasetV3Error):
            check_episode(wrong_unit, 1)

        future = deepcopy(base)
        future["candidates"][0]["observed_at"] = "2026-09-25T00:00:00+00:00"
        with self.assertRaisesRegex(DatasetV3Error, "future candidate"):
            check_episode(future, 1)

        shared = deepcopy(base)
        amount = Decimal(shared["need"]["budget"]["value"])
        for candidate in shared["candidates"][:2]:
            candidate["available_cash"]["value"] = format(amount, "f")
        plan = shared["target"]["plans"][0]
        plan["allocations"] = {shared["candidates"][0]["id"]: format(amount * Decimal("0.3"), "f"),
                               shared["candidates"][1]["id"]: format(amount * Decimal("0.3"), "f")}
        plan["hold"] = format(amount * Decimal("0.4"), "f")
        plan["evidence_ids"] = ["e-0", "e-1"]
        with self.assertRaisesRegex(DatasetV3Error, "shared-risk"):
            check_episode(shared, 1)

        mislabeled = deepcopy(base)
        used = set(mislabeled["target"]["plans"][0]["evidence_ids"])
        extra = next(item["id"] for item in mislabeled["evidence"] if item["id"] not in used)
        mislabeled["target"]["plans"][0]["evidence_ids"].append(extra)
        with self.assertRaisesRegex(DatasetV3Error, "diverges"):
            check_episode(mislabeled, 1)

    def test_pair_change_declares_one_field(self):
        from research.fdc.validate_v3 import DatasetV3Error, validate

        bad = deepcopy(self.rows)
        bad[1]["need"]["horizon_days"] += 1
        path = Path(self.temp.name) / "bad.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in bad) + "\n")
        with self.assertRaisesRegex(DatasetV3Error, "exactly one"):
            validate(path, self.schema)

    def test_pair_loss_sees_question_change_and_invariant_horizon(self):
        from research.fdc.features_v3 import INPUT_KEYS, collate_pairs, dimensions, vectorize
        from research.fdc.model import Config, FinancialDecisionCore
        from research.fdc.train_v3 import objective

        base = next(item for item in self.rows if item["target"]["mode"] == "ask"
                    and item["parent_episode_id"] is None)
        changed = next(item for item in self.rows
                       if item["parent_episode_id"] == base["episode_id"])
        self.assertEqual(changed["target"]["mode"], "propose")
        cfg = Config(width=32, heads=4, layers=1, plans=2, text_width=32)
        dims = dimensions([base, changed])
        batch = collate_pairs([(vectorize(base, cfg, dims),
                                vectorize(changed, cfg, dims))])
        self.assertFalse(torch.equal(batch["target_question"][0],
                                     batch["target_question"][1]))
        outputs = FinancialDecisionCore(cfg)(
            **{name: batch[name] for name in INPUT_KEYS})
        no_pair_loss, _ = objective(outputs, batch, 0)
        pair_loss, _ = objective(outputs, batch, 0.1)
        allocation_only_loss, _ = objective(outputs, batch, 0.1, "allocation_mode")
        self.assertGreater(pair_loss.item(), no_pair_loss.item())
        self.assertGreaterEqual(pair_loss.item(), allocation_only_loss.item())

        invariant = next(item for item in self.rows
                         if item["changed_field"] == "need.horizon_days")
        original = next(item for item in self.rows
                        if item["episode_id"] == invariant["parent_episode_id"])
        self.assertEqual(invariant["expected_pair_effect"], "unchanged")
        self.assertEqual(original["target"], invariant["target"])

    def test_candidate_permutation_equivariance(self):
        from research.fdc.features_v3 import INPUT_KEYS, dimensions, vectorize
        from research.fdc.model import Config, FinancialDecisionCore

        row = next(item for item in self.rows if item["target"]["mode"] == "propose")
        cfg = Config(width=32, heads=4, layers=1, plans=2, text_width=32)
        data = vectorize(row, cfg, dimensions([row]))
        swapped = {name: value.clone() for name, value in data.items()}
        permutation = torch.tensor([1, 0, 2])
        for name in ("history", "history_valid", "times", "asset_features", "asset_valid"):
            swapped[name] = swapped[name][permutation]
        swapped["relations"] = swapped["relations"][permutation][:, permutation]
        model = FinancialDecisionCore(cfg).eval()
        with torch.no_grad():
            first = model(**{name: data[name].unsqueeze(0) for name in INPUT_KEYS})
            second = model(**{name: swapped[name].unsqueeze(0) for name in INPUT_KEYS})
        self.assertTrue(torch.allclose(first["allocation_logits"][..., permutation],
                                       second["allocation_logits"][..., :3], atol=1e-5))
        self.assertTrue(torch.allclose(first["allocation_logits"][..., -1],
                                       second["allocation_logits"][..., -1], atol=1e-5))

    def test_exact_research_projection_enforces_shared_risk(self):
        from research.fdc.projection_v3 import inspect_and_project

        row = next(item for item in self.rows if item["target"]["mode"] == "propose"
                   and all(candidate["status"] == "synthetic" for candidate in item["candidates"]))
        answer = inspect_and_project(row, [0.4, 0.4, 0.1, 0.1])
        self.assertIn("risk_shared", answer["raw_reasons"])
        self.assertFalse(answer["post_projection_violation"])
        amount = Decimal(row["need"]["budget"]["value"])
        allocated = [Decimal(value) for value in answer["corrected_allocations"]]
        self.assertEqual(sum(allocated, Decimal(answer["corrected_hold"])), amount)
        self.assertLessEqual(allocated[0] + allocated[1], amount * Decimal("0.45"))
        all_legacy = next(item for item in self.rows if item["target"]["mode"] == "abstain"
                          and all(candidate["status"] == "legacy" for candidate in item["candidates"]))
        blocked = inspect_and_project(all_legacy, [0.3, 0.3, 0.3, 0.1])
        self.assertTrue(blocked["abstain_required"])
        self.assertEqual(Decimal(blocked["corrected_hold"]),
                         Decimal(all_legacy["need"]["budget"]["value"]))


if __name__ == "__main__":
    unittest.main()
