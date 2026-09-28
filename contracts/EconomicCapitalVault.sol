// SPDX-License-Identifier: MIT
pragma solidity 0.8.20;

/// @notice Narrow registry interface. The vault treats a commitment as
///         executable only when the strict attested registry path is active.
interface IEconomicVaultRegistry {
    function activeBasket(bytes32 bindingHash) external view returns (bool);
    function baskets(bytes32 bindingHash) external view returns (
        bytes32 policyId, bytes32 stateRoot, bytes32 basketHash,
        bytes32 offchainCommitmentHash, uint256 amount, uint64 validUntil,
        bool committed, bool revoked, bool consumed
    );
    function policies(bytes32 policyId) external view returns (
        bytes32 policyHash, address asset, address target,
        uint256 maxAmount, uint64 expiresAt, bool active
    );
    function attestationRequired(bytes32 policyId) external view returns (bool);
    function authenticatedSourceByBinding(bytes32 bindingHash) external view returns (bytes32);
    function attestedEpochByBinding(bytes32 bindingHash) external view returns (uint64);
    function attestorEpoch() external view returns (uint64);
}

/// @notice Owner-funded, owner-signed basket execution boundary.
/// @dev The registry is evidence storage; this vault is a separate capital
///      boundary. Only an owner signature over exact route bytes can release
///      one committed input amount to a pinned target. The target must consume
///      the registry basket and return an approved output asset in the same
///      transaction. This contract is not deployed or audited.
contract EconomicCapitalVault {
    bytes32 private constant EXECUTION_DOMAIN = bytes32("ECONOMIC_VAULT_EXECUTE_V1");
    bytes32 private constant BATCH_DOMAIN = bytes32("ECONOMIC_VAULT_BATCH_V1");
    uint256 private constant SECP256K1_HALF_ORDER =
        0x7fffffffffffffffffffffffffffffff5d576e7357a4501ddfe92f46681b20a0;

    struct BasketTerms {
        bytes32 policyId;
        bytes32 basketHash;
        address inputAsset;
        address target;
        uint256 amount;
        uint64 validUntil;
    }

    struct VaultOrder {
        bytes32 bindingHash;
        address outputAsset;
        uint256 minOutput;
        uint64 deadline;
        bytes routeData;
    }

    address public immutable owner;
    address public guardian;
    IEconomicVaultRegistry public immutable registry;
    bytes32 public immutable registryCodeHash;
    bool public paused;
    bool private entered;

    mapping(address => bytes32) public allowedAssetCodeHash;
    mapping(address => bytes32) public allowedTargetCodeHash;
    mapping(bytes32 => bool) public executed;

    event PauseChanged(bool paused, address indexed actor);
    event GuardianChanged(address indexed oldGuardian, address indexed newGuardian);
    event AssetCodePinned(address indexed asset, bytes32 codeHash);
    event TargetCodePinned(address indexed target, bytes32 codeHash);
    event Deposited(address indexed asset, uint256 amount);
    event Withdrawn(address indexed asset, uint256 amount);
    event BasketExecuted(bytes32 indexed bindingHash, address indexed inputAsset,
                         address indexed outputAsset, address target,
                         uint256 inputAmount, uint256 outputAmount, bytes32 routeHash);
    event BatchExecuted(bytes32 indexed parentBasketHash, bytes32 indexed ordersHash,
                        uint256 orderCount);

    error OnlyOwner();
    error OnlyOwnerOrGuardian();
    error Paused();
    error ReentrantCall();
    error InvalidCode();
    error InvalidBasket();
    error InvalidRoute();
    error InvalidSignature();
    error InvalidTokenResponse();
    error BalanceMismatch();
    error TargetCallFailed();

    constructor(address registry_, address guardian_) {
        if (registry_ == address(0) || registry_.code.length == 0) revert InvalidCode();
        owner = msg.sender;
        guardian = guardian_;
        registry = IEconomicVaultRegistry(registry_);
        registryCodeHash = registry_.codehash;
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

    function setGuardian(address nextGuardian) external onlyOwner {
        address oldGuardian = guardian;
        guardian = nextGuardian;
        emit GuardianChanged(oldGuardian, nextGuardian);
    }

    function pause() external {
        if (msg.sender != owner && msg.sender != guardian) revert OnlyOwnerOrGuardian();
        if (!paused) {
            paused = true;
            emit PauseChanged(true, msg.sender);
        }
    }

    function unpause() external onlyOwner {
        if (paused) {
            paused = false;
            emit PauseChanged(false, msg.sender);
        }
    }

    function pinAsset(address asset, bool allowed) external onlyOwner {
        bytes32 codeHash = allowed ? _codeHash(asset) : bytes32(0);
        allowedAssetCodeHash[asset] = codeHash;
        emit AssetCodePinned(asset, codeHash);
    }

    function pinTarget(address target, bool allowed) external onlyOwner {
        bytes32 codeHash = allowed ? _codeHash(target) : bytes32(0);
        allowedTargetCodeHash[target] = codeHash;
        emit TargetCodePinned(target, codeHash);
    }

    function _codeHash(address account) internal view returns (bytes32) {
        if (account == address(0) || account.code.length == 0) revert InvalidCode();
        return account.codehash;
    }

    function _requirePinnedAsset(address asset) internal view {
        bytes32 pinned = allowedAssetCodeHash[asset];
        if (pinned == bytes32(0) || _codeHash(asset) != pinned) revert InvalidCode();
    }

    function _requirePinnedTarget(address target) internal view {
        bytes32 pinned = allowedTargetCodeHash[target];
        if (pinned == bytes32(0) || _codeHash(target) != pinned) revert InvalidCode();
    }

    function _balance(address asset) internal view returns (uint256) {
        (bool ok, bytes memory response) = asset.staticcall(
            abi.encodeWithSignature("balanceOf(address)", address(this))
        );
        if (!ok || response.length != 32) revert InvalidTokenResponse();
        return abi.decode(response, (uint256));
    }

    function _tokenCall(address asset, bytes memory payload) internal {
        (bool ok, bytes memory response) = asset.call(payload);
        if (!ok || (response.length != 0 &&
            (response.length != 32 || !abi.decode(response, (bool)))))
            revert InvalidTokenResponse();
    }

    /// @notice Only the customer owner can fund this vault. Fee-on-transfer
    ///         and inconsistent balance-reporting tokens are rejected.
    function deposit(address asset, uint256 amount) external onlyOwner nonReentrant {
        _requirePinnedAsset(asset);
        if (amount == 0) revert InvalidRoute();
        uint256 beforeBalance = _balance(asset);
        _tokenCall(asset, abi.encodeWithSignature(
            "transferFrom(address,address,uint256)", owner, address(this), amount));
        if (_balance(asset) != beforeBalance + amount) revert BalanceMismatch();
        emit Deposited(asset, amount);
    }

    /// @notice Emergency withdrawal goes only to the customer owner.
    function withdraw(address asset, uint256 amount) external onlyOwner nonReentrant {
        _requirePinnedAsset(asset);
        if (amount == 0) revert InvalidRoute();
        uint256 beforeBalance = _balance(asset);
        if (beforeBalance < amount) revert BalanceMismatch();
        _tokenCall(asset, abi.encodeWithSignature("transfer(address,uint256)", owner, amount));
        if (_balance(asset) + amount != beforeBalance) revert BalanceMismatch();
        emit Withdrawn(asset, amount);
    }

    function _terms(bytes32 bindingHash) internal view returns (BasketTerms memory terms) {
        if (_codeHash(address(registry)) != registryCodeHash ||
            !registry.activeBasket(bindingHash) || executed[bindingHash]) revert InvalidBasket();
        (bytes32 policyId, , bytes32 basketHash, , uint256 amount, uint64 validUntil,
         bool committed, bool revoked, bool consumed) = registry.baskets(bindingHash);
        (, address inputAsset, address target, , , bool active) = registry.policies(policyId);
        if (!committed || revoked || consumed || !active || amount == 0 ||
            !registry.attestationRequired(policyId) ||
            registry.authenticatedSourceByBinding(bindingHash) == bytes32(0) ||
            registry.attestedEpochByBinding(bindingHash) != registry.attestorEpoch())
            revert InvalidBasket();
        terms = BasketTerms(policyId, basketHash, inputAsset, target, amount, validUntil);
    }

    /// @notice Exact SHA-256 bytes for a raw secp256k1 owner signature.
    /// @dev Wallet-specific signing prefixes are intentionally unsupported.
    function executionDigest(
        bytes32 bindingHash, address inputAsset, uint256 amount, address target,
        address outputAsset, uint256 minOutput, bytes32 routeHash, uint64 deadline
    ) public view returns (bytes32) {
        return sha256(abi.encode(
            EXECUTION_DOMAIN, address(this), block.chainid, address(registry),
            bindingHash, inputAsset, amount, target, outputAsset,
            minOutput, routeHash, deadline
        ));
    }

    function batchDigest(bytes32 parentBasketHash, bytes32[] memory orderDigests,
                         uint64 deadline) public view returns (bytes32) {
        return sha256(abi.encode(
            BATCH_DOMAIN, address(this), block.chainid, address(registry),
            parentBasketHash, sha256(abi.encode(orderDigests)), deadline
        ));
    }

    function _verifyOwnerSignature(bytes32 messageHash, bytes calldata signature)
        internal view
    {
        if (signature.length != 65) revert InvalidSignature();
        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly {
            r := calldataload(signature.offset)
            s := calldataload(add(signature.offset, 32))
            v := byte(0, calldataload(add(signature.offset, 64)))
        }
        if (r == bytes32(0) || s == bytes32(0) ||
            uint256(s) > SECP256K1_HALF_ORDER || (v != 27 && v != 28) ||
            ecrecover(messageHash, v, r, s) != owner) revert InvalidSignature();
    }

    function _validateRoute(
        BasketTerms memory terms, address outputAsset,
        uint256 minOutput, uint64 deadline, bytes calldata routeData
    ) internal view returns (bytes32 routeHash) {
        if (deadline <= block.timestamp || deadline > terms.validUntil ||
            outputAsset == terms.inputAsset || minOutput == 0 || routeData.length < 4)
            revert InvalidRoute();
        _requirePinnedAsset(terms.inputAsset);
        _requirePinnedAsset(outputAsset);
        _requirePinnedTarget(terms.target);
        routeHash = sha256(routeData);
    }

    function _authorizeExecution(
        bytes32 bindingHash, BasketTerms memory terms, address outputAsset,
        uint256 minOutput, uint64 deadline, bytes calldata routeData,
        bytes calldata ownerSignature
    ) internal view returns (bytes32 routeHash) {
        routeHash = _validateRoute(terms, outputAsset,
                                   minOutput, deadline, routeData);
        _verifyOwnerSignature(executionDigest(
            bindingHash, terms.inputAsset, terms.amount, terms.target,
            outputAsset, minOutput, routeHash, deadline), ownerSignature);
    }

    function _executeRoute(
        bytes32 bindingHash, BasketTerms memory terms, address outputAsset,
        uint256 minOutput, bytes calldata routeData
    ) internal returns (uint256 outputAmount) {
        uint256 inputBefore = _balance(terms.inputAsset);
        uint256 outputBefore = _balance(outputAsset);
        if (inputBefore < terms.amount) revert BalanceMismatch();
        executed[bindingHash] = true;
        _tokenCall(terms.inputAsset, abi.encodeWithSignature(
            "transfer(address,uint256)", terms.target, terms.amount));
        (bool success, ) = terms.target.call(routeData);
        if (!success) revert TargetCallFailed();
        (, , , , , , , , bool consumed) = registry.baskets(bindingHash);
        if (!consumed) revert InvalidBasket();
        if (_balance(terms.inputAsset) + terms.amount != inputBefore)
            revert BalanceMismatch();
        uint256 outputAfter = _balance(outputAsset);
        if (outputAfter < outputBefore || outputAfter - outputBefore < minOutput)
            revert BalanceMismatch();
        outputAmount = outputAfter - outputBefore;
    }

    /// @notice Anyone may relay the exact owner-approved route. No token
    ///         allowance is granted to the target; only this basket's input
    ///         amount is transferred. The target must consume the basket and
    ///         return at least minOutput to the vault atomically.
    function executeBasket(
        bytes32 bindingHash, address outputAsset, uint256 minOutput,
        uint64 deadline, bytes calldata routeData, bytes calldata ownerSignature
    ) external nonReentrant returns (uint256 outputAmount) {
        if (paused) revert Paused();
        BasketTerms memory terms = _terms(bindingHash);
        bytes32 routeHash = _authorizeExecution(
            bindingHash, terms, outputAsset, minOutput, deadline,
            routeData, ownerSignature);
        outputAmount = _executeRoute(bindingHash, terms, outputAsset,
                                     minOutput, routeData);
        emit BasketExecuted(bindingHash, terms.inputAsset, outputAsset, terms.target,
                            terms.amount, outputAmount, routeHash);
    }

    /// @notice An owner signs the entire ordered set of child baskets. Every
    ///         leg must refer to the same parent basket hash. A failing leg
    ///         reverts all earlier token transfers and registry consumptions.
    function executeBatch(
        bytes32 parentBasketHash, VaultOrder[] calldata orders,
        uint64 batchDeadline, bytes calldata batchSignature
    ) external nonReentrant {
        if (paused) revert Paused();
        if (parentBasketHash == bytes32(0) || orders.length < 2 || orders.length > 8 ||
            batchDeadline <= block.timestamp) revert InvalidRoute();
        bytes32[] memory orderDigests = new bytes32[](orders.length);
        bytes32 previous;
        for (uint256 i = 0; i < orders.length; i++) {
            VaultOrder calldata order = orders[i];
            if (order.bindingHash <= previous || batchDeadline > order.deadline)
                revert InvalidRoute();
            previous = order.bindingHash;
            BasketTerms memory terms = _terms(order.bindingHash);
            if (terms.basketHash != parentBasketHash) revert InvalidBasket();
            bytes32 routeHash = _validateRoute(
                terms, order.outputAsset,
                order.minOutput, order.deadline, order.routeData);
            orderDigests[i] = executionDigest(
                order.bindingHash, terms.inputAsset, terms.amount, terms.target,
                order.outputAsset, order.minOutput, routeHash, order.deadline);
        }
        bytes32 ordersHash = sha256(abi.encode(orderDigests));
        _verifyOwnerSignature(batchDigest(parentBasketHash, orderDigests,
                                          batchDeadline), batchSignature);
        for (uint256 i = 0; i < orders.length; i++) {
            VaultOrder calldata order = orders[i];
            BasketTerms memory terms = _terms(order.bindingHash);
            uint256 outputAmount = _executeRoute(order.bindingHash, terms,
                order.outputAsset, order.minOutput, order.routeData);
            emit BasketExecuted(order.bindingHash, terms.inputAsset,
                                order.outputAsset, terms.target, terms.amount,
                                outputAmount, sha256(order.routeData));
        }
        emit BatchExecuted(parentBasketHash, ordersHash, orders.length);
    }
}
