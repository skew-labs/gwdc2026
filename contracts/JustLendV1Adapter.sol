// SPDX-License-Identifier: MIT
pragma solidity 0.8.20;

interface IGuardTokenV1 {
    function balanceOf(address account) external view returns (uint256);
}

/// @notice Fixed-market JustLend v1 adapter for one EconomicExecutionGuardV1.
/// @dev This contract has no arbitrary target or calldata path. It rejects
///      fee-on-transfer, false-return, nonzero protocol code and residual funds.
contract JustLendV1Adapter {
    address public immutable owner;
    address public immutable underlyingToken;
    address public immutable market;
    address public immutable shareToken;
    address public guard;
    bool private entered;

    event GuardBound(address indexed guard);
    event Supplied(address indexed recipient, uint256 inputAmount, uint256 shareAmount);
    event Redeemed(address indexed recipient, uint256 shareAmount, uint256 outputAmount);

    error OnlyOwner();
    error OnlyGuard();
    error InvalidBinding();
    error InvalidAmount();
    error ReentrantCall();
    error TokenCallFailed();
    error ProtocolError(uint256 code);
    error BalanceMismatch();

    constructor(address underlyingToken_, address market_, address shareToken_) {
        if (underlyingToken_ == address(0) || market_ == address(0) ||
            shareToken_ == address(0) || underlyingToken_ == market_ ||
            underlyingToken_ == shareToken_ || underlyingToken_.code.length == 0 ||
            market_.code.length == 0 || shareToken_.code.length == 0)
            revert InvalidBinding();
        owner = msg.sender;
        underlyingToken = underlyingToken_;
        market = market_;
        shareToken = shareToken_;
    }

    modifier onlyGuard() {
        if (msg.sender != guard) revert OnlyGuard();
        _;
    }

    modifier nonReentrant() {
        if (entered) revert ReentrantCall();
        entered = true;
        _;
        entered = false;
    }

    function bindGuard(address guard_) external {
        if (msg.sender != owner) revert OnlyOwner();
        if (guard != address(0) || guard_ == address(0) || guard_.code.length == 0)
            revert InvalidBinding();
        guard = guard_;
        emit GuardBound(guard_);
    }

    function _balance(address token, address account) internal view returns (uint256) {
        (bool ok, bytes memory result) = token.staticcall(
            abi.encodeWithSignature("balanceOf(address)", account));
        if (!ok || result.length != 32) revert TokenCallFailed();
        return abi.decode(result, (uint256));
    }

    function _tokenCall(address token, bytes memory payload) internal {
        (bool ok, bytes memory result) = token.call(payload);
        if (!ok || (result.length != 0 &&
            (result.length != 32 || !abi.decode(result, (bool)))))
            revert TokenCallFailed();
    }

    function _marketCall(bytes memory payload) internal {
        (bool ok, bytes memory result) = market.call(payload);
        if (!ok || result.length != 32) revert TokenCallFailed();
        uint256 code = abi.decode(result, (uint256));
        if (code != 0) revert ProtocolError(code);
    }

    function supply(address recipient, uint256 amount, uint256 minShares)
        external onlyGuard nonReentrant returns (uint256 shares)
    {
        if (recipient == address(0) || amount == 0 || minShares == 0)
            revert InvalidAmount();
        if (_balance(underlyingToken, address(this)) != 0 ||
            _balance(shareToken, address(this)) != 0) revert BalanceMismatch();
        _tokenCall(underlyingToken, abi.encodeWithSignature(
            "transferFrom(address,address,uint256)", msg.sender, address(this), amount));
        if (_balance(underlyingToken, address(this)) != amount) revert BalanceMismatch();
        _tokenCall(underlyingToken, abi.encodeWithSignature(
            "approve(address,uint256)", market, 0));
        _tokenCall(underlyingToken, abi.encodeWithSignature(
            "approve(address,uint256)", market, amount));
        _marketCall(abi.encodeWithSignature("mint(uint256)", amount));
        _tokenCall(underlyingToken, abi.encodeWithSignature(
            "approve(address,uint256)", market, 0));
        if (_balance(underlyingToken, address(this)) != 0) revert BalanceMismatch();
        shares = _balance(shareToken, address(this));
        if (shares < minShares) revert BalanceMismatch();
        _tokenCall(shareToken, abi.encodeWithSignature(
            "transfer(address,uint256)", recipient, shares));
        if (_balance(shareToken, address(this)) != 0) revert BalanceMismatch();
        emit Supplied(recipient, amount, shares);
    }

    function redeem(address recipient, uint256 shares, uint256 minUnderlying)
        external onlyGuard nonReentrant returns (uint256 output)
    {
        if (recipient == address(0) || shares == 0 || minUnderlying == 0)
            revert InvalidAmount();
        if (_balance(underlyingToken, address(this)) != 0 ||
            _balance(shareToken, address(this)) != 0) revert BalanceMismatch();
        _tokenCall(shareToken, abi.encodeWithSignature(
            "transferFrom(address,address,uint256)", msg.sender, address(this), shares));
        if (_balance(shareToken, address(this)) != shares) revert BalanceMismatch();
        _marketCall(abi.encodeWithSignature("redeem(uint256)", shares));
        if (_balance(shareToken, address(this)) != 0) revert BalanceMismatch();
        output = _balance(underlyingToken, address(this));
        if (output < minUnderlying) revert BalanceMismatch();
        _tokenCall(underlyingToken, abi.encodeWithSignature(
            "transfer(address,uint256)", recipient, output));
        if (_balance(underlyingToken, address(this)) != 0) revert BalanceMismatch();
        emit Redeemed(recipient, shares, output);
    }
}
