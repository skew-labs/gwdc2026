"""Atomic public reads checked against an independent ABI encoder/decoder."""

import json
import hashlib
import unittest
from pathlib import Path

from eth_abi import decode, encode
from eth_utils import keccak

from economic_machine.tron_constant import MULTICALL, SELECTORS, LEGACY_DELEGATORS, constant_result
from economic_machine.tron_multicall import encode_calls, decode_results, public_calls
from economic_machine.tron_products import discover_products
from economic_machine.tron_rpc_snapshot import capture_rpc, replay_rpc
from economic_machine.snapshot_assembly import SnapshotAssembler
from economic_machine.values import MachineError
from test_economic_tron_sources import AT, CONFIG, SCOPE, fixtures, rehash


def atomic_fixture(products, *, bad_header=False, failed_subcall=False, mode="FIXTURE", head_delay=0):
    calls = public_calls(products)
    count = 0
    values = []
    for contract, selector in calls:
        cap = next((p["capability"] for p in products.values() if p["capability"]["contract"] == contract), None)
        decimals = cap["token"]["decimals"] if cap else 18
        value = {"getCash()": 1000 * 10**decimals, "exchangeRateStored()": 125 * 10**(16+decimals-8),
                 "exchangeRate()": 11 * 10**17, "totalUnderlying()": 1100 * 10**6,
                 "getBlockNumber()": 100, "getCurrentBlockTimestamp()": 1790596799}[selector]
        data = encode(["uint256"], [value]) + (b"\0" * 64 if contract in LEGACY_DELEGATORS else b"")
        values.append((not (failed_subcall and selector == "getCash()"), data))

    def header(number):
        return {"blockID": f"{number:016x}" + "a" * 48,
                "block_header": {"raw_data": {"number": number, "timestamp": 1790596799000}}}

    def reader(path, payload):
        nonlocal count
        if path.endswith("getnowblock"):
            count += 1
            result = header(99 if count <= head_delay + 1 else 101)
        elif path.endswith("getblockbynum"):
            if payload != {"num": 100}:
                raise AssertionError("block query must match in-call block")
            result = header(100)
            if bad_header:
                result["block_header"]["raw_data"]["timestamp"] -= 1000
        elif path.endswith("getchainparameters"):
            result = {"chainParameter": [{"key": "getEnergyFee", "value": 100}]}
        else:
            decoded = decode(["(address,bool,bytes)[]"], bytes.fromhex(payload["parameter"]))[0]
            assert [("41" + address[2:], allow, data.hex()) for address, allow, data in decoded] == [
                (contract, False, SELECTORS[selector]) for contract, selector in calls]
            assert payload["contract_address"] == MULTICALL
            result = {"result": {"result": True}, "transaction": {"ret": [{}], "raw_data": {"contract": [
                {"type": "TriggerSmartContract", "parameter": {"value": {
                    "owner_address": payload["owner_address"], "contract_address": MULTICALL,
                    "data": SELECTORS[payload["function_selector"]] + payload["parameter"]}}}]}},
                "constant_result": [encode(["(bool,bytes)[]"], [values]).hex()]}
        return json.dumps(result).encode()
    raw = capture_rpc(CONFIG, products, mode=mode, transport=reader)
    raw["received_at"] = AT
    return rehash(raw), values


