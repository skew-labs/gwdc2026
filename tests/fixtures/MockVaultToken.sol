// SPDX-License-Identifier: MIT
pragma solidity 0.8.20;

/// @notice Simulator-only exact-transfer token; no deployed asset claim.
contract MockVaultToken {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _transfer(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 available = allowance[from][msg.sender];
        require(available >= amount, "ALLOWANCE");
        allowance[from][msg.sender] = available - amount;
        _transfer(from, to, amount);
        return true;
    }

    function _transfer(address from, address to, uint256 amount) internal {
        uint256 available = balanceOf[from];
        require(available >= amount, "BALANCE");
        balanceOf[from] = available - amount;
        balanceOf[to] += amount;
    }
}
