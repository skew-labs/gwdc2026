import copy
import unittest
from decimal import Decimal

from economic_machine.capabilities import capability_hash, normalize_capability, product_id
from economic_machine.preflight import compile_preflight
from economic_machine.tx_graph import (ACCOUNT_VERSION, LIFECYCLE_VERSION, QUOTE_VERSION,
    ROUTE_VERSION, compile_execution_graph, compile_lifecycle_graph,
    normalize_execution_quote, verify_execution_graph)
from economic_machine.values import MachineError, digest
from economic_machine.vault_batch import assess_execution_graph_compatibility
import test_finance_plan_service as finance_plan_service


TOKEN = "41" + "11" * 20
MARKET = "41" + "22" * 20
SHARE = MARKET
WALLET = "41" + "33" * 20
ROUTER = "41" + "44" * 20
HASH = "a" * 64
AT = "2026-09-28T12:01:00+00:00"
EXPIRY = "2026-09-28T12:05:00+00:00"
SCOPE = {"tenant_id": "tenant-a", "owner_id": "owner-a", "wallet": WALLET,
         "network": "tron-mainnet"}


def cap(*, protocol="justlend", contract=MARKET, asset="USDT", token=TOKEN,
        decimals=6, action="SUPPLY", supported=True):
    return normalize_capability({"schema_version": "economic-product-capability-1",
        "chain": "TRON", "network": "tron-mainnet", "contract": contract,
        "protocol": protocol, "protocol_version": "v1", "action": action,
        "token": {"asset": asset, "address": token, "decimals": decimals},
        "stages": {stage: {"status": "SUPPORTED" if supported else "UNSUPPORTED",
                            "evidence_hash": HASH if supported else None}
                   for stage in ("read", "quote", "simulate", "execute", "reconcile")}})


def product(name="justlend.v1.jUSDT", capability=None, **extra):
    capability = capability or cap()
    return {"product_id": name, "identity_hash": product_id(capability),
            "capability_hash": capability_hash(capability), "capability": capability,
            "market_status": "active", "new_supply_allowed": True, **extra}


def snapshot(prod):
    raw = {"scope": copy.deepcopy(SCOPE), "products": {prod["product_id"]: prod}}
    raw["snapshot_hash"] = digest(raw)
    return raw


def intent(snap, prod, amount="1000"):
    payload = {"schema_version": "economic-plan-intent-1", "status": "INTENT_PREPARED",
        "scope": copy.deepcopy(SCOPE), "mandate_policy_hash": "1" * 64,
        "mandate_draft_hash": "2" * 64, "revision": 1,
        "snapshot_hash": snap["snapshot_hash"], "comparison_hash": "3" * 64,
        "plan_hash": "b" * 64, "selected_plan": "GROWTH",
        "as_of": "2026-09-28T12:00:00Z", "valid_until": EXPIRY,
        "legs": [{"product_id": prod["product_id"], "identity_hash": prod["identity_hash"],
            "capability_hash": prod["capability_hash"], "action": prod["capability"]["action"],
            "protocol": prod["capability"]["protocol"], "weight_bps": 5000,
            "principal_asset": "USDT", "principal_amount": amount,
            "required_budget_base": amount, "cashflow_assumption_hash": "4" * 64,
            "quote_hash": "5" * 64, "status": "AWAITING_TRANSACTION_GRAPH"}],
        "cash_amount": "1000", "total_cost_base": "1",
        "blockers": ["PR06_TRANSACTION_GRAPH_REQUIRED"], "execution_authority": "NONE",
        "signature_status": "NOT_REQUESTED", "chain_status": "NOT_SUBMITTED"}
    payload["intent_hash"] = digest({"domain": "economic-plan-intent-1", "payload": payload})
    return payload


def asset(symbol, token, decimals, amount):
    return {"asset": symbol, "token_address": token, "decimals": decimals,
            "amount_base_units": str(amount)}


