import unittest

from economic_machine.capabilities import capability_hash, normalize_capability
from economic_machine.tron_actions import compile_action, verify_action
from economic_machine.values import MachineError


TOKEN = "41" + "11" * 20
MARKET = "41" + "22" * 20
WALLET = "41" + "33" * 20
HASH = "a" * 64
EXPIRY = "2026-09-28T13:00:00Z"


def capability(protocol="justlend", contract=MARKET, asset="USDT", token=TOKEN,
               action="SUPPLY"):
    return normalize_capability({"schema_version": "economic-product-capability-1",
        "chain": "TRON", "network": "tron-mainnet", "contract": contract,
        "protocol": protocol, "protocol_version": "v1", "action": action,
        "token": {"asset": asset, "address": token, "decimals": 6},
        "stages": {stage: {"status": "SUPPORTED", "evidence_hash": HASH}
                   for stage in ("read", "quote", "simulate", "execute", "reconcile")}})


def binding(operation, target, cap=None, status="VERIFIED"):
    cap = cap or capability()
    return {"schema_version": "tron-action-binding-1", "operation": operation,
        "network": "tron-mainnet", "target_address": target,
        "capability_hash": capability_hash(cap),
        "abi_evidence_hash": HASH if status == "VERIFIED" else None,
        "contract_code_hash": "b" * 64 if status == "VERIFIED" else None,
        "status": status,
        "source_url": "https://github.com/justlend/mcp-server-justlend"}


class TronActionTests(unittest.TestCase):
    def test_trc20_and_justlend_calls_have_fixed_abi_and_result_semantics(self):
        approve = compile_action(binding("TRC20_APPROVE", TOKEN),
            [MARKET, "1000000"], call_value_sun="0", fee_limit_sun="10000000",
            expires_at=EXPIRY)
        mint = compile_action(binding("JUSTLEND_SUPPLY", MARKET), ["1000000"],
            call_value_sun="0", fee_limit_sun="10000000", expires_at=EXPIRY)
        self.assertEqual((approve["function_selector"], len(approve["parameter_hex"]),
                          approve["result_semantics"]),
                         ("approve(address,uint256)", 128, "BOOL_TRUE"))
        self.assertEqual((mint["function_selector"], mint["result_semantics"]),
                         ("mint(uint256)", "UINT_ZERO"))
        self.assertTrue(verify_action(mint, binding("JUSTLEND_SUPPLY", MARKET),
                        ["1000000"], call_value_sun="0", fee_limit_sun="10000000",
                        expires_at=EXPIRY))

    def test_native_stake_is_not_encoded_as_smart_contract_call(self):
        cap = capability("tron-native", None, "TRX", None, "STAKE")
        action = compile_action(binding("TRON_STAKE", None, cap),
            ["1000000", "ENERGY"], call_value_sun="0", fee_limit_sun="1000000",
            expires_at=EXPIRY)
        self.assertEqual(action["transport"], "TRON_SYSTEM_CONTRACT")
        self.assertIsNone(action["parameter_hex"])
        self.assertEqual(action["native_parameters"][1]["value"], "ENERGY")

    def test_unverified_and_incomplete_vault_bindings_never_become_ready(self):
        vault_cap = capability("usdd", MARKET, "USDD", TOKEN, "MINT_USDD")
        action = compile_action(binding("USDD_VAULT_REPAY", MARKET, vault_cap),
            ["1", "0", "-100"], call_value_sun="0", fee_limit_sun="1000000",
            expires_at=EXPIRY)
        self.assertEqual(action["status"], "BLOCKED")
        self.assertIn("INCOMPLETE_VAULT_JOIN_EXIT_SEQUENCE", action["blockers"])

    def test_wrong_shape_and_nonpayable_value_are_rejected(self):
        with self.assertRaisesRegex(MachineError, "arguments"):
            compile_action(binding("JUSTLEND_SUPPLY", MARKET), [], call_value_sun="0",
                           fee_limit_sun="1", expires_at=EXPIRY)
        with self.assertRaisesRegex(MachineError, "nonpayable"):
            compile_action(binding("JUSTLEND_SUPPLY", MARKET), ["1"], call_value_sun="1",
                           fee_limit_sun="1", expires_at=EXPIRY)


if __name__ == "__main__":
    unittest.main()
