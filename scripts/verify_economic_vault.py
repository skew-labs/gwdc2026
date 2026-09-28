"""Pinned TRON compiler and ABI verification for the undeployed capital vault."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


TRON_SOLC_0820_SHA256 = "fa95cdeb30aaf521df75b87f57c6ffc1ee6fef5de190981a69cdb878da3a4e14"
EXPECTED_VERSION = "0.8.20+commit.5f1834bc"
EXPECTED_FUNCTIONS = {
    "owner": ([], ["address"]),
    "guardian": ([], ["address"]),
    "registry": ([], ["address"]),
    "registryCodeHash": ([], ["bytes32"]),
    "paused": ([], ["bool"]),
    "setGuardian": (["address"], []),
    "pause": ([], []),
    "unpause": ([], []),
    "pinAsset": (["address", "bool"], []),
    "pinTarget": (["address", "bool"], []),
    "allowedAssetCodeHash": (["address"], ["bytes32"]),
    "allowedTargetCodeHash": (["address"], ["bytes32"]),
    "executed": (["bytes32"], ["bool"]),
    "deposit": (["address", "uint256"], []),
    "withdraw": (["address", "uint256"], []),
    "executionDigest": (["bytes32", "address", "uint256", "address", "address",
                         "uint256", "bytes32", "uint64"], ["bytes32"]),
    "batchDigest": (["bytes32", "bytes32[]", "uint64"], ["bytes32"]),
    "executeBasket": (["bytes32", "address", "uint256", "uint64", "bytes", "bytes"],
                      ["uint256"]),
    "executeBatch": (["bytes32", "tuple[]", "uint64", "bytes"], []),
}
EXPECTED_EVENTS = {
    "PauseChanged", "GuardianChanged", "AssetCodePinned", "TargetCodePinned",
    "Deposited", "Withdrawn", "BasketExecuted", "BatchExecuted"
}


def _hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1048576), b""):
            result.update(chunk)
    return result.hexdigest()


def verify_vault(compiler: Path, source: Path) -> dict:
    compiler, source = Path(compiler), Path(source)
    compiler_hash = _hash(compiler)
    if compiler_hash != TRON_SOLC_0820_SHA256:
        raise ValueError("TRON compiler digest mismatch")
    version = subprocess.run([str(compiler), "--version"], check=True,
                             capture_output=True, text=True, timeout=20).stdout.strip()
    if EXPECTED_VERSION not in version or "solc.tron" not in version:
        raise ValueError("unexpected TRON compiler version")
    completed = subprocess.run(
        [str(compiler), "--optimize", "--optimize-runs", "200",
         "--combined-json", "abi,bin", str(source)],
        check=True, capture_output=True, text=True, timeout=60)
    contracts = json.loads(completed.stdout)["contracts"]
    selected = [artifact for name, artifact in contracts.items()
                if name.endswith(":EconomicCapitalVault")]
    if len(selected) != 1:
        raise ValueError("capital vault artifact missing or ambiguous")
    if any(artifact["bin"] for name, artifact in contracts.items()
           if not name.endswith(":EconomicCapitalVault")):
        raise ValueError("unexpected deployable artifact")
    artifact = selected[0]
    abi = artifact["abi"]
    functions = {item["name"]: ([part["type"] for part in item["inputs"]],
                                 [part["type"] for part in item["outputs"]])
                 for item in abi if item["type"] == "function"}
    events = {item["name"] for item in abi if item["type"] == "event"}
    if functions != EXPECTED_FUNCTIONS or events != EXPECTED_EVENTS:
        raise ValueError("capital vault ABI changed")
    batch = next(item for item in abi if item["type"] == "function"
                 and item["name"] == "executeBatch")
    if [part["type"] for part in batch["inputs"][1]["components"]] != [
            "bytes32", "address", "uint256", "uint64", "bytes"]:
        raise ValueError("batch order tuple ABI changed")
    constructor = [item for item in abi if item["type"] == "constructor"]
    if len(constructor) != 1 or [part["type"] for part in constructor[0]["inputs"]] != [
            "address", "address"]:
        raise ValueError("vault constructor ABI changed")
    executed = next(item for item in abi if item["type"] == "event"
                    and item["name"] == "BasketExecuted")
    if ([(part["type"], part["indexed"]) for part in executed["inputs"]] !=
            [("bytes32", True), ("address", True), ("address", True),
             ("address", False), ("uint256", False), ("uint256", False),
             ("bytes32", False)]):
        raise ValueError("basket execution event ABI changed")
    batch_event = next(item for item in abi if item["type"] == "event"
                       and item["name"] == "BatchExecuted")
    if [(part["type"], part["indexed"]) for part in batch_event["inputs"]] != [
            ("bytes32", True), ("bytes32", True), ("uint256", False)]:
        raise ValueError("batch execution event ABI changed")
    if any(item.get("stateMutability") == "payable" or item["type"] in {
            "receive", "fallback"} for item in abi):
        raise ValueError("vault must not expose a payable ABI")
    bytecode = bytes.fromhex(artifact["bin"])
    if not bytecode or len(bytecode) > 24576:
        raise ValueError("invalid or oversized capital vault bytecode")
    return {"contract": "EconomicCapitalVault", "chain_family": "TRON_TVM",
            "compiler_sha256": compiler_hash, "compiler_version": version,
            "source_sha256": _hash(source),
            "abi_sha256": hashlib.sha256(json.dumps(
                abi, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "bytecode_sha256": hashlib.sha256(bytecode).hexdigest(),
            "bytecode_bytes": len(bytecode), "functions": sorted(functions),
            "events": sorted(events), "execution": "NOT_TESTED_ON_TVM",
            "deployment": "NONE", "audit": "NONE"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compile and verify TRON capital vault")
    parser.add_argument("--compiler", type=Path, required=True)
    parser.add_argument("--source", type=Path,
                        default=Path("contracts/EconomicCapitalVault.sol"))
    options = parser.parse_args()
    print(json.dumps(verify_vault(options.compiler, options.source), sort_keys=True))
