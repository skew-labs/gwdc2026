"""Native Stake 2.0 lifecycle with exact signatures and durable-before-send CAS.

Each stake/vote/unstake/claim is separately reviewed. Native system calls use
bandwidth; they have no on-chain fee_limit. The current full burn is reserved,
and its resource price is rechecked before broadcast. No private key is used.
"""

from .stake_codec import BANDWIDTH_BYTES
import hashlib, re
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal as D
from economic_machine.values import MachineError, digest, decstr
from economic_machine.tron_crypto import recover_tron_address
from economic_machine.tron_sources import address_base58
from economic_machine.mandate import reserve_amount
from .native_execution import money, atoms, normalize_wallet_signature, ACK
from .stake_market import rpc, read, PRODUCT
from .stake_codec import validate_unsigned, decode

ENDPOINT = {
    "STAKE": ("freezebalancev2", "FreezeBalanceV2Contract"),
    "VOTE": ("votewitnessaccount", "VoteWitnessContract"),
    "UNSTAKE": ("unfreezebalancev2", "UnfreezeBalanceV2Contract"),
    "WITHDRAW": ("withdrawexpireunfreeze", "WithdrawExpireUnfreezeContract"),
    "CLAIM": ("withdrawbalance", "WithdrawBalanceContract"),
}
TERMINAL = {"POSITION_RECONCILED", "FAILED", "EXPIRED_NOT_OBSERVED"}


def frozen(account):
    return sum(
        x.get("amount", 0)
        for x in account.get("frozenV2", [])
        if x.get("type") == "ENERGY"
    )


def votes(account):
    return sorted(
        [
            {"vote_address": v["vote_address"], "vote_count": v["vote_count"]}
            for v in account.get("votes", [])
        ],
        key=lambda v: v["vote_address"],
    )


def unfreezing(account):
    return sum(
        x.get("unfreeze_amount", 0)
        for x in account.get("unfrozenV2", [])
        if x.get("type") == "ENERGY"
    )


