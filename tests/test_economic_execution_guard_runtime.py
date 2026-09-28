"""Isolated EVM runtime tests for undeployed TVM-targeted guard contracts."""

import hashlib
import json
import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVM_SOLC_SHA256 = "0479d44fdf9c501c25337fdc540419f1593b884a87b47f023da4f1c700fda782"


@unittest.skipUnless(os.environ.get("ECON_EVM_SOLC"), "isolated EVM compiler unavailable")
class ExecutionGuardRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from eth_abi import decode, encode
        from eth_tester import EthereumTester
        from eth_utils import keccak
        cls.decode, cls.encode = staticmethod(decode), staticmethod(encode)
        cls.EthereumTester, cls.keccak = EthereumTester, staticmethod(keccak)
        compiler = Path(os.environ["ECON_EVM_SOLC"])
        if hashlib.sha256(compiler.read_bytes()).hexdigest() != EVM_SOLC_SHA256:
            raise AssertionError("EVM test compiler digest mismatch")
        paths = [ROOT / "contracts" / "JustLendV1Adapter.sol",
                 ROOT / "contracts" / "EconomicExecutionGuardV1.sol",
                 ROOT / "tests" / "fixtures" / "MockJustLendGuard.sol"]
        result = subprocess.run([str(compiler), "--optimize", "--optimize-runs", "200",
            "--evm-version", "paris", "--combined-json", "abi,bin",
            *(str(path) for path in paths)], check=True, capture_output=True, text=True,
            timeout=60)
        artifacts = json.loads(result.stdout)["contracts"]
        cls.bytecodes = {name.split(":")[-1]: artifact["bin"]
                         for name, artifact in artifacts.items() if artifact["bin"]}

    def setUp(self):
        self.tester = self.EthereumTester()
        self.owner, self.stranger = self.tester.get_accounts()[:2]
        self.token = self.deploy("MockGuardToken", [], [])
        self.market = self.deploy("MockJustLendMarket", ["address"], [self.token])
        self.adapter = self.deploy("JustLendV1Adapter",
                                   ["address", "address", "address"],
                                   [self.token, self.market, self.market])
        self.guard = self.deploy("EconomicExecutionGuardV1",
            ["address", "address", "uint256", "uint256", "uint256"],
            [self.owner, self.adapter, 1000, 1000, 1500])
        self.assertSuccess(self.send(self.adapter, "bindGuard", ["address"], [self.guard]))
        self.assertSuccess(self.send(self.token, "faucet", ["address", "uint256"],
                                     [self.owner, 2000]))
        self.assertSuccess(self.send(self.token, "faucet", ["address", "uint256"],
                                     [self.market, 2000]))
        self.assertSuccess(self.send(self.token, "approve", ["address", "uint256"],
                                     [self.guard, 2000]))
        self.now = self.tester.get_block_by_number("latest")["timestamp"]

    def deploy(self, name, types, args):
        data = "0x" + self.bytecodes[name] + self.encode(types, args).hex()
        tx = self.tester.send_transaction({"from": self.owner, "gas": 8_000_000, "data": data})
        receipt = self.tester.get_transaction_receipt(tx)
        self.assertEqual(receipt["status"], 1)
        return receipt["contract_address"]

    def data(self, name, types, args):
        return "0x" + self.keccak(text=name + "(" + ",".join(types) + ")")[:4].hex() + \
            self.encode(types, args).hex()

    def send(self, contract, name, types, args, *, sender=None):
        tx = self.tester.send_transaction({"from": sender or self.owner, "to": contract,
            "gas": 5_000_000, "data": self.data(name, types, args)})
        return self.tester.get_transaction_receipt(tx)

    def call(self, contract, name, types, args, outputs):
        result = self.tester.call({"from": self.owner, "to": contract, "gas": 5_000_000,
                                   "data": self.data(name, types, args)})
        return self.decode(outputs, bytes.fromhex(result.removeprefix("0x")))

    def assertSuccess(self, receipt):
        self.assertEqual(receipt["status"], 1)

    def balance(self, token, account):
        return self.call(token, "balanceOf", ["address"], [account], ["uint256"])[0]

    def allowance(self, token, owner, spender):
        return self.call(token, "allowance", ["address", "address"],
                         [owner, spender], ["uint256"])[0]

    def execute(self, action="executeSupply", *, amount=100, minimum=90, nonce=0,
                step=b"\x33" * 32, sender=None, deadline=None, max_fee=1_000_000):
        types = ["bytes32", "bytes32", "bytes32", "uint256", "uint256", "uint256",
                 "uint64", "uint64"]
        args = [b"\x11" * 32, b"\x22" * 32, step, amount, minimum, max_fee,
                nonce, deadline if deadline is not None else self.now + 600]
        return self.send(self.guard, action, types, args, sender=sender)

    def test_supply_moves_exact_amount_to_position_and_leaves_no_guard_or_adapter_balance(self):
        self.assertSuccess(self.execute())
        self.assertEqual(self.balance(self.token, self.owner), 1900)
        self.assertEqual(self.balance(self.market, self.owner), 100)
        for holder in (self.guard, self.adapter):
            self.assertEqual(self.balance(self.token, holder), 0)
            self.assertEqual(self.balance(self.market, holder), 0)
        self.assertEqual(self.allowance(self.token, self.guard, self.adapter), 0)
        self.assertEqual(self.allowance(self.token, self.adapter, self.market), 0)
        self.assertEqual(self.call(self.guard, "nextNonce", [], [], ["uint64"]), (1,))

    def test_only_owner_exact_nonce_step_and_limits_can_execute(self):
        self.assertEqual(self.execute(sender=self.stranger)["status"], 0)
        self.assertEqual(self.execute(nonce=1)["status"], 0)
        self.assertEqual(self.execute(amount=1001)["status"], 0)
        self.assertEqual(self.execute(deadline=self.now)["status"], 0)
        self.assertEqual(self.execute(max_fee=0)["status"], 0)
        self.assertEqual(self.execute(step=b"\x00" * 32)["status"], 0)
        self.assertEqual(self.send(self.adapter, "bindGuard", ["address"],
                                   [self.guard])["status"], 0)
        self.assertSuccess(self.execute(amount=800))
        self.assertEqual(self.execute(amount=701, nonce=1, step=b"\x44" * 32)["status"], 0)
        self.assertEqual(self.execute(nonce=1)["status"], 0)
        self.assertEqual(self.balance(self.market, self.owner), 800)

    def test_protocol_error_false_token_fee_token_and_short_position_roll_back(self):
        self.assertSuccess(self.send(self.market, "setModes", ["uint256", "bool", "bool"],
                                     [7, False, False]))
        self.assertEqual(self.execute()["status"], 0)
        self.assertSuccess(self.send(self.market, "setModes", ["uint256", "bool", "bool"],
                                     [0, True, False]))
        self.assertEqual(self.execute()["status"], 0)
        self.assertSuccess(self.send(self.market, "setModes", ["uint256", "bool", "bool"],
                                     [0, False, True]))
        self.assertEqual(self.execute(minimum=100)["status"], 0)
        self.assertSuccess(self.send(self.market, "setModes", ["uint256", "bool", "bool"],
                                     [0, False, False]))
        self.assertSuccess(self.send(self.token, "setModes", ["bool", "bool"], [True, False]))
        self.assertEqual(self.execute()["status"], 0)
        self.assertSuccess(self.send(self.token, "setModes", ["bool", "bool"], [False, True]))
        self.assertEqual(self.execute()["status"], 0)
        self.assertEqual(self.balance(self.token, self.owner), 2000)
        self.assertEqual(self.balance(self.market, self.owner), 0)

    def test_preexisting_adapter_residuals_are_rejected(self):
        self.assertSuccess(self.send(self.token, "faucet", ["address", "uint256"],
                                     [self.adapter, 1]))
        self.assertEqual(self.execute()["status"], 0)
        self.assertEqual(self.balance(self.token, self.owner), 2000)

    def test_preexisting_guard_residuals_are_rejected(self):
        self.assertSuccess(self.send(self.token, "faucet", ["address", "uint256"],
                                     [self.guard, 1]))
        self.assertEqual(self.execute()["status"], 0)
        self.assertEqual(self.balance(self.token, self.owner), 2000)
        self.assertEqual(self.balance(self.token, self.guard), 1)

    def test_owner_contract_reentry_during_token_transfer_is_rejected(self):
        token = self.deploy("MockGuardToken", [], [])
        market = self.deploy("MockJustLendMarket", ["address"], [token])
        adapter = self.deploy("JustLendV1Adapter", ["address", "address", "address"],
                              [token, market, market])
        owner_contract = self.deploy("ReentrantGuardOwner", [], [])
        guard = self.deploy("EconomicExecutionGuardV1",
            ["address", "address", "uint256", "uint256", "uint256"],
            [owner_contract, adapter, 1000, 1000, 1500])
        self.assertSuccess(self.send(adapter, "bindGuard", ["address"], [guard]))
        self.assertSuccess(self.send(token, "faucet", ["address", "uint256"],
                                     [owner_contract, 200]))
        self.assertSuccess(self.send(owner_contract, "approveToken",
                                     ["address", "address", "uint256"],
                                     [token, guard, 200]))
        self.assertSuccess(self.send(token, "setReentryMode", ["bool", "address"],
                                     [True, owner_contract]))
        types = ["address", "bytes32", "bytes32", "bytes32", "uint256", "uint256",
                 "uint256", "uint64", "uint64"]
        args = [guard, b"\x11" * 32, b"\x22" * 32, b"\x66" * 32,
                100, 100, 1_000_000, 0, self.now + 600]
        self.assertSuccess(self.send(owner_contract, "startSupply", types, args))
        self.assertEqual(self.call(owner_contract, "reentryAttempted", [], [], ["bool"]),
                         (True,))
        self.assertEqual(self.call(owner_contract, "reentrySucceeded", [], [], ["bool"]),
                         (False,))
        self.assertEqual(self.balance(market, owner_contract), 100)
        self.assertEqual(self.call(guard, "nextNonce", [], [], ["uint64"]), (1,))

    def test_redeem_returns_underlying_to_fixed_owner_and_resets_allowances(self):
        self.assertSuccess(self.execute(amount=200, minimum=200))
        self.assertSuccess(self.send(self.market, "approve", ["address", "uint256"],
                                     [self.guard, 200]))
        self.assertSuccess(self.execute("executeRedeem", amount=150, minimum=150,
                                        nonce=1, step=b"\x55" * 32))
        self.assertEqual(self.balance(self.market, self.owner), 50)
        self.assertEqual(self.balance(self.token, self.owner), 1950)
        self.assertEqual(self.allowance(self.market, self.guard, self.adapter), 0)
        self.assertEqual(self.allowance(self.market, self.adapter, self.market), 0)
        self.assertEqual(self.balance(self.token, self.guard), 0)
        self.assertEqual(self.balance(self.market, self.guard), 0)


if __name__ == "__main__":
    unittest.main()
