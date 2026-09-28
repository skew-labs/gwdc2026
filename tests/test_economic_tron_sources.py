"""Adversarial source replay tests. All wallet values are synthetic fixtures."""

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import localcontext
from pathlib import Path

from economic_machine.snapshot_assembly import SnapshotAssembler
from economic_machine.tron_products import discover_products
from economic_machine.tron_rpc_snapshot import capture_rpc, replay_rpc
from economic_machine.tron_sources import (address_base58, address_hex, capture_from_store,
    make_capture, parse_raw, source_url, validate_capture)
from economic_machine.state import apply_delta
from economic_machine.values import MachineError, digest
from finance_service.context import AuthenticatedContext
from finance_service.snapshot_service import SnapshotService
from finagent.normalize import normalize
from finagent.store import Store


ROOT = Path(__file__).resolve().parents[1]
AT = "2026-09-28T12:00:00+00:00"
CONFIG = json.loads((ROOT / "config/tron_product_registry.json").read_text())
WALLET, PROXY, URN = ("41" + digit * 40 for digit in ("1", "2", "3"))
SCOPE = {"tenant_id": "tenant-a", "owner_id": "owner-a", "wallet": WALLET, "network": "tron-mainnet"}
ADDRESSES = [
    ("jUSDT", "USDT", "TXJgMdjVX5dKiQaUi9QobwNxtSQaFqccvd", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", 6),
    ("jUSDD", "USDD", "TKFRELGGoRgiayhwJTNNLqCNjFoLBh3Mnf", "TXDk8mbtRbXeYuMNS83CfKPaYYT8XWv9Hz", 18),
    ("jUSDDOLD", "USDDOLD", "TX7kybeP6UwTBRHLNPYmswFESHfyjm9bAS", "TPYmHEhy5n8TCEfYGqW2rPxsghSfzghPDn", 18),
]
STRX = "TU3kjFuhtEo42tsCBtfYUAZxoqQ4yuSLQ5"


def directory():
    def record(address):
        value = address_hex(address)
        return {"network": "mainnet", "address": {"base58": address_base58(value),
                "hex_tron": "0x" + value, "hex_evm": "0x" + value[2:]}}
    entries = {symbol: {"symbol": symbol, "underlying_symbol": asset,
        "status": "legacy" if symbol == "jUSDDOLD" else "active", "decimals": 8,
        "underlying_decimals": decimals, "delegator": record(contract), "underlying": record(token)}
        for symbol, asset, contract, token, decimals in ADDRESSES}
    entries["jsTRX"] = {"underlying": record(STRX), "underlying_decimals": 18}
    return {"networks": {"mainnet": {"jtokens": entries, "strx": {"staked_trx": record(STRX)}}}}


def market_data():
    return {"code": 0, "data": {"tokenList": [{"address": contract, "symbol": symbol,
        "underlyingSymbol": asset, "underlyingAddress": token, "underlyingDecimal": decimals,
        "supplyRate": "0.02", "borrowRate": "0.08", "exchangeRate": "1.25", "cash": "1000",
        "totalBorrows": "500", "totalSupply": "1200"}
        for symbol, asset, contract, token, decimals in ADDRESSES]}}


def capture(source, payload, *, mode="FIXTURE", scope=None, at=AT):
    return make_capture(source, json.dumps(payload).encode(), mode=mode, scope=scope, received_at=at)


def fixtures(mode="FIXTURE", wallet=False):
    captures = [capture("justlend_contracts", directory(), mode=mode),
                capture("justlend_markets_v1", market_data(), mode=mode),
                capture("justlend_usdd_rewards_v1", {"code": 0, "data": {ADDRESSES[0][2]: {"USDD": "0"}}}, mode=mode),
                capture("justlend_strx_v1", {"code": 0, "data": {"stakeInfo": {
                    "strxAddress": STRX, "decimal": "18", "underlyingDecimal": "6", "exchangeRate": "1.1",
                    "supplyRate": "0.06", "totalUnderlying": "1100", "totalSupply": "1000", "reserves": "0"},
                    "rentInfo": {"priceFor10KEnergByRent": "0.8", "totalDelegatedEnergyTrx": "20"}}}, mode=mode),
                capture("usdd_tron_collateral", {"code": 0, "data": {"items": [
                    {"chain": "tron", "vaultType": ilk, "contractAddress": contract, "collateralType": 1,
                     "debt": "101", "mintedUSDD": "100", "line": "1000000", "lockedValue": "500",
                     "stabilityFee": "0.005", "minCollateralRatio": "1.2", "collateralRatio": "5", "apy": None}
                    for ilk, contract in CONFIG["usdd_joins"].items()]}}, mode=mode),
                capture("usdd_vault_config", {"code": 0, "data": {"items": [
                    {"ilk": "TRX-A", "dust": "1000", "maxMinted": "1000000", "minCollateralRatio": "1.2", "stabilityFee": "0.005"}]}}, mode=mode),
                capture("usdd_earn_apy", {"code": 0, "data": {"tronApy": "0.04"}}, mode=mode)]
    if wallet:
        def page(row):
            return {"code": 0, "data": {"totalCount": 1, "totalPage": 1, "list": [row]}}
        captures += [capture("justlend_account_v1", page({"address": address_base58(WALLET), "tokens": [
            {"address": ADDRESSES[0][2], "underlyingSymbol": "USDT", "supplyBalanceUnderlying": "0",
             "borrowBalanceUnderlying": "0", "supplyBalanceJtoken": "0"}]}), mode=mode, scope=SCOPE),
            capture("justlend_strx_account_v1", page({"address": address_base58(WALLET), "availableWithdrawAmount": "0",
                "unstakingAmount": "3", "sTRXBalance": "2"}), mode=mode, scope=SCOPE),
            capture("justlend_rental_account_v1", page({"renter": address_base58(WALLET), "receiver": address_base58(PROXY),
                "rentType": "Energy", "delegatedAmount": "100", "rentRemainAmount": "5", "rentAmountPerDay": "1"}), mode=mode, scope=SCOPE)]
    return captures


def modify(captures, source, edit):
    for index, old in enumerate(captures):
        if old["source_id"] == source:
            payload = validate_capture(old)
            edit(payload)
            captures[index] = capture(source, payload, mode=old["mode"], scope=old["scope"], at=old["received_at"])
            return
    raise AssertionError("missing source")


def rehash(raw):
    raw["capture_hash"] = digest({k: v for k, v in raw.items() if k != "capture_hash"})
    return raw


def rpc_fixture(products, *, wallet=False, mode="FIXTURE", changed_block=False, wrong_owner=False):
    count = 0
    def reader(path, payload):
        nonlocal count
        if path.endswith("getnowblock"):
            count += 1
            height = 100 + int(changed_block and count > 1)
            return json.dumps({"blockID": f"{height:016x}" + "a"*48,
                "block_header": {"raw_data": {"number": height, "timestamp": 1790596799000}}}).encode()
        if path.endswith("getaccount"):
            return json.dumps({"address": WALLET, "balance": 1000001, "frozenV2": [{"type": "ENERGY", "amount": 2000000}],
                "unfrozenV2": [{"type": "ENERGY", "unfreeze_amount": 1000000, "unfreeze_expire_time": 1791800000000}]}).encode()
        if path.endswith("getaccountresource"):
            return b'{"EnergyLimit":10000,"NetLimit":1000}'
        if path.endswith("getchainparameters"):
            return b'{"chainParameter":[{"key":"getEnergyFee","value":100},{"key":"getTransactionFee","value":1000},{"key":"getUnfreezeDelayDays","value":14}]}'
        selector, contract = payload["function_selector"], payload["contract_address"]
        decimals = next((p["capability"]["token"]["decimals"] for p in products.values()
                         if p["capability"]["contract"] == contract), 6)
        mapping = {"getCash()": [1000 * 10**decimals], "exchangeRateStored()": [125 * 10**(16 + decimals - 8)],
            "balanceOf(address)": [0], "borrowBalanceStored(address)": [0], "exchangeRate()": [11 * 10**17],
            "totalUnderlying()": [1100 * 10**6], "vat()": [int(address_hex(CONFIG["usdd_contracts"]["vat"])[2:], 16)],
            "proxies(address)": [int(PROXY[2:], 16)], "owner()": [int(WALLET[2:], 16)],
            "owns(uint256)": [int((URN if wrong_owner else PROXY)[2:], 16)], "urns(uint256)": [int(URN[2:], 16)],
            "ilks(uint256)": [int.from_bytes(b"TRX-A".ljust(32, b"\0"), "big")],
            "urns(bytes32,address)": [500*10**18, 100*10**18],
            "ilks(bytes32)": [200*10**18, 101*10**25, 2*10**27, 1000000*10**45, 10**45]}
        return json.dumps({"result": {"result": True}, "transaction": {"ret": [{"ret": "SUCCESS"}]},
            "constant_result": ["".join(f"{v:064x}" for v in mapping[selector])]}).encode()
    raw = capture_rpc(CONFIG, products, scope=SCOPE if wallet else None, vault_ids=[1] if wallet else [],
                      mode=mode, transport=reader)
    raw["received_at"] = AT
    return rehash(raw)


class SourceCaptureTests(unittest.TestCase):
    def test_base58_checksum_and_canonical_identity(self):
        for _, _, contract, token, _ in ADDRESSES:
            for address in (contract, token):
                self.assertEqual(address_base58(address_hex(address)), address)
        self.assertEqual(address_hex("0x" + WALLET), WALLET)
        for invalid in (True, "0x" + "1"*40, "41"+"0"*40, STRX[:-1]+"1", "T"*34):
            with self.subTest(invalid=invalid), self.assertRaises(MachineError):
                address_hex(invalid)

    def test_json_numbers_preserve_precision_and_reject_ambiguous_values(self):
        self.assertEqual(parse_raw(b'{"n":0.12345678901234567890123456789}')["n"], "0.12345678901234567890123456789")
        for bad in (b'{"x":1,"x":2}', b'{"x":NaN}', b'[]', b'{} '*400000):
            with self.assertRaises(MachineError):
                parse_raw(bad)

    def test_exact_bytes_commitment_including_bom(self):
        body = b'\xef\xbb\xbf{ "code":0,"data":{} }'
        raw = make_capture("usdd_earn_apy", body, received_at=AT)
        self.assertEqual(raw["raw_sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(validate_capture(raw), {"code": 0, "data": {}})

    def test_receipt_time_cannot_be_relabelled_as_source_time_even_with_new_hash(self):
        for key, value in (("observed_at", AT), ("block", {"number": 1}), ("provider_group", "independent"),
                           ("source_time_status", "KNOWN"), ("url", "http://127.0.0.1"), ("network", "tron-nile")):
            raw = fixtures()[1]
            raw[key] = value
            with self.subTest(key=key), self.assertRaises(MachineError):
                validate_capture(rehash(raw))

    def test_private_api_never_unscoped_or_other_network(self):
        with self.assertRaises(MachineError):
            source_url("justlend_account_v1", None)
        with self.assertRaises(MachineError):
            source_url("justlend_account_v1", {**SCOPE, "network": "tron-nile"})
        self.assertIn("addresses=" + address_base58(WALLET), source_url("justlend_account_v1", SCOPE))

    def test_store_replay_rejects_supply_borrow_metric_swap(self):
        with tempfile.TemporaryDirectory() as directory_name:
            store = Store(Path(directory_name))
            payload = market_data()
            markets, facts = normalize("justlend_markets_v1", payload)
            row = {"id": "justlend_markets_v1", "url": source_url("justlend_markets_v1", None)}
            store.save_snapshot(row, AT, json.dumps(payload).encode(), payload, markets, facts)
            capture_from_store(store, row["id"])
            with store.connect() as db:
                db.execute("UPDATE facts SET json_path=replace(json_path, 'supplyRate', 'borrowRate') WHERE metric_id='supply_apy'")
            with self.assertRaisesRegex(MachineError, "metric semantics"):
                capture_from_store(store, row["id"])


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.assembler = SnapshotAssembler(CONFIG)
        self.captures = fixtures()

    def assemble(self, **kwargs):
        return self.assembler.assemble(self.captures, as_of=AT, **kwargs)

    def test_same_input_replays_exact_snapshot_and_capture_order_does_not_matter(self):
        first = self.assemble()
        self.captures.reverse()
        self.assertEqual(first, self.assemble())
        self.assertEqual(first, self.assembler.verify(first))

    def test_legacy_product_is_distinct_and_not_open_for_new_supply(self):
        products = self.assemble()["products"]
        current, old = (products["justlend.v1." + symbol] for symbol in ("jUSDD", "jUSDDOLD"))
        self.assertNotEqual(current["identity_hash"], old["identity_hash"])
        self.assertFalse(old["new_supply_allowed"])
        self.assertEqual(current["capability"]["token"]["decimals"], 18)
        self.assertEqual(current["share_decimals"], 8)
        self.assertTrue(all(p["capability"]["stages"]["execute"]["status"] == "UNSUPPORTED" for p in products.values()))

    def test_valid_zero_missing_and_transport_error_remain_distinct(self):
        snap = self.assemble()
        self.assertEqual(snap["facts"]["justlend.v1.jUSDT.reward_apy"]["quality"], "VALID_ZERO")
        self.assertEqual(snap["facts"]["justlend.v1.jUSDD.reward_apy"]["quality"], "MISSING")
        self.captures = [c for c in self.captures if c["source_id"] != "justlend_strx_v1"]
        self.captures.append(make_capture("justlend_strx_v1", None, received_at=AT, mode="FIXTURE", error="TRANSPORT_ERROR"))
        self.assertEqual(self.assemble()["facts"]["justlend.strx.aggregate_apy"]["quality"], "ERROR")

    def test_api_receipt_is_not_observation_time_and_provider_count_is_not_quorum(self):
        snap = self.assemble()
        self.assertEqual(snap["provider_groups"], ["justlend", "usdd"])
        self.assertIsNone(snap["oracle_quorum"])
        self.assertFalse(snap["execution_enabled"])
        self.assertTrue(all(f["observed_at"] is None and not f["state_eligible"] for f in snap["facts"].values()))

    def test_vault_aggregate_debt_is_not_a_personal_balance_or_earn_apy(self):
        snap = self.assemble()
        fact = snap["facts"]["usdd.vault.TRX-A.type_debt"]
        self.assertEqual(fact["economic_role"], "COLLATERAL_TYPE_AGGREGATE")
        self.assertFalse(any(p.startswith("wallet.") for p in snap["facts"]))
        self.assertNotIn("usdd.vault.TRX-A.apy", snap["facts"])
        self.assertEqual(snap["facts"]["usdd.earn.apy"]["economic_role"], "READ_ONLY_UNSUPPORTED_ROUTE")

    def test_market_unit_and_identity_mismatch_rejected(self):
        for key, value in (("underlyingDecimal", 18), ("underlyingDecimal", True), ("underlyingDecimal", "6"),
            ("underlyingSymbol", "USDD"), ("underlyingAddress", ADDRESSES[1][3]), ("symbol", "jUSDD")):
            self.captures = fixtures()
            modify(self.captures, "justlend_markets_v1", lambda p: p["data"]["tokenList"][0].update({key: value}))
            with self.subTest(key=key, value=value), self.assertRaises(MachineError):
                self.assemble()

    def test_directory_network_or_address_representation_mismatch_rejected(self):
        for key, value in (("network", "nile"), ("address", {"base58": ADDRESSES[1][2], "hex_tron": WALLET, "hex_evm": "0x"+WALLET[2:]})):
            self.captures = fixtures()
            modify(self.captures, "justlend_contracts", lambda p: p["networks"]["mainnet"]["jtokens"]["jUSDT"]["delegator"].update({key: value}))
            with self.assertRaises(MachineError):
                self.assemble()

    def test_duplicate_rows_and_boolean_success_codes_rejected(self):
        for edit in (lambda p: p["data"]["tokenList"].append(copy.deepcopy(p["data"]["tokenList"][0])),
                     lambda p: p.update(code=False), lambda p: p.update(code=200)):
            self.captures = fixtures()
            modify(self.captures, "justlend_markets_v1", edit)
            with self.assertRaises(MachineError):
                self.assemble()

    def test_v2_and_duplicate_source_captures_rejected(self):
        self.captures.append(capture("justlend_markets_v2", {"code": 200, "data": {}}))
        with self.assertRaisesRegex(MachineError, "V2"):
            self.assemble()
        self.captures = fixtures() + [fixtures()[0]]
        with self.assertRaisesRegex(MachineError, "duplicate"):
            self.assemble()

    def test_future_stale_and_cross_source_skew_are_explicit(self):
        for at, expected in (("2026-09-28T12:00:01Z", "FUTURE_RECEIPT"), ("2026-09-28T10:00:00Z", "STALE_RECEIPT")):
            self.captures = fixtures()
            old = self.captures[1]
            self.captures[1] = capture(old["source_id"], validate_capture(old), at=at)
            snap = self.assemble()
            self.assertEqual(snap["source_status"][old["source_id"]], expected)
            self.assertFalse(snap["facts"]["justlend.v1.jUSDT.supply_apy"]["state_eligible"])
        self.assertIn("SOURCE_RECEIPT_SKEW", snap["unavailable"])

    def test_snapshot_metric_tamper_rejected_even_if_hash_recomputed(self):
        snap = self.assemble()
        snap["facts"]["justlend.v1.jUSDT.supply_apy"]["value"] = "0.08"
        snap["snapshot_hash"] = digest({k: v for k, v in snap.items() if k != "snapshot_hash"})
        with self.assertRaisesRegex(MachineError, "raw replay"):
            self.assembler.verify(snap)

    def test_fixture_and_live_observations_cannot_mix(self):
        self.captures[1] = capture("justlend_markets_v1", market_data(), mode="LIVE_READ")
        with self.assertRaisesRegex(MachineError, "mix"):
            self.assemble()

    def test_usdd_wrong_join_and_wrong_chain_rejected(self):
        for key, value in (("contractAddress", STRX), ("chain", "ethereum"), ("collateralType", True)):
            self.captures = fixtures()
            modify(self.captures, "usdd_tron_collateral", lambda p: p["data"]["items"][0].update({key: value}))
            with self.assertRaises(MachineError):
                self.assemble()

    def test_chain_parameters_distinguish_protobuf_zero_and_absent_key(self):
        self.captures.append(capture("tron_chain_parameters", {"chainParameter": [
            {"key": "getEnergyFee"}, {"key": "getTransactionFee", "value": 1000},
            {"key": "getRemoveThePowerOfTheGr", "value": -1}]}))
        facts = self.assemble()["facts"]
        self.assertEqual(facts["tron.chain.getEnergyFee"]["quality"], "VALID_ZERO")
        self.assertEqual(facts["tron.chain.getEnergyFee"]["normalization"], "PROTOBUF_SCALAR_DEFAULT_ZERO")
        self.assertEqual(facts["tron.chain.getUnfreezeDelayDays"]["quality"], "MISSING")
        self.assertFalse(facts["tron.chain.getTransactionFee"]["state_eligible"])

    def test_chain_parameter_boolean_and_duplicate_are_rejected(self):
        for rows in ([{"key": "getEnergyFee", "value": True}], [{"key": "getEnergyFee", "value": -1}],
                     [{"key": "getEnergyFee", "value": 1}, {"key": "getEnergyFee", "value": 1}]):
            self.captures = fixtures() + [capture("tron_chain_parameters", {"chainParameter": rows})]
            with self.assertRaises(MachineError):
                self.assemble()

    def test_wallet_zero_is_valid_but_missing_market_is_not_zero(self):
        self.captures = fixtures(wallet=True)
        snap = self.assemble(scope=SCOPE)
        self.assertEqual(snap["facts"]["wallet.justlend.v1.jUSDT.borrow"]["quality"], "VALID_ZERO")
        self.assertEqual(snap["facts"]["wallet.justlend.v1.jUSDD.borrow"]["quality"], "MISSING")
        self.assertIn("STRX_PER_REQUEST_UNLOCK_TIMES_UNKNOWN", snap["unavailable"])

    def test_wallet_filter_ignored_by_source_is_rejected(self):
        self.captures = fixtures(wallet=True)
        modify(self.captures, "justlend_account_v1", lambda p: p["data"]["list"][0].update(address=address_base58(PROXY)))
        with self.assertRaisesRegex(MachineError, "another wallet"):
            self.assemble(scope=SCOPE)

    def test_private_capture_other_tenant_and_missing_scope_rejected(self):
        self.captures = fixtures(wallet=True)
        for scope in (None, {**SCOPE, "tenant_id": "tenant-b"}):
            with self.assertRaisesRegex(MachineError, "scope"):
                self.assemble(scope=scope)

    def test_partial_wallet_pagination_rejected(self):
        self.captures = fixtures(wallet=True)
        modify(self.captures, "justlend_account_v1", lambda p: p["data"].update(totalPage=2))
        with self.assertRaisesRegex(MachineError, "pagination"):
            self.assemble(scope=SCOPE)

    def test_state_delta_invalidates_old_facts_without_writing_account_balances(self):
        previous = {"owner_id": "owner-a", "network": "tron-mainnet", "sequence": 0, "as_of": AT,
            "facts": {"tron.input.old": {"value": "7", "unit": "USDT", "quality": "VALID", "source_id": "old",
                      "source_hash": "a"*64, "observed_at": AT}}, "balances": {"USDT": "10"},
            "exposures": {}, "daily_losses": {}, "quotes": {}}
        delta = self.assembler.state_delta(self.assemble(), previous)
        state, _ = apply_delta(previous, delta)
        self.assertEqual(state["facts"]["tron.input.old"]["quality"], "MISSING")
        self.assertEqual(state["balances"], previous["balances"])
        self.assertTrue(all(f["value"] is None for f in state["facts"].values()))


class RpcSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.captures = fixtures()
        self.products = discover_products(CONFIG, self.captures[0])
        self.assembler = SnapshotAssembler(CONFIG)

    def test_rpc_units_do_not_use_float_or_ambient_decimal_precision(self):
        raw = rpc_fixture(self.products, wallet=True)
        self.assertIsNone(raw["error"])
        with localcontext() as context:
            context.prec = 3
            result = replay_rpc(raw, CONFIG, self.products)
        facts = result["observations"]
        self.assertEqual(facts["wallet.TRX.balance"]["value"], "1.000001")
        self.assertEqual(facts["justlend.v1.jUSDT.exchange_rate"]["value"], "1.25")
        self.assertEqual(facts["justlend.v1.jUSDD.exchange_rate"]["value"], "1.25")
        self.assertEqual(facts["wallet.usdd.vault.1.stored_accrued_debt"]["value"], "101")
        self.assertEqual(facts["wallet.native_stake.BANDWIDTH"]["value"], "0")
        self.assertEqual(facts["wallet.native_unstake.0.ENERGY.amount"]["value"], "1")
        self.assertEqual(result["vault_ownership"][0]["wallet"], WALLET)
        self.assertIn("VAULT_ENUMERATION_PARTIAL", result["unavailable"])

    def test_wrong_vault_owner_fails_entire_batch(self):
        raw = rpc_fixture(self.products, wallet=True, wrong_owner=True)
        self.assertEqual(raw["error"], "INCOMPLETE_RPC_READ")
        self.assertIsNone(replay_rpc(raw, CONFIG, self.products))

    def test_transcript_request_substitution_rejected_even_with_new_hash(self):
        raw = rpc_fixture(self.products)
        raw["records"][1]["payload"]["function_selector"] = "transfer(address,uint256)"
        with self.assertRaisesRegex(MachineError, "request/scope"):
            replay_rpc(rehash(raw), CONFIG, self.products)

    def test_raw_bytes_tamper_and_truncated_transcript_rejected(self):
        for kind in ("raw", "truncate", "trailing"):
            raw = rpc_fixture(self.products)
            if kind == "raw":
                raw["records"][1]["raw_text"] += " "
            elif kind == "truncate":
                raw["records"].pop()
            else:
                raw["records"].append(copy.deepcopy(raw["records"][-1]))
            with self.subTest(kind=kind), self.assertRaises(MachineError):
                replay_rpc(rehash(raw), CONFIG, self.products)

    def test_missing_vm_result_and_extra_abi_words_are_not_success(self):
        for edit in (lambda p: p["transaction"].update(ret=[{}]),
                     lambda p: p["constant_result"].__setitem__(0, p["constant_result"][0] + "0" * 64),
                     lambda p: p["transaction"].update(ret=[{"ret": "REVERT"}])):
            raw = rpc_fixture(self.products)
            record = raw["records"][1]
            payload = json.loads(record["raw_text"])
            edit(payload)
            record["raw_text"] = json.dumps(payload)
            record["raw_sha256"] = hashlib.sha256(record["raw_text"].encode()).hexdigest()
            with self.assertRaises(MachineError):
                replay_rpc(rehash(raw), CONFIG, self.products)

    def test_changed_solid_block_and_fullnode_fields_are_withheld(self):
        raw = rpc_fixture(self.products, changed_block=True)
        snap = self.assembler.assemble(self.captures, as_of=AT, rpc=raw)
        self.assertIn("SOLID_BLOCK_CHANGED", snap["facts"]["rpc.justlend.v1.jUSDT.available_cash"]["withheld_reasons"])
        self.assertIn("FULLNODE_BLOCK_UNKNOWN", snap["facts"]["rpc.tron.chain.getEnergyFee"]["withheld_reasons"])

    def test_api_rpc_disagreement_is_visible_and_not_quorum(self):
        modify(self.captures, "justlend_markets_v1", lambda p: p["data"]["tokenList"][0].update(cash="999"))
        snap = self.assembler.assemble(self.captures, as_of=AT, rpc=rpc_fixture(self.products))
        mismatch = [c for c in snap["comparisons"] if c["status"] == "DISAGREE"]
        self.assertEqual(len(mismatch), 1)
        self.assertFalse(mismatch[0]["aligned_source_block"])
        self.assertIn("SOURCE_DISAGREEMENT", snap["facts"]["rpc.justlend.v1.jUSDT.available_cash"]["withheld_reasons"])

    def test_live_mode_branch_admits_same_solid_block_only(self):
        # Synthetic transport exercises live-mode validation; no real live claim.
        captures = fixtures(mode="LIVE_READ")
        products = discover_products(CONFIG, captures[0])
        raw = rpc_fixture(products, mode="LIVE_READ")
        snap = self.assembler.assemble(captures, as_of=AT, rpc=raw)
        self.assertTrue(snap["facts"]["rpc.justlend.v1.jUSDT.available_cash"]["state_eligible"])
        self.assertFalse(snap["facts"]["rpc.tron.chain.getEnergyFee"]["state_eligible"])
        self.assertFalse(snap["execution_enabled"])

    def test_fixture_rpc_never_promoted_to_live_state(self):
        snap = self.assembler.assemble(self.captures, as_of=AT, rpc=rpc_fixture(self.products))
        self.assertTrue(all(not f["state_eligible"] for f in snap["facts"].values()))

    def test_expired_rpc_and_registry_withhold_dependent_facts(self):
        captures = fixtures(mode="LIVE_READ")
        products = discover_products(CONFIG, captures[0])
        raw = rpc_fixture(products, mode="LIVE_READ")
        raw["received_at"] = "2026-09-28T10:00:00Z"
        snap = self.assembler.assemble(captures, as_of=AT, rpc=rehash(raw))
        self.assertIn("RPC_RECEIPT_STALE_OR_FUTURE", snap["facts"]["rpc.justlend.strx.exchange_rate"]["withheld_reasons"])

    def test_wallet_rpc_delta_applies_without_touching_other_owner(self):
        captures = fixtures(wallet=True)
        products = discover_products(CONFIG, captures[0])
        snap = self.assembler.assemble(captures, as_of=AT, scope=SCOPE, rpc=rpc_fixture(products, wallet=True))
        previous = {"owner_id": "owner-a", "network": "tron-mainnet", "sequence": 0, "as_of": AT,
                    "facts": {}, "balances": {}, "exposures": {}, "daily_losses": {}, "quotes": {}}
        apply_delta(previous, self.assembler.state_delta(snap, previous, state_scope=SCOPE))
        for other in (None, {**SCOPE, "tenant_id": "tenant-b"}, {**SCOPE, "wallet": PROXY}):
            with self.assertRaisesRegex(MachineError, "scope"):
                self.assembler.state_delta(snap, previous, state_scope=other)
        previous["owner_id"] = "owner-b"
        with self.assertRaisesRegex(MachineError, "scope"):
            self.assembler.state_delta(snap, previous)

    def test_snapshot_service_uses_authenticated_scope_and_rejects_fixtures(self):
        captures = fixtures(wallet=True)
        class Repository:
            def read_scope(self, scope):
                self.scope = scope
                return captures, None
        repo = Repository()
        context = AuthenticatedContext(**SCOPE, session_id="session-a", issued_at="2026-09-28T11:00:00Z",
                                       expires_at="2026-09-28T13:00:00Z", trace_id="trace-a")
        service = SnapshotService(self.assembler, repo, lambda: AT)
        with self.assertRaisesRegex(MachineError, "fixtures"):
            service.read(context)
        self.assertEqual(repo.scope, SCOPE)


if __name__ == "__main__":
    unittest.main()
