"""Pinned TRON compiler and ABI verification for PR07 undeployed contracts."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


TRON_SOLC_0820_SHA256 = "fa95cdeb30aaf521df75b87f57c6ffc1ee6fef5de190981a69cdb878da3a4e14"
EXPECTED_VERSION = "0.8.20+commit.5f1834bc"
EXPECTED = {
    "JustLendV1Adapter": {
        "functions": {"owner", "underlyingToken", "market", "shareToken", "guard",
                      "bindGuard", "supply", "redeem"},
        "events": {"GuardBound", "Supplied", "Redeemed"},
        "constructor": ["address", "address", "address"],
    },
    "EconomicExecutionGuardV1": {
        "functions": {"owner", "adapter", "underlyingToken", "market", "shareToken",
                      "adapterCodeHash", "underlyingCodeHash", "marketCodeHash",
                      "shareCodeHash", "maxSupplyAmount", "maxRedeemShares",
                      "cumulativeSupplyLimit", "cumulativeSupplied", "nextNonce", "usedStep",
                      "executionDigest", "executeSupply", "executeRedeem"},
        "events": {"GuardExecution"},
        "constructor": ["address", "address", "uint256", "uint256", "uint256"],
    },
}


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_guard(compiler, sources):
    compiler = Path(compiler)
    sources = [Path(item) for item in sources]
    if _hash(compiler) != TRON_SOLC_0820_SHA256:
        raise ValueError("TRON compiler digest mismatch")
    version = subprocess.run([str(compiler), "--version"], check=True, capture_output=True,
                             text=True, timeout=20).stdout.strip()
    if EXPECTED_VERSION not in version or "solc.tron" not in version:
        raise ValueError("unexpected TRON compiler version")
    for source in sources:
        lowered = source.read_text().lower()
        lowered = re.sub(r"/\*.*?\*/", "", lowered, flags=re.DOTALL)
        lowered = re.sub(r"//[^\n]*", "", lowered)
        if any(term in lowered for term in ("delegatecall", "selfdestruct", "tx.origin")):
            raise ValueError("forbidden execution primitive in guard source")
    result = subprocess.run([str(compiler), "--optimize", "--optimize-runs", "200",
        "--combined-json", "abi,bin", *(str(item) for item in sources)], check=True,
        capture_output=True, text=True, timeout=60)
    contracts = json.loads(result.stdout)["contracts"]
    deployable = {name.split(":")[-1]: artifact for name, artifact in contracts.items()
                  if artifact["bin"]}
    if set(deployable) != set(EXPECTED):
        raise ValueError("unexpected deployable guard artifact set")
    artifacts = []
    for name, expected in EXPECTED.items():
        artifact = deployable[name]
        abi = artifact["abi"]
        functions = {item["name"] for item in abi if item["type"] == "function"}
        events = {item["name"] for item in abi if item["type"] == "event"}
        constructors = [item for item in abi if item["type"] == "constructor"]
        if functions != expected["functions"] or events != expected["events"]:
            raise ValueError(name + " ABI surface changed")
        if len(constructors) != 1 or [item["type"] for item in constructors[0]["inputs"]] != (
                expected["constructor"]):
            raise ValueError(name + " constructor changed")
        if any(item.get("stateMutability") == "payable" or item["type"] in {
                "receive", "fallback"} for item in abi):
            raise ValueError(name + " exposes payable/fallback ABI")
        bytecode = bytes.fromhex(artifact["bin"])
        if not bytecode or len(bytecode) > 24576:
            raise ValueError(name + " bytecode is empty or oversized")
        artifacts.append({"contract": name,
            "source_sha256": _hash(next(item for item in sources if item.stem == name)),
            "abi_sha256": hashlib.sha256(json.dumps(
                abi, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "bytecode_sha256": hashlib.sha256(bytecode).hexdigest(),
            "bytecode_bytes": len(bytecode), "functions": sorted(functions),
            "events": sorted(events)})
    return {"chain_family": "TRON_TVM", "compiler_sha256": _hash(compiler),
        "compiler_version": version, "artifacts": artifacts,
        "execution": "NOT_TESTED_ON_TVM", "deployment": "NONE", "audit": "NONE"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--compiler", type=Path, required=True)
    parser.add_argument("--source", action="append", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify_guard(args.compiler, args.source), sort_keys=True))
