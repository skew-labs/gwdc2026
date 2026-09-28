"""Solidified TRON body, receipt, block and basket event boundaries."""

import copy
import hashlib
import unittest

from economic_machine.tron_consumption_read import (
    BASKET_CONSUMED_TOPIC, CONFIG_VERSION, assess_basket_consumption,
    read_tron_transaction,
)
from economic_machine.values import MachineError


class TronConsumptionReadTests(unittest.TestCase):
    def setUp(self):
        self.raw_data_hex = "01020304"
        self.txid = hashlib.sha256(bytes.fromhex(self.raw_data_hex)).hexdigest()
        self.registry = "11" * 20
        self.target = "22" * 20
        self.asset = "33" * 20
        self.policy_id = "44" * 32
        self.binding_hash = "55" * 32
        self.block_time = 1790563404000
        self.binding = {
            "schema_version": "economic-basket-registry-binding-1",
            "registry_address": self.registry,
            "target_address": self.target,
            "asset_address": self.asset,
            "policy_id": self.policy_id,
            "binding_hash": self.binding_hash,
            "amount_base_units": "1000000000",
            "valid_until_epoch_seconds": self.block_time // 1000 + 300,
        }
        self.anchor = self.block(10, self.block_time - 100000)
        self.inclusion = self.block(500, self.block_time)
        self.inclusion["transactions"] = [{"txID": self.txid,
                                           "raw_data_hex": self.raw_data_hex}]
        self.tip = self.block(600, self.block_time + 60000)
        self.config = {
            "schema_version": CONFIG_VERSION,
            "solidity_url": "https://node.example.invalid",
            "api_key_env": None,
            "max_block_age_ms": 120000,
            "network_anchor_height": 10,
            "network_anchor_block_id": self.anchor["blockID"],
        }
        self.body = {
            "txID": self.txid,
            "raw_data_hex": self.raw_data_hex,
            "raw_data": {"contract": [{"type": "TriggerSmartContract",
                                       "parameter": {"value": {
                                           "contract_address": "41" + self.target}}}]},
            "ret": [{"contractRet": "SUCCESS"}],
        }
        self.event = {
            "address": self.registry,
            "topics": [BASKET_CONSUMED_TOPIC, self.binding_hash, self.policy_id,
                       self.target.rjust(64, "0")],
            "data": (self.asset.rjust(64, "0") + f"{1000000000:064x}"),
        }
        self.info = {
            "id": self.txid, "blockNumber": 500,
            "blockTimeStamp": self.block_time,
            "fee": 1000000,
            "receipt": {"result": "SUCCESS", "energy_usage_total": 13045,
                        "energy_fee": 800000, "net_fee": 200000, "net_usage": 345},
            "log": [self.event],
        }
        self.calls = []

    @staticmethod
    def block(number, timestamp):
        return {"blockID": f"{number:016x}" + "ab" * 24,
                "block_header": {"raw_data": {"number": number,
                                               "timestamp": timestamp}}}

    def node(self, view, path, payload):
        self.calls.append((view, path, payload))
        if path == "/walletsolidity/getnowblock":
            return copy.deepcopy(self.tip)
        if path == "/walletsolidity/getblockbynum":
            return copy.deepcopy(self.anchor if payload["num"] == 10 else self.inclusion)
        if path == "/walletsolidity/gettransactionbyid":
            return copy.deepcopy(self.body)
        if path == "/walletsolidity/gettransactioninfobyid":
            return copy.deepcopy(self.info)
        raise AssertionError(path)

    def observe(self, **kwargs):
        return read_tron_transaction(self.txid, self.config,
                                     now_ms=self.tip["block_header"]["raw_data"]["timestamp"] + 1000,
                                     transport=kwargs.get("transport", self.node))

    def test_exact_event_is_observed_without_settlement_authority(self):
        observation = self.observe()
        assessment = assess_basket_consumption(self.binding, observation)
        self.assertEqual(assessment["status"], "CONSUMPTION_OBSERVED")
        self.assertEqual(assessment["settlement_status"], "NOT_VERIFIED")
        self.assertEqual(assessment["execution_authority"], "NONE")
        self.assertEqual(observation["status"], "SOLID_EXECUTED")
        self.assertEqual(observation["details"]["resource_receipt"], {
            "total_fee_sun": "1000000", "energy_fee_sun": "800000",
            "bandwidth_fee_sun": "200000", "energy_usage_total": "13045",
            "bandwidth_usage": "345"})
        self.assertEqual(len(self.calls), 6)
        self.assertEqual({call[1] for call in self.calls}, {
            "/walletsolidity/getnowblock", "/walletsolidity/getblockbynum",
            "/walletsolidity/gettransactionbyid",
            "/walletsolidity/gettransactioninfobyid"})

    def test_v2_source_hashes_survive_consumption_assessment(self):
        self.binding["schema_version"] = "economic-basket-registry-binding-2"
        self.binding["authenticated_source_hash"] = "66" * 32
        self.binding["signed_assembly_hash"] = "77" * 32
        assessment = assess_basket_consumption(self.binding, self.observe())
        self.assertEqual(assessment["status"], "CONSUMPTION_OBSERVED")
        self.assertEqual(assessment["authenticated_source_hash"], "66" * 32)
        self.assertEqual(assessment["signed_assembly_hash"], "77" * 32)
        self.assertEqual(assessment["settlement_status"], "NOT_VERIFIED")

    def test_execution_or_event_mismatch_is_withheld(self):
        scenarios = (
            ("wrong amount", lambda: self.event.__setitem__(
                "data", self.asset.rjust(64, "0") + f"{900000000:064x}"),
             "BASKET_EVENT_DATA_MISMATCH"),
            ("wrong policy", lambda: self.event["topics"].__setitem__(2, "66" * 32),
             "BASKET_EVENT_TOPICS_MISMATCH"),
            ("wrong registry", lambda: self.event.__setitem__("address", "77" * 20),
             "BASKET_EVENT_NOT_UNIQUE"),
            ("wrong target", lambda: self.body["raw_data"]["contract"][0]["parameter"]
             ["value"].__setitem__("contract_address", "41" + "88" * 20),
             "CALL_TARGET_MISMATCH"),
            ("failed receipt", lambda: self.info["receipt"].__setitem__("result", "REVERT"),
             "EXECUTION_FAILED"),
        )
        original_body, original_info = copy.deepcopy(self.body), copy.deepcopy(self.info)
        for label, mutate, reason in scenarios:
            with self.subTest(label=label):
                mutate()
                result = assess_basket_consumption(self.binding, self.observe())
                self.assertEqual(result["status"], "WITHHELD")
                self.assertIn(reason, result["reason_codes"])
                self.body, self.info = copy.deepcopy(original_body), copy.deepcopy(original_info)
                self.event = self.info["log"][0]
        self.info["log"].append(copy.deepcopy(self.event))
        self.assertIn("BASKET_EVENT_NOT_UNIQUE", assess_basket_consumption(
            self.binding, self.observe())["reason_codes"])

    def test_missing_is_pending_not_failure_and_receipt_only_is_invalid(self):
        self.body, self.info = {}, {}
        result = self.observe()
        self.assertEqual(result["status"], "NOT_OBSERVED")
        self.assertEqual(assess_basket_consumption(self.binding, result)["status"], "WITHHELD")
        self.body = {"txID": self.txid, "raw_data_hex": self.raw_data_hex}
        result = self.observe()
        self.assertEqual(result["status"], "SOLID_BODY_RECEIPT_MISSING")
        self.body, self.info = {}, {"id": self.txid}
        with self.assertRaisesRegex(MachineError, "receipt without"):
            self.observe()

    def test_anchor_block_inclusion_and_timestamp_must_agree(self):
        self.config["network_anchor_block_id"] = "ff" * 32
        with self.assertRaisesRegex(MachineError, "anchor height mismatch"):
            self.observe()
        self.config["network_anchor_block_id"] = self.anchor["blockID"]
        self.inclusion["transactions"] = [{"txID": "ee" * 32}]
        with self.assertRaisesRegex(MachineError, "absent from solidified block"):
            self.observe()
        self.inclusion["transactions"] = [{"txID": self.txid,
                                           "raw_data_hex": self.raw_data_hex}]
        self.info["blockTimeStamp"] += 1000
        with self.assertRaisesRegex(MachineError, "receipt block mismatch"):
            self.observe()

    def test_transaction_id_must_hash_body_and_block_raw_bytes(self):
        self.body["raw_data_hex"] = "01020305"
        with self.assertRaisesRegex(MachineError, "ID does not hash raw bytes"):
            self.observe()
        self.body["raw_data_hex"] = self.raw_data_hex
        self.inclusion["transactions"][0]["raw_data_hex"] = "01020305"
        with self.assertRaisesRegex(MachineError, "ID does not hash raw bytes"):
            self.observe()

    def test_malformed_log_and_modified_observation_fail_closed(self):
        self.event["topics"][0] = "bad"
        with self.assertRaisesRegex(MachineError, "log topic"):
            self.observe()
        self.event["topics"][0] = BASKET_CONSUMED_TOPIC
        observation = self.observe()
        observation["status"] = "NOT_OBSERVED"
        with self.assertRaisesRegex(MachineError, "hash mismatch"):
            assess_basket_consumption(self.binding, observation)

    def test_expired_binding_and_failed_body_are_withheld(self):
        observed = self.observe()
        self.binding["valid_until_epoch_seconds"] = self.block_time // 1000
        self.assertIn("BASKET_EXPIRED_AT_BLOCK", assess_basket_consumption(
            self.binding, observed)["reason_codes"])
        self.binding["valid_until_epoch_seconds"] += 300
        self.body["ret"][0]["contractRet"] = "REVERT"
        observed = self.observe()
        self.assertEqual(observed["status"], "SOLID_EXECUTION_FAILED")
        self.assertEqual(assess_basket_consumption(self.binding, observed)["status"],
                         "WITHHELD")


if __name__ == "__main__":
    unittest.main()
