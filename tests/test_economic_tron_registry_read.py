"""The customer node reader remains observation-only under hostile RPC replies."""

import copy
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from economic_machine.basket import commit_basket
from economic_machine.chain_binding import prepare_chain_binding
from economic_machine.cli import main
from economic_machine.portfolio import select_portfolio
from economic_machine.tron_registry_read import (
    CONFIG_VERSION, assess_registry_observation, read_registry_observation,
)
from economic_machine.values import MachineError, digest


CASES = Path(__file__).resolve().parents[1] / "cases"


def word(value):
    return f"{value:064x}" if isinstance(value, int) else value.rjust(64, "0")


class RegistryReaderTests(unittest.TestCase):
    def setUp(self):
        def read(name):
            return json.loads((CASES / name).read_text(encoding="utf-8"))

        self.request = read("economic_portfolio_candidates_demo.json")
        self.state = read("economic_basket_state_demo.json")
        self.policy = read("economic_basket_policy_demo.json")
        self.context = read("economic_basket_registry_context_demo.json")
        self.commitment = commit_basket(self.request, select_portfolio(self.request),
                                        self.state, self.policy,
                                        valid_until="2026-09-25T12:03:00+00:00")
        self.binding = prepare_chain_binding(self.commitment, self.request, self.state,
                                             self.policy, self.context)
        self.block_ms = (self.binding["valid_until_epoch_seconds"] - 60) * 1000
        self.runtime = "6000"
        self.config = {
            "schema_version": CONFIG_VERSION,
            "solidity_url": "https://solid.example.invalid",
            "fullnode_url": "https://full.example.invalid",
            "owner_address": "11" * 20,
            "expected_runtime_sha256": hashlib.sha256(bytes.fromhex(self.runtime)).hexdigest(),
            "max_block_age_ms": 120000,
            "api_key_env": None,
        }
        self.calls = []
        self.replies = {
            "computeBasketBinding(bytes32,bytes32,bytes32,bytes32,uint256,uint64)":
                [self.binding["binding_hash"]],
            "policies(bytes32)": [
                self.binding["policy_hash"], self.binding["asset_address"],
                self.binding["target_address"], 2000000000,
                self.binding["valid_until_epoch_seconds"] + 100, 1,
            ],
            "baskets(bytes32)": [
                self.binding["policy_id"], self.binding["state_root"],
                self.binding["basket_hash"], self.binding["basket_commitment_hash"],
                int(self.binding["amount_base_units"]),
                self.binding["valid_until_epoch_seconds"], 1, 0, 0,
            ],
            "bindingByCommitment(bytes32)": [self.binding["binding_hash"]],
            "activeBasket(bytes32)": [1],
            "paused()": [0],
            "reservedBasketAmount(bytes32)": [1000000000],
            "consumedBasketAmount(bytes32)": [0],
            "basketBudgetRemaining(bytes32)": [1000000000],
            "assetBudgets(address)": [3000000000, 1500000000, 500000000, 1],
            "assetBudgetRemaining(address)": [1000000000],
            "attestationRequired(bytes32)": [0],
            "authenticatedSourceByBinding(bytes32)": [0],
            "attestedEpochByBinding(bytes32)": [0],
            "attestorEpoch()": [1],
        }
        self.block = {
            "blockID": f"{123:016x}" + "ab" * 24,
            "block_header": {"raw_data": {"number": 123, "timestamp": self.block_ms}},
        }

    def node(self, view, path, payload):
        self.calls.append((view, path, payload))
        if path == "/walletsolidity/getnowblock":
            return self.block
        if path == "/wallet/getcontractinfo":
            return {"runtimecode": self.runtime}
        self.assertEqual(path, "/walletsolidity/triggerconstantcontract")
        selector = payload["function_selector"]
        return {"result": {"result": True},
                "transaction": {"ret": [{"ret": "SUCCESS"}]},
                "constant_result": ["".join(word(item) for item in self.replies[selector])]}

    def observe(self, **kwargs):
        return read_registry_observation(self.binding, self.config,
                                         now_ms=self.block_ms + 1000,
                                         transport=kwargs.get("transport", self.node))

    def test_matching_snapshot_is_still_not_execution_authority(self):
        observation = self.observe()
        result = assess_registry_observation(self.binding, observation)
        self.assertEqual(result["status"], "OBSERVED_MATCH")
        self.assertEqual(result["source_trust"], "NODE_RESPONSE_ONLY")
        self.assertEqual(result["execution_authority"], "NONE")
        self.assertEqual(len(self.calls), 18)
        self.assertEqual(self.calls[0][:2], ("solidity", "/walletsolidity/getnowblock"))
        self.assertEqual(self.calls[-1][:2], ("solidity", "/walletsolidity/getnowblock"))
        self.assertEqual({call[1] for call in self.calls}, {
            "/walletsolidity/getnowblock", "/wallet/getcontractinfo",
            "/walletsolidity/triggerconstantcontract"})
        self.assertEqual(self.calls[1][2]["value"], "41" + self.binding["registry_address"])
        asset_call = next(call for call in self.calls if call[2] is not None and
                          call[2].get("function_selector") == "assetBudgets(address)")
        self.assertEqual(asset_call[2]["parameter"],
                         self.binding["asset_address"].rjust(64, "0"))

    def test_v2_source_hashes_survive_read_only_assessment(self):
        self.binding["schema_version"] = "economic-basket-registry-binding-2"
        self.binding["authenticated_source_hash"] = "66" * 32
        self.binding["signed_assembly_hash"] = "77" * 32
        self.replies["attestationRequired(bytes32)"] = [1]
        self.replies["authenticatedSourceByBinding(bytes32)"] = ["66" * 32]
        self.replies["attestedEpochByBinding(bytes32)"] = [3]
        self.replies["attestorEpoch()"] = [3]
        observation = self.observe()
        assessment = assess_registry_observation(self.binding, observation)
        self.assertEqual(assessment["status"], "OBSERVED_MATCH")
        self.assertEqual(assessment["authenticated_source_hash"], "66" * 32)
        self.assertEqual(assessment["signed_assembly_hash"], "77" * 32)
        self.assertEqual(assessment["execution_authority"], "NONE")

    def test_vault_leg_binding_requires_current_attestor_epoch(self):
        self.binding["schema_version"] = "economic-vault-leg-registry-binding-1"
        self.binding["authenticated_source_hash"] = "66" * 32
        self.binding["signed_assembly_hash"] = "77" * 32
        self.replies["attestationRequired(bytes32)"] = [1]
        self.replies["authenticatedSourceByBinding(bytes32)"] = ["66" * 32]
        self.replies["attestedEpochByBinding(bytes32)"] = [3]
        self.replies["attestorEpoch()"] = [3]
        self.assertEqual(assess_registry_observation(
            self.binding, self.observe())["status"], "OBSERVED_MATCH")
        self.replies["attestorEpoch()"] = [4]
        assessment = assess_registry_observation(self.binding, self.observe())
        self.assertEqual(assessment["status"], "WITHHELD")
        self.assertIn("ATTESTED_EPOCH_INVALID", assessment["reason_codes"])
        self.assertEqual(assessment["execution_authority"], "NONE")

    def test_v2_onchain_attestation_requirement_and_source_must_match(self):
        self.binding["schema_version"] = "economic-basket-registry-binding-2"
        self.binding["authenticated_source_hash"] = "66" * 32
        self.binding["signed_assembly_hash"] = "77" * 32
        for selector, answer, reason in (
            ("attestationRequired(bytes32)", [0], "ATTESTATION_NOT_REQUIRED"),
            ("authenticatedSourceByBinding(bytes32)", ["77" * 32],
             "AUTHENTICATED_SOURCE_MISMATCH"),
            ("attestedEpochByBinding(bytes32)", [0], "ATTESTED_EPOCH_MISSING"),
            ("attestorEpoch()", [2], "ATTESTED_EPOCH_INVALID"),
        ):
            with self.subTest(selector=selector):
                self.replies["attestationRequired(bytes32)"] = [1]
                self.replies["authenticatedSourceByBinding(bytes32)"] = ["66" * 32]
                self.replies["attestedEpochByBinding(bytes32)"] = [3]
                self.replies["attestorEpoch()"] = [3]
                self.replies[selector] = answer
                self.assertIn(reason, assess_registry_observation(
                    self.binding, self.observe())["reason_codes"])

    def test_registry_mismatch_and_replay_are_withheld(self):
        for selector, index, replacement, reason in (
            ("baskets(bytes32)", 8, 1, "BASKET_CONSUMED"),
            ("baskets(bytes32)", 7, 1, "BASKET_REVOKED"),
            ("policies(bytes32)", 2, "12" * 20, "POLICY_TARGET"),
            ("bindingByCommitment(bytes32)", 0, "aa" * 32, "COMMITMENT_LINK"),
            ("activeBasket(bytes32)", 0, 0, "BASKET_INACTIVE"),
            ("paused()", 0, 1, "REGISTRY_PAUSED"),
            ("reservedBasketAmount(bytes32)", 0, 0, "BASKET_NOT_RESERVED"),
            ("consumedBasketAmount(bytes32)", 0, 1500000000, "POLICY_BUDGET_EXCEEDED"),
            ("basketBudgetRemaining(bytes32)", 0, 0, "POLICY_BUDGET_MISMATCH"),
            ("assetBudgets(address)", 1, 0, "ASSET_BASKET_NOT_RESERVED"),
            ("assetBudgets(address)", 2, 2000000000, "ASSET_BUDGET_EXCEEDED"),
            ("assetBudgets(address)", 3, 0, "ASSET_BUDGET_UNCONFIGURED"),
            ("assetBudgetRemaining(address)", 0, 0, "ASSET_BUDGET_MISMATCH"),
        ):
            with self.subTest(reason=reason):
                old = self.replies[selector][index]
                self.replies[selector][index] = replacement
                result = assess_registry_observation(self.binding, self.observe())
                self.assertEqual(result["status"], "WITHHELD")
                self.assertIn(reason, result["reason_codes"])
                self.replies[selector][index] = old

    def test_code_hash_mismatch_and_tampering_are_withheld(self):
        observation = self.observe()
        old_schema = copy.deepcopy(observation)
        old_schema["schema_version"] = "economic-tron-registry-observation-2"
        old_schema["observation_hash"] = digest({k: v for k, v in old_schema.items()
                                                  if k != "observation_hash"})
        with self.assertRaisesRegex(MachineError, "unsupported registry observation"):
            assess_registry_observation(self.binding, old_schema)
        changed = copy.deepcopy(observation)
        changed["runtime_sha256"] = "ff" * 32
        changed["observation_hash"] = digest({k: v for k, v in changed.items()
                                               if k != "observation_hash"})
        self.assertIn("CODE_HASH", assess_registry_observation(
            self.binding, changed)["reason_codes"])
        changed["runtime_sha256"] = observation["runtime_sha256"]
        with self.assertRaisesRegex(MachineError, "hash mismatch"):
            assess_registry_observation(self.binding, changed)

    def test_changed_or_stale_solid_block_fails_closed(self):
        reads = 0

        def unstable(view, path, payload):
            nonlocal reads
            result = self.node(view, path, payload)
            if path == "/walletsolidity/getnowblock":
                reads += 1
                if reads == 2:
                    result = copy.deepcopy(result)
                    result["blockID"] = f"{124:016x}" + "ab" * 24
                    result["block_header"]["raw_data"]["number"] = 124
            return result

        with self.assertRaisesRegex(MachineError, "view changed"):
            self.observe(transport=unstable)
        with self.assertRaisesRegex(MachineError, "stale"):
            read_registry_observation(self.binding, self.config,
                                      now_ms=self.block_ms + 120001, transport=self.node)

    def test_malformed_or_failed_constant_call_fails_closed(self):
        def failed(view, path, payload):
            result = self.node(view, path, payload)
            if path == "/walletsolidity/triggerconstantcontract":
                result = {**result, "transaction": {"ret": [{"ret": "REVERT"}]}}
            return result

        with self.assertRaisesRegex(MachineError, "VM failed"):
            self.observe(transport=failed)
        def missing_vm_result(view, path, payload):
            result = self.node(view, path, payload)
            if path == "/walletsolidity/triggerconstantcontract":
                result = {key: value for key, value in result.items() if key != "transaction"}
            return result

        with self.assertRaisesRegex(MachineError, "transaction result missing"):
            self.observe(transport=missing_vm_result)
        self.replies["paused()"][0] = 2
        with self.assertRaisesRegex(MachineError, "ABI boolean"):
            self.observe()
        self.replies["paused()"][0] = 0
        self.runtime = ""
        with self.assertRaisesRegex(MachineError, "bytecode missing"):
            self.observe()

    def test_unsafe_reader_config_rejected(self):
        for url in ("http://public.example.invalid", "https://u:p@node.invalid",
                    "https://node.invalid?key=secret", "https://node.invalid:bad",
                    "https://node.invalid/#fragment"):
            with self.subTest(url=url):
                self.config["solidity_url"] = url
                with self.assertRaises(MachineError):
                    self.observe()
        self.config["solidity_url"] = "https://solid.example.invalid"
        self.binding["amount_base_units"] = "-1"
        with self.assertRaisesRegex(MachineError, "amount"):
            self.observe()
        self.binding["amount_base_units"] = "9" * 10000
        with self.assertRaisesRegex(MachineError, "amount"):
            self.observe()

    def test_cli_refuses_node_read_when_binding_replay_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = {}
            for name, value in (("request", self.request), ("state", self.state),
                                ("policy", self.policy), ("commitment", self.commitment),
                                ("chain-context", self.context),
                                ("chain-binding", {**self.binding, "binding_hash": "aa" * 32}),
                                ("reader-config", self.config)):
                path = Path(directory) / (name + ".json")
                path.write_text(json.dumps(value), encoding="utf-8")
                paths[name] = path
            argv = []
            for name, path in paths.items():
                argv.extend(("--" + name, str(path)))
            for command, reader in (("tron-observe", "read_registry_observation"),
                                    ("tron-basket-observe", "read_tron_transaction")):
                with self.subTest(command=command), \
                        patch.object(sys, "argv", ["economic-machine", command, *argv,
                                                   "--txid", "aa" * 32]), \
                        patch("economic_machine.cli." + reader) as node_read, \
                        contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        main()
                    self.assertEqual(raised.exception.code, 2)
                    node_read.assert_not_called()

    def test_cli_observation_and_offline_reassessment_replay_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            args = []
            for name, value in (("request", self.request), ("state", self.state),
                                ("policy", self.policy), ("commitment", self.commitment),
                                ("chain-context", self.context),
                                ("chain-binding", self.binding)):
                path = Path(directory) / (name + ".json")
                path.write_text(json.dumps(value), encoding="utf-8")
                args.extend(("--" + name, str(path)))
            config_path = Path(directory) / "reader.json"
            config_path.write_text(json.dumps(self.config), encoding="utf-8")
            observation = self.observe()
            output = io.StringIO()
            with patch.object(sys, "argv", ["economic-machine", "tron-observe",
                                             *args, "--reader-config", str(config_path)]), \
                    patch("economic_machine.cli.read_registry_observation",
                          return_value=observation) as node_read, \
                    contextlib.redirect_stdout(output):
                main()
            node_read.assert_called_once()
            report = json.loads(output.getvalue())
            self.assertTrue(report["verified_replay"])
            self.assertEqual(report["assessment"]["status"], "OBSERVED_MATCH")
            report_path = Path(directory) / "observation-report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            output = io.StringIO()
            with patch.object(sys, "argv", ["economic-machine", "tron-assess",
                                             *args, "--observation", str(report_path)]), \
                    contextlib.redirect_stdout(output):
                main()
            offline = json.loads(output.getvalue())
            self.assertTrue(offline["verified_replay"])
            self.assertEqual(offline["assessment"], report["assessment"])


if __name__ == "__main__":
    unittest.main()
