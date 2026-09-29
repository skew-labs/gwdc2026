"""Bounded live reads and projections of the existing PR03/04 calculation core."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from urllib.request import Request, build_opener
from urllib.parse import urlsplit

from economic_machine.snapshot_assembly import SnapshotAssembler, PUBLIC
from economic_machine.tron_sources import SOURCES, MAX_BYTES, make_capture, parse_raw, address_base58
from economic_machine.tron_registry_read import _NoRedirect
from economic_machine.plan_compiler import compare_plans, REQUEST_VERSION
from economic_machine.values import MachineError, canonical, digest, decstr

RPC = {"tron-nile": "https://nile.trongrid.io", "tron-mainnet": "https://api.trongrid.io"}


class MachineObservations:
    def __init__(self, clock, request=None):
        self.clock = clock
        self.request = request or self._request
        self.config = json.loads((Path(__file__).resolve().parents[2] / "config/tron_product_registry.json").read_text())
        self.mainnet_assembler = SnapshotAssembler(self.config)
        from .nile_observations import NileAssembler
        self.nile_assembler = NileAssembler()
        self.nile_native_assembler = NileAssembler("TRX")
        self.assembler = self.mainnet_assembler

    def assembler_for(self, network, base_asset="USDT"):
        return (self.nile_native_assembler if base_asset == "TRX" else self.nile_assembler) if network == "tron-nile" else self.mainnet_assembler

    @staticmethod
    def _request(url, body=None):
        req = Request(url, data=None if body is None else canonical(body), headers={
            "User-Agent": "EconomicMachine/1.0", "Content-Type": "application/json", "Accept": "application/json"})
        block_read=urlsplit(url).path.rsplit("/",1)[-1] in {"getblock", "getblockbynum", "getblockbyid", "getnowblock"}
        limit=8*MAX_BYTES if block_read else MAX_BYTES
        with build_opener(_NoRedirect()).open(req, timeout=10) as response:
            data = response.read(limit + 1)
        parse_raw(data,max_bytes=limit)
        return data

    def _capture(self, name):
        try:
            raw = self.request(SOURCES[name][2])
            return make_capture(name, raw, received_at=self.clock())
        except Exception:
            return make_capture(name, None, received_at=self.clock(), error="TRANSPORT_ERROR")

    def read(self, context, base_asset="USDT"):
        scope = context.authorize(self.clock())
        network = scope["network"].removeprefix("tron-")
        root = RPC[scope["network"]]
        address = address_base58(scope["wallet"])
        balances, snapshots = [], []
        def account():
            return self.request(root + "/walletsolidity/getaccount", {"address": address, "visible": True})
        with ThreadPoolExecutor(max_workers=10) as pool:
            account_future = pool.submit(account)
            futures = [pool.submit(self._capture, name) for name in PUBLIC] if network == "mainnet" else []
            captures = [f.result() for f in futures]
            try:
                account_raw = account_future.result()
                value = parse_raw(account_raw)
                if value.get("address"):
                    balance = value.get("balance", 0)  # protobuf scalar default in an existing account
                    if type(balance) is not int or balance < 0:
                        raise MachineError("invalid node balance")
                    balances.append({"value": decstr(Decimal(balance) / 10**6), "symbol": "TRX", "decimals": 6})
                    if network == "nile":
                        trn = next((x["value"] for x in value.get("assetV2", []) if x["key"] == "1005416"), None)
                        if type(trn) is int and trn >= 0:
                            balances.append({"value": decstr(Decimal(trn) / 10**6), "symbol": "TRN (TRC10 #1005416)", "decimals": 6})
                account_status = "VALID" if value.get("address") else "UNAVAILABLE"
                account_hash = digest(value)
            except Exception:
                account_status, account_hash = "UNAVAILABLE", digest({"status": "account-unavailable"})
        at = self.clock()
        expiry = (datetime.fromisoformat(at) + timedelta(minutes=5)).isoformat()
        snapshots.append(dict(id="wallet-account", network=network, observed_at=at, expires_at=expiry,
            source="TRON solidity account read" if account_status == "VALID" else "Account unavailable or not activated",
            source_url=root + "/walletsolidity/getaccount", block=None, root=account_hash, status=account_status))
        snapshot = None
        if captures:
            for c in captures:
                snapshots.append(dict(id=c["source_id"], network=network, observed_at=c["received_at"],
                    expires_at=expiry, source=c["source_id"] + " · HTTP receipt time; source block unknown",
                    source_url=c["url"], block=None, root=c["capture_hash"], status="UNAVAILABLE" if c["error"] else "VALID"))
            snapshot = self.mainnet_assembler.assemble(captures, as_of=at, scope=scope)
            snapshots.append(dict(id="combined-market", network=network, observed_at=at,
                expires_at=expiry, source="Combined public observations; execution verification pending",
                source_url=None, block=None, root=snapshot["snapshot_hash"], status="VALID"))
        if network == "nile":
            snapshot = self.assembler_for(context.network, base_asset).read(scope, self.clock, self.request)
            if snapshot["wallet_token_balance"] is not None and base_asset != "TRX":
                balances.append(self.amount(snapshot["wallet_token_balance"], base_asset))
            at = self.clock()
            expiry = (datetime.fromisoformat(at) + timedelta(minutes=5)).isoformat()
            snapshots.append(dict(id="nile-justlend-rpc", network=network, observed_at=at, expires_at=expiry,
                source="Nile JustLend RPC · independent calls; source block not pinned",
                source_url=RPC["tron-nile"], block=None, root=snapshot["snapshot_hash"],
                status="VALID" if snapshot["wallet_token_balance"] is not None else "UNAVAILABLE"))
        return {"balances": balances, "snapshots": snapshots, "snapshot": snapshot,
                "observed_at": at, "expires_at": expiry}

    @staticmethod
    def amount(value, symbol="USDT"):
        return {"value": str(value), "symbol": symbol, "decimals": 6 if symbol in {"USDT", "TRX"} else 18}

    def compare(self, context, aggregate, observation, assumptions=None):
        record = aggregate["revisions"][-1]
        network = context.network.removeprefix("tron-")
        snapshot = observation["snapshot"]
        result = dict(id="comparison-" + digest({"policy": record["policy_hash"], "observed_at": observation["observed_at"]})[:24],
            network=network, mandate_hash=record["policy_hash"], snapshot_root=snapshot["snapshot_hash"] if snapshot else digest(observation),
            expires_at=observation["expires_at"], status="INFEASIBLE", plans=[], reason=None,
            usdd_vault={"status": "EXCLUDED", "reason": "Borrowing was not permitted by the confirmed mandate." if not record["mandate"]["terms"]["borrowing"]["consent"] else "A bound debt, collateral and repayment quote is required for this vault."},
            math_version="economic-plan-comparison-2 · positive net entry", adapter_version="machine-bridge-1",
            search_scope="Complete 10% allocation grid; source-bound rates with explicitly reviewed cost, liquidity and risk assumptions. No execution quote is implied.")
        if not assumptions:
            result["reason"] = "Enter cost, redemption and risk assumptions before calculating plans. Unknown inputs cannot be replaced with zero."
            return result, None
        base_asset = record["mandate"]["terms"]["base_asset"]
        amount = lambda value: self.amount(value, base_asset)
        if base_asset != "USDT" and not (base_asset == "TRX" and network == "nile"):
            result["reason"] = "The current cashflow core requires USDT as its valuation base. A verified cross-asset valuation is needed for this mandate."
            return result, None
        prices = {base_asset: "1"}
        market = next((c for c in snapshot["captures"] if c["source_id"] == "justlend_markets_v1"), None)
        if market and market["raw_text"]:
            rows = parse_raw(market["raw_text"].encode())["data"]["tokenList"]
            by_asset = {r["underlyingSymbol"]: r for r in rows}
            with localcontext() as ctx:
                ctx.prec = 48
                usdt_trx = Decimal(by_asset["USDT"]["underlyingPriceInTrx"])
                if usdt_trx > 0:
                    prices["TRX"] = decstr((1 / usdt_trx).quantize(Decimal("1e-18")))
                    prices["USDD"] = decstr((Decimal(by_asset["USDD"]["underlyingPriceInTrx"]) / usdt_trx).quantize(Decimal("1e-18")))
        templates = []
        names = ["justlend.v1.jUSDT", "justlend.v1.jUSDD"] if network == "mainnet" else ["justlend.v1.j" + base_asset]
        if network == "nile":
            result["search_scope"] = "Nile-specific JustLend " + base_asset + " and wallet cash, complete " + ("1%" if base_asset == "TRX" else "10%") + " allocation grid. USDD excluded: a verified Nile valuation is unavailable. Incentive proceeds excluded."
        for name in names:
            quote = {"product_id": name, "principal": "1", "prices_base": prices,
                "costs_base": {k: assumptions[k + "_cost"] for k in ("entry", "exit", "conversion", "network")},
                "redemption_seconds": assumptions["redemption_seconds"], "reward_claim_seconds": None,
                "reward_haircut_bps": 10000, "exit_tranches": None, "native": None, "vault": None, "resources": None}
            templates.append({"product_id": name, "max_bps": 10000, "current_bps": 0,
                "daily_loss_bps": assumptions["daily_loss_bps"], "stress_loss_bps": {"user_stress": assumptions["stress_loss_bps"]}, "quote": quote})
        request = {"schema_version": REQUEST_VERSION, "snapshot_hash": snapshot["snapshot_hash"],
            "grid_step_bps": 100 if base_asset == "TRX" else 1000, "min_plan_distance_bps": 100 if base_asset == "TRX" else 1000, "max_plan_age_seconds": 300,
            "scenarios": ["user_stress"], "product_templates": templates}
        core = compare_plans(record, snapshot, request, assembler=self.assembler_for(context.network, base_asset), at=self.clock())
        result["id"] = core["comparison_hash"]
        if core["status"] != "COMPARISON_READY":
            if core["exclusion_histogram"].get("NET_BENEFIT_NOT_POSITIVE"):
                result["reason"] = "Keep your cash for now. Two distinct investments with positive projected returns after entry, operating and exit costs were not available within your conditions. Your limits have not been changed."
            else:
                result["reason"] = "No two allocations satisfy these conditions: " + json.dumps(core["exclusion_histogram"], sort_keys=True)
        else:
            result["status"] = "READY"
            for p in core["plans"]:
                allocations = []
                for row in p["cashflows"]:
                    name = row["product_id"]
                    cap = snapshot["products"][name]["capability"]
                    detail = row.get("detail", {})
                    allocations.append(dict(product=name, protocol=cap["protocol"],
                        amount=amount(row["accounting"]["principal_base"]), share_bps=p["weights_bps"][name],
                        kind="SUPPLY", exit_description="Assumed settlement within " + str(assumptions["redemption_seconds"]) + " seconds, subject to observed pool cash; not a guaranteed exit.",
                        participation_terms="Supply underlying tokens to the network-specific JustLend market. Rates may change. Incentives are excluded from forecast proceeds.",
                        base_yield=self.amount(detail["base"]["income"], cap["token"]["asset"]) if "base" in detail else None,
                        incentive_rewards=None, costs=[{"label": "Reviewed cost assumption", "amount": amount(row["accounting"]["total_cost_base"])}]))
                allocations.append(dict(product="Wallet cash", protocol="Wallet", amount=amount(p["cash_amount"]), share_bps=p["cash_bps"], kind="CASH", exit_description="Uninvested wallet cash", participation_terms="Retained in your wallet", base_yield=None, incentive_rewards=None, costs=[]))
                result["plans"].append(dict(id=p["name"], hash=p["plan_hash"], title="Lower stress exposure" if p["name"] == "CONSERVATIVE" else "Higher projected income", summary="Calculated from current public observations and your reviewed assumptions. Execution requires separately verified chain state and transaction quotes.", allocations=allocations, expected_net_return=amount(p["net_income_base"]), estimated_fees=amount(p["total_cost_base"]), immediate_cash=amount(p["cash_amount"]), recoverable_cash=[{"days": x["after_seconds"] // 86400, "amount": amount(x["available"]), "evidence": "PR04 liquidity check against the reviewed settlement assumption"} for x in p["liquidity_checks"]], risks=["Source observation time and block are not supplied by the public APIs.", "Stress and daily-loss rates are explicit assumptions, not measured probabilities.", "Asset price changes, smart contract and liquidity risk remain."], eligible=p["eligible"], violations=p["reason_codes"]))
        return result, {"comparison": core, "request": request}
