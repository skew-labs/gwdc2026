"""TRON source-time and on-chain unit checks; run on Cherry."""

import unittest

from research.fdc.real_tron import event_rows, history_rows


class RealTronTests(unittest.TestCase):
    def test_history_time_and_units(self):
        fetched = "2026-09-24T05:00:00+00:00"
        payload = {"code": 0, "data": {"items": [
            {"statisticTime": 1790121600000, "time": None,
             "collateralValue": "200", "debt": "100", "mintedUSDD": "100",
             "usddTotalSupply": "99", "totalSupplyValue": "99",
             "earnTvl": "0", "earnApy": "0.04"},
            {"statisticTime": 1893456000000, "time": None,
             "collateralValue": "201", "debt": "100", "mintedUSDD": "100",
             "usddTotalSupply": "99", "totalSupplyValue": "99",
             "earnTvl": "0", "earnApy": "0.04"}]}}
        rows = history_rows(payload, raw_sha="a" * 64, fetched_at=fetched)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["measurements"]["collateralValue"]["unit"], "USD")
        self.assertEqual(rows[0]["measurements"]["debt"]["unit"], "USDD")
        self.assertEqual(rows[0]["source_available_at"], fetched)
        self.assertIsNone(rows[0]["decision_label"])

    def test_event_decimals_and_market_identity(self):
        market = {"market_address": "T" + "a" * 33,
                  "underlying_symbol": "USDT", "underlying_address": "T" + "b" * 33,
                  "underlying_decimals": 6}
        entry = {"event_name": "JTokenStatus", "contract_address": market["market_address"],
                 "block_timestamp": 1790121600000, "block_number": 1, "event_index": 0,
                 "transaction_id": "a" * 64, "result": {"totalCash": "123456789"}}
        rows = event_rows({"data": [entry]}, source_url="https://api.trongrid.io/test",
                          raw_sha="b" * 64, fetched_at="2026-09-24T05:00:00+00:00",
                          market=market)
        self.assertEqual(rows[0]["measurements"]["available_cash"]["value"], "123.456789")
        self.assertEqual(rows[0]["measurements"]["available_cash"]["unit"], "USDT")
        self.assertEqual(rows[0]["metadata"]["confirmation_filter"], "only_confirmed=true")
        entry["contract_address"] = "T" + "c" * 33
        with self.assertRaisesRegex(ValueError, "contract"):
            event_rows({"data": [entry]}, source_url="https://api.trongrid.io/test",
                       raw_sha="b" * 64, fetched_at="2026-09-24T05:00:00+00:00",
                       market=market)


if __name__ == "__main__":
    unittest.main()
