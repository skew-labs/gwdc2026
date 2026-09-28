"""TRON policy contract compile check; runs only with the pinned compiler."""

import os
import json
import unittest
from pathlib import Path

from scripts.verify_economic_contract import verify_contract


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("ECON_TRON_SOLC"), "pinned TRON compiler unavailable")
class EconomicContractTests(unittest.TestCase):
    def test_registry_compiles_with_expected_nonpayable_abi(self):
        compiler = Path(os.environ["ECON_TRON_SOLC"])
        source = ROOT / "contracts" / "EconomicPolicyRegistry.sol"
        first = verify_contract(compiler, source)
        second = verify_contract(compiler, source)
        self.assertEqual(first, second)
        self.assertEqual(first["chain_family"], "TRON_TVM")
        self.assertEqual(first["execution"], "NOT_TESTED")
        self.assertEqual(first["deployment"], "NONE")
        self.assertLess(first["bytecode_bytes"], 24576)
        manifest = json.loads((ROOT / "contracts" /
                               "EconomicPolicyRegistry.compile-manifest.json").read_text())
        self.assertEqual(first, manifest)


if __name__ == "__main__":
    unittest.main()
