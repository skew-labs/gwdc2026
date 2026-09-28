"""Reproducible TRON policy-registry compilation without deployment."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


TRON_SOLC_0820_SHA256 = "fa95cdeb30aaf521df75b87f57c6ffc1ee6fef5de190981a69cdb878da3a4e14"
EXPECTED_VERSION = "0.8.20+commit.5f1834bc"
EXPECTED_FUNCTIONS = {
    "owner", "guardian", "paused", "policies",
    "setGuardian", "pause", "unpause", "registerPolicy", "revokePolicy",
    "baskets", "computeBasketBinding", "commitBasket", "revokeBasket", "activeBasket",
    "consumeBasket", "bindingByCommitment", "reservedBasketAmount",
    "consumedBasketAmount", "releaseExpiredBasket", "basketBudgetRemaining",
    "assetBudgets", "assetBudgetRemaining", "configureAssetBudget",
    "attestors", "activeAttestorCount", "attestorEpoch", "setAttestor",
    "attestationRequired", "registerAttestedPolicy", "attestationDigest",
    "commitBasketAttested", "authenticatedSourceByBinding",
    "attestedEpochByBinding",
}
EXPECTED_EVENTS = {"PolicyRegistered", "PolicyRevoked",
                   "PauseChanged", "GuardianChanged", "BasketCommitted", "BasketRevoked",
                   "BasketConsumed", "BasketExpiredReleased", "AssetBudgetConfigured",
                   "AttestorChanged", "AttestedPolicyRegistered", "BasketAttested"}
EXPECTED_BASKET_ABI = {
    "computeBasketBinding": (["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "uint64"],
                             ["bytes32"]),
    "commitBasket": (["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "uint64"],
                     ["bytes32"]),
    "activeBasket": (["bytes32"], ["bool"]),
    "revokeBasket": (["bytes32"], []),
    "consumeBasket": (["bytes32"], ["address", "uint256", "bytes32"]),
    "bindingByCommitment": (["bytes32"], ["bytes32"]),
    "baskets": (["bytes32"], ["bytes32", "bytes32", "bytes32", "bytes32",
                              "uint256", "uint64", "bool", "bool", "bool"]),
    "reservedBasketAmount": (["bytes32"], ["uint256"]),
    "consumedBasketAmount": (["bytes32"], ["uint256"]),
    "basketBudgetRemaining": (["bytes32"], ["uint256"]),
    "releaseExpiredBasket": (["bytes32"], []),
    "assetBudgets": (["address"], ["uint256", "uint256", "uint256", "bool"]),
    "assetBudgetRemaining": (["address"], ["uint256"]),
    "configureAssetBudget": (["address", "uint256"], []),
    "setAttestor": (["address", "bool"], []),
    "registerAttestedPolicy": (["bytes32", "bytes32", "address", "address", "uint256", "uint64"], []),
    "attestationRequired": (["bytes32"], ["bool"]),
    "attestationDigest": (["bytes32", "bytes32"], ["bytes32"]),
    "commitBasketAttested": (["bytes32", "bytes32", "bytes32", "bytes32",
                              "uint256", "uint64", "bytes32", "bytes[]"], ["bytes32"]),
    "authenticatedSourceByBinding": (["bytes32"], ["bytes32"]),
    "attestedEpochByBinding": (["bytes32"], ["uint64"]),
}


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1048576), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_contract(compiler: Path, source: Path) -> dict:
    compiler, source = Path(compiler), Path(source)
    compiler_hash = _sha256_file(compiler)
    if compiler_hash != TRON_SOLC_0820_SHA256:
        raise ValueError("TRON compiler digest mismatch")
    version = subprocess.run([str(compiler), "--version"], check=True,
                             capture_output=True, text=True, timeout=20).stdout.strip()
    if EXPECTED_VERSION not in version or "solc.tron" not in version:
        raise ValueError("unexpected TRON compiler version")
    completed = subprocess.run(
        [str(compiler), "--optimize", "--optimize-runs", "200",
         "--combined-json", "abi,bin", str(source)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    compiled = json.loads(completed.stdout)
    if len(compiled.get("contracts", {})) != 1:
        raise ValueError("expected exactly one policy contract")
    artifact = next(iter(compiled["contracts"].values()))
    abi = artifact["abi"]
    functions = {item["name"] for item in abi if item["type"] == "function"}
    events = {item["name"] for item in abi if item["type"] == "event"}
    if functions != EXPECTED_FUNCTIONS or events != EXPECTED_EVENTS:
        raise ValueError("policy registry ABI changed")
    for name, (inputs, outputs) in EXPECTED_BASKET_ABI.items():
        entry = next(item for item in abi if item["type"] == "function" and item["name"] == name)
        if ([item["type"] for item in entry["inputs"]],
                [item["type"] for item in entry["outputs"]]) != (inputs, outputs):
            raise ValueError("basket registry ABI types changed")
    basket_event = next(item for item in abi if item["type"] == "event"
                        and item["name"] == "BasketCommitted")
    if ([item["type"] for item in basket_event["inputs"]] !=
            ["bytes32", "bytes32", "bytes32", "bytes32", "bytes32",
             "address", "address", "uint256", "uint64"]
            or [item["indexed"] for item in basket_event["inputs"]] !=
            [True, True, True, False, False, False, False, False, False]):
        raise ValueError("basket commitment event ABI changed")
    consumed_event = next(item for item in abi if item["type"] == "event"
                          and item["name"] == "BasketConsumed")
    if ([item["type"] for item in consumed_event["inputs"]] !=
            ["bytes32", "bytes32", "address", "address", "uint256"]
            or [item["indexed"] for item in consumed_event["inputs"]] !=
            [True, True, True, False, False]):
        raise ValueError("basket consumption event ABI changed")
    released_event = next(item for item in abi if item["type"] == "event"
                          and item["name"] == "BasketExpiredReleased")
    if ([item["type"] for item in released_event["inputs"]] !=
            ["bytes32", "bytes32", "uint256"]
            or [item["indexed"] for item in released_event["inputs"]] !=
            [True, True, False]):
        raise ValueError("basket expiry release event ABI changed")
    budget_event = next(item for item in abi if item["type"] == "event"
                        and item["name"] == "AssetBudgetConfigured")
    if ([item["type"] for item in budget_event["inputs"]] !=
            ["address", "uint256", "uint256"]
            or [item["indexed"] for item in budget_event["inputs"]] !=
            [True, False, False]):
        raise ValueError("asset budget configuration event ABI changed")
    attested_event = next(item for item in abi if item["type"] == "event"
                          and item["name"] == "BasketAttested")
    if ([item["type"] for item in attested_event["inputs"]] !=
            ["bytes32", "bytes32", "uint64"]
            or [item["indexed"] for item in attested_event["inputs"]] !=
            [True, True, False]):
        raise ValueError("basket attestation event ABI changed")
    if any(item.get("stateMutability") == "payable" or item["type"] in {"receive", "fallback"}
           for item in abi):
        raise ValueError("policy registry must not expose a payable ABI")
    bytecode = bytes.fromhex(artifact["bin"])
    if not bytecode or len(bytecode) > 24576:
        raise ValueError("invalid or oversized policy bytecode")
    return {"contract": "EconomicPolicyRegistry", "chain_family": "TRON_TVM",
            "compiler_sha256": compiler_hash, "compiler_version": version,
            "source_sha256": _sha256_file(source),
            "abi_sha256": hashlib.sha256(json.dumps(abi, sort_keys=True,
                                                   separators=(",", ":")).encode()).hexdigest(),
            "bytecode_sha256": hashlib.sha256(bytecode).hexdigest(),
            "bytecode_bytes": len(bytecode),
            "functions": sorted(functions), "events": sorted(events),
            "execution": "NOT_TESTED", "deployment": "NONE"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compile and verify TRON policy registry")
    parser.add_argument("--compiler", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=Path("contracts/EconomicPolicyRegistry.sol"))
    options = parser.parse_args()
    print(json.dumps(verify_contract(options.compiler, options.source), sort_keys=True))
