"""Native Stake 2.0 plans using the existing cashflow and allocation compiler."""

from .stake_codec import BANDWIDTH_BYTES
from decimal import Decimal as D
from economic_machine.plan_compiler import compare_plans, REQUEST_VERSION
from economic_machine.values import MachineError, decstr, digest
from .stake_market import PRODUCT, read, enrich, StakeAssembler, forecast_rate
from .native_execution import money


def compare(bridge, context, state, record):
    t = record["mandate"]["terms"]
    at = bridge.clock()
    if context.network != "tron-nile" or t["base_asset"] != "TRX":
        raise MachineError("Native staking rollout currently requires Nile TRX.")
    if not {"STAKE", "VOTE"}.issubset(t["allowed_actions"]):
        raise MachineError(
            "Explicit staking and representative voting permission required. Edit and confirm conditions."
        )
    assumptions = state.get("planning_assumptions")
    if not assumptions:
        raise MachineError("Review loss and cost assumptions first.")
    market = read(bridge.observations.request, bridge.clock, context)
    observation = bridge.observations.read(context, "TRX")
    snapshot = enrich(observation["snapshot"], market)
    assembler = StakeAssembler(bridge.observations.nile_native_assembler)
    capital = D(t["capital"][0]["amount"])
    params = market["parameters"]
    # Reserve full bandwidth burn on each of stake, vote, claim rewards,
    # unstake and withdraw, including eventual exit. No future resource credit.
    per = D(BANDWIDTH_BYTES * params["getTransactionFee"]) / 10**6
    quote = {
        "product_id": PRODUCT,
        "principal": "1",
        "prices_base": {"TRX": "1"},
        "costs_base": {
            "entry": decstr(per * 2),
            "exit": decstr(per * 3),
            "conversion": "0",
            "network": "0",
        },
        "redemption_seconds": market["unfreeze_days"] * 86400,
        "reward_claim_seconds": None,
        "reward_haircut_bps": 10000,
        "exit_tranches": None,
        "vault": None,
        "resources": None,
        "native": {
            "voting_rate": {
                "annual_fraction": decstr(
                    (
                        D(forecast_rate(market["representative"], params, int(capital)))
                        * D(
                            max(
                                0,
                                t["horizon_seconds"]
                                - params["getMaintenanceTimeInterval"] // 1000,
                            )
                        )
                        / D(t["horizon_seconds"])
                    ).quantize(D("1e-18"))
                ),
                "convention": "SIMPLE_APR",
                "day_count": "ACT_365F",
            },
            "lock_remaining_seconds": 0,
            "resource_recovery_seconds": 0,
            "window": None,
            "rental": None,
        },
    }
    template = {
        "product_id": PRODUCT,
        "max_bps": 10000,
        "current_bps": 0,
        "daily_loss_bps": assumptions["daily_loss_bps"],
        "stress_loss_bps": {"user_stress": assumptions["stress_loss_bps"]},
        "quote": quote,
    }
    req = {
        "schema_version": REQUEST_VERSION,
        "snapshot_hash": snapshot["snapshot_hash"],
        "grid_step_bps": 100,
        "min_plan_distance_bps": 1000,
        "max_plan_age_seconds": 300,
        "scenarios": ["user_stress"],
        "product_templates": [template],
    }
    core = compare_plans(record, snapshot, req, assembler=assembler, at=bridge.clock())
    expiry = min(observation["expires_at"], market["expires_at"])
    out = {
        "id": core["comparison_hash"],
        "network": "nile",
        "mandate_hash": record["policy_hash"],
        "snapshot_root": snapshot["snapshot_hash"],
        "expires_at": expiry,
        "status": "INFEASIBLE",
        "plans": [],
        "reason": "No two native staking allocations meet the confirmed limits and full entry/exit costs.",
        "usdd_vault": {
            "status": "UNAVAILABLE",
            "reason": "Nile Vault USDD and the JustLend destination token are incompatible; no cross-network substitution.",
        },
        "math_version": "economic-plan-comparison-2 / native-voting-1",
        "adapter_version": "native-stake-v2-1",
        "search_scope": "Native Stake + voting and wallet cash; complete 1% allocation grid, rounded down to whole TRX votes. Distinct plans differ by at least 10%. Current native jTRX remains available through JustLend-only conditions. sTRX is excluded until its Nile yield and complete exit cost can be verified.",
        "candidate_count": core["enumerated_candidates"],
        "eligible_candidates": core["eligible_candidates"],
        "exclusion_histogram": core["exclusion_histogram"],
    }
    if core["status"] == "COMPARISON_READY":
        out.update(status="READY", reason=None)
        for p in core["plans"]:
            row = p["cashflows"][0]
            principal = row["accounting"]["principal_base"]
            m = lambda v: {"value": str(v), "symbol": "TRX", "decimals": 6}
            out["plans"].append(
                {
                    "id": p["name"],
                    "hash": p["plan_hash"],
                    "title": (
                        "More cash available"
                        if p["name"] == "CONSERVATIVE"
                        else "Higher projected voting income"
                    ),
                    "summary": "Stake TRX and vote for the named representative. Voting income is variable; Energy rental income is excluded.",
                    "allocations": [
                        {
                            "product": PRODUCT,
                            "protocol": "TRON Native",
                            "amount": m(principal),
                            "share_bps": p["weights_bps"][PRODUCT],
                            "kind": "STAKE",
                            "exit_description": f"Unstake, then wait the current {market['unfreeze_days']}-day chain delay and claim. Future delay changes may apply.",
                            "participation_terms": "Vote for "
                            + market["representative"]["address_base58"]
                            + "; current representative commission "
                            + str(market["representative"]["brokerage_percent"])
                            + "%. Both stake and vote require separate wallet signatures.",
                            "base_yield": m(row["detail"]["voting"]["income"]),
                            "incentive_rewards": None,
                            "costs": [
                                {
                                    "label": "Full bandwidth reserve: stake + vote + reward claim + unstake + withdrawal",
                                    "amount": m(row["accounting"]["total_cost_base"]),
                                }
                            ],
                        },
                        {
                            "product": "Wallet cash",
                            "protocol": "Wallet",
                            "amount": m(p["cash_amount"]),
                            "share_bps": p["cash_bps"],
                            "kind": "CASH",
                            "exit_description": "Immediately available wallet cash.",
                            "participation_terms": "Entry and exit reserves excluded.",
                            "base_yield": None,
                            "incentive_rewards": None,
                            "costs": [],
                        },
                    ],
                    "expected_net_return": m(p["net_income_base"]),
                    "estimated_fees": m(p["total_cost_base"]),
                    "immediate_cash": m(p["cash_amount"]),
                    "recoverable_cash": [
                        {
                            "days": x["after_seconds"] // 86400,
                            "amount": m(x["available"]),
                            "evidence": "Current chain unfreeze delay and confirmed cash requirements.",
                        }
                        for x in p["liquidity_checks"]
                    ],
                    "risks": [
                        market["forecast_basis"],
                        "TRX price exposure remains. Unstaking has a waiting period. Voting takes effect at a maintenance boundary.",
                        "Forecast excludes Energy sale income; resource credits are not counted as cash.",
                    ],
                    "eligible": True,
                    "violations": [],
                }
            )
    state["observation"] = {**observation, "snapshot": snapshot, "expires_at": expiry}
    state["workspace"].update(
        balances=observation["balances"],
        snapshots=observation["snapshots"]
        + [
            {
                "id": "native-voting",
                "network": "nile",
                "observed_at": market["observed_at"],
                "expires_at": expiry,
                "source": "TRON RPC: chain reward parameters, top-127 votes and current representative commission",
                "source_url": market["source_url"],
                "block": market["block"],
                "root": market["hash"],
                "status": "VALID",
            }
        ],
    )
    return out, {
        "kind": "NATIVE_STAKE",
        "comparison": core,
        "request": req,
        "market": market,
        "snapshot": snapshot,
    }
