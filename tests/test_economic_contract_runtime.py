"""EVM-source smoke tests for registry semantics; TVM runtime still untested.

The same Solidity source is compiled with pinned Ethereum solc because TRON's
compiler emits TVM-only opcodes. These are ephemeral simulator accounts only.
"""

import hashlib
import json
import os
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from economic_machine.basket import commit_basket
from economic_machine.chain_binding import compute_attestation_digest, prepare_chain_binding
from economic_machine.portfolio import select_portfolio
from economic_machine.tron_consumption_read import BASKET_CONSUMED_TOPIC


ROOT = Path(__file__).resolve().parents[1]
EVM_SOLC_SHA256 = "0479d44fdf9c501c25337fdc540419f1593b884a87b47f023da4f1c700fda782"


@unittest.skipUnless(os.environ.get("ECON_EVM_SOLC"), "isolated EVM compiler unavailable")
class BasketRegistryRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not os.environ.get("ECON_EVM_SOLC"):
            return
        from eth_abi import decode, encode
        from eth_tester import EthereumTester
        from eth_utils import keccak
        cls.decode, cls.encode = staticmethod(decode), staticmethod(encode)
        cls.EthereumTester, cls.keccak = EthereumTester, staticmethod(keccak)
        compiler = Path(os.environ["ECON_EVM_SOLC"])
        if hashlib.sha256(compiler.read_bytes()).hexdigest() != EVM_SOLC_SHA256:
            raise AssertionError("EVM test compiler digest mismatch")
        result = subprocess.run(
            [str(compiler), "--optimize", "--optimize-runs", "200", "--evm-version", "paris",
             "--combined-json", "abi,bin", str(ROOT / "contracts" / "EconomicPolicyRegistry.sol"),
             str(ROOT / "tests" / "fixtures" / "MockBasketTarget.sol")],
            check=True, capture_output=True, text=True, timeout=60)
        artifacts = json.loads(result.stdout)["contracts"]
        cls.bytecode = next(value["bin"] for name, value in artifacts.items()
                            if name.endswith(":EconomicPolicyRegistry"))
        cls.mock_bytecode = next(value["bin"] for name, value in artifacts.items()
                                 if name.endswith(":MockBasketTarget"))

    def setUp(self):
        self.tester = self.EthereumTester()
        self.owner, self.guardian, self.asset, self.target, self.stranger = self.tester.get_accounts()[:5]
        constructor = self.encode(["address"], [self.guardian])
        tx = self.tester.send_transaction({"from": self.owner, "gas": 8000000,
                                            "data": "0x" + self.bytecode + constructor.hex()})
        receipt = self.tester.get_transaction_receipt(tx)
        self.assertEqual(receipt["status"], 1)
        self.registry = receipt["contract_address"]
        self.assertEqual(self.send("configureAssetBudget", ["address", "uint256"],
                                   [self.asset, 10000000000])["status"], 1)
        self.now = self.tester.get_block_by_number("latest")["timestamp"]

    def _data(self, name, types, args):
        signature = name + "(" + ",".join(types) + ")"
        return "0x" + self.keccak(text=signature)[:4].hex() + self.encode(types, args).hex()

    def send(self, name, types, args, *, sender=None):
        tx = self.tester.send_transaction({"from": sender or self.owner, "to": self.registry,
                                            "gas": 2000000, "data": self._data(name, types, args)})
        return self.tester.get_transaction_receipt(tx)

    def call(self, name, types, args, outputs):
        result = self.tester.call({"from": self.owner, "to": self.registry,
                                   "gas": 2000000, "data": self._data(name, types, args)})
        return self.decode(outputs, bytes.fromhex(result.removeprefix("0x")))

    def register(self, policy_id, policy_hash, *, expiry=None):
        return self.send("registerPolicy",
                         ["bytes32", "bytes32", "address", "address", "uint256", "uint64"],
                         [policy_id, policy_hash, self.asset, self.target,
                          1000000000, expiry or self.now + 3600])

    def _basket(self):
        def read(name):
            return json.loads((ROOT / "cases" / name).read_text(encoding="utf-8"))
        request = read("economic_portfolio_candidates_demo.json")
        state = read("economic_basket_state_demo.json")
        policy = read("economic_basket_policy_demo.json")
        context = read("economic_basket_registry_context_demo.json")
        at = datetime.fromtimestamp(self.now, tz=timezone.utc)
        stamp = lambda value: value.isoformat()
        request["as_of"] = stamp(at)
        for product in request["products"]:
            product["observed_at"] = stamp(at)
        state["as_of"] = stamp(at)
        state["facts"]["portfolio.nav"]["observed_at"] = stamp(at)
        policy["effective_at"] = stamp(at - timedelta(seconds=1))
        policy["expires_at"] = stamp(at + timedelta(hours=1))
        valid_until = stamp(at + timedelta(minutes=3))
        commitment = commit_basket(request, select_portfolio(request), state,
                                   policy, valid_until=valid_until)
        context.update({"chain_id": self.tester.backend.chain.chain_id,
                        "registry_address": self.registry[2:].lower(),
                        "asset_address": self.asset[2:].lower(),
                        "target_address": self.target[2:].lower()})
        binding = prepare_chain_binding(commitment, request, state, policy, context)
        return commitment, binding

    def _commit(self, commitment, binding):
        policy_id = bytes.fromhex(commitment["policy_id"])
        self.assertEqual(self.register(policy_id, bytes.fromhex(commitment["policy_hash"]))["status"], 1)
        types = ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "uint64"]
        args = [policy_id, bytes.fromhex(commitment["state_root"]),
                bytes.fromhex(commitment["basket_hash"]),
                bytes.fromhex(commitment["commitment_hash"]),
                int(binding["amount_base_units"]), binding["valid_until_epoch_seconds"]]
        self.assertEqual(self.send("commitBasket", types, args)["status"], 1)
        return bytes.fromhex(binding["binding_hash"])

    def test_hash_parity_owner_gate_pause_and_revocation(self):
        commitment, binding = self._basket()
        policy_id = bytes.fromhex(commitment["policy_id"])
        self.assertEqual(self.register(policy_id, bytes.fromhex(commitment["policy_hash"]))["status"], 1)
        types = ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "uint64"]
        args = [policy_id, bytes.fromhex(commitment["state_root"]),
                bytes.fromhex(commitment["basket_hash"]),
                bytes.fromhex(commitment["commitment_hash"]),
                int(binding["amount_base_units"]), binding["valid_until_epoch_seconds"]]
        calculated = self.call("computeBasketBinding", types, args, ["bytes32"])[0]
        self.assertEqual(calculated.hex(), binding["binding_hash"])
        self.assertEqual(self.send("commitBasket", types, args, sender=self.stranger)["status"], 0)
        self.assertEqual(self.send("commitBasket", types, args)["status"], 1)
        key = bytes.fromhex(binding["binding_hash"])
        self.assertEqual(self.call("activeBasket", ["bytes32"], [key], ["bool"]), (True,))
        self.assertEqual(self.call("bindingByCommitment", ["bytes32"],
                                   [bytes.fromhex(commitment["commitment_hash"])],
                                   ["bytes32"]), (key,))
        altered = list(args)
        altered[1] = bytes.fromhex("44" * 32)
        self.assertEqual(self.send("commitBasket", types, altered)["status"], 0)
        self.assertEqual(self.send("commitBasket", types, args)["status"], 0)
        self.assertEqual(self.send("pause", [], [], sender=self.guardian)["status"], 1)
        self.assertEqual(self.call("activeBasket", ["bytes32"], [key], ["bool"]), (False,))
        self.assertEqual(self.send("unpause", [], [], sender=self.guardian)["status"], 0)
        self.assertEqual(self.send("unpause", [], [])["status"], 1)
        self.assertEqual(self.call("activeBasket", ["bytes32"], [key], ["bool"]), (True,))
        self.assertEqual(self.send("revokeBasket", ["bytes32"], [key], sender=self.guardian)["status"], 1)
        self.assertEqual(self.call("activeBasket", ["bytes32"], [key], ["bool"]), (False,))

    def test_policy_revocation_disables_basket(self):
        commitment, binding = self._basket()
        policy_id = bytes.fromhex(commitment["policy_id"])
        self.assertEqual(self.register(policy_id, bytes.fromhex(commitment["policy_hash"]))["status"], 1)
        types = ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "uint64"]
        args = [policy_id, bytes.fromhex(commitment["state_root"]),
                bytes.fromhex(commitment["basket_hash"]),
                bytes.fromhex(commitment["commitment_hash"]),
                int(binding["amount_base_units"]), binding["valid_until_epoch_seconds"]]
        self.assertEqual(self.send("commitBasket", types, args)["status"], 1)
        key = bytes.fromhex(binding["binding_hash"])
        self.assertEqual(self.send("revokePolicy", ["bytes32"], [policy_id])["status"], 1)
        self.assertEqual(self.call("activeBasket", ["bytes32"], [key], ["bool"]), (False,))
        self.assertEqual(self.send("commitBasket", types, args)["status"], 0)

    def test_expiry_disables_unrevoked_basket(self):
        commitment, binding = self._basket()
        policy_id = bytes.fromhex(commitment["policy_id"])
        self.assertEqual(self.register(policy_id, bytes.fromhex(commitment["policy_hash"]))["status"], 1)
        types = ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "uint64"]
        args = [policy_id, bytes.fromhex(commitment["state_root"]),
                bytes.fromhex(commitment["basket_hash"]),
                bytes.fromhex(commitment["commitment_hash"]),
                int(binding["amount_base_units"]), binding["valid_until_epoch_seconds"]]
        self.assertEqual(self.send("commitBasket", types, args)["status"], 1)
        key = bytes.fromhex(binding["binding_hash"])
        self.assertEqual(self.call("activeBasket", ["bytes32"], [key], ["bool"]), (True,))
        self.tester.time_travel(binding["valid_until_epoch_seconds"] + 1)
        self.assertEqual(self.call("activeBasket", ["bytes32"], [key], ["bool"]), (False,))

    def test_amount_hash_and_policy_expiry_fail_closed(self):
        commitment, binding = self._basket()
        policy_id = bytes.fromhex(commitment["policy_id"])
        self.assertEqual(self.register(policy_id, bytes.fromhex(commitment["policy_hash"]))["status"], 1)
        types = ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "uint64"]
        args = [policy_id, bytes.fromhex(commitment["state_root"]),
                bytes.fromhex(commitment["basket_hash"]),
                bytes.fromhex(commitment["commitment_hash"]),
                int(binding["amount_base_units"]), binding["valid_until_epoch_seconds"]]
        for index, invalid in ((1, bytes(32)), (4, 1000000001),
                               (5, self.now + 3601), (5, self.now)):
            changed = list(args)
            changed[index] = invalid
            self.assertEqual(self.send("commitBasket", types, changed)["status"], 0)
        self.assertEqual(self.send("pause", [], [], sender=self.guardian)["status"], 1)
        self.assertEqual(self.send("commitBasket", types, args)["status"], 0)
        self.assertEqual(self.send("unpause", [], [])["status"], 1)
        self.assertEqual(self.send("commitBasket", types, args)["status"], 1)

    def test_only_policy_target_consumes_once(self):
        commitment, binding = self._basket()
        key = self._commit(commitment, binding)
        self.assertEqual(self.send("consumeBasket", ["bytes32"], [key],
                                   sender=self.stranger)["status"], 0)
        self.assertEqual(self.send("consumeBasket", ["bytes32"], [key])["status"], 0)
        result = self.send("consumeBasket", ["bytes32"], [key], sender=self.target)
        self.assertEqual(result["status"], 1)
        self.assertEqual(len(result["logs"]), 1)
        event = result["logs"][0]
        self.assertEqual(event["topics"][0].lower(), "0x" + self.keccak(
            text="BasketConsumed(bytes32,bytes32,address,address,uint256)").hex())
        self.assertEqual(BASKET_CONSUMED_TOPIC, event["topics"][0].removeprefix("0x").lower())
        self.assertEqual(event["topics"][1].lower(), "0x" + binding["binding_hash"])
        self.assertEqual(self.call("activeBasket", ["bytes32"], [key], ["bool"]), (False,))
        record = self.call("baskets", ["bytes32"], [key],
                           ["bytes32", "bytes32", "bytes32", "bytes32",
                            "uint256", "uint64", "bool", "bool", "bool"])
        self.assertEqual(record[-1], True)
        self.assertEqual(self.send("consumeBasket", ["bytes32"], [key],
                                   sender=self.target)["status"], 0)
        self.assertEqual(self.send("revokeBasket", ["bytes32"], [key],
                                   sender=self.guardian)["status"], 0)

    def test_target_revert_rolls_back_consumption(self):
        tx = self.tester.send_transaction({
            "from": self.owner, "gas": 2000000,
            "data": "0x" + self.mock_bytecode
            + self.encode(["address"], [self.registry]).hex()})
        deployed = self.tester.get_transaction_receipt(tx)
        self.assertEqual(deployed["status"], 1)
        self.target = deployed["contract_address"]
        commitment, binding = self._basket()
        key = self._commit(commitment, binding)
        policy_id = bytes.fromhex(commitment["policy_id"])
        self.assertEqual(self.call("reservedBasketAmount", ["bytes32"],
                                   [policy_id], ["uint256"]), (1000000000,))
        failed = self.tester.send_transaction({
            "from": self.stranger, "to": self.target, "gas": 2000000,
            "data": self._data("revertAfterConsume", ["bytes32"], [key])})
        self.assertEqual(self.tester.get_transaction_receipt(failed)["status"], 0)
        self.assertEqual(self.call("activeBasket", ["bytes32"], [key], ["bool"]), (True,))
        self.assertEqual(self.call("reservedBasketAmount", ["bytes32"],
                                   [policy_id], ["uint256"]), (1000000000,))
        self.assertEqual(self.call("consumedBasketAmount", ["bytes32"],
                                   [policy_id], ["uint256"]), (0,))
        self.assertEqual(self.call("assetBudgets", ["address"], [self.asset],
                                   ["uint256", "uint256", "uint256", "bool"]),
                         (10000000000, 1000000000, 0, True))
        succeeded = self.tester.send_transaction({
            "from": self.stranger, "to": self.target, "gas": 2000000,
            "data": self._data("consume", ["bytes32"], [key])})
        self.assertEqual(self.tester.get_transaction_receipt(succeeded)["status"], 1)
        self.assertEqual(self.call("activeBasket", ["bytes32"], [key], ["bool"]), (False,))
        self.assertEqual(self.call("reservedBasketAmount", ["bytes32"],
                                   [policy_id], ["uint256"]), (0,))
        self.assertEqual(self.call("consumedBasketAmount", ["bytes32"],
                                   [policy_id], ["uint256"]), (1000000000,))
        self.assertEqual(self.call("assetBudgets", ["address"], [self.asset],
                                   ["uint256", "uint256", "uint256", "bool"]),
                         (10000000000, 0, 1000000000, True))

    def test_asset_budget_shared_across_policies_and_lowering_is_bounded(self):
        self.assertEqual(self.send("configureAssetBudget", ["address", "uint256"],
                                   [self.asset, 1500000000], sender=self.stranger)["status"], 0)
        self.assertEqual(self.send("configureAssetBudget", ["address", "uint256"],
                                   [self.asset, 1500000000])["status"], 1)
        policy_a, policy_b = bytes.fromhex("a1" * 32), bytes.fromhex("b1" * 32)
        self.assertEqual(self.register(policy_a, bytes.fromhex("a2" * 32))["status"], 1)
        self.assertEqual(self.register(policy_b, bytes.fromhex("b2" * 32))["status"], 1)
        types = ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "uint64"]

        def basket(policy_id, seed, amount):
            args = [policy_id, bytes([seed]) * 32, bytes([seed + 1]) * 32,
                    bytes([seed + 2]) * 32, amount, self.now + 600]
            key = self.call("computeBasketBinding", types, args, ["bytes32"])[0]
            return args, key

        def asset_budget():
            return self.call("assetBudgets", ["address"], [self.asset],
                             ["uint256", "uint256", "uint256", "bool"])

        first, first_key = basket(policy_a, 1, 900000000)
        second_large, _ = basket(policy_b, 11, 700000000)
        second, second_key = basket(policy_b, 11, 600000000)
        self.assertEqual(self.send("commitBasket", types, first)["status"], 1)
        self.assertEqual(self.send("commitBasket", types, second_large)["status"], 0)
        self.assertEqual(self.send("commitBasket", types, second)["status"], 1)
        self.assertEqual(asset_budget(), (1500000000, 1500000000, 0, True))
        self.assertEqual(self.send("configureAssetBudget", ["address", "uint256"],
                                   [self.asset, 1499999999])["status"], 0)
        self.assertEqual(self.send("revokeBasket", ["bytes32"], [first_key],
                                   sender=self.guardian)["status"], 1)
        self.assertEqual(asset_budget(), (1500000000, 600000000, 0, True))
        self.assertEqual(self.send("consumeBasket", ["bytes32"], [second_key],
                                   sender=self.target)["status"], 1)
        self.assertEqual(asset_budget(), (1500000000, 0, 600000000, True))
        replacement, _ = basket(policy_a, 21, 900000000)
        self.assertEqual(self.send("commitBasket", types, replacement)["status"], 1)
        self.assertEqual(asset_budget(), (1500000000, 900000000, 600000000, True))
        self.assertEqual(self.send("configureAssetBudget", ["address", "uint256"],
                                   [self.asset, 1400000000])["status"], 0)
        self.assertEqual(self.send("configureAssetBudget", ["address", "uint256"],
                                   [self.asset, 1600000000])["status"], 1)
        self.assertEqual(self.call("assetBudgetRemaining", ["address"],
                                   [self.asset], ["uint256"]), (100000000,))

    def test_attested_policy_blocks_unsigned_downgrade_and_requires_distinct_keys(self):
        commitment, binding = self._basket()
        policy_id = bytes.fromhex(commitment["policy_id"])
        policy_hash = bytes.fromhex(commitment["policy_hash"])
        policy_types = ["bytes32", "bytes32", "address", "address", "uint256", "uint64"]
        policy_args = [policy_id, policy_hash, self.asset, self.target,
                       1000000000, self.now + 3600]
        self.assertEqual(self.send("registerAttestedPolicy", policy_types, policy_args)["status"], 0)
        signers = self.tester.get_accounts()[5:7]
        for signer in signers:
            self.assertEqual(self.send("setAttestor", ["address", "bool"],
                                       [signer, True])["status"], 1)
        self.assertEqual(self.send("setAttestor", ["address", "bool"],
                                   [self.stranger, True], sender=self.stranger)["status"], 0)
        self.assertEqual(self.call("activeAttestorCount", [], [], ["uint8"]), (2,))
        self.assertEqual(self.send("registerAttestedPolicy", policy_types, policy_args,
                                   sender=self.stranger)["status"], 0)
        self.assertEqual(self.send("registerAttestedPolicy", policy_types, policy_args)["status"], 1)
        self.assertEqual(self.call("attestationRequired", ["bytes32"],
                                   [policy_id], ["bool"]), (True,))
        basket_types = ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "uint64"]
        basket_args = [policy_id, bytes.fromhex(commitment["state_root"]),
                       bytes.fromhex(commitment["basket_hash"]),
                       bytes.fromhex(commitment["commitment_hash"]),
                       int(binding["amount_base_units"]), binding["valid_until_epoch_seconds"]]
        self.assertEqual(self.send("commitBasket", basket_types, basket_args)["status"], 0)
        source_hash = bytes.fromhex("66" * 32)
        digest = self.call("attestationDigest", ["bytes32", "bytes32"],
                           [bytes.fromhex(binding["binding_hash"]), source_hash],
                           ["bytes32"])[0]
        prepared_binding = {**binding,
                            "schema_version": "economic-basket-registry-binding-2",
                            "authenticated_source_hash": source_hash.hex()}
        self.assertEqual(compute_attestation_digest(prepared_binding, 3), digest.hex())
        child_binding = {**prepared_binding,
                         "schema_version": "economic-vault-leg-registry-binding-1"}
        self.assertEqual(compute_attestation_digest(child_binding, 3), digest.hex())
        signatures = []
        for signer in sorted(signers, key=lambda item: int(item, 16)):
            index = self.tester.get_accounts().index(signer)
            raw = self.tester.backend.account_keys[index].sign_msg_hash(digest).to_bytes()
            signatures.append(raw[:-1] + bytes([raw[-1] + 27]))
        strict_types = basket_types + ["bytes32", "bytes[]"]
        strict_args = basket_args + [source_hash, signatures]
        self.assertEqual(self.send("commitBasketAttested", strict_types,
                                   basket_args + [bytes(32), signatures])["status"], 0)
        self.assertEqual(self.send("commitBasketAttested", strict_types,
                                   basket_args + [source_hash, signatures[:1]])["status"], 0)
        self.assertEqual(self.send("commitBasketAttested", strict_types,
                                   basket_args + [source_hash, [signatures[0]] * 2])["status"], 0)
        self.assertEqual(self.send("commitBasketAttested", strict_types,
                                   basket_args + [source_hash, signatures[::-1]])["status"], 0)
        from eth_keys.constants import SECPK1_N
        original_s = int.from_bytes(signatures[0][32:64], "big")
        high_s = SECPK1_N - original_s
        malleable = (signatures[0][:32] + high_s.to_bytes(32, "big")
                     + bytes([55 - signatures[0][-1]]))
        self.assertEqual(self.send("commitBasketAttested", strict_types,
                                   basket_args + [source_hash, [malleable, signatures[1]]])["status"], 0)
        self.assertEqual(self.send("commitBasketAttested", strict_types,
                                   basket_args + [source_hash, [signatures[0][:-1] + b"\x00",
                                                               signatures[1]]])["status"], 0)
        self.assertEqual(self.send("commitBasketAttested", strict_types,
                                   basket_args + [bytes.fromhex("77" * 32), signatures])["status"], 0)
        self.assertEqual(self.send("setGuardian", ["address"], [signers[0]])["status"], 0)
        self.assertEqual(self.send("commitBasketAttested", strict_types, strict_args,
                                   sender=self.stranger)["status"], 0)
        registered = self.send("commitBasketAttested", strict_types, strict_args)
        self.assertEqual(registered["status"], 1)
        self.assertEqual(len(registered["logs"]), 2)
        attested_topic = "0x" + self.keccak(text="BasketAttested(bytes32,bytes32,uint64)").hex()
        self.assertEqual(registered["logs"][1]["topics"][0].lower(), attested_topic)
        key = bytes.fromhex(binding["binding_hash"])
        self.assertEqual(self.call("authenticatedSourceByBinding", ["bytes32"],
                                   [key], ["bytes32"]), (source_hash,))
        self.assertEqual(self.call("attestedEpochByBinding", ["bytes32"],
                                   [key], ["uint64"]), (3,))
        self.assertEqual(self.send("commitBasketAttested", strict_types, strict_args)["status"], 0)
        self.assertEqual(self.send("commitBasket", basket_types, basket_args)["status"], 0)
        self.assertEqual(self.send("consumeBasket", ["bytes32"], [key],
                                   sender=self.target)["status"], 1)

    def test_attestor_rotation_revokes_prepared_signatures(self):
        commitment, binding = self._basket()
        signers = self.tester.get_accounts()[5:8]
        for signer in signers[:2]:
            self.assertEqual(self.send("setAttestor", ["address", "bool"],
                                       [signer, True])["status"], 1)
        policy_id = bytes.fromhex(commitment["policy_id"])
        self.assertEqual(self.send("registerAttestedPolicy",
                                   ["bytes32", "bytes32", "address", "address", "uint256", "uint64"],
                                   [policy_id, bytes.fromhex(commitment["policy_hash"]),
                                    self.asset, self.target, 1000000000,
                                    self.now + 3600])["status"], 1)
        basket_types = ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "uint64"]
        basket_args = [policy_id, bytes.fromhex(commitment["state_root"]),
                       bytes.fromhex(commitment["basket_hash"]),
                       bytes.fromhex(commitment["commitment_hash"]),
                       int(binding["amount_base_units"]), binding["valid_until_epoch_seconds"]]
        source_hash = bytes.fromhex("66" * 32)
        key = bytes.fromhex(binding["binding_hash"])
        old_digest = self.call("attestationDigest", ["bytes32", "bytes32"],
                               [key, source_hash], ["bytes32"])[0]
        signatures = []
        for signer in sorted(signers[:2], key=lambda item: int(item, 16)):
            index = self.tester.get_accounts().index(signer)
            raw = self.tester.backend.account_keys[index].sign_msg_hash(old_digest).to_bytes()
            signatures.append(raw[:-1] + bytes([raw[-1] + 27]))
        self.assertEqual(self.send("setAttestor", ["address", "bool"],
                                   [signers[2], True])["status"], 1)
        strict_types = basket_types + ["bytes32", "bytes[]"]
        self.assertEqual(self.send("commitBasketAttested", strict_types,
                                   basket_args + [source_hash, signatures])["status"], 0)
        self.assertNotEqual(self.call("attestationDigest", ["bytes32", "bytes32"],
                                      [key, source_hash], ["bytes32"])[0], old_digest)
        self.assertEqual(self.call("authenticatedSourceByBinding", ["bytes32"],
                                   [key], ["bytes32"]), (bytes(32),))

    def test_unconfigured_asset_cannot_register_policy(self):
        unknown_asset = self.tester.get_accounts()[5]
        policy_id, policy_hash = bytes.fromhex("c1" * 32), bytes.fromhex("c2" * 32)
        types = ["bytes32", "bytes32", "address", "address", "uint256", "uint64"]
        args = [policy_id, policy_hash, unknown_asset, self.target,
                500000000, self.now + 3600]
        self.assertEqual(self.send("registerPolicy", types, args)["status"], 0)
        self.assertEqual(self.send("configureAssetBudget", ["address", "uint256"],
                                   [unknown_asset, 400000000])["status"], 1)
        self.assertEqual(self.send("registerPolicy", types, args)["status"], 0)
        self.assertEqual(self.send("configureAssetBudget", ["address", "uint256"],
                                   [unknown_asset, 500000000])["status"], 1)
        self.assertEqual(self.send("registerPolicy", types, args)["status"], 1)
        basket_types = ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "uint64"]
        basket = [policy_id, bytes.fromhex("c3" * 32), bytes.fromhex("c4" * 32),
                  bytes.fromhex("c5" * 32), 500000000, self.now + 600]
        self.assertEqual(self.send("commitBasket", basket_types, basket)["status"], 1)
        self.assertEqual(self.call("assetBudgets", ["address"], [unknown_asset],
                                   ["uint256", "uint256", "uint256", "bool"]),
                         (500000000, 500000000, 0, True))
        self.assertEqual(self.call("assetBudgets", ["address"], [self.asset],
                                   ["uint256", "uint256", "uint256", "bool"]),
                         (10000000000, 0, 0, True))

    def test_unbudgeted_legacy_intent_path_is_removed(self):
        policy_id, policy_hash = bytes.fromhex("d1" * 32), bytes.fromhex("d2" * 32)
        self.assertEqual(self.register(policy_id, policy_hash)["status"], 1)
        receipt = self.send("commitIntent", ["bytes32", "bytes32", "uint256", "uint64"],
                            [policy_id, bytes.fromhex("d3" * 32),
                             100000000, self.now + 600])
        self.assertEqual(receipt["status"], 0)
        self.assertEqual(self.call("assetBudgets", ["address"], [self.asset],
                                   ["uint256", "uint256", "uint256", "bool"]),
                         (10000000000, 0, 0, True))

    def test_policy_budget_caps_concurrent_and_cumulative_baskets(self):
        policy_id, policy_hash = bytes.fromhex("11" * 32), bytes.fromhex("22" * 32)
        self.assertEqual(self.register(policy_id, policy_hash)["status"], 1)
        types = ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "uint64"]

        def basket(seed, amount):
            args = [policy_id, bytes([seed]) * 32, bytes([seed + 10]) * 32,
                    bytes([seed + 20]) * 32, amount, self.now + 600]
            key = self.call("computeBasketBinding", types, args, ["bytes32"])[0]
            return args, key

        def budget():
            return (self.call("reservedBasketAmount", ["bytes32"], [policy_id],
                              ["uint256"])[0],
                    self.call("consumedBasketAmount", ["bytes32"], [policy_id],
                              ["uint256"])[0],
                    self.call("basketBudgetRemaining", ["bytes32"], [policy_id],
                              ["uint256"])[0])

        first, first_key = basket(1, 600000000)
        self.assertEqual(self.send("commitBasket", types, first)["status"], 1)
        self.assertEqual(budget(), (600000000, 0, 400000000))
        too_large, _ = basket(2, 500000000)
        self.assertEqual(self.send("commitBasket", types, too_large)["status"], 0)
        second, second_key = basket(2, 400000000)
        self.assertEqual(self.send("commitBasket", types, second)["status"], 1)
        self.assertEqual(budget(), (1000000000, 0, 0))
        self.assertEqual(self.send("revokeBasket", ["bytes32"], [first_key],
                                   sender=self.guardian)["status"], 1)
        self.assertEqual(budget(), (400000000, 0, 600000000))
        third, third_key = basket(3, 500000000)
        self.assertEqual(self.send("commitBasket", types, third)["status"], 1)
        self.assertEqual(self.send("consumeBasket", ["bytes32"], [second_key],
                                   sender=self.target)["status"], 1)
        self.assertEqual(budget(), (500000000, 400000000, 100000000))
        fourth, fourth_key = basket(4, 200000000)
        self.assertEqual(self.send("commitBasket", types, fourth)["status"], 0)
        self.assertEqual(self.send("revokeBasket", ["bytes32"], [third_key])
                         ["status"], 1)
        self.assertEqual(budget(), (0, 400000000, 600000000))
        fourth[4] = 600000000
        fourth_key = self.call("computeBasketBinding", types, fourth, ["bytes32"])[0]
        self.assertEqual(self.send("commitBasket", types, fourth)["status"], 1)
        self.assertEqual(self.send("consumeBasket", ["bytes32"], [fourth_key],
                                   sender=self.target)["status"], 1)
        self.assertEqual(budget(), (0, 1000000000, 0))
        fifth, _ = basket(5, 1)
        self.assertEqual(self.send("commitBasket", types, fifth)["status"], 0)

    def test_expired_reservation_can_be_released_without_replay(self):
        policy_id, policy_hash = bytes.fromhex("31" * 32), bytes.fromhex("32" * 32)
        self.assertEqual(self.register(policy_id, policy_hash)["status"], 1)
        types = ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "uint64"]
        args = [policy_id, bytes.fromhex("33" * 32), bytes.fromhex("34" * 32),
                bytes.fromhex("35" * 32), 900000000, self.now + 30]
        key = self.call("computeBasketBinding", types, args, ["bytes32"])[0]
        self.assertEqual(self.send("commitBasket", types, args)["status"], 1)
        self.assertEqual(self.send("releaseExpiredBasket", ["bytes32"], [key],
                                   sender=self.stranger)["status"], 0)
        self.tester.time_travel(self.now + 31)
        released = self.send("releaseExpiredBasket", ["bytes32"], [key],
                             sender=self.stranger)
        self.assertEqual(released["status"], 1)
        self.assertEqual(released["logs"][0]["topics"][0].lower(), "0x" + self.keccak(
            text="BasketExpiredReleased(bytes32,bytes32,uint256)").hex())
        self.assertEqual(self.call("reservedBasketAmount", ["bytes32"],
                                   [policy_id], ["uint256"]), (0,))
        self.assertEqual(self.call("assetBudgets", ["address"], [self.asset],
                                   ["uint256", "uint256", "uint256", "bool"]),
                         (10000000000, 0, 0, True))
        self.assertEqual(self.send("releaseExpiredBasket", ["bytes32"], [key])
                         ["status"], 0)
        changed = list(args)
        changed[1] = bytes.fromhex("36" * 32)
        changed[5] = self.now + 600
        self.assertEqual(self.send("commitBasket", types, changed)["status"], 0)
        changed[3] = bytes.fromhex("37" * 32)
        changed[4] = 1000000000
        self.assertEqual(self.send("commitBasket", types, changed)["status"], 1)
        self.assertEqual(self.call("basketBudgetRemaining", ["bytes32"],
                                   [policy_id], ["uint256"]), (0,))


if __name__ == "__main__":
    unittest.main()
