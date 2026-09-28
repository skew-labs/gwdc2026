"""Isolated EVM execution tests for the undeployed TRON vault source.

All balances, accounts, signatures and venue fills here are synthetic. TVM
execution, a real TRC20, a production signer and deployment remain untested.
"""

import hashlib
import json
import os
import subprocess
import unittest
from pathlib import Path

from economic_machine.vault_batch import (compute_vault_batch_digest,
                                          compute_vault_order_digest)


ROOT = Path(__file__).resolve().parents[1]
EVM_SOLC_SHA256 = "0479d44fdf9c501c25337fdc540419f1593b884a87b47f023da4f1c700fda782"


@unittest.skipUnless(os.environ.get("ECON_EVM_SOLC"), "isolated EVM compiler unavailable")
class CapitalVaultRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from eth_abi import decode, encode
        from eth_tester import EthereumTester
        from eth_utils import keccak
        cls.decode, cls.encode = staticmethod(decode), staticmethod(encode)
        cls.EthereumTester, cls.keccak = EthereumTester, staticmethod(keccak)
        compiler = Path(os.environ["ECON_EVM_SOLC"])
        if hashlib.sha256(compiler.read_bytes()).hexdigest() != EVM_SOLC_SHA256:
            raise AssertionError("EVM test compiler digest mismatch")
        paths = [ROOT / "contracts" / "EconomicPolicyRegistry.sol",
                 ROOT / "contracts" / "EconomicCapitalVault.sol",
                 ROOT / "tests" / "fixtures" / "MockVaultToken.sol",
                 ROOT / "tests" / "fixtures" / "MockVaultTarget.sol"]
        result = subprocess.run(
            [str(compiler), "--optimize", "--optimize-runs", "200",
             "--evm-version", "paris", "--combined-json", "abi,bin",
             *(str(path) for path in paths)],
            check=True, capture_output=True, text=True, timeout=60)
        artifacts = json.loads(result.stdout)["contracts"]
        cls.bytecodes = {name.split(":")[-1]: artifact["bin"]
                         for name, artifact in artifacts.items()}

    def setUp(self):
        self.tester = self.EthereumTester()
        accounts = self.tester.get_accounts()
        self.owner, self.relayer, self.guardian, self.stranger = accounts[:4]
        self.attestors = accounts[5:7]
        self.registry = self.deploy("EconomicPolicyRegistry", ["address"], [self.guardian])
        self.vault = self.deploy("EconomicCapitalVault", ["address", "address"],
                                 [self.registry, self.guardian])
        self.input = self.deploy("MockVaultToken", [], [])
        self.output = self.deploy("MockVaultToken", [], [])
        self.target = self.deploy("MockVaultTarget", ["address", "address", "address"],
                                  [self.registry, self.vault, self.output])
        self.assertSuccess(self.send(self.registry, "configureAssetBudget",
                                     ["address", "uint256"], [self.input, 1000]))
        for token in (self.input, self.output):
            self.assertSuccess(self.send(self.vault, "pinAsset", ["address", "bool"],
                                         [token, True]))
        self.assertSuccess(self.send(self.vault, "pinTarget", ["address", "bool"],
                                     [self.target, True]))
        self.assertSuccess(self.send(self.input, "mint", ["address", "uint256"],
                                     [self.owner, 1000]))
        self.assertSuccess(self.send(self.output, "mint", ["address", "uint256"],
                                     [self.target, 1000]))
        self.assertSuccess(self.send(self.input, "approve", ["address", "uint256"],
                                     [self.vault, 500]))
        self.assertSuccess(self.send(self.vault, "deposit", ["address", "uint256"],
                                     [self.input, 500]))
        for signer in self.attestors:
            self.assertSuccess(self.send(self.registry, "setAttestor",
                                         ["address", "bool"], [signer, True]))
        self.now = self.tester.get_block_by_number("latest")["timestamp"]

    def _data(self, name, types, args):
        selector = self.keccak(text=name + "(" + ",".join(types) + ")")[:4]
        return "0x" + selector.hex() + self.encode(types, args).hex()

    def deploy(self, name, types, args):
        data = "0x" + self.bytecodes[name] + self.encode(types, args).hex()
        tx = self.tester.send_transaction({"from": self.owner, "gas": 8000000,
                                            "data": data})
        receipt = self.tester.get_transaction_receipt(tx)
        self.assertEqual(receipt["status"], 1, name)
        return receipt["contract_address"]

    def send(self, address, name, types, args, *, sender=None):
        tx = self.tester.send_transaction({"from": sender or self.owner,
                                            "to": address, "gas": 3000000,
                                            "data": self._data(name, types, args)})
        return self.tester.get_transaction_receipt(tx)

    def call(self, address, name, types, args, outputs):
        result = self.tester.call({"from": self.owner, "to": address,
                                   "gas": 3000000,
                                   "data": self._data(name, types, args)})
        return self.decode(outputs, bytes.fromhex(result.removeprefix("0x")))

    def assertSuccess(self, receipt):
        self.assertEqual(receipt["status"], 1)

    def sign(self, signer, message_hash):
        index = self.tester.get_accounts().index(signer)
        raw = self.tester.backend.account_keys[index].sign_msg_hash(message_hash).to_bytes()
        return raw[:-1] + bytes([raw[-1] + 27])

    def balance(self, token, holder):
        return self.call(token, "balanceOf", ["address"], [holder], ["uint256"])[0]

    def basket(self, *, second=False, parent_hash=None):
        policy_id = bytes.fromhex(("77" if second else "11") * 32)
        policy_hash = bytes.fromhex(("88" if second else "22") * 32)
        state_root = bytes.fromhex("33" * 32)
        basket_hash = parent_hash or bytes.fromhex("44" * 32)
        commitment = bytes.fromhex(("99" if second else "55") * 32)
        source_hash = bytes.fromhex("66" * 32)
        amount = 100
        expiry = self.now + 3600
        valid_until = self.now + 600
        policy_types = ["bytes32", "bytes32", "address", "address", "uint256", "uint64"]
        self.assertSuccess(self.send(
            self.registry, "registerAttestedPolicy", policy_types,
            [policy_id, policy_hash, self.input, self.target, 300, expiry]))
        basket_types = ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "uint64"]
        basket_args = [policy_id, state_root, basket_hash, commitment, amount, valid_until]
        binding = self.call(self.registry, "computeBasketBinding", basket_types,
                            basket_args, ["bytes32"])[0]
        attestation = self.call(self.registry, "attestationDigest",
                                ["bytes32", "bytes32"],
                                [binding, source_hash], ["bytes32"])[0]
        signatures = [self.sign(signer, attestation)
                      for signer in sorted(self.attestors, key=lambda item: int(item, 16))]
        self.assertSuccess(self.send(
            self.registry, "commitBasketAttested", basket_types + ["bytes32", "bytes[]"],
            basket_args + [source_hash, signatures]))
        return binding, amount, valid_until

    def authorization(self, binding, amount, valid_until, *, output_amount=90,
                      min_output=80, route="fill", signer=None, deadline=None):
        if route == "fill":
            data = self._data("fill", ["bytes32", "uint256"],
                              [binding, output_amount])
        elif route == "fail":
            data = self._data("failAfterConsume", ["bytes32"], [binding])
        else:
            data = self._data("skipConsumption", ["uint256"], [output_amount])
        route_hash = hashlib.sha256(bytes.fromhex(data.removeprefix("0x"))).digest()
        deadline = deadline or valid_until - 60
        digest = self.call(
            self.vault, "executionDigest",
            ["bytes32", "address", "uint256", "address", "address",
             "uint256", "bytes32", "uint64"],
            [binding, self.input, amount, self.target, self.output,
             min_output, route_hash, deadline], ["bytes32"])[0]
        expected = hashlib.sha256(self.encode(
            ["bytes32", "address", "uint256", "address", "bytes32", "address",
             "uint256", "address", "address", "uint256", "bytes32", "uint64"],
            [b"ECONOMIC_VAULT_EXECUTE_V1".ljust(32, b"\0"), self.vault,
             self.tester.backend.chain.chain_id, self.registry, binding,
             self.input, amount, self.target, self.output,
             min_output, route_hash, deadline])).digest()
        self.assertEqual(digest, expected)
        signature = self.sign(signer or self.owner, digest)
        args = [binding, self.output, min_output, deadline,
                bytes.fromhex(data.removeprefix("0x")), signature]
        types = ["bytes32", "address", "uint256", "uint64", "bytes", "bytes"]
        return types, args

    def batch_authorization(self, entries, parent_hash, *, second_output=90,
                            signer=None):
        deadline = min(item[2] for item in entries) - 60
        orders = []
        digests = []
        for index, (binding, amount, valid_until) in enumerate(sorted(entries)):
            output_amount = second_output if index else 90
            route_data = bytes.fromhex(self._data(
                "fill", ["bytes32", "uint256"],
                [binding, output_amount]).removeprefix("0x"))
            orders.append((binding, self.output, 80, valid_until - 60, route_data))
            route_hash = hashlib.sha256(route_data).digest()
            digests.append(self.call(
                self.vault, "executionDigest",
                ["bytes32", "address", "uint256", "address", "address",
                 "uint256", "bytes32", "uint64"],
                [binding, self.input, amount, self.target, self.output,
                 80, route_hash, valid_until - 60], ["bytes32"])[0])
            self.assertEqual(digests[-1].hex(), compute_vault_order_digest(
                vault_address=self.vault[2:].lower(),
                registry_address=self.registry[2:].lower(),
                chain_id=self.tester.backend.chain.chain_id,
                binding_hash=binding.hex(),
                input_asset_address=self.input[2:].lower(),
                amount_base_units=amount,
                target_address=self.target[2:].lower(),
                output_asset_address=self.output[2:].lower(),
                min_output_base_units=80, route_hash=route_hash.hex(),
                deadline_epoch_seconds=valid_until - 60))
        orders_hash = hashlib.sha256(self.encode(["bytes32[]"], [digests])).digest()
        batch_digest = self.call(
            self.vault, "batchDigest", ["bytes32", "bytes32[]", "uint64"],
            [parent_hash, digests, deadline], ["bytes32"])[0]
        expected = hashlib.sha256(self.encode(
            ["bytes32", "address", "uint256", "address", "bytes32", "bytes32", "uint64"],
            [b"ECONOMIC_VAULT_BATCH_V1".ljust(32, b"\0"), self.vault,
             self.tester.backend.chain.chain_id, self.registry,
             parent_hash, orders_hash, deadline])).digest()
        self.assertEqual(batch_digest, expected)
        offline_orders_hash, offline_batch_digest = compute_vault_batch_digest(
            vault_address=self.vault[2:].lower(),
            registry_address=self.registry[2:].lower(),
            chain_id=self.tester.backend.chain.chain_id,
            parent_basket_hash=parent_hash.hex(),
            order_digests=[item.hex() for item in digests],
            batch_deadline_epoch_seconds=deadline)
        self.assertEqual(orders_hash.hex(), offline_orders_hash)
        self.assertEqual(batch_digest.hex(), offline_batch_digest)
        types = ["bytes32", "(bytes32,address,uint256,uint64,bytes)[]", "uint64", "bytes"]
        return types, [parent_hash, orders, deadline,
                       self.sign(signer or self.owner, batch_digest)]

    def test_owner_signed_exact_route_moves_only_committed_capital_once(self):
        binding, amount, valid_until = self.basket()
        types, args = self.authorization(binding, amount, valid_until)
        self.assertEqual(self.balance(self.input, self.vault), 500)
        result = self.send(self.vault, "executeBasket", types, args,
                           sender=self.relayer)
        self.assertSuccess(result)
        self.assertEqual(self.balance(self.input, self.vault), 400)
        self.assertEqual(self.balance(self.input, self.target), 100)
        self.assertEqual(self.balance(self.output, self.vault), 90)
        self.assertEqual(self.call(self.vault, "executed", ["bytes32"],
                                   [binding], ["bool"]), (True,))
        self.assertEqual(self.call(self.registry, "activeBasket", ["bytes32"],
                                   [binding], ["bool"]), (False,))
        self.assertEqual(self.send(self.vault, "executeBasket", types, args,
                                   sender=self.relayer)["status"], 0)

    def test_wrong_signer_or_changed_route_cannot_spend(self):
        binding, amount, valid_until = self.basket()
        types, args = self.authorization(binding, amount, valid_until,
                                         signer=self.stranger)
        self.assertEqual(self.send(self.vault, "executeBasket", types, args,
                                   sender=self.relayer)["status"], 0)
        types, args = self.authorization(binding, amount, valid_until)
        changed = list(args)
        changed[2] = 1
        self.assertEqual(self.send(self.vault, "executeBasket", types, changed,
                                   sender=self.relayer)["status"], 0)
        changed = list(args)
        changed[4] = bytes.fromhex(self._data(
            "fill", ["bytes32", "uint256"], [binding, 99]).removeprefix("0x"))
        self.assertEqual(self.send(self.vault, "executeBasket", types, changed,
                                   sender=self.relayer)["status"], 0)
        from eth_keys.constants import SECPK1_N
        changed = list(args)
        signature = args[5]
        high_s = SECPK1_N - int.from_bytes(signature[32:64], "big")
        changed[5] = (signature[:32] + high_s.to_bytes(32, "big")
                      + bytes([55 - signature[64]]))
        self.assertEqual(self.send(self.vault, "executeBasket", types, changed,
                                   sender=self.relayer)["status"], 0)
        self.assertEqual(self.balance(self.input, self.vault), 500)
        self.assertEqual(self.call(self.registry, "activeBasket", ["bytes32"],
                                   [binding], ["bool"]), (True,))

    def test_target_failure_short_output_and_missing_consumption_roll_back(self):
        binding, amount, valid_until = self.basket()
        for route, output in (("fail", 90), ("fill", 79), ("skip", 90)):
            types, args = self.authorization(binding, amount, valid_until,
                                             output_amount=output, route=route)
            self.assertEqual(self.send(self.vault, "executeBasket", types, args,
                                       sender=self.relayer)["status"], 0)
            self.assertEqual(self.balance(self.input, self.vault), 500)
            self.assertEqual(self.balance(self.output, self.vault), 0)
            self.assertEqual(self.call(self.vault, "executed", ["bytes32"],
                                       [binding], ["bool"]), (False,))
            self.assertEqual(self.call(self.registry, "activeBasket", ["bytes32"],
                                       [binding], ["bool"]), (True,))

    def test_guardian_pause_code_pin_and_attestor_rotation_block_spend(self):
        binding, amount, valid_until = self.basket()
        types, args = self.authorization(binding, amount, valid_until)
        self.assertSuccess(self.send(self.vault, "pause", [], [], sender=self.guardian))
        self.assertEqual(self.send(self.vault, "executeBasket", types, args,
                                   sender=self.relayer)["status"], 0)
        self.assertEqual(self.send(self.vault, "unpause", [], [],
                                   sender=self.guardian)["status"], 0)
        self.assertSuccess(self.send(self.vault, "unpause", [], []))
        self.assertSuccess(self.send(self.vault, "pinTarget", ["address", "bool"],
                                     [self.target, False]))
        self.assertEqual(self.send(self.vault, "executeBasket", types, args,
                                   sender=self.relayer)["status"], 0)
        self.assertSuccess(self.send(self.vault, "pinTarget", ["address", "bool"],
                                     [self.target, True]))
        self.assertSuccess(self.send(self.registry, "setAttestor",
                                     ["address", "bool"], [self.attestors[0], False]))
        self.assertEqual(self.send(self.vault, "executeBasket", types, args,
                                   sender=self.relayer)["status"], 0)
        self.assertEqual(self.balance(self.input, self.vault), 500)

    def test_only_owner_funds_or_withdraws_and_receives_withdrawal(self):
        self.assertEqual(self.send(self.vault, "deposit", ["address", "uint256"],
                                   [self.input, 1], sender=self.stranger)["status"], 0)
        self.assertEqual(self.send(self.vault, "withdraw", ["address", "uint256"],
                                   [self.input, 50], sender=self.stranger)["status"], 0)
        self.assertSuccess(self.send(self.vault, "withdraw", ["address", "uint256"],
                                     [self.input, 50]))
        self.assertEqual(self.balance(self.input, self.vault), 450)
        self.assertEqual(self.balance(self.input, self.owner), 550)

    def test_legacy_registry_basket_cannot_open_vault(self):
        policy_id = bytes.fromhex("aa" * 32)
        policy_hash = bytes.fromhex("bb" * 32)
        state_root = bytes.fromhex("cc" * 32)
        basket_hash = bytes.fromhex("dd" * 32)
        commitment = bytes.fromhex("ee" * 32)
        valid_until = self.now + 600
        self.assertSuccess(self.send(
            self.registry, "registerPolicy",
            ["bytes32", "bytes32", "address", "address", "uint256", "uint64"],
            [policy_id, policy_hash, self.input, self.target, 300, self.now + 3600]))
        basket_types = ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "uint64"]
        basket_args = [policy_id, state_root, basket_hash, commitment, 100, valid_until]
        binding = self.call(self.registry, "computeBasketBinding", basket_types,
                            basket_args, ["bytes32"])[0]
        self.assertSuccess(self.send(self.registry, "commitBasket",
                                     basket_types, basket_args))
        types, args = self.authorization(binding, 100, valid_until)
        self.assertEqual(self.send(self.vault, "executeBasket", types, args,
                                   sender=self.relayer)["status"], 0)
        self.assertEqual(self.balance(self.input, self.vault), 500)

    def test_output_asset_revocation_empty_vault_and_expiry_fail_closed(self):
        binding, amount, valid_until = self.basket()
        types, args = self.authorization(binding, amount, valid_until)
        self.assertSuccess(self.send(self.vault, "pinAsset", ["address", "bool"],
                                     [self.output, False]))
        self.assertEqual(self.send(self.vault, "executeBasket", types, args,
                                   sender=self.relayer)["status"], 0)
        self.assertSuccess(self.send(self.vault, "pinAsset", ["address", "bool"],
                                     [self.output, True]))
        self.assertSuccess(self.send(self.vault, "withdraw", ["address", "uint256"],
                                     [self.input, 500]))
        self.assertEqual(self.send(self.vault, "executeBasket", types, args,
                                   sender=self.relayer)["status"], 0)
        self.assertSuccess(self.send(self.input, "approve", ["address", "uint256"],
                                     [self.vault, 500]))
        self.assertSuccess(self.send(self.vault, "deposit", ["address", "uint256"],
                                     [self.input, 500]))
        self.tester.time_travel(valid_until + 1)
        self.assertEqual(self.send(self.vault, "executeBasket", types, args,
                                   sender=self.relayer)["status"], 0)
        self.assertEqual(self.balance(self.input, self.vault), 500)

    def test_two_leg_batch_is_atomic_and_replay_protected(self):
        parent = bytes.fromhex("44" * 32)
        entries = [self.basket(parent_hash=parent),
                   self.basket(second=True, parent_hash=parent)]
        types, failing = self.batch_authorization(entries, parent, second_output=79)
        self.assertEqual(self.send(self.vault, "executeBatch", types, failing,
                                   sender=self.relayer)["status"], 0)
        self.assertEqual(self.balance(self.input, self.vault), 500)
        self.assertEqual(self.balance(self.output, self.vault), 0)
        for binding, _, _ in entries:
            self.assertEqual(self.call(self.registry, "activeBasket", ["bytes32"],
                                       [binding], ["bool"]), (True,))
        types, success = self.batch_authorization(entries, parent)
        self.assertSuccess(self.send(self.vault, "executeBatch", types, success,
                                     sender=self.relayer))
        self.assertEqual(self.balance(self.input, self.vault), 300)
        self.assertEqual(self.balance(self.output, self.vault), 180)
        for binding, _, _ in entries:
            self.assertEqual(self.call(self.vault, "executed", ["bytes32"],
                                       [binding], ["bool"]), (True,))
            self.assertEqual(self.call(self.registry, "activeBasket", ["bytes32"],
                                       [binding], ["bool"]), (False,))
        self.assertEqual(self.send(self.vault, "executeBatch", types, success,
                                   sender=self.relayer)["status"], 0)

    def test_batch_order_parent_and_signature_are_bound(self):
        parent = bytes.fromhex("44" * 32)
        entries = [self.basket(parent_hash=parent),
                   self.basket(second=True, parent_hash=parent)]
        types, args = self.batch_authorization(entries, parent,
                                                signer=self.stranger)
        self.assertEqual(self.send(self.vault, "executeBatch", types, args,
                                   sender=self.relayer)["status"], 0)
        types, args = self.batch_authorization(entries, parent)
        for changed in (
                [bytes.fromhex("ab" * 32), args[1], args[2], args[3]],
                [args[0], args[1][::-1], args[2], args[3]],
                [args[0], [args[1][0], args[1][0]], args[2], args[3]],
                [args[0], args[1][:1], args[2], args[3]]):
            self.assertEqual(self.send(self.vault, "executeBatch", types, changed,
                                       sender=self.relayer)["status"], 0)
        self.assertEqual(self.balance(self.input, self.vault), 500)


if __name__ == "__main__":
    unittest.main()
