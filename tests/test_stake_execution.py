"""Synthetic native lifecycle; no real key, signature or transaction is submitted."""

import copy, hashlib, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta
from unittest.mock import patch
from economic_machine.values import MachineError, canonical, digest
from economic_machine.signed_tx_validation import _encode_varint as vi
from finance_service.machine_bridge import MachineBridge, empty_workspace
from finance_service.operational_repository import OperationalRepository
from finance_service.context import AuthenticatedContext
from finance_service.stake_codec import (
    TYPES,
    decode,
    validate_unsigned,
    BANDWIDTH_BYTES,
)
from finance_service.stake_execution import ENDPOINT
from finance_service.native_execution import READER
from test_economic_tron_execution import wallet_for, sign_hash
from test_portfolio_review import policy

KEY = int.from_bytes(
    b"\x42" * 32, "big"
)  # Synthetic test fixture, never a funded account.
REP = "41" + "12" * 20


def field(n, value):
    return (
        vi(n * 8) + vi(value)
        if isinstance(value, int)
        else vi(n * 8 + 2) + vi(len(value)) + value
    )


def transaction(action, value, now):
    kind = ENDPOINT[action][1]
    body = field(1, bytes.fromhex(value["owner_address"]))
    if action in ("STAKE", "UNSTAKE"):
        body += field(
            2, value["frozen_balance" if action == "STAKE" else "unfreeze_balance"]
        ) + field(3, 1)
    if action == "VOTE":
        for vote in value["votes"]:
            body += field(
                2,
                field(1, bytes.fromhex(vote["vote_address"]))
                + field(2, vote["vote_count"]),
            )
    ms = int(datetime.fromisoformat(now).timestamp() * 1000)
    any_body = field(1, ("type.googleapis.com/protocol." + kind).encode()) + field(
        2, body
    )
    contract = field(1, TYPES[kind]) + field(2, any_body)
    raw = (
        field(1, bytes.fromhex("0123"))
        + field(4, bytes.fromhex("12" * 8))
        + field(8, ms + 60000)
        + field(11, contract)
        + field(14, ms)
    )
    return {
        "visible": False,
        "txID": hashlib.sha256(raw).hexdigest(),
        "raw_data_hex": raw.hex(),
        "raw_data": {
            "ref_block_bytes": "0123",
            "ref_block_hash": "12" * 8,
            "expiration": ms + 60000,
            "timestamp": ms,
            "contract": [
                {
                    "type": kind,
                    "parameter": {
                        "type_url": "type.googleapis.com/protocol." + kind,
                        "value": copy.deepcopy(value),
                    },
                }
            ],
        },
    }


class StakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = "2026-09-29T12:00:00+00:00"
        self.owner = wallet_for(KEY)
        self.broadcasts = 0
        self.timeout = False
        self.price = 1000
        self.reward = 0
        self.account = {"address": self.owner, "balance": 900000000}
        self.after = None
        self.ctx = AuthenticatedContext(
            tenant_id="t",
            owner_id="u",
            wallet=self.owner,
            network="tron-nile",
            session_id="s",
            trace_id="t",
            issued_at=self.now,
            expires_at="2026-10-01T12:00:00+00:00",
        )
        self.b = MachineBridge(
            OperationalRepository(Path(self.tmp.name) / "db"),
            lambda: self.now,
            None,
            SimpleNamespace(request=self.rpc),
        )
        self.e = self.b.stake
        p = policy()
        t = p["mandate"]["terms"]
        t.update(
            horizon_seconds=365 * 86400,
            capital=[{"asset": "TRX", "amount": "900"}],
            protocol_caps_bps={"tron-native": 10000},
            allowed_actions=["HOLD", "STAKE", "VOTE", "UNSTAKE", "CLAIM"],
        )
        t["limits"] = {
            k: {"asset": "TRX", "amount": "900" if k != "fee_amount" else "20"}
            for k in t["limits"]
        }
        w = empty_workspace("nile")
        w["mandate"] = {
            "hash": p["policy_hash"],
            "status": "CONFIRMED",
            "terms": t,
            "constraints": {"max_fee": {"value": "1", "symbol": "TRX", "decimals": 6}},
        }
        self.state = {
            "workspace": w,
            "mandates": {"m": {"revisions": [p]}},
            "requests": {},
        }
        self.params = {
            "getWitnessPayPerBlock": 8000000,
            "getWitness127PayPerBlock": 128000000,
            "getTransactionFee": 1000,
            "getUnfreezeDelayDays": 1,
            "getMaintenanceTimeInterval": 1800000,
        }
        self.rep = {
            "address": REP,
            "address_base58": "TTestRepresentative",
            "brokerage_percent": 20,
            "votes": "10000000",
            "total_top127_votes": "1000000000",
            "annual_fraction": "0.1",
        }
        self.market = {
            "hash": "b" * 64,
            "representative": self.rep,
            "parameters": self.params,
            "evidence": {
                "brokerages": {REP: {"brokerage": 20}},
                "witnesses": {
                    "witnesses": [
                        {"address": REP, "voteCount": 10000000, "isJobs": True}
                    ]
                },
            },
        }
        self.addCleanup(patch.stopall)
        patch("finance_service.stake_execution.read", return_value=self.market).start()
        self.plan = {
            "id": "GROWTH",
            "hash": "c" * 64,
            "expected_net_return": {"value": "10"},
            "estimated_fees": {"value": "2"},
            "allocations": [
                {
                    "product": "tron.native.stake",
                    "kind": "STAKE",
                    "amount": {"value": "600"},
                }
            ],
        }

    def rpc(self, url, body):
        path = url.split("/")[-1]
        if path == "getaccount":
            r = self.after or self.account
        elif path == "getblockbynum":
            r = {
                "blockID": READER["network_anchor_block_id"],
                "block_header": {"raw_data": {"number": 1, "timestamp": 1}},
            }
        elif path == "getchainparameters":
            r = {
                "chainParameter": [
                    {"key": k, "value": self.price if k == "getTransactionFee" else v}
                    for k, v in self.params.items()
                ]
            }
        elif path == "getBrokerage":
            r = {"brokerage": 20}
        elif path == "getReward":
            r = {"reward": self.reward}
        elif path in [x[0] for x in ENDPOINT.values()]:
            action = next(k for k, v in ENDPOINT.items() if v[0] == path)
            r = transaction(
                action, {k: v for k, v in body.items() if k != "visible"}, self.now
            )
        elif path == "broadcasttransaction":
            stored = self.b.load(self.ctx)[1]
            assert stored["workspace"]["execution"]["status"] == "SUBMISSION_UNKNOWN"
            self.broadcasts += 1
            if self.timeout:
                raise TimeoutError("response lost after send")
            r = {"result": True}
        else:
            raise AssertionError(path)
        return canonical(r)

    def reviewed(self):
        self.e.create(self.ctx, self.state, self.plan, {"market": self.market})
        self.b.commit(self.ctx, 0, self.state)
        return self.state["workspace"]["graph"]

    def prepared(self, graph=None):
        g = graph or self.reviewed()
        v, s = self.b.load(self.ctx)
        self.e.approve(
            self.ctx,
            s,
            {
                **{
                    k: g[k] for k in ["plan_hash", "mandate_hash", "account", "network"]
                },
                "graph_id": g["id"],
                "graph_hash": g["hash"],
            },
        )
        self.b.commit(self.ctx, v, s)
        p = self.e.prepare(
            self.ctx,
            g["id"],
            {
                "approval_id": s["workspace"]["approval"]["id"],
                "account": g["account"],
                "step_id": g["steps"][0]["id"],
            },
        )
        tx = p["transaction"]
        tx["signature"] = [sign_hash(bytes.fromhex(tx["txID"]), KEY).hex()]
        return {
            "graph_id": g["id"],
            "step_id": g["steps"][0]["id"],
            "approval_id": s["workspace"]["approval"]["id"],
            "signed_transaction": tx,
        }, p

    def proof(self, fee=250000, status="SUCCESS"):
        return {
            "status": status,
            "fee_sun": fee,
            "block": {
                "timestamp_ms": int(datetime.fromisoformat(self.now).timestamp() * 1000)
            },
            "hash": "d" * 64,
            "receipt": {},
        }

    def test_canonical_ids_owner_resource_and_bandwidth(self):
        self.assertEqual(TYPES["FreezeBalanceV2Contract"], 54)
        self.assertEqual(TYPES["UnfreezeBalanceV2Contract"], 55)
        self.assertEqual(TYPES["WithdrawExpireUnfreezeContract"], 56)
        for action in ENDPOINT:
            value = {"owner_address": self.owner}
            if action == "STAKE":
                value.update(frozen_balance=600000000, resource="ENERGY")
            if action == "UNSTAKE":
                value.update(unfreeze_balance=600000000, resource="ENERGY")
            if action == "VOTE":
                value["votes"] = [{"vote_address": REP, "vote_count": 600}]
            tx = transaction(action, value, self.now)
            validate_unsigned(
                tx,
                {"kind": ENDPOINT[action][1], "value": value},
                int(datetime.fromisoformat(self.now).timestamp() * 1000),
            )
            self.assertLess(
                len(bytes.fromhex(tx["raw_data_hex"])) + 128, BANDWIDTH_BYTES
            )

    def test_duplicate_fields_and_unknown_permission_fail(self):
        tx = transaction(
            "STAKE",
            {
                "owner_address": self.owner,
                "frozen_balance": 10**6,
                "resource": "ENERGY",
            },
            self.now,
        )
        for suffix in [field(14, 1), field(99, 0)]:
            with self.assertRaises(MachineError):
                decode(bytes.fromhex(tx["raw_data_hex"]) + suffix)

    def test_tampered_projection_and_wrong_signature_never_broadcast(self):
        payload, p = self.prepared()
        bad = copy.deepcopy(payload)
        bad["signed_transaction"]["raw_data"]["contract"][0]["parameter"]["value"][
            "frozen_balance"
        ] = 1
        with self.assertRaises(MachineError):
            self.e.submit(self.ctx, bad)
        bad = copy.deepcopy(payload)
        bad["signed_transaction"]["signature"] = [
            sign_hash(bytes.fromhex(p["transaction"]["txID"]), KEY + 1).hex()
        ]
        with self.assertRaisesRegex(MachineError, "wallet"):
            self.e.submit(self.ctx, bad)
        self.assertEqual(self.broadcasts, 0)

    def test_durable_before_send_and_retry_does_not_rebroadcast(self):
        payload, _ = self.prepared()
        self.timeout = True
        self.e.submit(self.ctx, payload)
        self.e.submit(self.ctx, payload)
        self.assertEqual(self.broadcasts, 1)
        with self.assertRaises(MachineError):
            self.b.mutate(self.ctx, "POST", "/v1/mandates", {}, "replace")

    def test_fee_price_or_account_change_blocks_before_send(self):
        payload, _ = self.prepared()
        self.price += 1
        with self.assertRaisesRegex(MachineError, "price"):
            self.e.submit(self.ctx, payload)
        self.price = 1000
        self.account["balance"] -= 1
        with self.assertRaisesRegex(MachineError, "account changed"):
            self.e.submit(self.ctx, payload)
        self.assertEqual(self.broadcasts, 0)

    def test_existing_votes_and_unfreeze_queue_block_new_attribution(self):
        self.account["votes"] = [{"vote_address": REP, "vote_count": 1}]
        with self.assertRaisesRegex(MachineError, "pre-existing"):
            self.reviewed()
        self.state.pop("stake_workflow", None)
        self.account.pop("votes")
        self.account["unfrozenV2"] = [{"type": "ENERGY", "unfreeze_amount": 1}]
        with self.assertRaisesRegex(MachineError, "pre-existing"):
            self.reviewed()

    def test_outgoing_delegated_stake_blocks_attribution(self):
        self.account["account_resource"] = {
            "delegated_frozenV2_balance_for_energy": 1000000
        }
        with self.assertRaisesRegex(MachineError, "pre-existing"):
            self.reviewed()

    def test_exact_stake_then_vote_lifecycle_and_actual_fees(self):
        payload, _ = self.prepared()
        self.e.submit(self.ctx, payload)
        self.after = {
            "address": self.owner,
            "balance": 299750000,
            "frozenV2": [{"type": "ENERGY", "amount": 600000000}],
        }
        with patch("finance_service.stake_receipt.observe", return_value=self.proof()):
            self.e.reconcile(self.ctx)
        v, s = self.b.load(self.ctx)
        self.assertEqual(s["stake_workflow"]["status"], "NEXT_REVIEW")
        self.assertEqual(s["workspace"]["stake_position"]["fees"]["value"], "0.25")
        self.assertEqual(s["workspace"]["execution"]["status"], "POSITION_RECONCILED")
        self.e.next(self.ctx, s)
        self.b.commit(self.ctx, v, s)
        payload, _ = self.prepared(s["workspace"]["graph"])
        self.assertEqual(
            payload["signed_transaction"]["raw_data"]["contract"][0]["type"],
            "VoteWitnessContract",
        )
        self.e.submit(self.ctx, payload)
        self.after["balance"] -= 250000
        self.after["votes"] = [{"vote_address": REP, "vote_count": 600}]
        with patch("finance_service.stake_receipt.observe", return_value=self.proof()):
            self.e.reconcile(self.ctx)
            self.e.reconcile(self.ctx)
        s = self.b.load(self.ctx)[1]
        self.assertEqual(s["stake_workflow"]["status"], "COMPLETE")
        self.assertEqual(s["stake_position"]["status"], "EARNING")
        self.assertEqual(len(s["stake_receipts"]), 2)
        self.assertEqual(
            s["workspace"]["stake_position"]["net_income"]["value"], "-0.5"
        )

    def test_receipt_mismatched_balance_disputes_and_blocks_next(self):
        payload, _ = self.prepared()
        self.e.submit(self.ctx, payload)
        self.after = {
            "address": self.owner,
            "balance": 299750001,
            "frozenV2": [{"type": "ENERGY", "amount": 600000000}],
        }
        with patch("finance_service.stake_receipt.observe", return_value=self.proof()):
            self.e.reconcile(self.ctx)
        _, s = self.b.load(self.ctx)
        self.assertEqual(s["workspace"]["execution"]["status"], "DISPUTED")
        with self.assertRaises(MachineError):
            self.e.next(self.ctx, s)

    def test_expired_absence_unlocks_only_after_verified_proof(self):
        payload, _ = self.prepared()
        pointer = {
            "txid": payload["signed_transaction"]["txID"],
            "graph_id": payload["graph_id"],
            "step_id": payload["step_id"],
        }
        with patch(
            "finance_service.stake_receipt.observe", return_value={"status": "PENDING"}
        ):
            self.e.reconcile(self.ctx, pointer)
        self.assertEqual(len(self.b.workspace(self.ctx)["prepared_transactions"]), 1)
        with patch(
            "finance_service.stake_receipt.observe",
            return_value={"status": "EXPIRED_NOT_OBSERVED"},
        ):
            self.e.reconcile(self.ctx, pointer)
        self.assertEqual(self.b.workspace(self.ctx)["prepared_transactions"], [])
        with self.assertRaises(MachineError):
            self.e.submit(self.ctx, payload)
        self.assertEqual(self.broadcasts, 0)

    def test_same_policy_cumulative_spending_includes_lending(self):
        self.state["review_spend_ledger"] = {
            "old": {
                "policy_hash": "a" * 64,
                "amount": "500000000",
                "fee": "1",
                "action": "SUPPLY",
            }
        }
        with self.assertRaisesRegex(MachineError, "Cumulative"):
            self.reviewed()

    def test_expired_policy_cannot_authorize_step(self):
        self.now = "2026-10-10T12:00:00+00:00"
        with self.assertRaises(MachineError):
            self.e.create(self.ctx, self.state, self.plan, {"market": self.market})

    def earned(self):
        self.test_exact_stake_then_vote_lifecycle_and_actual_fees()

    def lifecycle_prepared(self, action):
        v, s = self.b.load(self.ctx)
        self.e.lifecycle(self.ctx, s, {"action": action})
        self.b.commit(self.ctx, v, s)
        return self.prepared(s["workspace"]["graph"])

    def test_unstake_wait_withdraw_and_preserved_receipts(self):
        self.earned()
        payload, _ = self.lifecycle_prepared("UNSTAKE")
        self.e.submit(self.ctx, payload)
        ms = int(datetime.fromisoformat(self.now).timestamp() * 1000)
        self.after = {
            "address": self.owner,
            "balance": 299250000,
            "frozenV2": [{"type": "ENERGY"}],
            "unfrozenV2": [
                {
                    "type": "ENERGY",
                    "unfreeze_amount": 600000000,
                    "unfreeze_expire_time": ms + 86400000,
                }
            ],
        }
        with patch("finance_service.stake_receipt.observe", return_value=self.proof()):
            self.e.reconcile(self.ctx)
        v, s = self.b.load(self.ctx)
        self.assertEqual(s["stake_position"]["status"], "UNFREEZING")
        with self.assertRaisesRegex(MachineError, "waiting"):
            self.e.lifecycle(self.ctx, s, {"action": "WITHDRAW"})
        self.now = (
            datetime.fromisoformat(self.now) + timedelta(days=1, seconds=1)
        ).isoformat()
        payload, _ = self.lifecycle_prepared("WITHDRAW")
        self.e.submit(self.ctx, payload)
        self.after = {
            "address": self.owner,
            "balance": 899000000,
            "frozenV2": [{"type": "ENERGY"}],
        }
        proof = self.proof()
        proof["receipt"]["withdraw_expire_amount"] = 600000000
        with patch("finance_service.stake_receipt.observe", return_value=proof):
            self.e.reconcile(self.ctx)
        s = self.b.load(self.ctx)[1]
        self.assertEqual(s["stake_position"]["status"], "CLOSED")
        self.assertEqual(s["workspace"]["positions"], [])
        self.assertEqual(len(s["stake_receipts"]), 4)
        self.assertEqual(s["workspace"]["stake_position"]["fees"]["value"], "1")

    def test_reward_claim_exact_credit_and_zero_or_cooldown_rejected(self):
        self.earned()
        v, s = self.b.load(self.ctx)
        with self.assertRaisesRegex(MachineError, "No claimable"):
            self.e.lifecycle(self.ctx, s, {"action": "CLAIM"})
        self.reward = 1000000
        payload, _ = self.lifecycle_prepared("CLAIM")
        self.e.submit(self.ctx, payload)
        self.after["balance"] += 750000
        self.after["latest_withdraw_time"] = int(
            datetime.fromisoformat(self.now).timestamp() * 1000
        )
        self.reward = 0
        proof = self.proof()
        proof["receipt"]["withdraw_amount"] = 1000000
        with patch("finance_service.stake_receipt.observe", return_value=proof):
            self.e.reconcile(self.ctx)
        v, s = self.b.load(self.ctx)
        self.assertEqual(s["workspace"]["stake_position"]["realized"]["value"], "1")
        self.assertEqual(
            s["workspace"]["stake_position"]["net_income"]["value"], "0.25"
        )
        self.reward = 100
        with self.assertRaisesRegex(MachineError, "cooldown"):
            self.e.lifecycle(self.ctx, s, {"action": "CLAIM"})

    def test_failed_transaction_fees_remain_in_shared_limits(self):
        payload, _ = self.prepared()
        self.e.submit(self.ctx, payload)
        self.after = {"address": self.owner, "balance": 899750000}
        with patch(
            "finance_service.stake_receipt.observe",
            return_value=self.proof(status="FAILED"),
        ):
            self.e.reconcile(self.ctx)
        s = self.b.load(self.ctx)[1]
        self.assertEqual(s["workspace"]["execution"]["status"], "FAILED")
        self.assertEqual(next(iter(s["review_spend_ledger"].values()))["fee"], "250000")

    def test_late_broadcast_response_does_not_regress_completed_receipt(self):
        payload, _ = self.prepared()
        original = self.e.rpc

        def request(ctx, path, body=None):
            if path.endswith("broadcasttransaction"):
                self.after = {
                    "address": self.owner,
                    "balance": 299750000,
                    "frozenV2": [{"type": "ENERGY", "amount": 600000000}],
                }
                with patch(
                    "finance_service.stake_receipt.observe", return_value=self.proof()
                ):
                    self.e.reconcile(ctx)
                return {"result": True}
            return original(ctx, path, body)

        with patch.object(self.e, "rpc", side_effect=request):
            self.e.submit(self.ctx, payload)
        self.assertEqual(
            self.b.workspace(self.ctx)["execution"]["status"], "POSITION_RECONCILED"
        )


