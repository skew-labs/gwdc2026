// SPDX-License-Identifier: MIT
pragma solidity 0.8.20;

interface IBasketRegistry {
    function consumeBasket(bytes32 bindingHash)
        external returns (address asset, uint256 amount, bytes32 basketHash);
}

/// @notice EVM-only test fixture; it never transfers assets.
contract MockBasketTarget {
    IBasketRegistry public immutable registry;

    constructor(address registry_) {
        registry = IBasketRegistry(registry_);
    }

    function consume(bytes32 bindingHash) external {
        registry.consumeBasket(bindingHash);
    }

    function revertAfterConsume(bytes32 bindingHash) external {
        registry.consumeBasket(bindingHash);
        revert("ROLLBACK");
    }
}
