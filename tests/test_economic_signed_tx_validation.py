import copy
import hashlib
import unittest

from economic_machine.approval import (build_wallet_signature_request, confirm_approval,
                                       prepare_approval_card)
from economic_machine.preflight import compile_preflight
from economic_machine.signed_tx_validation import validate_signed_transaction
from economic_machine.tron_crypto import function_selector, keccak256, recover_tron_address
import economic_machine.tron_crypto as crypto
from economic_machine.values import MachineError
from test_economic_approval import AT, anchor, confirmation, session
import test_economic_tx_graph as fixtures


def varint(value):
    result = bytearray()
    while True:
        byte = value & 0x7f
        value >>= 7
        result.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(result)


def field_varint(number, value):
    return varint(number << 3) + varint(value)


def field_bytes(number, value):
    return varint((number << 3) | 2) + varint(len(value)) + value


def raw_transaction(request, **changes):
    values = {"owner": bytes.fromhex(request["owner_address"]),
        "contract": bytes.fromhex(request["contract_address"]),
        "data": bytes.fromhex(request["data_hex"]),
        "call_value": int(request["call_value_sun"]),
        "fee": int(request["fee_limit_sun"]), "timestamp": request["timestamp_ms"],
        "expiration": request["expiration_ms"],
        "ref_bytes": bytes.fromhex(request["ref_block_bytes"]),
        "ref_hash": bytes.fromhex(request["ref_block_hash"])}
    values.update(changes)
    trigger = (field_bytes(1, values["owner"]) + field_bytes(2, values["contract"])
               + (field_varint(3, values["call_value"]) if values["call_value"] else b"")
               + field_bytes(4, values["data"]))
    any_value = (field_bytes(1, b"type.googleapis.com/protocol.TriggerSmartContract")
                 + field_bytes(2, trigger))
    contract = field_varint(1, 31) + field_bytes(2, any_value)
    return (field_bytes(1, values["ref_bytes"]) + field_bytes(4, values["ref_hash"])
            + field_varint(8, values["expiration"]) + field_bytes(11, contract)
            + field_varint(14, values["timestamp"]) + field_varint(18, values["fee"]))


