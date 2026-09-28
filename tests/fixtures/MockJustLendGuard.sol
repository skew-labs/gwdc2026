// SPDX-License-Identifier: MIT
pragma solidity 0.8.20;

interface IMockUnderlying {
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
}

interface IReentryCallback {
    function onTokenTransfer() external;
}

interface IReentryGuard {
    function executeSupply(bytes32 planHash, bytes32 graphHash, bytes32 stepHash,
        uint256 amount, uint256 minShares, uint256 maxFeeSun,
        uint64 nonce, uint64 deadline) external returns (uint256);
}

contract MockGuardToken {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    bool public falseReturn;
    bool public feeOnTransfer;
    bool public callbackOnTransferFrom;
    address public callbackAccount;

    function setModes(bool falseReturn_, bool feeOnTransfer_) external {
        falseReturn = falseReturn_;
        feeOnTransfer = feeOnTransfer_;
    }

    function setReentryMode(bool enabled, address account) external {
        callbackOnTransferFrom = enabled;
        callbackAccount = account;
    }

    function faucet(address account, uint256 amount) external {
        balanceOf[account] += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        if (falseReturn) return false;
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        if (falseReturn) return false;
        _transfer(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        if (falseReturn) return false;
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= amount, "allowance");
        allowance[from][msg.sender] = allowed - amount;
        _transfer(from, to, amount);
        if (callbackOnTransferFrom && from == callbackAccount && from.code.length != 0) {
            IReentryCallback(from).onTokenTransfer();
        }
        return true;
    }

    function _transfer(address from, address to, uint256 amount) internal {
        require(balanceOf[from] >= amount, "balance");
        balanceOf[from] -= amount;
        balanceOf[to] += feeOnTransfer && amount != 0 ? amount - 1 : amount;
    }
}

contract ReentrantGuardOwner is IReentryCallback {
    IReentryGuard public guard;
    bytes32 public planHash;
    bytes32 public graphHash;
    bytes32 public stepHash;
    uint256 public amount;
    uint256 public minimum;
    uint256 public maxFeeSun;
    uint64 public nonce;
    uint64 public deadline;
    bool public reentryAttempted;
    bool public reentrySucceeded;

    function approveToken(address token, address spender, uint256 value) external {
        require(IMockUnderlying(token).transfer(address(this), 0), "token probe");
        (bool ok, bytes memory result) = token.call(
            abi.encodeWithSignature("approve(address,uint256)", spender, value));
        require(ok && (result.length == 0 || abi.decode(result, (bool))), "approve");
    }

    function startSupply(address guard_, bytes32 planHash_, bytes32 graphHash_,
        bytes32 stepHash_, uint256 amount_, uint256 minimum_, uint256 maxFeeSun_,
        uint64 nonce_, uint64 deadline_) external returns (uint256) {
        guard = IReentryGuard(guard_);
        planHash = planHash_;
        graphHash = graphHash_;
        stepHash = stepHash_;
        amount = amount_;
        minimum = minimum_;
        maxFeeSun = maxFeeSun_;
        nonce = nonce_;
        deadline = deadline_;
        return guard.executeSupply(planHash_, graphHash_, stepHash_, amount_, minimum_,
                                   maxFeeSun_, nonce_, deadline_);
    }

    function onTokenTransfer() external {
        reentryAttempted = true;
        try guard.executeSupply(planHash, graphHash, keccak256(abi.encode(stepHash)),
                                amount, minimum, maxFeeSun, nonce, deadline) {
            reentrySucceeded = true;
        } catch {
            reentrySucceeded = false;
        }
    }
}

contract MockJustLendMarket {
    IMockUnderlying public immutable underlying;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    uint256 public protocolCode;
    bool public skipPosition;
    bool public underDeliver;

    constructor(address underlying_) {
        underlying = IMockUnderlying(underlying_);
    }

    function setModes(uint256 protocolCode_, bool skipPosition_, bool underDeliver_) external {
        protocolCode = protocolCode_;
        skipPosition = skipPosition_;
        underDeliver = underDeliver_;
    }

    function faucetShares(address account, uint256 amount) external {
        balanceOf[account] += amount;
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
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= amount, "allowance");
        allowance[from][msg.sender] = allowed - amount;
        _transfer(from, to, amount);
        return true;
    }

    function _transfer(address from, address to, uint256 amount) internal {
        require(balanceOf[from] >= amount, "balance");
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
    }

    function mint(uint256 amount) external returns (uint256) {
        if (protocolCode != 0) return protocolCode;
        require(underlying.transferFrom(msg.sender, address(this), amount), "underlying");
        if (!skipPosition) balanceOf[msg.sender] += underDeliver ? amount - 1 : amount;
        return 0;
    }

    function redeem(uint256 shares) external returns (uint256) {
        if (protocolCode != 0) return protocolCode;
        require(balanceOf[msg.sender] >= shares, "shares");
        balanceOf[msg.sender] -= shares;
        uint256 output = underDeliver ? shares - 1 : shares;
        require(underlying.transfer(msg.sender, output), "underlying");
        return 0;
    }
}
