import json
import unittest

from finance_service.context import AuthenticatedContext
from finance_service.tron_proof import TronProofService


AT = "2026-09-29T12:00:00+00:00"
WALLET = "41" + "11" * 20
TXID = "a" * 64


class Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode()
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, _maximum): return self.body


class QueueOpener:
    def __init__(self, *items): self.items = list(items)
    def __call__(self, _request, timeout):
        self.assert_timeout = timeout
        return Response(self.items.pop(0))


def context(network="tron-nile"):
    return AuthenticatedContext(tenant_id="tenant", owner_id="owner", wallet=WALLET,
        network=network, session_id="session", issued_at="2026-09-29T11:59:00+00:00",
        expires_at="2026-09-29T12:10:00+00:00", trace_id="trace")


def transaction(**value_changes):
    value = {"owner_address": WALLET, "to_address": WALLET, "amount": 1}
    value.update(value_changes)
    return {"txID": TXID, "signature": ["1" * 130], "raw_data": {"contract": [{
        "type": "TransferContract", "parameter": {"value": value}}]}}


class TronProofTests(unittest.TestCase):
    def test_solid_self_transfer_is_verified_from_two_node_reads(self):
        opener = QueueOpener(transaction(), {"id": TXID, "blockNumber": 123,
                                              "receipt": {"result": "SUCCESS"}})
        result = TronProofService(object(), lambda: AT, opener=opener).verify(context(), TXID)
        self.assertEqual(result["status"], "SOLID_EXECUTED")
        self.assertEqual(result["amount_sun"], 1)
        self.assertEqual(result["execution_authority"], "NONE")

    def test_unobserved_and_pending_solid_never_claim_success(self):
        pending = TronProofService(object(), lambda: AT,
            opener=QueueOpener({})).verify(context(), TXID)
        self.assertEqual(pending["status"], "PENDING_NODE_OBSERVATION")
        solidity = TronProofService(object(), lambda: AT,
            opener=QueueOpener(transaction(), {})).verify(context(), TXID)
        self.assertEqual(solidity["status"], "PENDING_SOLIDITY")

    def test_wrong_amount_address_network_or_type_fail_closed(self):
        cases = [transaction(amount=2), transaction(to_address="41" + "22" * 20),
                 {**transaction(), "raw_data": {"contract": [{"type": "TriggerSmartContract",
                  "parameter": {"value": {}}}]}}]
        for item in cases:
            with self.subTest(item=item), self.assertRaises(ValueError):
                TronProofService(object(), lambda: AT,
                    opener=QueueOpener(item)).verify(context(), TXID)
        with self.assertRaisesRegex(ValueError, "Nile-only"):
            TronProofService(object(), lambda: AT,
                opener=QueueOpener(transaction())).verify(context("tron-mainnet"), TXID)


if __name__ == "__main__":
    unittest.main()