class SignedTransactionTests(unittest.TestCase):
    def setUp(self):
        self.private_key = int.from_bytes(bytes.fromhex("11" * 32), "big")
        public = crypto._multiply(self.private_key, crypto._G)
        public_bytes = public[0].to_bytes(32, "big") + public[1].to_bytes(32, "big")
        wallet = "41" + keccak256(public_bytes)[-20:].hex()
        self.old_scope = copy.deepcopy(fixtures.SCOPE)
        fixtures.SCOPE = {**fixtures.SCOPE, "wallet": wallet}
        prod, snap, intent, account, quote, bindings, graph = fixtures.supply_graph(
            allowance=1_000_000_000)
        step = next(row for row in graph["steps"] if row["operation"] == "JUSTLEND_SUPPLY")
        manifest = compile_preflight(graph, fixtures.fresh(account), [fixtures.simulation(
            step, decoded="0", outputs=[fixtures.asset(
                "jUSDT", fixtures.SHARE, 8, 99_000_000_000)])], at=AT)
        active = session(graph["scope"])
        card = prepare_approval_card(graph, manifest, active, step["step_id"],
            execution_path="DIRECT_WALLET", nonce="0", prepared_at=AT,
            expires_at="2026-09-28T12:03:00Z")
        approved = confirm_approval(card, active, confirmation(card, graph["scope"]))
        self.session = active
        self.request = build_wallet_signature_request(approved, active, anchor(), issued_at=AT)

    def tearDown(self):
        fixtures.SCOPE = self.old_scope

    def sign_hash(self, message_hash, private_key):
        z = int.from_bytes(message_hash, "big")
        nonce = int.from_bytes(hashlib.sha256(
            private_key.to_bytes(32, "big") + message_hash).digest(), "big") % crypto._N
        if nonce == 0:
            nonce = 1
        point = crypto._multiply(nonce, crypto._G)
        r = point[0] % crypto._N
        s = (crypto._inverse(nonce, crypto._N) * (z + r * private_key)) % crypto._N
        recovery = point[1] & 1
        if s > crypto._HALF_N:
            s = crypto._N - s
            recovery ^= 1
        return r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([recovery])

    def signed(self, raw=None, private_key=None):
        raw = raw or raw_transaction(self.request)
        txid = hashlib.sha256(raw).digest()
        signature = self.sign_hash(txid, private_key or self.private_key)
        return {"visible": False, "txID": txid.hex(), "raw_data_hex": raw.hex(),
                "signature": [signature.hex()]}

    def test_keccak_selector_and_recovered_tron_address_match_known_vectors(self):
        self.assertEqual(keccak256(b"").hex(),
            "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertEqual(function_selector("approve(address,uint256)"), "095ea7b3")
        signed = self.signed()
        self.assertEqual(recover_tron_address(bytes.fromhex(signed["txID"]),
            bytes.fromhex(signed["signature"][0])), self.request["owner_address"])

    def test_exact_signed_protobuf_and_signature_validate_without_broadcast(self):
        result = validate_signed_transaction(self.signed(), self.request, self.session, [], at=AT)
        self.assertEqual(result["status"], "SIGNED_PAYLOAD_VERIFIED")
        self.assertEqual(result["signer_address"], self.request["owner_address"])
        self.assertEqual(result["chain_status"], "NOT_SUBMITTED")
        self.assertEqual(result["execution_authority"], "EXACT_SIGNED_PAYLOAD_ONLY")

    def test_amount_target_fee_deadline_and_data_tampering_are_rejected(self):
        cases = {
            "recipient-owner": {"owner": bytes.fromhex("41" + "88" * 20)},
            "target": {"contract": bytes.fromhex("41" + "99" * 20)},
            "fee": {"fee": int(self.request["fee_limit_sun"]) + 1},
            "deadline": {"expiration": self.request["expiration_ms"] + 1},
            "amount-data": {"data": bytes.fromhex(self.request["data_hex"][:-2] + "01")},
        }
        for label, change in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(MachineError, "differ"):
                validate_signed_transaction(self.signed(raw_transaction(self.request, **change)),
                                            self.request, self.session, [], at=AT)

    def test_wrong_signer_session_and_replay_are_rejected(self):
        stranger = int.from_bytes(bytes.fromhex("22" * 32), "big")
        with self.assertRaisesRegex(MachineError, "recover approved wallet"):
            validate_signed_transaction(self.signed(private_key=stranger), self.request,
                                        self.session, [], at=AT)
        changed = copy.deepcopy(self.session)
        changed["auth_context_hash"] = "9" * 64
        with self.assertRaisesRegex(MachineError, "session changed"):
            validate_signed_transaction(self.signed(), self.request, changed, [], at=AT)
        for field, value in (("network", "tron-nile"),
                             ("wallet", "41" + "77" * 20)):
            changed = copy.deepcopy(self.session)
            changed["scope"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                    MachineError, "session changed"):
                validate_signed_transaction(self.signed(), self.request, changed, [], at=AT)
        with self.assertRaisesRegex(MachineError, "replay"):
            validate_signed_transaction(self.signed(), self.request, self.session,
                                        [self.request["request_hash"]], at=AT)

    def test_txid_signature_and_protobuf_shape_are_independently_checked(self):
        signed = self.signed()
        forged = copy.deepcopy(signed)
        forged["txID"] = "00" * 32
        with self.assertRaisesRegex(MachineError, "txID"):
            validate_signed_transaction(forged, self.request, self.session, [], at=AT)
        high = copy.deepcopy(signed)
        raw_signature = bytes.fromhex(high["signature"][0])
        high_s = crypto._N - int.from_bytes(raw_signature[32:64], "big")
        high["signature"][0] = (raw_signature[:32] + high_s.to_bytes(32, "big")
                                 + bytes([1 - raw_signature[64]])).hex()
        with self.assertRaisesRegex(MachineError, "noncanonical"):
            validate_signed_transaction(high, self.request, self.session, [], at=AT)
        malformed_raw = raw_transaction(self.request) + field_varint(18, 1)
        with self.assertRaisesRegex(MachineError, "duplicate"):
            validate_signed_transaction(self.signed(malformed_raw), self.request,
                                        self.session, [], at=AT)


if __name__ == "__main__":
    unittest.main()
