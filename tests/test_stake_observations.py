"""Replay, clock, chain identity and native receipt boundary tests."""

import copy, hashlib, json, unittest
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from economic_machine.values import MachineError, digest
from finance_service.stake_market import normalize, forecast_rate, blocks_per_year
from finance_service.stake_receipt import observe
from finance_service.native_execution import READER
from test_stake_execution import transaction, REP

AT = "2026-09-29T12:00:00+00:00"
MS = int(datetime.fromisoformat(AT).timestamp() * 1000)
OWNER = "41" + "34" * 20


def block(height, at=MS):
    return {
        "blockID": format(height, "016x") + "ab" * 24,
        "block_header": {"raw_data": {"number": height, "timestamp": at}},
    }


def evidence():
    return {
        "observed_at": AT,
        "network": "tron-nile",
        "wallet": OWNER,
        "account": {"address": OWNER, "balance": 900000000},
        "head": block(100),
        "parameters": {
            "chainParameter": [
                {"key": k, "value": v}
                for k, v in {
                    "getWitnessPayPerBlock": 8000000,
                    "getWitness127PayPerBlock": 128000000,
                    "getTransactionFee": 1000,
                    "getUnfreezeDelayDays": 1,
                    "getAllowNewReward": 1,
                    "getMaintenanceTimeInterval": 1800000,
                }.items()
            ]
        },
        "witnesses": {
            "witnesses": [
                {
                    "address": "41" + format(i + 1, "040x"),
                    "voteCount": 1000000 + i,
                    "isJobs": True,
                }
                for i in range(127)
            ]
        },
        "brokerages": {
            "41" + format(i + 1, "040x"): {"brokerage": 20} for i in range(127)
        },
    }


class NativeMarketTests(unittest.TestCase):
    def test_network_specific_maintenance_and_commission_not_mainnet_constant(self):
        m = normalize(evidence(), AT)
        r = m["representative"]
        p = m["parameters"]
        self.assertEqual(blocks_per_year(p), D(28704 * 365))
        self.assertEqual(
            blocks_per_year({**p, "getMaintenanceTimeInterval": 21600000}),
            D(28792 * 365),
        )
        self.assertEqual(forecast_rate({**r, "brokerage_percent": 100}, p, 0), "0")
        self.assertLess(D(forecast_rate(r, p, 900)), D(forecast_rate(r, p, 0)))

    def test_stale_future_wrong_wallet_incomplete_vote_evidence_fails_closed(self):
        for kind in [
            "stale",
            "future",
            "owner",
            "missing-votes",
            "missing-maintenance",
        ]:
            e = evidence()
            if kind == "stale":
                e["head"] = block(100, MS - 301000)
            if kind == "future":
                e["observed_at"] = "2026-09-29T12:01:00+00:00"
            if kind == "owner":
                e["account"]["address"] = REP
            if kind == "missing-votes":
                e["witnesses"]["witnesses"].pop()
            if kind == "missing-maintenance":
                e["parameters"]["chainParameter"] = [
                    p
                    for p in e["parameters"]["chainParameter"]
                    if p["key"] != "getMaintenanceTimeInterval"
                ]
            with self.subTest(kind=kind), self.assertRaises(MachineError):
                normalize(e, AT)

    def test_selected_forecast_replays_exact_evidence(self):
        a = normalize(evidence(), AT)
        b = normalize(copy.deepcopy(a["evidence"]), AT)
        self.assertEqual(a, b)
        e = evidence()
        e["brokerages"][a["representative"]["address"]]["brokerage"] = 100
        self.assertNotEqual(normalize(e, AT)["hash"], a["hash"])


class NativeReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tx = transaction(
            "STAKE",
            {"owner_address": OWNER, "frozen_balance": 600000000, "resource": "ENERGY"},
            AT,
        )
        self.now = AT
        self.head = block(100)
        self.body = {}
        self.info = {}
        self.full = {}
        self.anchor = READER["network_anchor_block_id"]
        self.member = True
        self.ctx = SimpleNamespace(wallet=OWNER, network="tron-nile")
        self.adapter = SimpleNamespace(rpc=self.rpc, clock=lambda: self.now)

    def rpc(self, ctx, path, body=None):
        if path.endswith("getnowblock"):
            return self.head
        if path.endswith("getblockbynum"):
            if body["num"] == 1:
                return {**block(1), "blockID": self.anchor}
            return {**block(99), "transactions": [self.body] if self.member else []}
        if path == "walletsolidity/gettransactionbyid":
            return self.body
        if path == "walletsolidity/gettransactioninfobyid":
            return self.info
        if path in ("wallet/gettransactionbyid", "wallet/gettransactioninfobyid"):
            return self.full
        raise AssertionError(path)

    def solid(self):
        self.body = {**self.tx, "ret": [{"contractRet": "SUCCESS"}]}
        self.info = {
            "id": self.tx["txID"],
            "blockNumber": 99,
            "blockTimeStamp": MS,
            "fee": 200000,
            "receipt": {"net_fee": 200000},
        }

    def test_system_success_does_not_require_tvm_success_field(self):
        self.solid()
        r = observe(self.adapter, self.ctx, self.tx)
        self.assertEqual(r["status"], "SUCCESS")
        self.assertEqual(r["fee_sun"], 200000)

    def test_raw_bytes_membership_network_and_fee_mismatch_reject(self):
        self.solid()
        self.member = False
        with self.assertRaisesRegex(MachineError, "membership"):
            observe(self.adapter, self.ctx, self.tx)
        self.member = True
        self.body = {**self.body, "raw_data_hex": "00"}
        with self.assertRaisesRegex(MachineError, "bytes"):
            observe(self.adapter, self.ctx, self.tx)
        self.solid()
        self.anchor = "0000000000000001" + "00" * 24
        with self.assertRaisesRegex(MachineError, "anchor"):
            observe(self.adapter, self.ctx, self.tx)
        self.anchor = READER["network_anchor_block_id"]
        self.info["fee"] = -1
        with self.assertRaisesRegex(MachineError, "malformed"):
            observe(self.adapter, self.ctx, self.tx)

    def test_wall_clock_expiry_alone_never_releases_lock(self):
        self.now = (datetime.fromisoformat(AT) + timedelta(seconds=121)).isoformat()
        self.assertEqual(observe(self.adapter, self.ctx, self.tx)["status"], "PENDING")
        self.head = block(110, MS + 121000)
        self.full = {"txID": self.tx["txID"]}
        self.assertEqual(observe(self.adapter, self.ctx, self.tx)["status"], "PENDING")
        self.full = {}
        self.assertEqual(
            observe(self.adapter, self.ctx, self.tx)["status"], "EXPIRED_NOT_OBSERVED"
        )

    def test_failed_contract_result_never_success(self):
        self.solid()
        self.body["ret"] = [{"contractRet": "REVERT"}]
        self.assertEqual(observe(self.adapter, self.ctx, self.tx)["status"], "FAILED")
        self.body["ret"] = [{"contractRet": "UNKNOWN"}]
        with self.assertRaisesRegex(MachineError, "unresolved"):
            observe(self.adapter, self.ctx, self.tx)
        self.solid()
        self.info["result"] = "FAILED"
        with self.assertRaisesRegex(MachineError, "contradict"):
            observe(self.adapter, self.ctx, self.tx)
