"""Mandate contract tests: exact units, identity, evidence and legacy boundaries."""

import copy
import json
import unittest
from decimal import Decimal, localcontext
from pathlib import Path

from economic_machine.application import compile_review_projections
from economic_machine.capabilities import capability_hash, normalize_capability, product_id
from economic_machine.mandate import draft_hash, normalize_mandate, policy_hash, reserve_amount
from economic_machine.values import MachineError


CASE = Path(__file__).resolve().parents[1] / "cases/economic_mandate_demo.json"
AT = "2026-09-28T12:01:00Z"


def fixture():
    return json.loads(CASE.read_text())


def confirmed(raw):
    return {"mandate": normalize_mandate(raw), "status": "CONFIRMED",
            "draft_hash": draft_hash(raw), "policy_hash": policy_hash(raw)}


def projection_parameters():
    return {"at": AT, "program_id": "program-demo", "agent_id": "ALPHA",
            "state_root": "a" * 64, "allowed_fact_paths": ["wallet.usdt"],
            "allowed_products": ["justlend_usdt"], "capital_fact_path": "wallet.usdt",
            "max_plan_age_ms": 60000}


class MandateContractTests(unittest.TestCase):
    def setUp(self):
        self.raw = fixture()

    def test_equivalent_decimal_order_and_utc_notation_have_same_hash(self):
        other = copy.deepcopy(self.raw)
        other["terms"]["capital"][0]["amount"] = "10000.000000"
        other["terms"]["allowed_actions"].reverse()
        other["terms"]["effective_at"] = "2026-09-28T12:00:00+00:00"
        self.assertEqual(policy_hash(self.raw), policy_hash(other))
        self.assertEqual(draft_hash(self.raw), draft_hash(other))
        self.assertEqual(normalize_mandate(self.raw), normalize_mandate(normalize_mandate(self.raw)))

    def test_policy_hash_excludes_trace_and_revision_but_draft_hash_does_not(self):
        other = copy.deepcopy(self.raw)
        other.update(revision=2, trace_id="another-trace")
        self.assertEqual(policy_hash(self.raw), policy_hash(other))
        self.assertNotEqual(draft_hash(self.raw), draft_hash(other))

    def test_semantic_change_changes_hash_and_account_context_is_bound(self):
        edits = [("terms", "horizon_seconds", 86400),
                 ("scope", "network", "tron-mainnet"),
                 ("scope", "tenant_id", "another-tenant"),
                 ("scope", "owner_id", "another-owner"),
                 ("scope", "wallet", "41" + "2" * 40)]
        for section, key, value in edits:
            with self.subTest(key=key):
                other = copy.deepcopy(self.raw)
                other[section][key] = value
                if key == "horizon_seconds":
                    other["terms"]["withdrawals"] = []
                self.assertNotEqual(policy_hash(self.raw), policy_hash(other))

    def test_amount_and_ratio_remain_distinct_when_capital_changes(self):
        fixed = {"kind": "AMOUNT", "asset": "USDT", "amount": "3000"}
        ratio = {"kind": "BPS", "value": 3000}
        self.assertEqual(reserve_amount(fixed, "10000"), Decimal(3000))
        self.assertEqual(reserve_amount(ratio, "10000"), Decimal(3000))
        self.assertEqual(reserve_amount(fixed, "20000"), Decimal(3000))
        self.assertEqual(reserve_amount(ratio, "20000"), Decimal(6000))
        other = copy.deepcopy(self.raw)
        other["terms"]["immediate_cash"] = fixed
        self.assertNotEqual(policy_hash(self.raw), policy_hash(other))

    def test_unit_coercion_floats_and_bools_are_rejected(self):
        for bad in (3000, 3000.0, True, "30%", "3,000", "3e3", "NaN"):
            with self.subTest(bad=bad):
                raw = copy.deepcopy(self.raw)
                raw["terms"]["capital"][0]["amount"] = bad
                with self.assertRaises(MachineError):
                    normalize_mandate(raw)
        for bad in (True, "3000", 30.0, -1, 10001):
            raw = copy.deepcopy(self.raw)
            raw["terms"]["immediate_cash"]["value"] = bad
            with self.assertRaises(MachineError):
                normalize_mandate(raw)

    def test_missing_and_extra_financial_fields_are_not_defaulted(self):
        for key in self.raw["terms"]:
            with self.subTest(key=key):
                raw = copy.deepcopy(self.raw)
                del raw["terms"][key]
                with self.assertRaises(MachineError):
                    normalize_mandate(raw)
        self.raw["terms"]["node_url"] = "https://example.invalid"
        with self.assertRaises(MachineError):
            normalize_mandate(self.raw)

    def test_unknown_borrowing_can_be_a_draft_but_never_a_confirmed_projection(self):
        self.raw["terms"]["borrowing"]["consent"] = None
        self.assertIsNone(normalize_mandate(self.raw)["terms"]["borrowing"]["consent"])
        with self.assertRaisesRegex(MachineError, "unresolved"):
            compile_review_projections(confirmed(self.raw), **projection_parameters())

    def test_borrowing_consent_cannot_be_inferred_from_debt_or_opcode(self):
        for consent in (None, False):
            for change in ("debt", "action"):
                raw = copy.deepcopy(self.raw)
                raw["terms"]["borrowing"]["consent"] = consent
                if change == "debt":
                    raw["terms"]["borrowing"]["max_debt"]["amount"] = "1"
                else:
                    raw["terms"]["allowed_actions"].append("MINT_USDD")
                with self.assertRaisesRegex(MachineError, "consent"):
                    normalize_mandate(raw)
        self.raw["terms"]["borrowing"]["consent"] = True
        self.raw["terms"]["borrowing"]["max_debt"]["amount"] = "500"
        self.raw["terms"]["allowed_actions"].append("MINT_USDD")
        self.assertTrue(normalize_mandate(self.raw)["terms"]["borrowing"]["consent"])

    def test_forged_quote_and_missing_provenance_rejected(self):
        self.raw["source_refs"]["borrowing"][0]["quote"] = "차입 허용합니다"
        with self.assertRaisesRegex(MachineError, "source quote"):
            normalize_mandate(self.raw)
        self.raw = fixture()
        self.raw["source_refs"]["capital"] = []
        with self.assertRaisesRegex(MachineError, "source reference"):
            normalize_mandate(self.raw)

    def test_changed_source_is_bound_to_confirmation_without_changing_policy_hash(self):
        other = copy.deepcopy(self.raw)
        other["source_messages"][0]["text"] += " 추가 확인."
        self.assertEqual(policy_hash(self.raw), policy_hash(other))
        self.assertNotEqual(draft_hash(self.raw), draft_hash(other))

    def test_withdrawal_deadline_amount_and_cumulative_consistency(self):
        edits = [("after_seconds", 0), ("after_seconds", 3000000),
                 ("minimum", {"kind": "AMOUNT", "asset": "USDT", "amount": "2000"}),
                 ("minimum", {"kind": "AMOUNT", "asset": "USDD", "amount": "5000"}),
                 ("minimum", {"kind": "AMOUNT", "asset": "USDT", "amount": "20000"})]
        for key, value in edits:
            with self.subTest(value=value):
                raw = copy.deepcopy(self.raw)
                raw["terms"]["withdrawals"][0][key] = value
                with self.assertRaises(MachineError):
                    normalize_mandate(raw)

    def test_mixed_assets_preserved_but_legacy_projection_fails_closed(self):
        self.raw["terms"]["capital"].append({"asset": "TRX", "amount": "100"})
        self.assertEqual(len(normalize_mandate(self.raw)["terms"]["capital"]), 2)
        with self.assertRaisesRegex(MachineError, "multi-asset"):
            compile_review_projections(confirmed(self.raw), **projection_parameters())

    def test_legacy_views_share_budget_and_keep_unmapped_constraints_explicit(self):
        result = compile_review_projections(confirmed(self.raw), **projection_parameters())
        self.assertEqual(result["legacy_need"]["liquid_reserve"], "3000")
        self.assertEqual(result["inference_scope"]["sandbox"]["capital_limit"], "10000")
        self.assertEqual(result["inference_scope"]["sandbox"]["max_exposure"], "4000")
        self.assertEqual(result["basket_policy"]["max_capital"], "10000")
        self.assertEqual(result["mandate_policy_hash"], policy_hash(self.raw))
        self.assertEqual(result["constraints"]["withdrawals"][0]["minimum"]["amount"], "5000")
        self.assertIn("FULL_MANDATE_PLAN_REPLAY", result["remaining_verification"])
        self.assertEqual((result["execution_authority"], result["chain_status"]), ("NONE", "NOT_SUBMITTED"))

    def test_subday_horizon_not_rounded_into_legacy_need(self):
        self.raw["terms"]["horizon_seconds"] = 3600
        self.raw["terms"]["withdrawals"] = []
        result = compile_review_projections(confirmed(self.raw), **projection_parameters())
        self.assertIsNone(result["legacy_need"])
        self.assertEqual(result["legacy_need_status"], "UNREPRESENTABLE")

    def test_projection_rejects_tampering_and_draft_status(self):
        record = confirmed(self.raw)
        record["mandate"]["terms"]["immediate_cash"]["value"] = 1000
        with self.assertRaisesRegex(MachineError, "commitment"):
            compile_review_projections(record, **projection_parameters())
        record = confirmed(self.raw)
        record["status"] = "DRAFT"
        with self.assertRaisesRegex(MachineError, "confirmed"):
            compile_review_projections(record, **projection_parameters())

    def test_math_independent_of_callers_decimal_precision(self):
        with localcontext() as ctx:
            ctx.prec = 3
            self.assertEqual(reserve_amount({"kind": "BPS", "value": 3333}, "10000.123456"),
                             Decimal("3333.0411478848"))
            result = compile_review_projections(confirmed(self.raw), **projection_parameters())
            self.assertEqual(result["inference_scope"]["sandbox"]["max_exposure"], "4000")


