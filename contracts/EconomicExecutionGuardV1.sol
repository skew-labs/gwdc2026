// SPDX-License-Identifier: MIT
pragma solidity 0.8.20;

interface IGuardAdapterV1 {
    function underlyingToken() external view returns (address);
    function market() external view returns (address);
    function shareToken() external view returns (address);
    function supply(address recipient, uint256 amount, uint256 minShares)
        external returns (uint256);
    function redeem(address recipient, uint256 shares, uint256 minUnderlying)
        external returns (uint256);
}

interface IExecutionGuardTokenV1 {
    function balanceOf(address account) external view returns (uint256);
}

/// @notice User-owned, fixed-adapter boundary for one JustLend v1 market.
/// @dev The user's transaction signature authorizes the exact calldata by
///      making msg.sender the immutable owner. The contract has no relayer
///      signature path, delegatecall, arbitrary target, arbitrary recipient,
///      upgrade hook, withdrawal function or persistent custody design.
contract EconomicExecutionGuardV1 {
    bytes32 private constant DOMAIN = bytes32("ECONOMIC_EXECUTION_GUARD_V1");

    address public immutable owner;
    IGuardAdapterV1 public immutable adapter;
    address public immutable underlyingToken;
    address public immutable market;
    address public immutable shareToken;
    bytes32 public immutable adapterCodeHash;
    bytes32 public immutable underlyingCodeHash;
    bytes32 public immutable marketCodeHash;
    bytes32 public immutable shareCodeHash;
    uint256 public immutable maxSupplyAmount;
    uint256 public immutable maxRedeemShares;
    uint256 public immutable cumulativeSupplyLimit;
    uint256 public cumulativeSupplied;
    uint64 public nextNonce;
    bool private entered;

    mapping(bytes32 => bool) public usedStep;

    event GuardExecution(bytes32 indexed planHash, bytes32 indexed graphHash,
                         bytes32 indexed stepHash, uint8 action, uint256 inputAmount,
                         uint256 outputAmount, uint256 maxFeeSun, uint64 nonce);

    error OnlyOwner();
    error ReentrantCall();
    error InvalidBinding();
    error InvalidCommitment();
    error InvalidAmount();
    error InvalidNonce();
    error Expired();
    error TokenCallFailed();
    error BalanceMismatch();

    constructor(address owner_, address adapter_, uint256 maxSupplyAmount_,
                uint256 maxRedeemShares_, uint256 cumulativeSupplyLimit_) {
        if (owner_ == address(0) || adapter_ == address(0) || adapter_.code.length == 0 ||
            maxSupplyAmount_ == 0 || maxRedeemShares_ == 0 ||
            cumulativeSupplyLimit_ < maxSupplyAmount_) revert InvalidBinding();
        owner = owner_;
        adapter = IGuardAdapterV1(adapter_);
        adapterCodeHash = adapter_.codehash;
        address underlying = IGuardAdapterV1(adapter_).underlyingToken();
        address venue = IGuardAdapterV1(adapter_).market();
        address shares = IGuardAdapterV1(adapter_).shareToken();
        if (underlying == address(0) || venue == address(0) || shares == address(0) ||
            underlying == venue || underlying == shares || underlying.code.length == 0 ||
            venue.code.length == 0 || shares.code.length == 0) revert InvalidBinding();
        underlyingToken = underlying;
        market = venue;
        shareToken = shares;
        underlyingCodeHash = underlying.codehash;
        marketCodeHash = venue.codehash;
        shareCodeHash = shares.codehash;
        maxSupplyAmount = maxSupplyAmount_;
        maxRedeemShares = maxRedeemShares_;
        cumulativeSupplyLimit = cumulativeSupplyLimit_;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert OnlyOwner();
        _;
    }

    modifier nonReentrant() {
        if (entered) revert ReentrantCall();
        entered = true;
        _;
        entered = false;
    }

    function _checkCode() internal view {
        if (address(adapter).codehash != adapterCodeHash ||
            underlyingToken.codehash != underlyingCodeHash ||
            market.codehash != marketCodeHash || shareToken.codehash != shareCodeHash)
            revert InvalidBinding();
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

    function executionDigest(uint8 action, bytes32 planHash, bytes32 graphHash,
        bytes32 stepHash, uint256 amount, uint256 minOutput, uint256 maxFeeSun,
        uint64 nonce, uint64 deadline) public view returns (bytes32) {
        return sha256(abi.encode(DOMAIN, address(this), block.chainid, owner,
            address(adapter), action, planHash, graphHash, stepHash, amount,
            minOutput, maxFeeSun, nonce, deadline));
    }

    function _authorize(bytes32 planHash, bytes32 graphHash, bytes32 stepHash,
                        uint256 amount, uint256 minimum, uint256 maxFeeSun,
                        uint64 nonce, uint64 deadline) internal {
        if (planHash == bytes32(0) || graphHash == bytes32(0) || stepHash == bytes32(0)
            || usedStep[stepHash]) revert InvalidCommitment();
        if (amount == 0 || minimum == 0 || maxFeeSun == 0) revert InvalidAmount();
        if (nonce != nextNonce) revert InvalidNonce();
        if (deadline <= block.timestamp) revert Expired();
        usedStep[stepHash] = true;
        nextNonce = nonce + 1;
        _checkCode();
    }

    function executeSupply(bytes32 planHash, bytes32 graphHash, bytes32 stepHash,
        uint256 amount, uint256 minShares, uint256 maxFeeSun,
        uint64 nonce, uint64 deadline) external onlyOwner nonReentrant returns (uint256 output) {
        _authorize(planHash, graphHash, stepHash, amount, minShares, maxFeeSun,
                   nonce, deadline);
        if (amount > maxSupplyAmount || cumulativeSupplied + amount > cumulativeSupplyLimit)
            revert InvalidAmount();
        uint256 guardInputBefore = _balance(underlyingToken, address(this));
        uint256 guardSharesBefore = _balance(shareToken, address(this));
        if (guardInputBefore != 0 || guardSharesBefore != 0) revert BalanceMismatch();
        uint256 ownerSharesBefore = _balance(shareToken, owner);
        _tokenCall(underlyingToken, abi.encodeWithSignature(
            "transferFrom(address,address,uint256)", owner, address(this), amount));
        if (_balance(underlyingToken, address(this)) != guardInputBefore + amount)
            revert BalanceMismatch();
        _tokenCall(underlyingToken, abi.encodeWithSignature(
            "approve(address,uint256)", address(adapter), 0));
        _tokenCall(underlyingToken, abi.encodeWithSignature(
            "approve(address,uint256)", address(adapter), amount));
        output = adapter.supply(owner, amount, minShares);
        _tokenCall(underlyingToken, abi.encodeWithSignature(
            "approve(address,uint256)", address(adapter), 0));
        if (output < minShares || _balance(underlyingToken, address(this)) != guardInputBefore ||
            _balance(shareToken, address(this)) != guardSharesBefore ||
            _balance(shareToken, owner) != ownerSharesBefore + output)
            revert BalanceMismatch();
        cumulativeSupplied += amount;
        emit GuardExecution(planHash, graphHash, stepHash, 1, amount, output, maxFeeSun, nonce);
    }

    function executeRedeem(bytes32 planHash, bytes32 graphHash, bytes32 stepHash,
        uint256 shares, uint256 minUnderlying, uint256 maxFeeSun,
        uint64 nonce, uint64 deadline) external onlyOwner nonReentrant returns (uint256 output) {
        _authorize(planHash, graphHash, stepHash, shares, minUnderlying, maxFeeSun,
                   nonce, deadline);
        if (shares > maxRedeemShares) revert InvalidAmount();
        uint256 guardSharesBefore = _balance(shareToken, address(this));
        uint256 guardInputBefore = _balance(underlyingToken, address(this));
        if (guardSharesBefore != 0 || guardInputBefore != 0) revert BalanceMismatch();
        uint256 ownerInputBefore = _balance(underlyingToken, owner);
        _tokenCall(shareToken, abi.encodeWithSignature(
            "transferFrom(address,address,uint256)", owner, address(this), shares));
        if (_balance(shareToken, address(this)) != guardSharesBefore + shares)
            revert BalanceMismatch();
        _tokenCall(shareToken, abi.encodeWithSignature(
            "approve(address,uint256)", address(adapter), 0));
        _tokenCall(shareToken, abi.encodeWithSignature(
            "approve(address,uint256)", address(adapter), shares));
        output = adapter.redeem(owner, shares, minUnderlying);
        _tokenCall(shareToken, abi.encodeWithSignature(
            "approve(address,uint256)", address(adapter), 0));
        if (output < minUnderlying || _balance(shareToken, address(this)) != guardSharesBefore ||
            _balance(underlyingToken, address(this)) != guardInputBefore ||
            _balance(underlyingToken, owner) != ownerInputBefore + output)
            revert BalanceMismatch();
        emit GuardExecution(planHash, graphHash, stepHash, 2, shares, output, maxFeeSun, nonce);
    }
}