class StakeExecution:
    def __init__(self, bridge):
        self.bridge = bridge
        self.clock = bridge.clock

    def rpc(self, context, path, body=None):
        return rpc(self.bridge.observations.request, context.network, path, body)

    def account(self, context):
        a = self.rpc(
            context,
            "walletsolidity/getaccount",
            {"address": context.wallet, "visible": False},
        )
        if a.get("address") != context.wallet or type(a.get("balance", 0)) is not int:
            raise MachineError("Native staking account unavailable.")
        return a

    def policy(self, context, state):
        from .portfolio_review import confirmed_policy

        p = confirmed_policy(state)
        m = state["workspace"].get("mandate")
        if (
            not p
            or not m
            or m["status"] != "CONFIRMED"
            or p["policy_hash"] != m["hash"]
        ):
            raise MachineError(
                "Confirm current conditions before reviewing a native staking action."
            )
        t = p["mandate"]["terms"]
        at = self.clock()
        if (
            context.network != "tron-nile"
            or t["base_asset"] != "TRX"
            or not t["effective_at"] <= at < t["expires_at"]
        ):
            raise MachineError("Current Nile TRX staking conditions required.")
        return p, t

    def create(self, context, state, plan, core):
        p, t = self.policy(context, state)
        if state.get("stake_position", {}).get("status") not in (None, "CLOSED"):
            raise MachineError(
                "Manage or close your existing native stake before opening another."
            )
        if (
            not {"STAKE", "VOTE"}.issubset(t["allowed_actions"])
            or t["protocol_caps_bps"].get("tron-native", 0) <= 0
        ):
            raise MachineError("Explicit Native Stake and voting permission required.")
        if state.get("stake_workflow", {}).get("status") not in (
            None,
            "COMPLETE",
            "CANCELLED",
            "FAILED",
        ):
            raise MachineError(
                "Complete or recover the existing staking workflow first."
            )
        amount = sum(
            atoms(a["amount"]["value"])
            for a in plan["allocations"]
            if a["product"] == PRODUCT
        )
        if (
            amount <= 0
            or amount % 10**6
            or any(
                a["kind"] != "CASH" and a["product"] != PRODUCT
                for a in plan["allocations"]
            )
        ):
            raise MachineError(
                "Native staking review requires whole TRX and one supported staking allocation."
            )
        if D(plan["expected_net_return"]["value"]) <= 0:
            raise MachineError("Native staking forecast must cover full costs.")
        if amount > atoms(t["limits"]["single_amount"]["amount"]) or amount > atoms(
            t["limits"]["cumulative_amount"]["amount"]
        ):
            raise MachineError("Native staking exceeds investment limits.")
        state["stake_workflow"] = {
            "id": "stake-" + digest({"plan": plan["hash"], "at": self.clock()})[:24],
            "status": "REVIEW",
            "amount_sun": str(amount),
            "representative": core["market"]["representative"],
            "market_hash": core["market"]["hash"],
            "policy_hash": p["policy_hash"],
            "plan": deepcopy(plan),
            "cursor": 0,
            "forecast": {
                "gross": decstr(
                    D(plan["expected_net_return"]["value"])
                    + D(plan["estimated_fees"]["value"])
                ),
                "net": plan["expected_net_return"]["value"],
                "fees": plan["estimated_fees"]["value"],
                "horizon_seconds": t["horizon_seconds"],
            },
            "steps": [
                {"action": "STAKE", "status": "WAITING"},
                {"action": "VOTE", "status": "WAITING"},
            ],
        }
        self.review(context, state)

    def review(self, context, state):
        p, t = self.policy(context, state)
        wf = state.get("stake_workflow")
        if not wf or wf["status"] == "COMPLETE":
            raise MachineError("No next staking step.")
        step = wf["steps"][wf["cursor"]]
        if step.get("submission") or step.get("transaction"):
            raise MachineError("Reconcile the existing native wallet request first.")
        action = step["action"]
        required = "CLAIM" if action == "WITHDRAW" else action
        if required not in t["allowed_actions"]:
            raise MachineError("This staking action is outside confirmed permission.")
        if wf["policy_hash"] != p["policy_hash"]:
            raise MachineError(
                "Staking conditions changed; recover under a freshly confirmed plan."
            )
        from economic_machine.tron_registry_read import _block
        from .native_execution import READER

        anchor = _block(
            self.rpc(
                context,
                "walletsolidity/getblockbynum",
                {"num": READER["network_anchor_height"]},
            )
        )
        if anchor["block_id"] != READER["network_anchor_block_id"]:
            raise MachineError("Native staking network anchor mismatch.")
        a = self.account(context)
        params = self.rpc(context, "wallet/getchainparameters")
        price = next(
            (
                v.get("value", 0)
                for v in params.get("chainParameter", [])
                if v["key"] == "getTransactionFee"
            ),
            None,
        )
        if type(price) is not int or price <= 0:
            raise MachineError("Native bandwidth price unavailable.")
        cap = BANDWIDTH_BYTES * price
        if cap > atoms(
            state["workspace"]["mandate"]["constraints"]["max_fee"]["value"]
        ):
            raise MachineError(
                "Native bandwidth reserve exceeds your transaction fee limit."
            )
        total_paid = sum(
            int(v["fee"])
            for v in state.get("review_spend_ledger", {}).values()
            if v["policy_hash"] == p["policy_hash"]
        )
        remaining = (5 - wf["cursor"]) if action in ("STAKE", "VOTE") else 1
        if total_paid + cap * remaining > atoms(t["limits"]["fee_amount"]["amount"]):
            raise MachineError(
                "Complete staking lifecycle exceeds remaining fee budget."
            )
        amount = int(wf["amount_sun"])
        value = {"owner_address": context.wallet}
        if action == "STAKE":
            spent = sum(
                int(v["amount"])
                for v in state.get("review_spend_ledger", {}).values()
                if v["policy_hash"] == p["policy_hash"]
            )
            if (
                any(x.get("amount", 0) for x in a.get("frozenV2", []))
                or a.get("votes")
                or a.get("unfrozenV2")
                or a.get("frozen")
                or a.get("delegated_frozen_balance_for_bandwidth", 0)
                or a.get("delegated_frozenV2_balance_for_bandwidth", 0)
                or a.get("account_resource", {}).get(
                    "delegated_frozen_balance_for_energy", 0
                )
                or a.get("account_resource", {}).get(
                    "delegated_frozenV2_balance_for_energy", 0
                )
                or a.get("account_resource", {})
                .get("frozen_balance_for_energy", {})
                .get("frozen_balance")
            ):
                raise MachineError(
                    "This first native release requires no pre-existing stake, votes or unfreeze queue, so rewards and exits can be attributed exactly."
                )
            step["reward_before"] = self.rpc(
                context,
                "walletsolidity/getReward",
                {"address": context.wallet, "visible": False},
            ).get("reward", 0)
            if step["reward_before"]:
                raise MachineError(
                    "Claim existing rewards before starting a new tracked native stake."
                )
            if spent + amount > atoms(t["limits"]["cumulative_amount"]["amount"]):
                raise MachineError("Cumulative staking limit exceeded.")
            value.update(frozen_balance=amount, resource="ENERGY")
            if a.get("balance", 0) - amount - cap * remaining < int(
                reserve_amount(t["immediate_cash"], t["capital"][0]["amount"]) * 10**6
            ):
                raise MachineError(
                    "Stake plus full lifecycle reserve would consume required cash."
                )
        elif action == "VOTE":
            capital = atoms(t["capital"][0]["amount"])
            if (
                amount > capital * t["protocol_caps_bps"].get("tron-native", 0) // 10000
                or amount
                > capital * t["price_exposure_caps_bps"].get("TRX", 0) // 10000
            ):
                raise MachineError(
                    "Voting would exceed current native or TRX allocation limits. Review unstaking instead."
                )
            if a.get("balance", 0) - cap * remaining < int(
                reserve_amount(t["immediate_cash"], t["capital"][0]["amount"]) * 10**6
            ):
                raise MachineError("Voting would consume required cash.")
            current = {v["vote_address"]: v["vote_count"] for v in votes(a)}
            address = wf["representative"]["address"]
            current[address] = current.get(address, 0) + amount // 10**6
            value["votes"] = [
                {"vote_address": k, "vote_count": v} for k, v in sorted(current.items())
            ]
            if (
                sum(current.values())
                > sum(x.get("amount", 0) for x in a.get("frozenV2", [])) // 10**6
            ):
                raise MachineError("Insufficient Stake 2.0 voting power.")
            if len(current) > 30:
                raise MachineError(
                    "Too many existing representatives for an exact vote update."
                )
        elif action == "UNSTAKE":
            if (
                frozen(a) != amount
                or any(
                    x.get("amount", 0)
                    for x in a.get("frozenV2", [])
                    if x.get("type") != "ENERGY"
                )
                or a.get("unfrozenV2")
            ):
                raise MachineError(
                    "Native stake changed outside this workflow; an exact exit review is unavailable."
                )
            value.update(unfreeze_balance=amount, resource="ENERGY")
        elif action == "WITHDRAW":
            now = int(datetime.fromisoformat(self.clock()).timestamp() * 1000)
            if (
                sum(
                    x.get("unfreeze_amount", 0)
                    for x in a.get("unfrozenV2", [])
                    if x.get("unfreeze_expire_time", now + 1) <= now
                )
                != amount
            ):
                raise MachineError("Unstaking is still in its waiting period.")
        elif action == "CLAIM":
            reward = self.rpc(
                context,
                "wallet/getReward",
                {"address": context.wallet, "visible": False},
            ).get("reward")
            if type(reward) is not int or reward <= 0:
                raise MachineError("No claimable voting rewards currently observed.")
            if (
                int(datetime.fromisoformat(self.clock()).timestamp() * 1000)
                - a.get("latest_withdraw_time", 0)
                < 86400000
            ):
                raise MachineError("Voting reward claim has a 24-hour cooldown.")
        if a.get("balance", 0) < cap:
            raise MachineError("Wallet needs the native transaction fee reserve.")
        if action in ("STAKE", "VOTE"):
            live = read(self.bridge.observations.request, self.clock, context)
            chosen = wf["representative"]
            broker = (
                live["evidence"]["brokerages"]
                .get(chosen["address"], {})
                .get("brokerage")
            )
            if broker != chosen["brokerage_percent"]:
                raise MachineError(
                    "Representative commission changed; compare fresh plans before voting."
                )
            from .stake_market import forecast_rate

            old = next(
                (
                    r
                    for r in live["evidence"]["witnesses"]["witnesses"]
                    if r["address"] == chosen["address"]
                ),
                None,
            )
            top = sorted(
                live["evidence"]["witnesses"]["witnesses"],
                key=lambda r: -r.get("voteCount", 0),
            )[:127]
            if not old or old not in top[:27] or old.get("isJobs") is not True:
                raise MachineError("Selected representative is no longer active.")
            rate = D(
                forecast_rate(
                    {
                        **chosen,
                        "votes": str(old["voteCount"]),
                        "total_top127_votes": str(sum(x["voteCount"] for x in top)),
                    },
                    live["parameters"],
                    amount // 10**6,
                )
            )
            gross = (
                D(amount)
                / 10**6
                * rate
                * D(
                    max(
                        0,
                        t["horizon_seconds"]
                        - live["parameters"]["getMaintenanceTimeInterval"] // 1000,
                    )
                )
                / (365 * 86400)
            )
            if gross <= D(5 * cap) / 10**6:
                raise MachineError(
                    "Current voting income no longer covers complete lifecycle cost."
                )
        expiry = (
            datetime.fromisoformat(self.clock()) + timedelta(minutes=3)
        ).isoformat()
        kind = ENDPOINT[action][1]
        step.update(
            status="READY",
            expected={"kind": kind, "value": value},
            before=a,
            fee_cap_sun=cap,
            bandwidth_price=price,
            expires_at=expiry,
        )
        projection = {
            "id": wf["id"] + "-" + str(wf["cursor"]),
            "plan_id": wf["plan"]["id"],
            "plan_hash": wf["plan"]["hash"],
            "mandate_hash": p["policy_hash"],
            "network": "nile",
            "account": address_base58(context.wallet),
            "expires_at": expiry,
            "review_kind": "NATIVE_STAKE",
            "enforcement_scope": "DIRECT_PROTOCOL",
            "estimated_fee": money(cap),
            "disclosure": "Native system transaction; no on-chain fee_limit. The current full bandwidth burn is reserved and rechecked before broadcast. "
            + (
                "Voting keeps existing representative votes and adds only the reviewed amount."
                if action == "VOTE"
                else (
                    "Staking does not generate voting income until the separately reviewed vote is confirmed."
                    if action == "STAKE"
                    else "Exit and reward claims require current chain state."
                )
            ),
            "steps": [
                {
                    "id": "stake-step-" + str(wf["cursor"]),
                    "title": {
                        "STAKE": "Stake TRX for Energy",
                        "VOTE": "Activate voting rewards",
                        "UNSTAKE": "Request unstaking",
                        "WITHDRAW": "Withdraw unlocked TRX",
                        "CLAIM": "Claim voting rewards",
                    }[action],
                    "action": kind,
                    "native_votes": value.get("votes"),
                    "amount": money(reward if action == "CLAIM" else amount),
                    "recipient": (
                        address_base58(wf["representative"]["address"])
                        if action == "VOTE"
                        else address_base58(context.wallet)
                    ),
                    "depends_on": [],
                    "status": "READY",
                    "txid": None,
                    "fee_cap": money(cap),
                    "allowance_remaining": None,
                    "error": None,
                }
            ],
        }
        projection["hash"] = digest(
            {"projection": projection, "expected": step["expected"], "before": a}
        )
        step["graph"] = deepcopy(projection)
        state["workspace"].update(graph=projection, approval=None, execution=None)
        wf["status"] = "REVIEW"
        self.project_workflow(state)

    def approve(self, context, state, payload):
        wf = state["stake_workflow"]
        step = wf["steps"][wf["cursor"]]
        g = state["workspace"]["graph"]
        if any(
            payload.get(k) != v
            for k, v in {
                "graph_id": g["id"],
                "graph_hash": g["hash"],
                "plan_hash": g["plan_hash"],
                "mandate_hash": g["mandate_hash"],
                "account": g["account"],
                "network": "nile",
            }.items()
        ):
            raise MachineError("Native approval differs from reviewed scope.")
        self.check(context, state, step)
        a = {
            "id": digest(
                {"graph": g["hash"], "session": context.session_id, "at": self.clock()}
            ),
            "graph_hash": g["hash"],
            "plan_hash": g["plan_hash"],
            "mandate_hash": g["mandate_hash"],
            "account": g["account"],
            "network": "nile",
            "expires_at": g["expires_at"],
            "status": "APPROVED",
            "consented_at": self.clock(),
        }
        step.update(approval=a, session_id=context.session_id)
        state["workspace"]["approval"] = a

    def check(self, context, state, step):
        p, t = self.policy(context, state)
        if (
            p["policy_hash"] != state["stake_workflow"]["policy_hash"]
            or self.clock() >= step["expires_at"]
        ):
            raise MachineError("Native review expired or conditions changed.")
        current = self.account(context)
        keys = (
            "balance",
            "frozenV2",
            "unfrozenV2",
            "votes",
            "latest_withdraw_time",
            "allowance",
        )
        if any(current.get(k) != step["before"].get(k) for k in keys):
            raise MachineError(
                "Native account changed; refresh the step before approval."
            )
        params = self.rpc(context, "wallet/getchainparameters")
        price = next(
            (
                v.get("value", 0)
                for v in params.get("chainParameter", [])
                if v["key"] == "getTransactionFee"
            ),
            None,
        )
        if price != step["bandwidth_price"]:
            raise MachineError("Bandwidth price changed; refresh the step.")
        if step["action"] == "VOTE":
            representative = state["stake_workflow"]["representative"]
            brokerage = self.rpc(
                context,
                "wallet/getBrokerage",
                {"address": representative["address"], "visible": False},
            ).get("brokerage")
            if brokerage != representative["brokerage_percent"]:
                raise MachineError("Representative commission changed before signing.")

    def prepare(self, context, graph_id, payload):
        version, state = self.bridge.load(context)
        wf = state.get("stake_workflow")
        g = state["workspace"].get("graph")
        a = state["workspace"].get("approval")
        if (
            not wf
            or not g
            or not a
            or graph_id != g["id"]
            or payload.get("approval_id") != a["id"]
            or payload.get("step_id") != g["steps"][0]["id"]
            or payload.get("account") != g["account"]
        ):
            raise MachineError("Current native approval required.")
        step = wf["steps"][wf["cursor"]]
        if step.get("submission") or step.get("session_id") != context.session_id:
            raise MachineError("Native step was submitted or wallet session changed.")
        self.check(context, state, step)
        tx = step.get("transaction")
        if not tx:
            if any(
                not r.get("resolution") and not r.get("broadcast_attempted")
                for r in state.get("native_requests", {}).values()
            ):
                raise MachineError("Reconcile the previous wallet request first.")
            tx = self.rpc(
                context,
                "wallet/" + ENDPOINT[step["action"]][0],
                {**step["expected"]["value"], "visible": False},
            )
            validate_unsigned(
                tx,
                step["expected"],
                int(datetime.fromisoformat(self.clock()).timestamp() * 1000),
            )
            tx = {k: tx[k] for k in ("txID", "raw_data", "raw_data_hex")}
            tx["visible"] = False
            step["transaction"] = tx
            state.setdefault("native_requests", {})[tx["txID"]] = {
                "graph_id": g["id"],
                "step_id": g["steps"][0]["id"],
                "approval_id": a["id"],
                "transaction": deepcopy(tx),
                "prepared_at": self.clock(),
                "broadcast_attempted": False,
                "adapter": "STAKE",
            }
            self.bridge.commit(context, version, state)
        validate_unsigned(
            tx,
            step["expected"],
            int(datetime.fromisoformat(self.clock()).timestamp() * 1000),
        )
        return {
            "graph_hash": g["hash"],
            "approval_id": a["id"],
            "step_id": g["steps"][0]["id"],
            "network": "nile",
            "account": g["account"],
            "expires_at": min(
                g["expires_at"],
                datetime.fromtimestamp(
                    tx["raw_data"]["expiration"] / 1000,
                    datetime.fromisoformat(self.clock()).tzinfo,
                ).isoformat(),
            ),
            "transaction": deepcopy(tx),
            "simulation": False,
            "checks": [
                {
                    "name": "Exact native action",
                    "status": "PASS",
                    "detail": "Node-built protobuf, owner, resource, representative and amount match your review.",
                },
                {
                    "name": "Current wallet and bandwidth",
                    "status": "PASS",
                    "detail": "Confirmed state and full fee reserve checked. System transactions have no TVM simulation.",
                },
            ],
        }

    def submit(self, context, payload):
        version, state = self.bridge.load(context)
        wf = state["stake_workflow"]
        step = wf["steps"][wf["cursor"]]
        g = state["workspace"]["graph"]
        a = state["workspace"]["approval"]
        if (
            not g
            or not a
            or any(
                payload.get(k) != v
                for k, v in {
                    "graph_id": g["id"],
                    "step_id": g["steps"][0]["id"],
                    "approval_id": a["id"],
                }.items()
            )
        ):
            raise MachineError("Native submission context changed.")
        tx = payload.get("signed_transaction", {})
        prepared = step.get("transaction")
        if step.get("submission"):
            if tx.get("txID") == step["submission"]["txid"]:
                return ACK
            raise MachineError("Reconcile the original native transaction.")
        if not prepared or any(
            tx.get(k) != prepared[k] for k in ("txID", "raw_data", "raw_data_hex")
        ):
            raise MachineError("Signed native transaction differs from prepared bytes.")
        if step.get("session_id") != context.session_id:
            raise MachineError("Wallet session changed.")
        validate_unsigned(
            tx,
            step["expected"],
            int(datetime.fromisoformat(self.clock()).timestamp() * 1000),
        )
        signed = normalize_wallet_signature(tx)
        if (
            recover_tron_address(
                bytes.fromhex(tx["txID"]), bytes.fromhex(signed["signature"][0])
            )
            != context.wallet
        ):
            raise MachineError("Native signature does not recover the approved wallet.")
        self.check(context, state, step)
        row = state["native_requests"][tx["txID"]]
        if row.get("resolution"):
            raise MachineError("Native request is already closed.")
        step["submission"] = {"txid": tx["txID"], "at": self.clock()}
        row["broadcast_attempted"] = True
        row["signed_hash"] = digest(signed)
        self.project_execution(
            state,
            "SUBMISSION_UNKNOWN",
            "Signed native transaction recorded; checking chain inclusion.",
        )
        self.bridge.commit(context, version, state)
        try:
            reply = self.rpc(
                context,
                "wallet/broadcasttransaction",
                {**prepared, "signature": signed["signature"]},
            )
        except (OSError, ValueError):
            return ACK
        version, state = self.bridge.load(context)
        current = state.get("stake_workflow")
        if (
            not current
            or current["id"] != wf["id"]
            or current["cursor"] != wf["cursor"]
        ):
            return ACK
        step = current["steps"][current["cursor"]]
        if step["status"] in ("POSITION_RECONCILED", "FAILED", "DISPUTED"):
            return ACK
        step["node_response"] = reply
        self.project_execution(
            state,
            "SUBMITTED" if reply.get("result") is True else "SUBMISSION_UNKNOWN",
            "Waiting for the solidified transaction and native account changes.",
        )
        self.bridge.commit(context, version, state)
        return ACK

    def project_execution(self, state, status, message):
        wf = state["stake_workflow"]
        step = wf["steps"][wf["cursor"]]
        g = state["workspace"]["graph"]
        txid = step["submission"]["txid"]
        step["status"] = status
        g["steps"][0].update(
            status="BLOCKED" if status == "DISPUTED" else status, txid=txid
        )
        state["workspace"]["execution"] = {
            "id": txid,
            "graph_id": g["id"],
            "status": status,
            "network": "nile",
            "started_at": step["submission"]["at"],
            "updated_at": self.clock(),
            "message": message,
        }
        self.project_workflow(state)

    def project_workflow(self, state):
        wf = state.get("stake_workflow")
        current_graph = (
            wf["steps"][wf["cursor"]].get("graph", {}) if wf else {}
        )
        state["workspace"]["stake_workflow"] = (
            None
            if not wf
            else {
                "id": wf["id"],
                "status": wf["status"],
                "cursor": wf["cursor"],
                "plan_hash": wf["plan"]["hash"],
                "mandate_hash": wf["policy_hash"],
                "account": current_graph.get("account"),
                "network": current_graph.get("network"),
                "steps": [
                    {
                        "action": s["action"], "status": s["status"],
                        "graph_id": s.get("graph", {}).get("id"),
                        "graph_hash": s.get("graph", {}).get("hash"),
                        "step_id": (s.get("graph", {}).get("steps") or [{}])[0].get("id"),
                        "expires_at": s.get("expires_at"),
                    } for s in wf["steps"]
                ],
                "amount": money(int(wf["amount_sun"])),
                "representative": wf["representative"]["address_base58"],
                "forecast": wf["forecast"],
            }
        )

    def reconcile(self, context, payload=None):
        from .stake_receipt import observe

        version, state = self.bridge.load(context)
        if payload:
            prior = state.get("native_requests", {}).get(payload.get("txid"))
            if (
                prior
                and prior.get("adapter") == "STAKE"
                and payload.get("graph_id") == prior["graph_id"]
                and payload.get("step_id") == prior["step_id"]
            ):
                if prior.get("resolution"):
                    return {**ACK, "resolution": prior["resolution"]}
                if payload["txid"] in state.get("stake_receipts", {}):
                    return ACK
        wf = state.get("stake_workflow")
        if not wf:
            raise MachineError("No native staking workflow recorded.")
        step = wf["steps"][wf["cursor"]]
        tx = step.get("transaction")
        g = step.get("graph")
        if not tx or not g:
            raise MachineError("No native wallet request to reconcile.")
        if payload and any(
            payload.get(k) != v
            for k, v in {
                "txid": tx["txID"],
                "graph_id": g["id"],
                "step_id": g["steps"][0]["id"],
            }.items()
        ):
            raise MachineError("Native recovery pointer changed.")
        row = state["native_requests"][tx["txID"]]
        if row.get("resolution"):
            return {**ACK, "resolution": row["resolution"]}
        if step["status"] in ("POSITION_RECONCILED", "FAILED", "DISPUTED"):
            return ACK
        proof = observe(self, context, tx)
        step["receipt_evidence"] = proof
        if proof["status"] == "EXPIRED_NOT_OBSERVED":
            row["resolution"] = {
                "txid": tx["txID"],
                "graph_id": g["id"],
                "step_id": g["steps"][0]["id"],
                "status": "EXPIRED_NOT_OBSERVED",
                "message": "Solidified expiry passed and neither node observes this transaction. Review this step again.",
            }
            step.setdefault("attempt_history", []).append(
                {
                    k: deepcopy(step[k])
                    for k in ("transaction", "submission", "receipt_evidence")
                    if k in step
                }
            )
            for k in ("transaction", "submission", "approval", "session_id"):
                step.pop(k, None)
            step["status"] = "WAITING"
            wf["status"] = "REVIEW"
            state["workspace"].update(graph=None, approval=None, execution=None)
            self.project_workflow(state)
            self.bridge.commit(context, version, state)
            return {**ACK, "resolution": row["resolution"]}
        if proof["status"] == "PENDING":
            self.bridge.commit(context, version, state)
            return ACK
        step.setdefault("submission", {"txid": tx["txID"], "at": row["prepared_at"]})
        row["broadcast_attempted"] = True
        fee = proof["fee_sun"]
        action = step["action"]
        amount = int(wf["amount_sun"])
        before = step["before"]
        after = self.account(context)
        step["post_account"] = after
        # Exact debit/credit and resource deltas prevent another wallet action
        # from being silently attributed to this receipt.
        delta = after.get("balance", 0) - before.get("balance", 0) + fee
        valid = fee <= step["fee_cap_sun"]
        if proof["status"] == "SUCCESS":
            if action == "STAKE":
                valid = (
                    valid
                    and frozen(after) - frozen(before) == amount
                    and delta == -amount
                )
            elif action == "VOTE":
                valid = (
                    valid
                    and votes(after) == step["expected"]["value"]["votes"]
                    and delta == 0
                    and frozen(after) == frozen(before)
                )
            elif action == "UNSTAKE":
                valid = (
                    valid
                    and frozen(before) - frozen(after) == amount
                    and unfreezing(after) - unfreezing(before) == amount
                    and delta == 0
                )
            elif action == "WITHDRAW":
                valid = (
                    valid
                    and delta == proof["receipt"].get("withdraw_expire_amount")
                    and delta > 0
                )
            elif action == "CLAIM":
                valid = (
                    valid
                    and delta == proof["receipt"].get("withdraw_amount")
                    and delta > 0
                )
        status = (
            "FAILED"
            if proof["status"] == "FAILED"
            else "POSITION_RECONCILED" if valid else "DISPUTED"
        )
        self.project_execution(
            state,
            status,
            (
                "Native transaction and independent account changes reconciled."
                if valid and status == "POSITION_RECONCILED"
                else (
                    "Native receipt needs investigation; no further action is enabled."
                    if status == "DISPUTED"
                    else "Native transaction failed. Its actual fee remains recorded."
                )
            ),
        )
        event = {
            "txid": tx["txID"],
            "action": action,
            "policy_hash": wf["policy_hash"],
            "fee_sun": str(fee),
            "fee_estimate_sun": str(step["fee_cap_sun"]),
            "principal_sun": str(
                amount if action == "STAKE" and status == "POSITION_RECONCILED" else 0
            ),
            "reward_sun": str(delta if action == "CLAIM" and valid else 0),
            "status": status,
            "at": datetime.fromtimestamp(
                proof["block"]["timestamp_ms"] / 1000,
                datetime.fromisoformat(self.clock()).tzinfo,
            ).isoformat(),
            "proof_hash": proof["hash"],
            "workflow_id": wf["id"],
            "position_id": (
                wf["id"]
                if action == "STAKE"
                else state.get("stake_position", {}).get("workflow_id", wf["id"])
            ),
        }
        state.setdefault("stake_receipts", {})[tx["txID"]] = event
        state.setdefault("review_spend_ledger", {})[tx["txID"]] = {
            "policy_hash": wf["policy_hash"],
            "amount": event["principal_sun"],
            "fee": str(fee),
            "action": action,
        }
        if status == "POSITION_RECONCILED":
            wf["status"] = (
                "NEXT_REVIEW" if wf["cursor"] + 1 < len(wf["steps"]) else "COMPLETE"
            )
            if action == "STAKE":
                state["stake_position"] = {
                    "amount_sun": str(amount),
                    "status": "STAKED_NOT_VOTED",
                    "representative": wf["representative"],
                    "stake_txid": tx["txID"],
                    "started_at": event["at"],
                    "forecast": deepcopy(wf["forecast"]),
                    "workflow_id": wf["id"],
                    "reward_baseline_sun": str(step.get("reward_before", 0)),
                    "external_stake": frozen(before),
                    "external_votes": votes(before),
                }
            pos = state.get("stake_position")
            if pos and action == "VOTE":
                pos.update(status="EARNING", vote_txid=tx["txID"], voted_at=event["at"])
            if pos and action == "UNSTAKE":
                pos.update(
                    status="UNFREEZING",
                    ended_at=event["at"],
                    unfreeze_at=max(
                        (
                            v.get("unfreeze_expire_time", 0)
                            for v in after.get("unfrozenV2", [])
                            if v.get("type") == "ENERGY"
                        ),
                        default=0,
                    ),
                )
            if pos and action == "WITHDRAW":
                pos.update(status="CLOSED")
        else:
            wf["status"] = status
        self.project_workflow(state)
        self.refresh_position(context, state, account=after)
        state["workspace"]["evidence"].append(
            {
                "id": "stake-" + tx["txID"],
                "network": "nile",
                "provenance": "LIVE",
                "mandate_hash": wf["policy_hash"],
                "snapshot_root": wf["market_hash"],
                "plan_hash": wf["plan"]["hash"],
                "graph_hash": g["hash"],
                "approval_id": (step.get("approval") or {}).get("id"),
                "txids": [tx["txID"]],
                "outcome": state["workspace"]["execution"]["message"],
                "model_id": None,
                "model_approval_evidence": None,
                "flows": [],
                "energy": {
                    "status": "UNAVAILABLE",
                    "wh": None,
                    "basis": "Native bandwidth transaction; no physical energy measurement.",
                },
                "calculation": {"native_stake": deepcopy(step)},
            }
        )
        self.bridge.commit(context, version, state)
        return ACK

    def next(self, context, state):
        wf = state.get("stake_workflow")
        if not wf:
            raise MachineError("No staking workflow to continue.")
        step = wf["steps"][wf["cursor"]]
        if step["status"] == "POSITION_RECONCILED" and wf["cursor"] + 1 < len(
            wf["steps"]
        ):
            wf["cursor"] += 1
        elif step["status"] == "FAILED":
            history = step.setdefault("attempt_history", [])
            history.append(
                {k: deepcopy(v) for k, v in step.items() if k != "attempt_history"}
            )
            for key in (
                "transaction",
                "submission",
                "approval",
                "session_id",
                "receipt_evidence",
                "post_account",
            ):
                step.pop(key, None)
            step["status"] = "WAITING"
        elif step.get("transaction") or step.get("submission"):
            raise MachineError(
                "Reconcile the existing native request before continuing."
            )
        p, _ = self.policy(context, state)
        wf["policy_hash"] = p["policy_hash"]
        self.review(context, state)

    def cancel(self, context, state):
        wf = state.get("stake_workflow")
        if not wf:
            return
        if any(
            (s.get("submission") or s.get("transaction"))
            and s["status"] not in ("FAILED", "EXPIRED_NOT_OBSERVED")
            for s in wf["steps"]
        ):
            raise MachineError(
                "A signed or completed step cannot be cancelled. Reconcile it, then request unstaking."
            )
        wf["status"] = "CANCELLED"
        state["workspace"].update(graph=None, approval=None, execution=None)
        self.project_workflow(state)

    def lifecycle(self, context, state, payload):
        action = payload.get("action")
        pos = state.get("stake_position")
        old = state.get("stake_workflow")
        if (
            action not in ("UNSTAKE", "WITHDRAW", "CLAIM")
            or not pos
            or pos["status"] == "CLOSED"
            or pos["status"] == "REWARDS_PENDING"
            and action != "CLAIM"
        ):
            raise MachineError(
                "An open tracked native stake or pending rewards are required."
            )
        if old and any(
            s.get("transaction") and s["status"] not in TERMINAL for s in old["steps"]
        ):
            raise MachineError("Reconcile the previous native request first.")
        if action == "UNSTAKE" and pos["status"] == "UNFREEZING":
            raise MachineError("Unstaking was already requested.")
        if action == "WITHDRAW" and pos["status"] != "UNFREEZING":
            raise MachineError("Request unstaking before withdrawal.")
        p, t = self.policy(context, state)
        state.setdefault("stake_history", []).append(deepcopy(old))
        state["stake_workflow"] = {
            **deepcopy(old),
            "id": "stake-"
            + digest(
                {"at": self.clock(), "action": action, "policy": p["policy_hash"]}
            )[:24],
            "policy_hash": p["policy_hash"],
            "status": "REVIEW",
            "cursor": 0,
            "steps": [{"action": action, "status": "WAITING"}],
        }
        self.review(context, state)

    def refresh_position(self, context, state, account=None):
        pos = state.get("stake_position")
        if not pos:
            return
        a = account or self.account(context)
        at = self.clock()
        amount = int(pos["amount_sun"])
        status = pos["status"]
        reward = self.rpc(
            context,
            "walletsolidity/getReward",
            {"address": context.wallet, "visible": False},
        ).get("reward")
        if type(reward) is not int or reward < 0:
            raise MachineError("Live native reward amount unavailable.")
        if status in ("CLOSED", "REWARDS_PENDING"):
            status = (
                "REWARDS_PENDING" if type(reward) is int and reward > 0 else "CLOSED"
            )
            pos["status"] = status
        state["workspace"]["balances"] = [
            money(a.get("balance", 0)),
            *[b for b in state["workspace"]["balances"] if b["symbol"] != "TRX"],
        ]
        positions = [
            p for p in state["workspace"]["positions"] if p["id"] != "nile-native-stake"
        ]
        if status != "CLOSED":
            positions.append(
                {
                    "id": "nile-native-stake",
                    "product": PRODUCT,
                    "protocol": "TRON Native",
                    "network": "nile",
                    "provenance": "LIVE",
                    "principal": money(0 if status == "REWARDS_PENDING" else amount),
                    "current_value": money(
                        reward if status == "REWARDS_PENDING" else amount
                    ),
                    "debt": money(0),
                    "exit_status": {
                        "STAKED_NOT_VOTED": "Continue to voting to activate rewards.",
                        "EARNING": "Request unstaking, wait the chain delay, then withdraw.",
                        "REWARDS_PENDING": "Principal returned; voting rewards remain available to claim.",
                        "UNFREEZING": "Unstaking requested; withdraw after "
                        + datetime.fromtimestamp(
                            pos.get("unfreeze_at", 0) / 1000,
                            datetime.fromisoformat(at).tzinfo,
                        ).isoformat(),
                    }[status],
                    "receipt_txid": pos["stake_txid"],
                }
            )
        state["workspace"]["positions"] = positions
        receipts = [
            r
            for r in state.get("stake_receipts", {}).values()
            if r.get("position_id") == pos["workflow_id"]
        ]
        fees = sum(int(r["fee_sun"]) for r in receipts)
        realized = sum(int(r["reward_sun"]) for r in receipts)
        # Wallet-wide rewards cannot be attributed if pre-existing votes/stakes exist.
        complete = (
            type(reward) is int
            and not pos["external_stake"]
            and not pos["external_votes"]
            and not int(pos["reward_baseline_sun"])
        )
        if status in ("EARNING", "STAKED_NOT_VOTED") and frozen(a) != amount:
            complete = False
        if status == "EARNING" and votes(a) != [
            {
                "vote_address": pos["representative"]["address"],
                "vote_count": amount // 10**6,
            }
        ]:
            complete = False
        accrued = reward if complete else None
        elapsed = (
            0
            if not pos.get("voted_at")
            else max(
                0,
                min(
                    pos["forecast"]["horizon_seconds"],
                    int(
                        (
                            min(
                                datetime.fromisoformat(at),
                                datetime.fromisoformat(pos.get("ended_at", at)),
                            )
                            - datetime.fromisoformat(pos["voted_at"])
                        ).total_seconds()
                    ),
                ),
            )
        )
        # Use the reviewed fee reserve recorded for each receipt; no current rate substitution.
        forecast_now = (
            D(pos["forecast"]["gross"]) * elapsed / pos["forecast"]["horizon_seconds"]
            - D(sum(int(r.get("fee_estimate_sun", 0)) for r in receipts)) / 10**6
        )
        state["workspace"]["stake_position"] = {
            "status": status,
            "amount": money(amount),
            "representative": pos["representative"]["address_base58"],
            "as_of": at,
            "forecast_net": {
                "value": pos["forecast"]["net"],
                "symbol": "TRX",
                "decimals": 6,
            },
            "forecast_to_date": {
                "value": decstr(forecast_now.quantize(D("0.000001"))),
                "symbol": "TRX",
                "decimals": 6,
            },
            "net_return_pct": (
                decstr(
                    (D(realized + accrued - fees) / amount * 100).quantize(
                        D("0.000001")
                    )
                )
                if accrued is not None
                else None
            ),
            "fees": money(fees),
            "realized": money(realized) if complete else None,
            "accrued": money(accrued) if accrued is not None else None,
            "net_income": (
                money(realized + accrued - fees) if accrued is not None else None
            ),
            "basis": "Native staking only. Forecast holds initial rates constant. Rewards are wallet-wide; attribution is withheld for pre-existing votes or untracked account changes.",
            "unfreeze_at": pos.get("unfreeze_at"),
        }