class CapabilityContractTests(unittest.TestCase):
    def setUp(self):
        self.raw = {"schema_version": "economic-product-capability-1", "chain": "TRON",
                    "network": "tron-nile", "contract": "41" + "1" * 40,
                    "protocol": "justlend", "protocol_version": "v2", "action": "SUPPLY",
                    "token": {"asset": "USDT", "address": "41" + "2" * 40, "decimals": 6},
                    "stages": {key: {"status": "UNKNOWN", "evidence_hash": None}
                               for key in ("read", "quote", "simulate", "execute", "reconcile")}}

    def test_unknown_unsupported_and_supported_are_distinct(self):
        unknown = capability_hash(self.raw)
        self.raw["stages"]["execute"]["status"] = "UNSUPPORTED"
        self.assertNotEqual(unknown, capability_hash(self.raw))
        self.raw["stages"]["execute"]["status"] = "SUPPORTED"
        with self.assertRaises(MachineError):
            normalize_capability(self.raw)
        self.raw["stages"]["execute"]["evidence_hash"] = "a" * 64
        self.assertEqual(normalize_capability(self.raw)["stages"]["execute"]["status"], "SUPPORTED")

    def test_product_identity_separates_version_network_contract_and_action(self):
        original = product_id(self.raw)
        for field, value in (("network", "tron-mainnet"), ("protocol_version", "v1"),
                             ("contract", "41" + "3" * 40), ("action", "REDEEM")):
            changed = copy.deepcopy(self.raw)
            changed[field] = value
            self.assertNotEqual(original, product_id(changed))
        changed = copy.deepcopy(self.raw)
        changed["token"]["decimals"] = 8
        self.assertEqual(original, product_id(changed))
        self.assertNotEqual(capability_hash(self.raw), capability_hash(changed))

    def test_native_contract_and_token_are_not_silently_applied_to_justlend(self):
        self.raw["token"]["address"] = None
        with self.assertRaises(MachineError):
            normalize_capability(self.raw)
        self.raw["token"] = {"asset": "TRX", "address": None, "decimals": 6}
        self.raw["contract"] = None
        with self.assertRaises(MachineError):
            normalize_capability(self.raw)
        self.raw["protocol"] = "tron-native"
        self.raw["action"] = "STAKE"
        self.assertIsNone(normalize_capability(self.raw)["contract"])
