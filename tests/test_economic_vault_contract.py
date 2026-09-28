"""TRON capital-vault reproducibility check; no deployment or TVM execution."""

import json
import os
import unittest
from pathlib import Path

from scripts.verify_economic_vault import verify_vault


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("ECON_TRON_SOLC"), "pinned TRON compiler unavailable")
class CapitalVaultCompileTests(unittest.TestCase):
    def test_vault_compiles_to_pinned_nonpayable_abi_and_bytecode(self):
        compiler = Path(os.environ["ECON_TRON_SOLC"])
        source = ROOT / "contracts" / "EconomicCapitalVault.sol"
        first = verify_vault(compiler, source)
        self.assertEqual(first, verify_vault(compiler, source))
        manifest = json.loads((ROOT / "contracts" /
                               "EconomicCapitalVault.compile-manifest.json").read_text())
        self.assertEqual(first, manifest)
        self.assertEqual(first["deployment"], "NONE")
        self.assertEqual(first["execution"], "NOT_TESTED_ON_TVM")
        self.assertEqual(first["audit"], "NONE")
        self.assertLess(first["bytecode_bytes"], 24576)


if __name__ == "__main__":
    unittest.main()
