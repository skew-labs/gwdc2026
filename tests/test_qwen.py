"""Qwen response validation without credentials or provider calls."""

import unittest

from finagent.contracts import ContractError
from finagent.qwen import parse_model_answer


class QwenContractTests(unittest.TestCase):
    def test_complete_and_partial_drafts(self):
        answer = {"intent": "PLAN", "asset": "USDT", "amount": "2000",
                  "liquid_reserve": "500", "horizon_days": 30,
                  "risk": "balanced", "missing": []}
        self.assertEqual(parse_model_answer(answer)["amount"], "2000")
        partial = dict(answer, amount=None, missing=["amount"])
        self.assertIsNone(parse_model_answer(partial)["amount"])

    def test_missing_mismatch_and_float_rejected(self):
        answer = {"intent": "PLAN", "asset": "USDT", "amount": "2000",
                  "liquid_reserve": "500", "horizon_days": 30,
                  "risk": "balanced", "missing": ["amount"]}
        with self.assertRaises(ContractError):
            parse_model_answer(answer)
        with self.assertRaises(ContractError):
            parse_model_answer(dict(answer, amount=2000.0, missing=[]))


if __name__ == "__main__":
    unittest.main()