def account(snap, balances=None, *, positions=None, vaults=None, liquidity=None,
            allowances=None):
    return {"schema_version": ACCOUNT_VERSION, "scope": copy.deepcopy(SCOPE),
        "snapshot_hash": snap["snapshot_hash"], "observed_at": "2026-09-28T12:00:00Z",
        "valid_until": "2026-09-28T12:04:00Z",
        "balances": balances or [asset("USDT", TOKEN, 6, 1_000_000_000)],
        "allowances": allowances or [], "positions": positions or [], "vaults": vaults or [],
        "reservations": [], "market_liquidity": liquidity or []}


def quote(intent_value, prod, input_value=None, output=None, fee="1000000"):
    return {"schema_version": QUOTE_VERSION, "product_id": prod["product_id"],
        "intent_hash": intent_value["intent_hash"],
        "snapshot_hash": intent_value["snapshot_hash"],
        "input": input_value or asset("USDT", TOKEN, 6, 1_000_000_000),
        "minimum_output": output or asset("jUSDT", SHARE, 8, 99_000_000_000),
        "fee_limit_sun": fee, "quoted_at": "2026-09-28T12:00:30Z",
        "expires_at": EXPIRY, "simulation_status": "SIMULATED",
        "simulation_evidence_hash": "c" * 64}


def binding(operation, target, prod, cap_hash=None):
    return {"schema_version": "tron-action-binding-1", "operation": operation,
        "network": "tron-mainnet", "target_address": target,
        "capability_hash": cap_hash or prod["capability_hash"],
        "abi_evidence_hash": "d" * 64, "contract_code_hash": "e" * 64,
        "status": "VERIFIED", "source_url": "https://github.com/justlend/mcp-server-justlend"}


def supply_graph(*, balance=1_000_000_000, allowance=0, fee_budget="5000000",
                 supported=True):
    prod = product(capability=cap(supported=supported))
    snap, it = snapshot(prod), None
    it = intent(snap, prod)
    acc = account(snap, [asset("USDT", TOKEN, 6, balance)], allowances=[{
        "token_address": TOKEN, "spender_address": MARKET,
        "amount_base_units": str(allowance)}])
    bindings = [binding("TRC20_APPROVE", TOKEN, prod), binding("JUSTLEND_SUPPLY", MARKET, prod)]
    q = quote(it, prod)
    graph = compile_execution_graph(it, snap, acc, [q], [], bindings, at=AT,
                                    fee_budget_sun=fee_budget)
    return prod, snap, it, acc, q, bindings, graph


def simulation(step, status="SUCCESS", decoded=None, fee="100", outputs=None):
    return {"schema_version": "economic-step-simulation-1", "step_id": step["step_id"],
        "action_hash": step["action"]["action_hash"], "status": status,
        "decoded_result": decoded, "fee_estimate_sun": fee,
        "projected_outputs": outputs or [],
        "postcondition_results": [{"kind": item["kind"], "passed": True,
                                    "observed": "fixture"}
                                   for item in step["postconditions"]],
        "evidence_hash": "f" * 64, "recorded_at": AT}


def fresh(account_value):
    result = copy.deepcopy(account_value)
    result["observed_at"] = AT
    return result


