import unittest

from economic_machine.inference import (bind_model_opinion,
    deterministic_baseline_opinion, normalize_model_opinion)
from economic_machine.values import MachineError


class ModelOpinionTests(unittest.TestCase):
    def test_baseline_abstains_and_never_defaults_risk_to_zero(self):
        opinion = deterministic_baseline_opinion("a" * 64, "allocation-attractiveness",
            3600, valid_from="2026-09-28T12:00:00Z",
            valid_until="2026-09-28T12:05:00Z")
        self.assertEqual(opinion["mode"], "NOT_USED")
        self.assertIsNone(opinion["scores"]["risk_bps"])
        binding = bind_model_opinion(opinion, expected_input_root="a" * 64,
                                     at="2026-09-28T12:01:00Z")
        self.assertEqual((binding["status"], binding["optimizer_effect"],
                          binding["execution_authority"]),
                         ("NOT_USED", "NONE", "NONE"))

    def test_only_shadow_or_unused_modes_exist_and_input_mismatch_rejects(self):
        opinion = deterministic_baseline_opinion("a" * 64, "risk", 3600,
            valid_from="2026-09-28T12:00:00Z",
            valid_until="2026-09-28T12:05:00Z")
        result = bind_model_opinion(opinion, expected_input_root="b" * 64,
                                    at="2026-09-28T12:01:00Z")
        self.assertEqual((result["status"], result["reason_codes"]),
                         ("REJECTED", ["INPUT_ROOT_MISMATCH"]))
        with self.assertRaisesRegex(MachineError, "execution path"):
            normalize_model_opinion({**opinion, "mode": "ACTIVE"})
        with self.assertRaisesRegex(MachineError, "invent neutral"):
            normalize_model_opinion({**opinion, "scores": {
                **opinion["scores"], "risk_bps": "0"}})


if __name__ == "__main__":
    unittest.main()
