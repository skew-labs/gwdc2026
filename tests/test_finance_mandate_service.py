"""Adversarial lifecycle, atomicity, cross-account and reservation tests."""

import copy
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from economic_machine.mandate import draft_hash
from economic_machine.values import MachineError
from finance_service.context import AuthenticatedContext
from finance_service.mandate_service import MandateService
from finance_service.repository import InMemoryMandateRepository, VersionConflict


CASE = Path(__file__).resolve().parents[1] / "cases/economic_mandate_demo.json"


class MandateServiceTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads(CASE.read_text())
        self.at = "2026-09-28T12:01:00Z"
        self.context = AuthenticatedContext(**self.raw["scope"], session_id="session-demo",
            issued_at="2026-09-28T12:00:00Z", expires_at="2026-09-28T14:00:00Z", trace_id="trace-demo")
        self.repo = InMemoryMandateRepository()
        self.service = MandateService(self.repo, lambda: self.at)
        self.mid = self.raw["mandate_id"]

    def create(self, *, confirm=True):
        result = self.service.create(self.context, self.raw)
        if confirm:
            result = self.service.confirm(self.context, self.mid, expected_version=result["version"],
                                           expected_draft_hash=result["revisions"][-1]["draft_hash"])
        return result

    def review(self, aggregate, *, name="review-one", amount="3000", fee="10"):
        return self.service.prepare_review(self.context, self.mid,
            {"scope": self.context.scope, "review_id": name,
             "policy_hash": aggregate["revisions"][-1]["policy_hash"],
             "plan_hash": "a" * 64, "state_root": "b" * 64,
             "amount": {"asset": "USDT", "amount": amount},
             "fee": {"asset": "USDT", "amount": fee}, "expires_at": "2026-09-28T12:10:00Z"},
            expected_version=aggregate["version"])

    def hold(self, aggregate, name="review-one", *, context=None):
        return self.service.hold_review(context or self.context, self.mid, name,
            expected_version=aggregate["version"],
            expected_review_hash=aggregate["reviews"][name]["review_hash"])

    def revised(self):
        raw = copy.deepcopy(self.raw)
        raw["revision"] = 2
        raw["terms"]["immediate_cash"]["value"] = 4000
        return raw

    def test_draft_cannot_produce_a_review_or_legacy_scope(self):
        aggregate = self.create(confirm=False)
        with self.assertRaisesRegex(MachineError, "confirmed"):
            self.review(aggregate)
        with self.assertRaisesRegex(MachineError, "confirmed"):
            self.service.project(self.context, self.mid)
        self.assertEqual(self.service.get(self.context, self.mid)["version"], 1)

    def test_confirmation_binds_exact_draft_without_trade_authority(self):
        aggregate = self.create(confirm=False)
        with self.assertRaisesRegex(MachineError, "exact draft"):
            self.service.confirm(self.context, self.mid, expected_version=1, expected_draft_hash="0" * 64)
        aggregate = self.service.confirm(self.context, self.mid, expected_version=1,
            expected_draft_hash=aggregate["revisions"][-1]["draft_hash"])
        result = self.review(aggregate)["reviews"]["review-one"]
        self.assertEqual((result["execution_authority"], result["chain_status"]), ("NONE", "NOT_SUBMITTED"))
        self.assertIn("PR04_PLAN_REPLAY_REQUIRED", result["blockers"])

    def test_unresolved_borrowing_blocks_confirmation_and_retains_draft(self):
        self.raw["terms"]["borrowing"]["consent"] = None
        aggregate = self.create(confirm=False)
        with self.assertRaisesRegex(MachineError, "unresolved"):
            self.service.confirm(self.context, self.mid, expected_version=1,
                expected_draft_hash=aggregate["revisions"][-1]["draft_hash"])
        self.assertEqual(self.service.get(self.context, self.mid)["revisions"][-1]["status"], "DRAFT")

    def test_body_cannot_switch_owner_tenant_wallet_or_network(self):
        for field, value in (("owner_id", "other-owner"), ("tenant_id", "other-tenant"),
                             ("wallet", "41" + "2" * 40), ("network", "tron-mainnet")):
            with self.subTest(field=field):
                raw = copy.deepcopy(self.raw)
                raw["scope"][field] = value
                with self.assertRaisesRegex(MachineError, "authenticated context"):
                    self.service.create(self.context, raw)

    def test_same_mandate_id_is_isolated_across_accounts(self):
        self.create()
        for field, value in (("owner_id", "other-owner"), ("tenant_id", "other-tenant"),
                             ("wallet", "41" + "2" * 40), ("network", "tron-mainnet")):
            with self.subTest(field=field):
                other_context = replace(self.context, **{field: value})
                with self.assertRaisesRegex(MachineError, "not found"):
                    self.service.get(other_context, self.mid)
                other = copy.deepcopy(self.raw)
                other["scope"] = other_context.scope
                self.assertEqual(self.service.create(other_context, other)["version"], 1)
        self.assertEqual(self.service.get(self.context, self.mid)["revisions"][-1]["status"], "CONFIRMED")

    def test_revision_invalidates_unsigned_and_requires_fresh_confirmation(self):
        old = self.review(self.create())
        changed = self.service.revise(self.context, self.revised(), expected_version=old["version"])
        self.assertEqual([item["status"] for item in changed["revisions"]], ["SUPERSEDED", "DRAFT"])
        self.assertEqual(changed["reviews"]["review-one"]["status"], "INVALIDATED")
        self.assertNotEqual(changed["revisions"][0]["policy_hash"], changed["revisions"][1]["policy_hash"])
        with self.assertRaisesRegex(MachineError, "confirmed"):
            self.hold(changed)
        with self.assertRaisesRegex(MachineError, "exact draft"):
            self.service.confirm(self.context, self.mid, expected_version=changed["version"],
                                 expected_draft_hash=old["revisions"][-1]["draft_hash"])
        confirmed = self.service.confirm(self.context, self.mid, expected_version=changed["version"],
                                         expected_draft_hash=changed["revisions"][-1]["draft_hash"])
        with self.assertRaisesRegex(MachineError, "unsigned"):
            self.hold(confirmed)

    def test_revoke_preserves_pending_hold_and_invalidates_only_unsigned(self):
        aggregate = self.hold(self.review(self.create()))
        aggregate = self.review(aggregate, name="review-two", amount="1000")
        before = copy.deepcopy(aggregate["holds"])
        revoked = self.service.revoke(self.context, self.mid, expected_version=aggregate["version"])
        self.assertEqual(revoked["holds"], before)
        self.assertEqual(revoked["reviews"]["review-one"]["status"], "HELD")
        self.assertEqual(revoked["reviews"]["review-two"]["status"], "INVALIDATED")
        with self.assertRaisesRegex(MachineError, "resurrected"):
            self.service.revise(self.context, self.revised(), expected_version=revoked["version"])

    def test_tighter_revision_cannot_forget_existing_pending_capital(self):
        aggregate = self.hold(self.review(self.create()))
        changed = self.service.revise(self.context, self.revised(), expected_version=aggregate["version"])
        changed = self.service.confirm(self.context, self.mid, expected_version=changed["version"],
                                       expected_draft_hash=changed["revisions"][-1]["draft_hash"])
        self.assertEqual(changed["holds"], aggregate["holds"])
        with self.assertRaisesRegex(MachineError, "required cash"):
            self.review(changed, name="review-new", amount="3000")

    def test_multiple_unsigned_reviews_cannot_overbook_when_held(self):
        aggregate = self.review(self.create(), amount="4000")
        aggregate = self.review(aggregate, name="review-two", amount="4000")
        held = self.hold(aggregate)
        with self.assertRaisesRegex(MachineError, "cumulative"):
            self.hold(held, "review-two")
        after = self.service.get(self.context, self.mid)
        self.assertEqual(len(after["holds"]), 1)
        self.assertEqual(after["version"], held["version"])

    def test_fee_cash_single_and_cumulative_caps_checked_inside_transaction(self):
        aggregate = self.create()
        for amount, fee, error in (("4001", "10", "single"), ("1000", "21", "fee"), ("0", "0", "single")):
            with self.subTest(amount=amount, fee=fee):
                with self.assertRaisesRegex(MachineError, error):
                    self.review(aggregate, amount=amount, fee=fee)
        self.assertEqual(self.service.get(self.context, self.mid)["reviews"], {})
        self.assertEqual(self.service.get(self.context, self.mid)["version"], aggregate["version"])

    def test_expired_or_replaced_sessions_cannot_reuse_review(self):
        aggregate = self.review(self.create())
        other = replace(self.context, session_id="replacement-session")
        with self.assertRaisesRegex(MachineError, "different authenticated session"):
            self.hold(aggregate, context=other)
        self.at = "2026-09-28T14:00:00Z"
        with self.assertRaisesRegex(MachineError, "session"):
            self.hold(aggregate)
        self.assertEqual(self.repo.read(self.context.scope, self.mid)["holds"], {})

    def test_expired_review_and_mandate_never_release_an_existing_hold(self):
        aggregate = self.hold(self.review(self.create()))
        aggregate = self.review(aggregate, name="review-two", amount="1000")
        self.at = "2026-09-28T12:10:00Z"
        with self.assertRaisesRegex(MachineError, "review expired"):
            self.hold(aggregate, "review-two")
        self.at = "2026-09-28T13:00:00Z"
        with self.assertRaisesRegex(MachineError, "mandate not effective or expired"):
            self.review(aggregate, name="after-expiry")
        self.assertEqual(self.service.get(self.context, self.mid)["holds"], aggregate["holds"])

    def test_mutating_returned_data_cannot_change_repository(self):
        aggregate = self.create()
        aggregate["revisions"][-1]["mandate"]["terms"]["capital"][0]["amount"] = "999999"
        read = self.service.get(self.context, self.mid)
        self.assertEqual(read["revisions"][-1]["mandate"]["terms"]["capital"][0]["amount"], "10000")
        read["revisions"].clear()
        self.assertEqual(len(self.service.get(self.context, self.mid)["revisions"]), 1)

    def test_conflicting_edits_have_one_winner_and_no_partial_revision(self):
        aggregate = self.create()
        barrier = threading.Barrier(2)

        def attempt(value):
            raw = self.revised()
            raw["terms"]["immediate_cash"]["value"] = value
            barrier.wait(timeout=5)
            try:
                self.service.revise(self.context, raw, expected_version=aggregate["version"])
                return "committed"
            except VersionConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt, value) for value in (1000, 2000)]
            results = [future.result(timeout=10) for future in futures]
        self.assertCountEqual(results, ["committed", "conflict"])
        final = self.service.get(self.context, self.mid)
        self.assertEqual((len(final["revisions"]), final["version"]), (2, 3))

    def test_revision_racing_with_hold_never_loses_possible_external_effect(self):
        aggregate = self.review(self.create())
        barrier = threading.Barrier(2)

        def attempt(kind):
            barrier.wait(timeout=5)
            try:
                if kind == "revise":
                    self.service.revise(self.context, self.revised(), expected_version=aggregate["version"])
                else:
                    self.hold(aggregate)
                return kind
            except VersionConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt, kind) for kind in ("revise", "hold")]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(results.count("conflict"), 1)
        final = self.service.get(self.context, self.mid)
        if "hold" in results:
            final = self.service.revise(self.context, self.revised(), expected_version=final["version"])
            self.assertEqual(len(final["holds"]), 1)
            self.assertEqual(final["reviews"]["review-one"]["status"], "HELD")
        else:
            self.assertEqual(final["holds"], {})
            self.assertEqual(final["reviews"]["review-one"]["status"], "INVALIDATED")

    def test_failed_transaction_rolls_back_every_field(self):
        aggregate = self.create()

        def bad_mutation(current):
            current["revisions"][0]["status"] = "REVOKED"
            current["holds"]["fake"] = {"amount": "1"}
            raise MachineError("injected storage failure")

        with self.assertRaisesRegex(MachineError, "injected"):
            self.repo.transact(self.context.scope, self.mid, aggregate["version"], bad_mutation)
        self.assertEqual(self.service.get(self.context, self.mid), aggregate)

    def test_stored_review_payload_tampering_cannot_use_old_commitment(self):
        aggregate = self.review(self.create())

        def corrupt(current):
            current["reviews"]["review-one"]["amount"]["amount"] = "3500"
            return current

        corrupted = self.repo.transact(self.context.scope, self.mid, aggregate["version"], corrupt)
        with self.assertRaisesRegex(MachineError, "commitment mismatch"):
            self.hold(corrupted)
        self.assertEqual(self.service.get(self.context, self.mid)["holds"], {})

    def test_review_id_replay_cannot_replace_a_held_record(self):
        aggregate = self.hold(self.review(self.create()))
        with self.assertRaisesRegex(MachineError, "already used"):
            self.review(aggregate, amount="1000")
        self.assertEqual(self.service.get(self.context, self.mid)["holds"], aggregate["holds"])

    def test_session_and_policy_expiry_bound_new_review(self):
        aggregate = self.create()
        brief_session = replace(self.context, expires_at="2026-09-28T12:02:00Z")
        self.context = brief_session
        with self.assertRaisesRegex(MachineError, "validity"):
            self.review(aggregate)
        self.assertEqual(self.service.get(self.context, self.mid)["reviews"], {})

    def test_review_body_identity_mismatch_is_rejected_before_storage(self):
        aggregate = self.review(self.create())
        row = aggregate["reviews"]["review-one"]
        raw = {key: copy.deepcopy(row[key]) for key in
               ("scope", "review_id", "policy_hash", "plan_hash", "state_root", "amount", "fee", "expires_at")}
        raw["review_id"] = "another-review"
        raw["scope"]["owner_id"] = "another-owner"
        with self.assertRaisesRegex(MachineError, "authenticated context"):
            self.service.prepare_review(self.context, self.mid, raw, expected_version=aggregate["version"])
        self.assertEqual(self.service.get(self.context, self.mid), aggregate)

    def test_old_confirmation_and_nonconsecutive_revision_rejected(self):
        aggregate = self.create()
        raw = self.revised()
        raw["revision"] = 4
        with self.assertRaisesRegex(MachineError, "advance by one"):
            self.service.revise(self.context, raw, expected_version=aggregate["version"])
        with self.assertRaises(VersionConflict):
            self.service.confirm(self.context, self.mid, expected_version=1,
                                 expected_draft_hash=draft_hash(self.raw))

    def test_clock_rollback_cannot_reactivate_validity(self):
        aggregate = self.create()
        self.at = "2026-09-28T12:00:30Z"
        with self.assertRaisesRegex(MachineError, "backwards"):
            self.review(aggregate)

    def test_hold_blocks_base_asset_reinterpretation(self):
        aggregate = self.hold(self.review(self.create()))
        raw = self.revised()
        raw["terms"]["base_asset"] = "USDD"
        raw["terms"]["capital"][0]["asset"] = "USDD"
        raw["terms"]["withdrawals"][0]["minimum"]["asset"] = "USDD"
        raw["terms"]["borrowing"]["max_debt"]["asset"] = "USDD"
        for value in raw["terms"]["limits"].values():
            value["asset"] = "USDD"
        with self.assertRaisesRegex(MachineError, "base asset"):
            self.service.revise(self.context, raw, expected_version=aggregate["version"])
