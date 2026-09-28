"""Real Ed25519 signatures constrain the typed portfolio input boundary."""

import contextlib
import copy
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from economic_machine.authenticated_basket import (commit_authenticated_basket,
                                                   verify_authenticated_basket)
from economic_machine.basket import commit_basket, verify_basket
from economic_machine.chain_binding import (prepare_authenticated_chain_binding,
                                            prepare_attestation_message,
                                            prepare_chain_binding,
                                            verify_authenticated_chain_binding)
from economic_machine.cli import main
from economic_machine.signed_evidence import (
    DOMAIN, ED25519_SPKI_PREFIX, assemble_signed_portfolio_inputs,
    verify_signed_portfolio_inputs,
)
from economic_machine.values import MachineError, canonical, digest
from economic_machine.vault_batch import prepare_vault_batch, verify_vault_batch
from economic_machine.vault_registration import (
    assess_batch_registry, observe_batch_registry, prepare_batch_attestations)


CASES = Path(__file__).resolve().parents[1] / "cases"


class SignedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="economic-ed25519-test-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.openssl = shutil.which("openssl")
        if self.openssl is None:
            self.skipTest("OpenSSL executable unavailable")
        self.template = json.loads((CASES / "economic_portfolio_template_demo.json").read_text())
        self.plain = json.loads((CASES / "economic_product_bundle_demo.json").read_text())
        self.keys = {}
        issuers = []
        for name, role in (("market_alpha", "MARKET"), ("market_beta", "MARKET"),
                           ("model_local", "MODEL"), ("position_local", "POSITION")):
            private = self.folder / (name + ".pem")
            public = self.folder / (name + ".der")
            subprocess.run([self.openssl, "genpkey", "-algorithm", "ED25519",
                            "-out", str(private)], check=True, capture_output=True)
            subprocess.run([self.openssl, "pkey", "-in", str(private), "-pubout",
                            "-outform", "DER", "-out", str(public)],
                           check=True, capture_output=True)
            raw = public.read_bytes()
            self.assertEqual(raw[:len(ED25519_SPKI_PREFIX)], ED25519_SPKI_PREFIX)
            self.keys[name] = private
            issuers.append({"issuer_id": name, "role": role,
                            "public_key_hex": raw[len(ED25519_SPKI_PREFIX):].hex(),
                            "network": self.template["network"],
                            "asset": self.template["asset"],
                            "valid_from": "2026-09-25T12:00:00+00:00",
                            "valid_until": "2026-09-25T12:10:00+00:00",
                            "revoked": False})
        self.roots = {"schema_version": "economic-evidence-trust-roots-1",
                      "openssl_path": self.openssl,
                      "openssl_sha256": hashlib.sha256(Path(self.openssl).read_bytes()).hexdigest(),
                      "thresholds": {"MARKET": 2, "MODEL": 1, "POSITION": 1},
                      "issuers": issuers,
                      "manifest_hashes": {item["product_id"]: digest(item)
                                          for item in self.plain["manifests"]}}
        self.signed = {"schema_version": "economic-signed-product-bundle-1",
                       "manifests": self.plain["manifests"],
                       "observations": [self.sign("MARKET", item,
                                                  ("market_alpha", "market_beta"))
                                        for item in self.plain["observations"]],
                       "assumptions": [self.sign("MODEL", item, ("model_local",))
                                       for item in self.plain["assumptions"]],
                       "positions": self.sign("POSITION", self.plain["positions"],
                                              ("position_local",))}
        self.state = json.loads((CASES / "economic_basket_state_demo.json").read_text())
        self.policy = json.loads((CASES / "economic_basket_policy_demo.json").read_text())
        self.policy["allowed_products"] = sorted(item["product_id"]
                                                   for item in self.plain["manifests"])
        self.context = json.loads((CASES / "economic_basket_registry_context_demo.json").read_text())
        self.expiry = "2026-09-25T12:01:45+00:00"

    def sign(self, role, payload, signers, *, signed_at="2026-09-25T12:00:45+00:00"):
        message_path = self.folder / "message.bin"
        signature_path = self.folder / "signature.bin"
        signatures = []
        for name in signers:
            message_path.write_bytes(canonical({"domain": DOMAIN, "role": role,
                                                "signed_at": signed_at, "issuer_id": name,
                                                "payload": payload}))
            subprocess.run([self.openssl, "pkeyutl", "-sign", "-rawin",
                            "-inkey", str(self.keys[name]), "-in", str(message_path),
                            "-out", str(signature_path)], check=True, capture_output=True)
            signatures.append({"issuer_id": name,
                               "signature_hex": signature_path.read_bytes().hex()})
        return {"schema_version": "economic-signed-evidence-1", "role": role,
                "payload": payload, "signed_at": signed_at, "signatures": signatures}

    def test_pinned_independent_role_keys_verify_without_trade_authority(self):
        result = assemble_signed_portfolio_inputs(self.template, self.signed, self.roots)
        self.assertEqual(result["status"], "SIGNED_CLAIMS_ASSEMBLED")
        self.assertEqual(result["source_trust"], "PINNED_SIGNATURES_NOT_ECONOMIC_TRUTH")
        self.assertEqual(result["assembly"]["portfolio_verdict"]["selected_candidate_id"],
                         "hedged")
        self.assertEqual([item["signature_count"] for item in result["attestations"]],
                         [2, 2, 2, 1, 1, 1, 1])
        self.assertEqual((result["execution_authority"], result["chain_status"]),
                         ("NONE", "NOT_SUBMITTED"))
        self.assertTrue(verify_signed_portfolio_inputs(result, self.template,
                                                       self.signed, self.roots))
        altered = copy.deepcopy(result)
        altered["assembly"]["portfolio_verdict"]["selected_candidate_id"] = "naked"
        self.assertFalse(verify_signed_portfolio_inputs(altered, self.template,
                                                        self.signed, self.roots))

    def test_tamper_duplicate_witness_and_threshold_failure_are_rejected(self):
        changed = copy.deepcopy(self.signed)
        changed["observations"][0]["payload"]["market"]["entry_fee_bps"] = 0
        with self.assertRaisesRegex(MachineError, "signature invalid"):
            assemble_signed_portfolio_inputs(self.template, changed, self.roots)
        changed = copy.deepcopy(self.signed)
        changed["observations"][0]["signatures"][1] = copy.deepcopy(
            changed["observations"][0]["signatures"][0])
        with self.assertRaisesRegex(MachineError, "duplicate evidence signature issuer"):
            assemble_signed_portfolio_inputs(self.template, changed, self.roots)
        changed = copy.deepcopy(self.signed)
        changed["observations"][0]["signatures"].pop()
        with self.assertRaisesRegex(MachineError, "threshold not met"):
            assemble_signed_portfolio_inputs(self.template, changed, self.roots)
        changed = copy.deepcopy(self.signed)
        changed["observations"][0]["signatures"][0]["issuer_id"] = "market_beta"
        changed["observations"][0]["signatures"][1]["issuer_id"] = "market_alpha"
        with self.assertRaisesRegex(MachineError, "signature invalid"):
            assemble_signed_portfolio_inputs(self.template, changed, self.roots)

    def test_role_time_manifest_and_binary_pins_fail_closed(self):
        changed = copy.deepcopy(self.signed)
        changed["assumptions"][0]["signatures"][0]["issuer_id"] = "market_alpha"
        with self.assertRaisesRegex(MachineError, "not active for this role"):
            assemble_signed_portfolio_inputs(self.template, changed, self.roots)
        changed = copy.deepcopy(self.signed)
        changed["positions"]["signed_at"] = "2026-09-25T12:01:01+00:00"
        with self.assertRaisesRegex(MachineError, "outside decision window"):
            assemble_signed_portfolio_inputs(self.template, changed, self.roots)
        changed = copy.deepcopy(self.signed)
        changed["positions"]["signed_at"] = "2026-09-25T12:00:46+00:00"
        with self.assertRaisesRegex(MachineError, "signature invalid"):
            assemble_signed_portfolio_inputs(self.template, changed, self.roots)
        roots = copy.deepcopy(self.roots)
        roots["manifest_hashes"]["tron_lend"] = "0" * 64
        with self.assertRaisesRegex(MachineError, "pinned hash"):
            assemble_signed_portfolio_inputs(self.template, self.signed, roots)
        roots = copy.deepcopy(self.roots)
        roots["openssl_sha256"] = "0" * 64
        with self.assertRaisesRegex(MachineError, "executable hash mismatch"):
            assemble_signed_portfolio_inputs(self.template, self.signed, roots)
        fake = self.folder / "fake-openssl"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o700)
        roots = copy.deepcopy(self.roots)
        roots["openssl_path"] = str(fake)
        roots["openssl_sha256"] = hashlib.sha256(fake.read_bytes()).hexdigest()
        with self.assertRaisesRegex(MachineError, "negative self-test failed"):
            assemble_signed_portfolio_inputs(self.template, self.signed, roots)
        roots = copy.deepcopy(self.roots)
        roots["issuers"][0]["revoked"] = True
        with self.assertRaisesRegex(MachineError, "insufficient active"):
            assemble_signed_portfolio_inputs(self.template, self.signed, roots)

    def test_cli_signed_roundtrip_uses_only_test_keys(self):
        template_path = CASES / "economic_portfolio_template_demo.json"
        signed_path, roots_path, result_path = (self.folder / name for name in
                                                ("signed.json", "roots.json", "result.json"))
        signed_path.write_text(json.dumps(self.signed))
        roots_path.write_text(json.dumps(self.roots))
        output = io.StringIO()
        with patch.object(sys, "argv", ["economic-machine", "portfolio-assemble-signed",
                                        "--request", str(template_path),
                                        "--bundle", str(signed_path),
                                        "--trust-roots", str(roots_path)]):
            with contextlib.redirect_stdout(output):
                main()
        result_path.write_text(output.getvalue())
        output = io.StringIO()
        with patch.object(sys, "argv", ["economic-machine", "portfolio-assemble-signed-verify",
                                        "--request", str(template_path),
                                        "--bundle", str(signed_path),
                                        "--trust-roots", str(roots_path),
                                        "--observation", str(result_path)]):
            with contextlib.redirect_stdout(output):
                main()
        self.assertTrue(json.loads(output.getvalue())["verified_replay"])

    def test_authenticated_basket_and_registry_binding_replay_exact_signed_inputs(self):
        signed_assembly = assemble_signed_portfolio_inputs(self.template, self.signed,
                                                            self.roots)["assembly"]
        unsigned = commit_basket(signed_assembly["selection_request"],
                                 signed_assembly["portfolio_verdict"], self.state,
                                 self.policy, valid_until=self.expiry)
        signed = commit_authenticated_basket(self.template, self.signed, self.roots,
                                             self.state, self.policy,
                                             valid_until=self.expiry)
        self.assertEqual(signed["schema_version"], "economic-basket-commitment-2")
        self.assertEqual(signed["authenticated_source"]["status"],
                         "PINNED_SIGNATURES_NOT_ECONOMIC_TRUTH")
        self.assertEqual(signed["execution_authority"], "NONE")
        self.assertTrue(verify_authenticated_basket(signed, self.template, self.signed,
                                                    self.roots, self.state, self.policy))
        self.assertFalse(verify_basket(signed, signed_assembly["selection_request"],
                                       self.state, self.policy))
        self.assertNotEqual(signed["commitment_hash"], unsigned["commitment_hash"])
        unsigned_binding = prepare_chain_binding(unsigned, signed_assembly["selection_request"],
                                                 self.state, self.policy, self.context)
        binding = prepare_authenticated_chain_binding(signed, self.template, self.signed,
                                                      self.roots, self.state, self.policy,
                                                      self.context)
        self.assertEqual(binding["basket_commitment_hash"], signed["commitment_hash"])
        self.assertEqual(binding["signed_assembly_hash"],
                         signed["authenticated_source"]["signed_assembly_hash"])
        self.assertNotEqual(binding["binding_hash"], unsigned_binding["binding_hash"])
        self.assertTrue(verify_authenticated_chain_binding(binding, signed, self.template,
                                                            self.signed, self.roots, self.state,
                                                            self.policy, self.context))
        message = prepare_attestation_message(binding, signed, self.template, self.signed,
                                              self.roots, self.state, self.policy,
                                              self.context, attestor_epoch=3)
        self.assertEqual(message["status"], "PREPARED_NOT_SIGNED")
        self.assertEqual(len(message["message_hash"]), 64)
        self.assertEqual(message["authenticated_source_hash"],
                         binding["authenticated_source_hash"])
        changed = copy.deepcopy(binding)
        changed["trust_root_hash"] = "0" * 64
        self.assertFalse(verify_authenticated_chain_binding(changed, signed, self.template,
                                                             self.signed, self.roots, self.state,
                                                             self.policy, self.context))
        with self.assertRaisesRegex(MachineError, "authenticated chain binding does not replay"):
            prepare_attestation_message(changed, signed, self.template, self.signed,
                                        self.roots, self.state, self.policy, self.context,
                                        attestor_epoch=3)
        changed_bundle = copy.deepcopy(self.signed)
        changed_bundle["observations"][0]["payload"]["market"]["entry_fee_bps"] = 0
        self.assertFalse(verify_authenticated_basket(signed, self.template, changed_bundle,
                                                     self.roots, self.state, self.policy))

    def test_signed_multi_leg_basket_compiles_to_unsigned_atomic_vault_plan(self):
        parent = commit_authenticated_basket(
            self.template, self.signed, self.roots, self.state, self.policy,
            valid_until=self.expiry)
        self.assertEqual(len(parent["basket"]["legs"]), 3)
        context = {"schema_version": "economic-vault-batch-context-1",
                   "network": parent["network"], "chain_id": 777000001,
                   "registry_address": "11" * 20, "vault_address": "44" * 20,
                   "input_asset_address": "22" * 20,
                   "input_asset_decimals": 6}
        from economic_machine.chain_binding import _epoch_seconds
        expiry = _epoch_seconds(self.expiry)
        specs = [
            {"product_id": leg["product_id"],
             "policy_id": f"{index + 1:02x}" * 32,
             "policy_hash": f"{index + 11:02x}" * 32,
             "target_address": f"{index + 3:02x}" * 20,
             "output_asset_address": f"{index + 6:02x}" * 20,
             "min_output_base_units": "1000000",
             "route_data_hex": "12345678" + f"{index:02x}",
             "deadline_epoch_seconds": expiry - 10}
            for index, leg in enumerate(parent["basket"]["legs"])]
        args = (parent, self.template, self.signed, self.roots,
                self.state, self.policy, context, specs)
        plan = prepare_vault_batch(
            *args, prepared_at="2026-09-25T12:01:05+00:00",
            batch_deadline_epoch_seconds=expiry - 20)
        self.assertEqual(plan["execution_authority"], "NONE")
        self.assertEqual(plan["signature_status"], "NOT_SIGNED")
        self.assertEqual(plan["transaction_status"], "NOT_BUILT")
        self.assertEqual(plan["parent_basket_hash"], parent["basket_hash"])
        self.assertEqual(len(plan["orders"]), 3)
        self.assertEqual([item["registry_binding"]["binding_hash"]
                          for item in plan["orders"]],
                         sorted(item["registry_binding"]["binding_hash"]
                                for item in plan["orders"]))
        self.assertEqual(sum(int(item["amount_base_units"]) for item in plan["orders"]),
                         900000000)
        self.assertTrue(verify_vault_batch(
            plan, *args, prepared_at="2026-09-25T12:01:05+00:00",
            batch_deadline_epoch_seconds=expiry - 20))
        paths = {name: self.folder / (name + ".json") for name in
                 ("template", "signed", "roots", "state", "policy", "parent",
                  "context", "specs", "plan")}
        for name, value in (("template", self.template), ("signed", self.signed),
                            ("roots", self.roots), ("state", self.state),
                            ("policy", self.policy), ("parent", parent),
                            ("context", context), ("specs", {
                                "schema_version": "economic-vault-leg-specs-1", "legs": specs})):
            paths[name].write_text(json.dumps(value))
        cli_args = ["--request", str(paths["template"]), "--bundle", str(paths["signed"]),
                    "--trust-roots", str(paths["roots"]), "--state", str(paths["state"]),
                    "--policy", str(paths["policy"]), "--commitment", str(paths["parent"]),
                    "--vault-context", str(paths["context"]), "--leg-specs", str(paths["specs"]),
                    "--at", "2026-09-25T12:01:05+00:00",
                    "--batch-deadline", str(expiry - 20)]
        output = io.StringIO()
        with patch.object(sys, "argv", ["economic-machine", "vault-batch-plan-signed"] + cli_args):
            with contextlib.redirect_stdout(output):
                main()
        self.assertEqual(json.loads(output.getvalue()), plan)
        paths["plan"].write_text(output.getvalue())
        output = io.StringIO()
        with patch.object(sys, "argv", ["economic-machine", "vault-batch-verify-signed"]
                          + cli_args + ["--vault-plan", str(paths["plan"])]):
            with contextlib.redirect_stdout(output):
                main()
        self.assertEqual(json.loads(output.getvalue()), {"verified_replay": True})
        attested = prepare_batch_attestations(
            plan, *args, prepared_at="2026-09-25T12:01:05+00:00",
            batch_deadline_epoch_seconds=expiry - 20, attestor_epoch=3)
        self.assertEqual(attested["epoch_source"], "CALLER_CLAIM_NOT_VERIFIED")
        self.assertEqual(len(attested["items"]), 3)
        self.assertEqual([item["binding_hash"] for item in attested["items"]],
                         [item["registry_binding"]["binding_hash"]
                          for item in plan["orders"]])
        self.assertEqual(len({item["message_hash"] for item in attested["items"]}), 3)
        self.assertEqual(attested["execution_authority"], "NONE")
        with self.assertRaisesRegex(MachineError, "epoch"):
            prepare_batch_attestations(
                plan, *args, prepared_at="2026-09-25T12:01:05+00:00",
                batch_deadline_epoch_seconds=expiry - 20, attestor_epoch=0)
        output = io.StringIO()
        with patch.object(sys, "argv", ["economic-machine", "vault-batch-attest-signed"]
                          + cli_args + ["--vault-plan", str(paths["plan"]),
                                        "--attestor-epoch", "3"]):
            with contextlib.redirect_stdout(output):
                main()
        self.assertEqual(json.loads(output.getvalue()), attested)
        base_observation = {"block_id": "ab" * 32, "number": 123,
                            "timestamp_ms": (expiry - 40) * 1000,
                            "runtime_sha256": "aa" * 32,
                            "expected_runtime_sha256": "aa" * 32,
                            "endpoint_fingerprint": "bb" * 32,
                            "current_attestor_epoch": 3,
                            "observed_at_ms": (expiry - 40) * 1000,
                            "paused": False,
                            "asset_budget": {"reserved": "900000000"},
                            "policy": {"reserved_amount": "900000000"}}
        observations_by_leg = [copy.deepcopy(base_observation) for _ in plan["orders"]]
        with patch("economic_machine.vault_registration.read_registry_observation",
                   side_effect=observations_by_leg) as node_read:
            observed = observe_batch_registry(
                plan, *args, reader_config={},
                prepared_at="2026-09-25T12:01:05+00:00",
                batch_deadline_epoch_seconds=expiry - 20)
        self.assertEqual(node_read.call_count, 3)
        with patch("economic_machine.vault_registration.assess_registry_observation",
                   return_value={"status": "OBSERVED_MATCH"}):
            assessment = assess_batch_registry(
                observed, plan, *args,
                prepared_at="2026-09-25T12:01:05+00:00",
                batch_deadline_epoch_seconds=expiry - 20)
            self.assertEqual(assessment["status"], "OBSERVED_MATCH")
            self.assertEqual(assessment["execution_authority"], "NONE")
            observed_path = self.folder / "batch-observations.json"
            observed_path.write_text(json.dumps(observed))
            output = io.StringIO()
            with patch.object(sys, "argv", ["economic-machine", "vault-batch-assess-signed"]
                              + cli_args + ["--vault-plan", str(paths["plan"]),
                                            "--observation", str(observed_path)]):
                with contextlib.redirect_stdout(output):
                    main()
            self.assertEqual(json.loads(output.getvalue()), assessment)
            mixed = copy.deepcopy(observed)
            mixed["items"][1]["observation"]["block_id"] = "cd" * 32
            mixed["observation_set_hash"] = digest({
                key: value for key, value in mixed.items()
                if key != "observation_set_hash"})
            assessment = assess_batch_registry(
                mixed, plan, *args,
                prepared_at="2026-09-25T12:01:05+00:00",
                batch_deadline_epoch_seconds=expiry - 20)
            self.assertEqual(assessment["status"], "WITHHELD")
            self.assertIn("MIXED_REGISTRY_SNAPSHOT", assessment["reason_codes"])
            under_reserved = copy.deepcopy(observed)
            for item in under_reserved["items"]:
                item["observation"]["asset_budget"]["reserved"] = "350000000"
            under_reserved["observation_set_hash"] = digest({
                key: value for key, value in under_reserved.items()
                if key != "observation_set_hash"})
            assessment = assess_batch_registry(
                under_reserved, plan, *args,
                prepared_at="2026-09-25T12:01:05+00:00",
                batch_deadline_epoch_seconds=expiry - 20)
            self.assertIn("ASSET_BATCH_NOT_RESERVED", assessment["reason_codes"])
            self.assertEqual(assessment["status"], "WITHHELD")
            shared_specs = copy.deepcopy(specs)
            for key in ("policy_id", "policy_hash", "target_address"):
                shared_specs[1][key] = shared_specs[0][key]
            shared_args = (parent, self.template, self.signed, self.roots,
                           self.state, self.policy, context, shared_specs)
            shared_plan = prepare_vault_batch(
                *shared_args, prepared_at="2026-09-25T12:01:05+00:00",
                batch_deadline_epoch_seconds=expiry - 20)
            shared_items = [{"binding_hash": order["registry_binding"]["binding_hash"],
                             "observation": copy.deepcopy(base_observation)}
                            for order in shared_plan["orders"]]
            for item in shared_items:
                item["observation"]["policy"]["reserved_amount"] = "350000000"
            shared_observed = {
                "schema_version": "economic-vault-batch-registry-observations-1",
                "plan_hash": shared_plan["plan_hash"], "items": shared_items,
                "source_trust": "NODE_RESPONSE_ONLY", "execution_authority": "NONE"}
            shared_observed["observation_set_hash"] = digest(shared_observed)
            assessment = assess_batch_registry(
                shared_observed, shared_plan, *shared_args,
                prepared_at="2026-09-25T12:01:05+00:00",
                batch_deadline_epoch_seconds=expiry - 20)
            self.assertIn("POLICY_BATCH_NOT_RESERVED", assessment["reason_codes"])
            self.assertEqual(assessment["status"], "WITHHELD")
            changed_observation = copy.deepcopy(observed)
            changed_observation["items"][0]["observation"]["number"] = 124
            with self.assertRaisesRegex(MachineError, "hash mismatch"):
                assess_batch_registry(
                    changed_observation, plan, *args,
                    prepared_at="2026-09-25T12:01:05+00:00",
                    batch_deadline_epoch_seconds=expiry - 20)
        changed = copy.deepcopy(specs)
        changed[0]["route_data_hex"] = "1234567800ff"
        self.assertFalse(verify_vault_batch(
            plan, parent, self.template, self.signed, self.roots,
            self.state, self.policy, context, changed,
            prepared_at="2026-09-25T12:01:05+00:00",
            batch_deadline_epoch_seconds=expiry - 20))
        with patch("economic_machine.vault_registration.read_registry_observation") as node_read:
            with self.assertRaisesRegex(MachineError, "does not replay"):
                observe_batch_registry(
                    plan, parent, self.template, self.signed, self.roots,
                    self.state, self.policy, context, changed, {},
                    prepared_at="2026-09-25T12:01:05+00:00",
                    batch_deadline_epoch_seconds=expiry - 20)
            node_read.assert_not_called()
        with self.assertRaisesRegex(MachineError, "every noncash leg"):
            prepare_vault_batch(
                parent, self.template, self.signed, self.roots,
                self.state, self.policy, context, specs[:-1],
                prepared_at="2026-09-25T12:01:05+00:00",
                batch_deadline_epoch_seconds=expiry - 20)
        altered = copy.deepcopy(parent)
        altered["basket"]["legs"][0]["amount"] = "1"
        with self.assertRaisesRegex(MachineError, "does not replay"):
            prepare_vault_batch(
                altered, self.template, self.signed, self.roots,
                self.state, self.policy, context, specs,
                prepared_at="2026-09-25T12:01:05+00:00",
                batch_deadline_epoch_seconds=expiry - 20)

    def test_signed_claim_must_remain_valid_through_commitment_expiry(self):
        with self.assertRaisesRegex(MachineError, "signed product input expires"):
            commit_authenticated_basket(self.template, self.signed, self.roots,
                                        self.state, self.policy,
                                        valid_until="2026-09-25T12:02:01+00:00")
        shortened = copy.deepcopy(self.signed)
        observation = copy.deepcopy(shortened["observations"][0]["payload"])
        observation["valid_until"] = "2026-09-25T12:01:30+00:00"
        shortened["observations"][0] = self.sign("MARKET", observation,
                                                   ("market_alpha", "market_beta"))
        assumption = copy.deepcopy(shortened["assumptions"][0]["payload"])
        assumption["observation_hash"] = digest(observation)
        shortened["assumptions"][0] = self.sign("MODEL", assumption, ("model_local",))
        with self.assertRaisesRegex(MachineError, "signed product input expires"):
            commit_authenticated_basket(self.template, shortened, self.roots,
                                        self.state, self.policy, valid_until=self.expiry)
        old = commit_authenticated_basket(self.template, self.signed, self.roots,
                                          self.state, self.policy, valid_until=self.expiry)
        changed_roots = copy.deepcopy(self.roots)
        changed_roots["issuers"][0]["valid_until"] = "2026-09-25T12:09:59+00:00"
        self.assertFalse(verify_authenticated_basket(old, self.template, self.signed,
                                                     changed_roots, self.state, self.policy))

    def test_signed_cli_gates_chain_read_before_rpc(self):
        files = {name: self.folder / (name + ".json") for name in
                 ("template", "signed", "roots", "state", "policy", "context",
                  "commitment", "binding")}
        for name, value in (("template", self.template), ("signed", self.signed),
                            ("roots", self.roots), ("state", self.state),
                            ("policy", self.policy), ("context", self.context)):
            files[name].write_text(json.dumps(value))
        common = ["--request", str(files["template"]), "--bundle", str(files["signed"]),
                  "--trust-roots", str(files["roots"]), "--state", str(files["state"]),
                  "--policy", str(files["policy"])]

        def invoke(command, *extra):
            output = io.StringIO()
            with patch.object(sys, "argv", ["economic-machine", command, *common, *extra]):
                with contextlib.redirect_stdout(output):
                    main()
            return json.loads(output.getvalue())

        commitment = invoke("basket-commit-signed", "--valid-until", self.expiry)
        files["commitment"].write_text(json.dumps(commitment))
        self.assertTrue(invoke("basket-verify-signed", "--commitment",
                               str(files["commitment"]))["verified_replay"])
        binding = invoke("chain-bind-signed", "--commitment", str(files["commitment"]),
                         "--chain-context", str(files["context"]))
        files["binding"].write_text(json.dumps(binding))
        self.assertTrue(invoke("chain-bind-verify-signed", "--commitment",
                               str(files["commitment"]), "--chain-context",
                               str(files["context"]), "--chain-binding",
                               str(files["binding"]))["verified_replay"])
        message = invoke("chain-attest-message-signed", "--commitment",
                         str(files["commitment"]), "--chain-context",
                         str(files["context"]), "--chain-binding",
                         str(files["binding"]), "--attestor-epoch", "3")
        self.assertEqual(message["status"], "PREPARED_NOT_SIGNED")
        self.assertEqual(message["attestor_epoch"], 3)
        forged = copy.deepcopy(binding)
        forged["binding_hash"] = "0" * 64
        files["binding"].write_text(json.dumps(forged))
        with patch("economic_machine.cli.read_registry_observation") as node_read:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    invoke("tron-observe-signed", "--commitment", str(files["commitment"]),
                           "--chain-context", str(files["context"]),
                           "--chain-binding", str(files["binding"]),
                           "--reader-config", str(files["context"]))
            node_read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