class ExecutionGraphTests(unittest.TestCase):
    def test_real_pr04_service_intent_connects_to_pr06_graph_without_authority(self):
        prior = finance_plan_service.PlanServiceTests()
        prior.setUp()
        comparison = prior.service.compare(prior.context, prior.raw["mandate_id"], prior.request)
        prepared = prior.service.prepare_intent(prior.context, prior.raw["mandate_id"],
            prior.request, expected_comparison_hash=comparison["comparison_hash"],
            selected_plan="GROWTH", valid_until="2026-09-28T12:04:00Z")
        balances, quotes = [], []
        for leg in prepared["legs"]:
            prod = prior.snapshot["products"][leg["product_id"]]
            token = prod["capability"]["token"]
            amount = int(Decimal(leg["principal_amount"]) * (10 ** token["decimals"]))
            balances.append(asset(token["asset"], token["address"], token["decimals"], amount))
            quotes.append({"schema_version": QUOTE_VERSION, "product_id": leg["product_id"],
                "intent_hash": prepared["intent_hash"],
                "snapshot_hash": prepared["snapshot_hash"], "input": balances[-1],
                "minimum_output": asset("j" + token["asset"], prod["capability"]["contract"],
                                        prod["share_decimals"], 1),
                "fee_limit_sun": "1000000", "quoted_at": "2026-09-28T12:00:30Z",
                "expires_at": "2026-09-28T12:04:00Z", "simulation_status": "SIMULATED",
                "simulation_evidence_hash": "c" * 64})
        acc = account(prior.snapshot, balances)
        acc["scope"] = copy.deepcopy(prepared["scope"])
        graph = compile_execution_graph(prepared, prior.snapshot, acc, quotes, [], [], at=AT,
                                        fee_budget_sun="10000000")
        self.assertEqual(graph["intent_hash"], prepared["intent_hash"])
        self.assertEqual(graph["execution_authority"], "NONE")
        self.assertTrue(verify_execution_graph(graph, prepared, prior.snapshot, acc,
            quotes, [], [], at=AT, fee_budget_sun="10000000"))

    def test_supply_graph_reserves_then_approves_then_supplies_without_atomic_claim(self):
        prod, snap, it, acc, q, bindings, graph = supply_graph()
        self.assertEqual([s["operation"] for s in graph["steps"]],
                         ["RESERVE_BALANCE", "TRC20_APPROVE", "JUSTLEND_SUPPLY"])
        self.assertEqual(graph["execution_semantics"], "ORDERED_MULTI_TRANSACTION_NON_ATOMIC")
        self.assertEqual(graph["execution_quote_hashes"], [{"product_id": prod["product_id"],
            "quote_hash": digest(normalize_execution_quote(q))}])
        self.assertTrue(verify_execution_graph(graph, it, snap, acc, [q], [], bindings,
                                               at=AT, fee_budget_sun="5000000"))
        compatibility = assess_execution_graph_compatibility(graph)
        self.assertFalse(compatibility["compatible"])

    def test_missing_one_thousand_asset_is_detected_without_route(self):
        usdd_token = "41" + "55" * 20
        prod = product("justlend.v1.jUSDD", cap(asset="USDD", token=usdd_token))
        snap = snapshot(prod)
        prepared = intent(snap, prod)
        prepared["legs"][0]["principal_asset"] = "USDD"
        prepared.pop("intent_hash")
        prepared["intent_hash"] = digest({"domain": "economic-plan-intent-1",
                                           "payload": prepared})
        acc = account(snap, [asset("USDD", usdd_token, 6, 0)])
        q = quote(prepared, prod, input_value=asset("USDD", usdd_token, 6, 1_000_000_000),
                  output=asset("jUSDD", MARKET, 8, 99_000_000_000))
        graph = compile_execution_graph(prepared, snap, acc, [q], [], [], at=AT,
                                        fee_budget_sun="5000000")
        self.assertEqual(graph["status"], "BLOCKED")
        self.assertIn("FUNDING_SHORTFALL:USDD", graph["blockers"])
        self.assertFalse(any(step["operation"] == "CONVERT" for step in graph["steps"]))

    def test_conversion_requires_quote_capacity_abi_and_code_evidence(self):
        prod = product()
        snap, it = snapshot(prod), None
        it = intent(snap, prod)
        acc = account(snap, [asset("USDD", "41" + "55" * 20, 6, 1_010_000_000)])
        route = {"schema_version": ROUTE_VERSION, "route_id": "usdd-usdt-1",
            "network": "tron-mainnet", "scope": copy.deepcopy(SCOPE),
            "intent_hash": it["intent_hash"], "snapshot_hash": it["snapshot_hash"],
            "provider_id": "psm-route-fixture", "source_url": "https://usdd.io/",
            "from_amount": asset("USDD", "41" + "55" * 20, 6,
                                                               1_010_000_000),
            "minimum_to_amount": asset("USDT", TOKEN, 6, 1_000_000_000),
            "capacity_from_base_units": "2000000000", "target_address": ROUTER,
            "function_selector": "swap(uint256,uint256)", "parameter_hex": "00" * 64,
            "fee_limit_sun": "1000000", "quoted_at": "2026-09-28T12:00:30Z",
            "expires_at": EXPIRY, "status": "SIMULATED", "evidence_hash": "1" * 64,
            "abi_evidence_hash": "2" * 64, "contract_code_hash": "3" * 64}
        binds = [binding("TRC20_APPROVE", TOKEN, prod),
                 binding("JUSTLEND_SUPPLY", MARKET, prod)]
        graph = compile_execution_graph(it, snap, acc, [quote(it, prod)], [route], binds,
                                        at=AT, fee_budget_sun="5000000")
        conversion = next(step for step in graph["steps"] if step["operation"] == "CONVERT")
        approval = next(step for step in graph["steps"] if step["operation"] == "TRC20_APPROVE")
        supply = next(step for step in graph["steps"] if step["operation"] == "JUSTLEND_SUPPLY")
        self.assertIn(conversion["step_id"], approval["depends_on"])
        self.assertIn(approval["step_id"], supply["depends_on"])
        self.assertEqual(supply["inputs"][0]["availability"],
                         "DEPENDENCY_OUTPUT_UNCONFIRMED")
        self.assertEqual(graph["asset_conservation"][1]["remaining_observed_base_units"], "0")
        manifest = compile_preflight(graph, fresh(acc), [
            simulation(conversion, outputs=[asset("USDT", TOKEN, 6, 1_000_000_000)]),
            simulation(approval, decoded=True),
            simulation(supply, decoded="0", outputs=[asset(
                "jUSDT", SHARE, 8, 99_000_000_000)])], at=AT)
        supply_report = next(row for row in manifest["step_reports"]
                             if row["step_id"] == supply["step_id"])
        self.assertIn("EXPECTED_OUTPUT_IS_NOT_OBSERVED_BALANCE", supply_report["reasons"])
        bad = copy.deepcopy(route)
        bad["contract_code_hash"] = None
        with self.assertRaisesRegex(MachineError, "quote, ABI and code"):
            compile_execution_graph(it, snap, acc, [quote(it, prod)], [bad], binds,
                                    at=AT, fee_budget_sun="5000000")
        replayed = copy.deepcopy(route)
        replayed["intent_hash"] = "9" * 64
        with self.assertRaisesRegex(MachineError, "another scope, intent or snapshot"):
            compile_execution_graph(it, snap, acc, [quote(it, prod)], [replayed], binds,
                                    at=AT, fee_budget_sun="5000000")

    def test_binding_from_another_capability_cannot_activate_product(self):
        prod, snap, it, acc, q, bindings, _ = supply_graph()
        bindings[1]["capability_hash"] = "9" * 64
        with self.assertRaisesRegex(MachineError, "belong"):
            compile_execution_graph(it, snap, acc, [q], [], bindings, at=AT,
                                    fee_budget_sun="5000000")

    def test_legacy_supply_and_unsupported_capability_stay_blocked(self):
        prod, snap, it, acc, q, bindings, graph = supply_graph(supported=False)
        self.assertIn("CAPABILITY_EXECUTION_UNSUPPORTED:" + prod["product_id"], graph["blockers"])
        prod["new_supply_allowed"] = False
        snap = snapshot(prod)
        it = intent(snap, prod)
        acc["snapshot_hash"] = snap["snapshot_hash"]
        q = quote(it, prod)
        graph = compile_execution_graph(it, snap, acc, [q], [], bindings, at=AT,
                                        fee_budget_sun="5000000")
        self.assertIn("NEW_SUPPLY_DISABLED:" + prod["product_id"], graph["blockers"])

    def test_strx_stake_uses_native_trx_input_and_payable_call(self):
        strx_cap = cap(contract=MARKET, asset="sTRX", token=MARKET, decimals=18,
                       action="STAKE")
        prod = product("justlend.strx", strx_cap)
        snap = snapshot(prod)
        prepared = intent(snap, prod, amount="1000")
        prepared["legs"][0]["principal_asset"] = "TRX"
        prepared.pop("intent_hash")
        prepared["intent_hash"] = digest({"domain": "economic-plan-intent-1",
                                           "payload": prepared})
        acc = account(snap, [asset("TRX", None, 6, 1_000_000_000)])
        q = quote(prepared, prod, input_value=asset("TRX", None, 6, 1_000_000_000),
                  output=asset("sTRX", MARKET, 18, 900_000_000_000_000_000_000))
        graph = compile_execution_graph(prepared, snap, acc, [q], [],
            [binding("STRX_STAKE", MARKET, prod)], at=AT, fee_budget_sun="5000000")
        stake = next(step for step in graph["steps"] if step["operation"] == "STRX_STAKE")
        self.assertEqual(stake["action"]["call_value_sun"], "1000000000")
        self.assertEqual(stake["action"]["parameter_hex"], "")

    def test_approve_success_supply_revert_and_fee_spike_are_separate_failures(self):
        _, _, _, acc, _, _, graph = supply_graph()
        approve, supply = graph["steps"][1], graph["steps"][2]
        sims = [simulation(approve, decoded=True),
                simulation(supply, status="REVERTED", decoded=None,
                           outputs=[asset("jUSDT", SHARE, 8, 99_000_000_000)])]
        manifest = compile_preflight(graph, fresh(acc), sims, at=AT)
        reports = {row["step_id"]: row for row in manifest["step_reports"]}
        self.assertEqual(reports[approve["step_id"]]["simulation_status"], "SUCCESS")
        self.assertIn("SIMULATION_REVERTED", reports[supply["step_id"]]["reasons"])
        self.assertIn("CONFIRMED_DEPENDENCY_AND_FRESH_SNAPSHOT_REQUIRED",
                      reports[supply["step_id"]]["reasons"])
        expensive = [simulation(approve, decoded=True, fee="1000001"),
                     simulation(supply, decoded="0", outputs=[asset(
                         "jUSDT", SHARE, 8, 99_000_000_000)])]
        expensive_manifest = compile_preflight(graph, fresh(acc), expensive, at=AT)
        self.assertIn("STEP_FEE_LIMIT_EXCEEDED",
                      expensive_manifest["step_reports"][1]["reasons"])
        stale = copy.deepcopy(acc)
        with self.assertRaisesRegex(MachineError, "at or after graph creation"):
            compile_preflight(graph, stale, sims, at=AT)

    def test_protocol_return_values_are_checked_even_when_rpc_says_success(self):
        _, _, _, acc, _, _, graph = supply_graph()
        approve, supply = graph["steps"][1], graph["steps"][2]
        manifest = compile_preflight(graph, fresh(acc), [simulation(approve, decoded=False),
            simulation(supply, decoded="7", outputs=[asset(
                "jUSDT", SHARE, 8, 99_000_000_000)])], at=AT)
        self.assertIn("PROTOCOL_FALSE_RETURN", manifest["step_reports"][1]["reasons"])
        self.assertIn("PROTOCOL_ERROR_CODE", manifest["step_reports"][2]["reasons"])