class NativeRouteTests(unittest.TestCase):
    """Exercise authenticated HTTP dispatch through approval and native preflight."""

    def test_gateway_auth_scope_idempotency_and_native_dispatch(self):
        from dataclasses import replace
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from economic_machine.tron_sources import address_base58
        from finance_service.machine_bridge import router_for

        f = StakeTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.ctx = replace(
            f.ctx,
            tenant_id="machine",
            owner_id="workspace-native-test",
            session_id="gateway-workspace-native-test",
        )
        g = f.reviewed()
        app = FastAPI()
        secret = "synthetic-local-test-gateway-secret"
        app.include_router(router_for(f.b, secret))
        headers = {
            "authorization": "Bearer " + secret,
            "x-machine-workspace": "native-test",
            "x-verified-wallet": address_base58(f.owner),
            "idempotency-key": "native-approval",
        }
        body = {
            **{k: g[k] for k in ("plan_hash", "mandate_hash", "account", "network")},
            "graph_id": g["id"],
            "graph_hash": g["hash"],
        }
        with TestClient(app) as client:
            self.assertEqual(
                client.post("/v1/machine/approvals", json=body).status_code, 401
            )
            first = client.post("/v1/machine/approvals", headers=headers, json=body)
            self.assertEqual(first.status_code, 200, first.text)
            retry = client.post("/v1/machine/approvals", headers=headers, json=body)
            self.assertEqual(retry.json(), first.json())
            w = client.get("/v1/machine/workspace?network=nile", headers=headers).json()
            self.assertEqual(w["product_catalog"][0]["status"], "Enabled")
            prepared = client.post(
                "/v1/machine/execution-graphs/" + g["id"] + "/preflight",
                headers={**headers, "idempotency-key": "native-preflight"},
                json={
                    "network": "nile",
                    "account": g["account"],
                    "step_id": g["steps"][0]["id"],
                    "approval_id": w["approval"]["id"],
                },
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            self.assertEqual(
                prepared.json()["transaction"]["raw_data"]["contract"][0]["type"],
                "FreezeBalanceV2Contract",
            )
            self.assertFalse(prepared.json()["simulation"])
            locked = client.post(
                "/v1/machine/plan-comparisons",
                headers={**headers, "idempotency-key": "new-compare"},
                json={"network": "nile"},
            )
            self.assertEqual(locked.status_code, 409)
            self.assertEqual(f.broadcasts, 0)
