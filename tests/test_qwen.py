"""Qwen response validation without credentials or provider calls."""

import unittest

from finagent.contracts import ContractError
from finagent.qwen import parse_intent_answer, parse_model_answer


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

    def test_source_grounded_typed_intent_patch(self):
        message = "원금은 10,000 USDT이고 즉시 현금을 30% 남겨줘"
        answer = {"schema_version": "financial-intent-draft-1", "intent": "REVISE",
            "patch": {"capital": [{"asset": "USDT", "amount": "10000"}],
                      "immediate_cash": {"kind": "BPS", "value": 3000}},
            "evidence": {"capital": "10,000 USDT", "immediate_cash": "30%"},
            "missing": [], "reason_codes": []}
        self.assertEqual(parse_intent_answer(answer, message)["patch"]["capital"][0]
                         ["amount"], "10000")

    def test_korean_amount_period_liquidity_and_explicit_debt_are_distinct(self):
        message = "원금 10000 USDT, 기간 30일, 즉시 현금 20%, 차입 최대 500 USDT 허용"
        patch = {"capital": [{"asset": "USDT", "amount": "10000"}],
            "horizon_seconds": 2592000,
            "immediate_cash": {"kind": "BPS", "value": 2000},
            "borrowing_consent": True,
            "max_debt": {"asset": "USDT", "amount": "500"}}
        evidence = {"capital": "10000 USDT", "horizon_seconds": "30일",
            "immediate_cash": "20%", "borrowing_consent": "허용",
            "max_debt": "500 USDT"}
        parsed = parse_intent_answer({
            "schema_version": "financial-intent-draft-1", "intent": "REVISE",
            "patch": patch, "evidence": evidence, "missing": [], "reason_codes": []},
            message)
        self.assertEqual(parsed["patch"], patch)

    def test_intent_rejects_invented_evidence_authority_and_unbounded_borrowing(self):
        base = {"schema_version": "financial-intent-draft-1", "intent": "REVISE",
            "patch": {"risk_profile": "growth"},
            "evidence": {"risk_profile": "공격형"}, "missing": [], "reason_codes": []}
        with self.assertRaises(ContractError):
            parse_intent_answer(base, "안정형으로 해줘")
        with self.assertRaises(ContractError):
            parse_intent_answer({**base, "execute": {"sign": True}}, "공격형")
        borrowing = {**base,
            "patch": {"borrowing_consent": True},
            "evidence": {"borrowing_consent": "차입 허용"}}
        with self.assertRaises(ContractError):
            parse_intent_answer(borrowing, "차입 허용")


if __name__ == "__main__":
    unittest.main()
