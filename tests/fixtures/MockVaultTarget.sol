// SPDX-License-Identifier: MIT
pragma solidity 0.8.20;

interface IMockVaultRegistry {
    function consumeBasket(bytes32 bindingHash)
        external returns (address asset, uint256 amount, bytes32 basketHash);
}

interface IMockVaultToken {
    function transfer(address to, uint256 amount) external returns (bool);
}

/// @notice Simulator-only venue adapter; the real target must be separately audited.
contract MockVaultTarget {
    IMockVaultRegistry public immutable registry;
    address public immutable vault;
    IMockVaultToken public immutable output;

    constructor(address registry_, address vault_, address output_) {
        registry = IMockVaultRegistry(registry_);
        vault = vault_;
        output = IMockVaultToken(output_);
    }

    function fill(bytes32 bindingHash, uint256 outputAmount) external {
        require(msg.sender == vault, "VAULT_ONLY");
        registry.consumeBasket(bindingHash);
        require(output.transfer(vault, outputAmount), "OUTPUT_TRANSFER");
    }

    function failAfterConsume(bytes32 bindingHash) external {
        require(msg.sender == vault, "VAULT_ONLY");
        registry.consumeBasket(bindingHash);
        revert("TARGET_FAILURE");
    }

    function skipConsumption(uint256 outputAmount) external {
        require(msg.sender == vault, "VAULT_ONLY");
        require(output.transfer(vault, outputAmount), "OUTPUT_TRANSFER");
    }
}
