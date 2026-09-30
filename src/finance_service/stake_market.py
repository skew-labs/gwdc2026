"""Network-bound native staking observations and replayable voting forecasts.

No energy rental or mainnet incentive is credited to a Nile position. The
forecast holds present votes, brokerage and block rewards constant; it is not
an observed or guaranteed APY. Every read remains in the committed evidence.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from copy import deepcopy
from economic_machine.values import MachineError, digest, decstr
from economic_machine.tron_sources import parse_raw, address_hex, address_base58
from economic_machine.capabilities import (
    normalize_capability,
    capability_hash,
    product_id,
)
from .machine_observations import RPC

PRODUCT = "tron.native.stake"
SOURCE = "https://developers.tron.network/docs/reward-calculation"


def blocks_per_year(params):
    interval = params.get("getMaintenanceTimeInterval")
    if type(interval) is not int or interval < 6000:
        raise MachineError("Current maintenance interval required.")
    # 3-second slots; two slots are skipped at each network-specific maintenance.
    return (Decimal(86400) / 3 - Decimal(86400000) / interval * 2) * 365


def rpc(request, network, path, body=None):
    value = parse_raw(
        request(RPC[network] + "/" + path, body or {}),
        max_bytes=(
            8388608 if path.endswith(("getnowblock", "getblockbynum")) else 1048576
        ),
    )
    if value.get("Error") or value.get("error"):
        raise MachineError("Native staking observation unavailable: " + path)
    return value


def read(request, clock, context):
    if context.network not in RPC:
        raise MachineError("Unsupported staking network.")
    jobs = {
        "parameters": ("wallet/getchainparameters", {}),
        "witnesses": ("wallet/listwitnesses", {}),
        "account": (
            "walletsolidity/getaccount",
            {"address": context.wallet, "visible": False},
        ),
        "head": ("walletsolidity/getnowblock", {}),
    }
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            k: pool.submit(rpc, request, context.network, p, b)
            for k, (p, b) in jobs.items()
        }
        captured = {k: f.result() for k, f in futures.items()}
    witnesses = sorted(
        captured["witnesses"].get("witnesses", []),
        key=lambda w: (-w.get("voteCount", 0), w.get("address", "")),
    )[:127]
    if len(witnesses) < 27 or any(
        type(w.get("voteCount")) is not int or w["voteCount"] <= 0 for w in witnesses
    ):
        raise MachineError("Complete positive top-127 vote observations required.")
    active = [w for w in witnesses[:27] if w.get("isJobs") is True]
    if len(active) < 1:
        raise MachineError("No active representative observed.")
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            address_hex(w["address"]): pool.submit(
                rpc,
                request,
                context.network,
                "wallet/getBrokerage",
                {"address": address_hex(w["address"]), "visible": False},
            )
            for w in active
        }
        captured["brokerages"] = {k: f.result() for k, f in futures.items()}
    captured["observed_at"] = clock()
    captured["network"] = context.network
    captured["wallet"] = context.wallet
    return normalize(captured, clock())


def normalize(evidence, at):
    age = (
        datetime.fromisoformat(at) - datetime.fromisoformat(evidence["observed_at"])
    ).total_seconds()
    if not 0 <= age <= 300:
        raise MachineError("Staking observations expired.")
    head = evidence["head"]
    header = head.get("block_header", {}).get("raw_data", {})
    if (
        not 0
        <= datetime.fromisoformat(at).timestamp() * 1000 - header.get("timestamp", 0)
        <= 300000
    ):
        raise MachineError("Solidified staking head is stale.")
    account = evidence["account"]
    if address_hex(account.get("address")) != evidence["wallet"]:
        raise MachineError("Staking wallet identity mismatch.")
    params = {
        r["key"]: r.get("value", 0)
        for r in evidence["parameters"].get("chainParameter", [])
    }
    for k in (
        "getWitnessPayPerBlock",
        "getWitness127PayPerBlock",
        "getTransactionFee",
        "getUnfreezeDelayDays",
        "getMaintenanceTimeInterval",
    ):
        if type(params.get(k)) is not int or params[k] <= 0:
            raise MachineError("Current staking parameter missing: " + k)
    if params.get("getAllowNewReward") != 1:
        raise MachineError("Unsupported native reward distribution version.")
    witnesses = sorted(
        evidence["witnesses"]["witnesses"],
        key=lambda w: (-w.get("voteCount", 0), w.get("address", "")),
    )[:127]
    if len(witnesses) != 127 or any(
        type(w.get("voteCount")) is not int or w["voteCount"] <= 0 for w in witnesses
    ):
        raise MachineError("Complete positive top-127 witnesses required.")
    total = sum(w["voteCount"] for w in witnesses)
    rates = []
    with localcontext() as ctx:
        ctx.prec = 48
        for w in witnesses[:27]:
            address = address_hex(w["address"])
            b = evidence["brokerages"].get(address, {}).get("brokerage")
            if w.get("isJobs") is not True or type(b) is not int or not 0 <= b <= 100:
                continue
            # Candidate's incremental votes are accounted for when quoting an amount.
            rates.append(
                {
                    "address": address,
                    "address_base58": address_base58(address),
                    "brokerage_percent": b,
                    "votes": str(w["voteCount"]),
                    "total_top127_votes": str(total),
                }
            )
        if not rates:
            raise MachineError("No verified representative brokerage available.")
        for r in rates:
            r["annual_fraction"] = forecast_rate(r, params, 0)
        selected = max(
            rates, key=lambda r: (Decimal(r["annual_fraction"]), r["address"])
        )
    result = {
        "network": evidence["network"],
        "observed_at": evidence["observed_at"],
        "expires_at": min(
            (
                datetime.fromisoformat(evidence["observed_at"]) + timedelta(seconds=300)
            ).isoformat(),
            datetime.fromtimestamp(
                header["timestamp"] / 1000 + 300, datetime.fromisoformat(at).tzinfo
            ).isoformat(),
        ),
        "block": str(header["number"]),
        "block_id": head["blockID"],
        "representative": selected,
        "parameters": params,
        "wallet_balance_sun": str(account.get("balance", 0)),
        "unfreeze_days": params["getUnfreezeDelayDays"],
        "forecast_basis": "Constant current voting rewards, representative brokerage and vote weights; simple APR, no compounding. Current network maintenance pauses included; missed blocks may reduce income. Energy rental income excluded. Future votes, commission and rewards may change.",
        "source_url": SOURCE,
        "evidence": deepcopy(evidence),
    }
    result["hash"] = digest(result)
    return result


def forecast_rate(representative, params, added_votes):
    with localcontext() as ctx:
        ctx.prec = 48
        r = representative
        share = (Decimal(100) - r["brokerage_percent"]) / 100
        vote = Decimal(params["getWitness127PayPerBlock"]) / (
            Decimal(r["total_top127_votes"]) + added_votes
        )
        block = (
            Decimal(params["getWitnessPayPerBlock"])
            / 27
            / (Decimal(r["votes"]) + added_votes)
        )
        return decstr(
            ((vote + block) * share * blocks_per_year(params) / 10**6).quantize(
                Decimal("1e-18")
            )
        )


def enrich(snapshot, market):
    result = deepcopy(snapshot)
    evidence_hash = market["hash"]
    cap = normalize_capability(
        {
            "schema_version": "economic-product-capability-1",
            "chain": "TRON",
            "network": snapshot["network"],
            "contract": None,
            "protocol": "tron-native",
            "protocol_version": "stake-v2",
            "action": "STAKE",
            "token": {"asset": "TRX", "address": None, "decimals": 6},
            "stages": {
                s: {
                    "status": "SUPPORTED" if s in ("read", "quote") else "UNSUPPORTED",
                    "evidence_hash": evidence_hash,
                }
                for s in ("read", "quote", "simulate", "execute", "reconcile")
            },
        }
    )
    result["products"][PRODUCT] = {
        "product_id": PRODUCT,
        "identity_hash": product_id(cap),
        "capability_hash": capability_hash(cap),
        "capability": cap,
        "market_status": "active",
        "new_supply_allowed": True,
    }
    result["facts"]["tron.chain.getUnfreezeDelayDays"] = {
        "value": str(market["unfreeze_days"]),
        "unit": "days",
        "quality": "VALID",
        "availability": "AVAILABLE",
        "received_at": market["observed_at"],
        "observed_at": market["observed_at"],
        "block": market["block"],
        "capture_hash": evidence_hash,
        "source_id": "tron-stake-rpc",
        "state_eligible": False,
        "withheld_reasons": ["MULTI_CALL_NON_ATOMIC_READ"],
    }
    result["stake_market"] = deepcopy(market)
    result["base_snapshot"] = deepcopy(snapshot)
    result.pop("snapshot_hash", None)
    result["snapshot_hash"] = digest(result)
    return result


class StakeAssembler:
    def __init__(self, base):
        self.base = base
        self.config = base.config

    def verify(self, snapshot):
        base = self.base.verify(snapshot["base_snapshot"])
        market = snapshot["stake_market"]
        replay = normalize(market["evidence"], market["observed_at"])
        if replay != market or enrich(base, replay) != snapshot:
            raise MachineError("Native staking evidence replay mismatch.")
        return snapshot