class AtomicPublicReadTests(unittest.TestCase):
    def setUp(self):
        self.captures = fixtures(mode="LIVE_READ")
        self.products = discover_products(CONFIG, self.captures[0])
        self.calls = public_calls(self.products)

    def test_selectors_match_keccak_independently(self):
        for signature, selector in SELECTORS.items():
            self.assertEqual(keccak(text=signature)[:4].hex(), selector)

    def test_encoder_matches_independent_abi_library(self):
        values = [("0x" + contract[2:], False, bytes.fromhex(SELECTORS[selector])) for contract, selector in self.calls]
        self.assertEqual(encode_calls(self.calls), encode(["(address,bool,bytes)[]"], [values]).hex())

    def test_actual_public_response_replays_with_independent_abi_decoder(self):
        fixture = json.loads((Path(__file__).resolve().parents[1] / 'cases/tron_public_multicall_20260928.json').read_text())
        record = fixture['record']
        self.assertEqual(hashlib.sha256(record['raw_text'].encode()).hexdigest(), record['raw_sha256'])
        def request(path, payload):
            self.assertEqual((path, payload), (record['path'], record['payload']))
            return json.loads(record['raw_text'])
        raw = constant_result(request, MULTICALL, self.products['justlend.v1.jUSDT']['capability']['contract'],
                              'aggregate3((address,bool,bytes)[])', encode_calls(self.calls))
        ours = decode_results(raw, self.calls)
        independent = decode(['(bool,bytes)[]'], bytes.fromhex(raw))[0]
        for call, (success, data) in zip(self.calls, independent):
            self.assertTrue(success)
            self.assertEqual(ours[call], int.from_bytes(data[:32], 'big'))

    def test_all_product_getters_share_in_call_block_despite_head_moving(self):
        raw, _ = atomic_fixture(self.products, mode="LIVE_READ")
        self.assertIsNone(raw["error"])
        result = replay_rpc(raw, CONFIG, self.products)
        self.assertNotEqual(result["block_before"], result["block_after"])
        self.assertEqual(result["coherent_block"]["number"], 100)
        self.assertEqual(len(result["observations"]), 11)
        snap = SnapshotAssembler(CONFIG).assemble(self.captures, as_of=AT, rpc=raw)
        for path, fact in snap["facts"].items():
            if path.startswith("rpc.justlend."):
                self.assertTrue(fact["state_eligible"], path)
                self.assertEqual(fact["block"]["number"], 100)
        self.assertFalse(snap["facts"]["rpc.tron.chain.getEnergyFee"]["state_eligible"])

    def test_failed_subcall_or_mismatching_header_rejects_whole_batch(self):
        for options in ({"failed_subcall": True}, {"bad_header": True}):
            raw, _ = atomic_fixture(self.products, **options)
            self.assertEqual(raw["error"], "INCOMPLETE_RPC_READ")
            self.assertIsNone(replay_rpc(raw, CONFIG, self.products))

    def test_dynamic_offsets_count_truncation_and_trailing_bytes_rejected(self):
        _, values = atomic_fixture(self.products)
        good = encode(["(bool,bytes)[]"], [values])
        corruptions = [good[:-32], good+b"\0"*32, b"\0"*32+good[32:],
                       good[:32]+encode(["uint256"], [9])+good[64:],
                       good[:64]+encode(["uint256"], [0])+good[96:]]
        for bad in corruptions:
            with self.assertRaises(MachineError):
                decode_results(bad.hex(), self.calls)

    def test_unknown_or_mutating_getters_and_oversized_batch_rejected(self):
        for calls in ([(MULTICALL, "transfer(address,uint256)")], self.calls+self.calls):
            with self.assertRaises(MachineError):
                encode_calls(calls)

    def test_private_scope_cannot_relabel_atomic_public_capture(self):
        raw, _ = atomic_fixture(self.products)
        raw["scope"] = SCOPE
        with self.assertRaisesRegex(MachineError, "strategy"):
            replay_rpc(rehash(raw), CONFIG, self.products)

    def test_in_call_block_must_not_be_newer_than_solid_head(self):
        raw, _ = atomic_fixture(self.products, head_delay=99)
        self.assertEqual(raw["error"], "INCOMPLETE_RPC_READ")
        self.assertLessEqual(len(raw["records"]), 11)
        self.assertIsNone(replay_rpc(raw, CONFIG, self.products))

    def test_delayed_head_is_confirmed_without_repeating_product_reads(self):
        raw, _ = atomic_fixture(self.products, head_delay=2)
        self.assertIsNone(raw["error"])
        result = replay_rpc(raw, CONFIG, self.products)
        self.assertEqual(result["coherent_block"]["number"], 100)
        self.assertEqual(sum(r["path"].endswith("triggerconstantcontract") for r in raw["records"]), 1)
        self.assertTrue(all(r["payload"] == {} for r in raw["records"] if r["path"].endswith("getnowblock")))

    def test_fixture_batch_is_never_promoted(self):
        captures = fixtures()
        products = discover_products(CONFIG, captures[0])
        raw, _ = atomic_fixture(products)
        snapshot = SnapshotAssembler(CONFIG).assemble(captures, as_of=AT, rpc=raw)
        self.assertTrue(all(not f["state_eligible"] for f in snapshot["facts"].values()))


if __name__ == "__main__":
    unittest.main()
