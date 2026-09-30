"""Read-only native staking checks, preserving account alert/worker semantics."""

from .stake_codec import BANDWIDTH_BYTES
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal as D
from economic_machine.values import MachineError, digest, decstr
from economic_machine.mandate import reserve_amount
from .stake_market import read, forecast_rate
from .stake_execution import frozen, votes


def refresh(reviews, context, state, p, source, notify):
    b = reviews.bridge
    w = state["workspace"]
    at = b.clock()
    pos = state["stake_position"]
    review = {
        "id": "review-" + digest({"at": at, "scope": context.scope})[:24],
        "network": "nile",
        "product_label": "Native Stake",
        "policy_hash": p["policy_hash"] if p else None,
        "observed_at": at,
        "expires_at": (datetime.fromisoformat(at) + timedelta(minutes=5)).isoformat(),
        "source": source,
        "status": "DATA_UNAVAILABLE",
        "reason": "",
        "checks": [],
        "hold": None,
        "alternatives": [],
        "suggested": None,
        "execution_authority": "NONE",
        "snapshot_hash": None,
        "block": None,
        "limitations": [
            "Native staking and wallet cash. Reward forecast holds current chain parameters, votes and commission constant. Energy rental income excluded.",
            "Unstaking requires a waiting period and two separately signed transactions. No automatic trade.",
            "Full bandwidth burn is reserved; resource credits are not assumed. Native reward claims have a 24-hour cooldown.",
        ],
    }
    try:
        market = read(b.observations.request, b.clock, context)
        a = market["evidence"]["account"]
        b.stake.refresh_position(context, state, account=a)
        review.update(
            expires_at=market["expires_at"],
            snapshot_hash=market["hash"],
            block=market["block"],
        )
        state["stake_watch_evidence"] = market
        if any(x["product"] != "tron.native.stake" for x in w["positions"]):
            raise MachineError(
                "Combined native and lending rebalancing is not enabled; each position can be managed separately."
            )
        if not p:
            raise MachineError(
                "Confirm current conditions for staking compliance checks."
            )
        t = p["mandate"]["terms"]
        capital = D(t["capital"][0]["amount"])
        principal = (
            D(0) if pos["status"] == "REWARDS_PENDING" else D(pos["amount_sun"]) / 10**6
        )
        cash = D(a.get("balance", 0)) / 10**6
        if t["base_asset"] != "TRX":
            raise MachineError("TRX conditions are required for native staking review.")
        expiry = min(
            datetime.fromisoformat(p.get("confirmed_at", pos["started_at"]))
            + timedelta(seconds=t["horizon_seconds"]),
            datetime.fromisoformat(pos["started_at"])
            + timedelta(seconds=t["horizon_seconds"]),
        )
        remaining = max(0, int((expiry - datetime.fromisoformat(at)).total_seconds()))
        cost = D(BANDWIDTH_BYTES * market["parameters"]["getTransactionFee"]) / 10**6
        fees = sum(
            D(e["fee"]) / 10**6
            for e in state.get("review_spend_ledger", {}).values()
            if e["policy_hash"] == p["policy_hash"]
        )
        cash = min(cash, max(D(0), capital - principal - fees))
        cash_floor = reserve_amount(t["immediate_cash"], str(capital))
        checks = []
        if cash < cash_floor:
            checks.append("CASH_BELOW_CONFIRMED_MINIMUM")
        if (
            principal
            > capital * D(t["protocol_caps_bps"].get("tron-native", 0)) / 10000
        ):
            checks.append("NATIVE_PROTOCOL_CAP_EXCEEDED")
        if principal > capital * D(t["price_exposure_caps_bps"].get("TRX", 0)) / 10000:
            checks.append("TRX_EXPOSURE_CAP_EXCEEDED")
        if fees + cost * (2 if pos["status"] == "UNFREEZING" else 3) > D(
            t["limits"]["fee_amount"]["amount"]
        ):
            checks.append("FEE_BUDGET_EXCEEDED")
        elapsed = max(
            0,
            int(
                (
                    datetime.fromisoformat(at)
                    - datetime.fromisoformat(p["confirmed_at"])
                ).total_seconds()
            ),
        )
        for withdrawal in t["withdrawals"]:
            due = withdrawal["after_seconds"] - elapsed
            required = reserve_amount(withdrawal["minimum"], str(capital))
            recoverable = cash + (
                principal
                if due >= market["unfreeze_days"] * 86400 and pos["status"] == "EARNING"
                else D(0)
            )
            if recoverable < required:
                checks.append("WITHDRAWAL_LIQUIDITY_SHORTFALL")
        assumptions = (p.get("review_inputs") or {}).get("assumptions") or {}
        if "daily_loss_bps" not in assumptions or "stress_loss_bps" not in assumptions:
            raise MachineError("Current risk assumptions are unavailable.")
        if principal * D(assumptions["daily_loss_bps"]) / 10000 > D(
            t["limits"]["daily_loss"]["amount"]
        ):
            checks.append("DAILY_LOSS_LIMIT_EXCEEDED")
        if principal * D(assumptions["stress_loss_bps"]) / 10000 > D(
            t["limits"]["stress_loss"]["amount"]
        ):
            checks.append("STRESS_LOSS_LIMIT_EXCEEDED")
        if pos["status"] == "UNFREEZING":
            review.update(
                status="HOLD",
                reason="Unstaking is in progress. Withdraw unlocked TRX after the chain waiting period.",
            )
            remaining = 0
        elif pos["status"] == "REWARDS_PENDING":
            review.update(
                status="HOLD",
                reason="Your principal is back in the wallet. Review a reward claim for the remaining voting income.",
            )
        elif pos["status"] == "STAKED_NOT_VOTED":
            review.update(
                status="ADJUST",
                reason="Your TRX is staked but voting is not yet confirmed. Continue to voting to activate the reviewed reward route.",
            )
        else:
            if frozen(a) != int(pos["amount_sun"]) or votes(a) != [
                {
                    "vote_address": pos["representative"]["address"],
                    "vote_count": int(pos["amount_sun"]) // 10**6,
                }
            ]:
                raise MachineError(
                    "Native stake or votes changed outside this workflow. Income attribution and new actions require review."
                )
            witnesses = sorted(
                market["evidence"]["witnesses"]["witnesses"],
                key=lambda v: -v.get("voteCount", 0),
            )[:127]
            rep = next(
                (
                    v
                    for v in witnesses[:27]
                    if v["address"] == pos["representative"]["address"]
                    and v.get("isJobs") is True
                ),
                None,
            )
            broker = (
                market["evidence"]["brokerages"]
                .get(pos["representative"]["address"], {})
                .get("brokerage")
            )
            if rep is None or broker is None:
                raise MachineError(
                    "The selected representative is no longer an active verified producer. Review your votes."
                )
            rate = D(
                forecast_rate(
                    {
                        **pos["representative"],
                        "votes": str(rep["voteCount"]),
                        "total_top127_votes": str(
                            sum(v["voteCount"] for v in witnesses)
                        ),
                        "brokerage_percent": broker,
                    },
                    market["parameters"],
                    0,
                )
            )
            gross = principal * rate * remaining / (365 * 86400)
            exit_cost = cost * 3
            hold = {
                "action": "HOLD",
                "position": decstr(principal),
                "wallet_cash": decstr(cash),
                "gross_income": decstr(gross),
                "change_cost": "0",
                "exit_reserve": decstr(exit_cost),
                "future_exit_estimate": decstr(exit_cost),
                "net_income": decstr(gross - exit_cost),
                "eligible": not checks,
                "reasons": checks,
            }
            # Exit now and exit at the horizon each need the same native actions.
            # A negative remaining net alone does not make earlier exit cheaper.
            exit_option = {
                "action": "REDEEM",
                "position": "0",
                "wallet_cash": decstr(cash),
                "gross_income": "0",
                "change_cost": decstr(exit_cost),
                "exit_reserve": "0",
                "future_exit_estimate": "0",
                "net_income": decstr(-exit_cost),
                "net_improvement": decstr(-gross),
                "benefit_over_hold": decstr(-gross),
                "additional_cost": "0",
                "eligible": cash >= exit_cost and "UNSTAKE" in t["allowed_actions"],
                "reasons": [] if cash >= exit_cost else ["FEE_RESERVE_MISSING"],
                "delta": decstr(principal),
            }
            review.update(
                hold=hold,
                alternatives=[exit_option],
                rate={
                    "annual_fraction": decstr(rate),
                    "convention": "SIMPLE_APR",
                    "day_count": "ACT_365F",
                },
            )
            review.update(
                status="HOLD",
                reason="Keep the current stake under these conditions. Early withdrawal adds no projected income advantage after the remaining exit costs.",
            )
        if checks:
            review.update(
                status="POLICY_BREACH",
                reason="The live native position is outside confirmed conditions. Review the checks and request unstaking if appropriate.",
                checks=checks,
            )
        if (
            datetime.fromisoformat(at) >= datetime.fromisoformat(t["expires_at"])
            or remaining == 0
            and pos["status"] != "UNFREEZING"
        ):
            review.update(
                status="POLICY_INACTIVE",
                reason="Conditions or the investment horizon expired. Renew conditions before another signed action.",
            )
        if w.get("execution") and w["execution"]["status"] not in (
            "POSITION_RECONCILED",
            "FAILED",
        ):
            review.update(
                status="PENDING_EXECUTION",
                reason="Reconcile the recorded native transaction before considering another action.",
            )
        review.update(
            remaining_seconds=remaining,
            capital=decstr(capital),
            cash_floor=decstr(cash_floor),
            paid_fees=decstr(fees),
            remaining_budget=decstr(max(D(0), capital - fees)),
        )
    except (MachineError, KeyError, ValueError, OSError) as exc:
        review.update(status="DATA_UNAVAILABLE", reason=str(exc), suggested=None)
    if datetime.fromisoformat(b.clock()) >= datetime.fromisoformat(
        review["expires_at"]
    ):
        review.update(
            status="DATA_UNAVAILABLE",
            reason="Staking review expired during its reads. Check again.",
        )
    w["portfolio_review"] = review
    state["portfolio_reviews"] = (
        state.get("portfolio_reviews", []) + [deepcopy(review)]
    )[-50:]
    if notify:
        reviews.notifications(w, review, b.clock())
    return review
