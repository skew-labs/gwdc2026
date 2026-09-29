"""Golden bytes independently checked by the official TronWeb serializer.

All owners are an unfunded, public synthetic test key; no network transport.
"""
import json, unittest
from pathlib import Path
from finance_service.native_execution import unsigned_transaction
from economic_machine.signed_tx_validation import decode_trigger_raw

class WalletEncodingTests(unittest.TestCase):
    def test_server_bytes_match_wallet_serializer_for_payable_and_zero_value_calls(self):
        rows=json.loads((Path(__file__).parent/'fixtures/tronweb-trigger-transactions.json').read_text())
        for row in rows:
            with self.subTest(action=row['name']):
                tx=unsigned_transaction(row['request'])
                self.assertEqual(tx,row['transaction'])
                self.assertEqual(decode_trigger_raw(bytes.fromhex(tx['raw_data_hex']))['call_value_sun'],int(row['request']['call_value_sun']))
                if row['request']['call_value_sun']=='0':
                    self.assertNotIn('call_value',tx['raw_data']['contract'][0]['parameter']['value'])