class LifecycleTests(unittest.TestCase):
    def lifecycle_request(self, prod, snap, operation, item_in, output, **updates):
        request = {"schema_version": LIFECYCLE_VERSION, "scope": copy.deepcopy(SCOPE),
            "snapshot_hash": snap["snapshot_hash"], "product_id": prod["product_id"],
            "identity_hash": prod["identity_hash"], "capability_hash": prod["capability_hash"],
            "operation": operation, "input": item_in, "minimum_output": output,
            "recipient": WALLET, "resource": None, "vault_id": None,
            "claim_after": None, "requested_at": "2026-09-28T12:00:00Z",
            "valid_until": "2026-09-29T12:00:00Z"}
        request.update(updates)
        return request

    def test_liquidity_drop_is_caught_again_at_preflight(self):
        prod = product()
        snap = snapshot(prod)
        acc = account(snap, positions=[{"product_id": prod["product_id"],
            "shares_base_units": "1000", "underlying_base_units": "1000"}],
            liquidity=[{"product_id": prod["product_id"], "amount_base_units": "1000"}])
        req = self.lifecycle_request(prod, snap, "JUSTLEND_REDEEM_UNDERLYING",
            asset("USDT", TOKEN, 6, 500), asset("USDT", TOKEN, 6, 500))
        graph = compile_lifecycle_graph(prod, acc, req,
            [binding("JUSTLEND_REDEEM_UNDERLYING", MARKET, prod)], at=AT,
            fee_limit_sun="1000")
        fresh = copy.deepcopy(acc)
        fresh["observed_at"] = AT
        fresh["market_liquidity"][0]["amount_base_units"] = "100"
        manifest = compile_preflight(graph, fresh, [simulation(graph["steps"][0], decoded="0",
            outputs=[asset("USDT", TOKEN, 6, 500)])], at=AT)
        self.assertIn("FRESH_MARKET_LIQUIDITY_SHORTFALL",
                      manifest["step_reports"][0]["reasons"])

    def test_delayed_unstake_and_insufficient_vault_repay_are_explicit(self):
        strx_cap = cap(contract=MARKET, asset="sTRX", token=MARKET, decimals=18,
                       action="STAKE")
        strx = product("justlend.strx", strx_cap)
        snap = snapshot(strx)
        acc = account(snap, positions=[{"product_id": strx["product_id"],
            "shares_base_units": "1000", "underlying_base_units": "1000"}])
        req = self.lifecycle_request(strx, snap, "STRX_UNSTAKE",
            asset("sTRX", MARKET, 18, 500), asset("TRX", None, 6, 400))
        graph = compile_lifecycle_graph(strx, acc, req,
            [binding("STRX_UNSTAKE", MARKET, strx)], at=AT, fee_limit_sun="1000")
        self.assertIn("DELAYED_CLAIM_TIME_REQUIRED", graph["blockers"])
        queued = copy.deepcopy(req)
        queued["claim_after"] = "2026-09-28T13:00:00Z"
        queued_graph = compile_lifecycle_graph(strx, acc, queued,
            [binding("STRX_UNSTAKE", MARKET, strx),
             binding("STRX_CLAIM", MARKET, strx)], at=AT, fee_limit_sun="1000")
        self.assertEqual([step["operation"] for step in queued_graph["steps"]],
                         ["STRX_UNSTAKE", "STRX_CLAIM"])
        self.assertEqual(queued_graph["steps"][1]["depends_on"], ["step-001"])
        self.assertIn("CONFIRMED_WITHDRAWAL_AND_FRESH_SNAPSHOT_REQUIRED",
                      queued_graph["steps"][1]["blockers"])
        claim = copy.deepcopy(req)
        claim["operation"] = "STRX_CLAIM"
        claim["claim_after"] = "2026-09-28T12:00:30Z"
        claim_graph = compile_lifecycle_graph(strx, acc, claim,
            [binding("STRX_CLAIM", MARKET, strx)], at=AT, fee_limit_sun="1000")
        self.assertIn("CLAIMABLE_WITHDRAWAL_OBSERVATION_REQUIRED", claim_graph["blockers"])

        vault_cap = cap(protocol="usdd", contract=MARKET, asset="USDD", token=TOKEN,
                        action="MINT_USDD")
        vault = product("usdd.vault.TRX-A", vault_cap, ilk="TRX-A",
                        economic_role="COLLATERAL_DEBT")
        vsnap = snapshot(vault)
        vacc = account(vsnap, [asset("USDD", TOKEN, 6, 100)], vaults=[{
            "product_id": vault["product_id"], "vault_id": "7",
            "collateral_base_units": "1000", "debt_base_units": "1000"}])
        vreq = self.lifecycle_request(vault, vsnap, "USDD_VAULT_REPAY",
            asset("USDD", TOKEN, 6, 500), asset("USDD", TOKEN, 6, 500), vault_id="7")
        vgraph = compile_lifecycle_graph(vault, vacc, vreq, [], at=AT,
                                         fee_limit_sun="1000")
        self.assertIn("USDD_REPAY_BALANCE_SHORTFALL", vgraph["blockers"])
        self.assertIn("VAULT_EXACT_PROXY_JOIN_EXIT_CALLDATA_REQUIRED", vgraph["blockers"])


if __name__ == "__main__":
    unittest.main()
