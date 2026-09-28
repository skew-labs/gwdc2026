// SPDX-License-Identifier: MIT
pragma solidity 0.8.20;

/// @notice Owner-controlled policy and basket commitment registry for TRON.
/// @dev This contract has no intended deposit or venue-call path and does not
///      make an arbitrary external executor obey its records. TRON native
///      TransferContract can bypass fallback and strand unsolicited TRX here.
///      It is evidence storage, not a custody vault or settlement verifier.
contract EconomicPolicyRegistry {
    struct Policy {
        bytes32 policyHash;
        address asset;
        address target;
        uint256 maxAmount;
        uint64 expiresAt;
        bool active;
    }

    struct BasketRecord {
        bytes32 policyId;
        bytes32 stateRoot;
        bytes32 basketHash;
        bytes32 offchainCommitmentHash;
        uint256 amount;
        uint64 validUntil;
        bool committed;
        bool revoked;
        bool consumed;
    }

    struct AssetBudget {
        uint256 limit;
        uint256 reserved;
        uint256 consumed;
        bool configured;
    }

    bytes32 private constant BASKET_DOMAIN = bytes32("ECONOMIC_BASKET_REGISTRY_V1");
    bytes32 private constant ATTESTATION_DOMAIN = bytes32("ECONOMIC_BASKET_ATTEST_V1");
    uint256 private constant SECP256K1_HALF_ORDER =
        0x7fffffffffffffffffffffffffffffff5d576e7357a4501ddfe92f46681b20a0;

    address public immutable owner;
    address public guardian;
    bool public paused;
    mapping(bytes32 => Policy) public policies;
    mapping(bytes32 => BasketRecord) public baskets;
    mapping(bytes32 => bytes32) public bindingByCommitment;
    // Accounting is per policy, never a claim about wallet balances or fills.
    mapping(bytes32 => uint256) public reservedBasketAmount;
    mapping(bytes32 => uint256) public consumedBasketAmount;
    // Shared across all policies for one asset. Still not a wallet balance.
    mapping(address => AssetBudget) public assetBudgets;
    // Attestation is a quorum over a stated source hash, never proof of market truth.
    mapping(address => bool) public attestors;
    uint8 public activeAttestorCount;
    uint64 public attestorEpoch = 1;
    mapping(bytes32 => bool) public attestationRequired;
    mapping(bytes32 => bytes32) public authenticatedSourceByBinding;
    mapping(bytes32 => uint64) public attestedEpochByBinding;

    event PolicyRegistered(
        bytes32 indexed policyId, bytes32 indexed policyHash,
        address indexed asset, address target, uint256 maxAmount, uint64 expiresAt
    );
    event PolicyRevoked(bytes32 indexed policyId);
    event BasketCommitted(
        bytes32 indexed policyId, bytes32 indexed bindingHash,
        bytes32 indexed offchainCommitmentHash, bytes32 stateRoot,
        bytes32 basketHash, address asset, address target,
        uint256 amount, uint64 validUntil
    );
    event BasketRevoked(bytes32 indexed bindingHash, address indexed actor);
    event BasketExpiredReleased(
        bytes32 indexed bindingHash, bytes32 indexed policyId, uint256 amount
    );
    event BasketConsumed(
        bytes32 indexed bindingHash, bytes32 indexed policyId,
        address indexed target, address asset, uint256 amount
    );
    event PauseChanged(bool paused, address indexed actor);
    event GuardianChanged(address indexed previousGuardian, address indexed newGuardian);
    event AssetBudgetConfigured(
        address indexed asset, uint256 previousLimit, uint256 nextLimit
    );
    event AttestorChanged(address indexed attestor, bool active, uint64 epoch);
    event AttestedPolicyRegistered(bytes32 indexed policyId);
    event BasketAttested(bytes32 indexed bindingHash, bytes32 indexed sourceHash, uint64 epoch);

    error OnlyOwner();
    error OnlyOwnerOrGuardian();
    error Paused();
    error EmptyIdentity();
    error InvalidPolicy();
    error PolicyAlreadyRegistered();
    error PolicyNotActive();
    error PolicyExpired();
    error AmountOutsidePolicy();
    error BasketAlreadyCommitted();
    error CommitmentAlreadyBound();
    error BasketNotActive();
    error BasketNotExpired();
    error BasketBudgetExceeded();
    error InvalidAssetBudget();
    error AssetBudgetBelowUsed();
    error AssetBudgetExceeded();
    error OnlyPolicyTarget();
    error InvalidAttestor();
    error AttestationRequired();
    error AttestationNotRequired();
    error InvalidAttestation();

    constructor(address guardian_) {
        owner = msg.sender;
        guardian = guardian_;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert OnlyOwner();
        _;
    }

    function setGuardian(address nextGuardian) external onlyOwner {
        if (attestors[nextGuardian]) revert InvalidAttestor();
        address previous = guardian;
        guardian = nextGuardian;
        emit GuardianChanged(previous, nextGuardian);
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

    /// @notice Configure a per-asset registry ceiling shared by all policies.
    /// @dev Owner authority is not a customer signature or proof of holdings.
    function configureAssetBudget(address asset, uint256 limit) external onlyOwner {
        if (asset == address(0) || limit == 0) revert InvalidAssetBudget();
        AssetBudget storage budget = assetBudgets[asset];
        if (limit < budget.reserved + budget.consumed) revert AssetBudgetBelowUsed();
        uint256 previous = budget.limit;
        budget.limit = limit;
        budget.configured = true;
        emit AssetBudgetConfigured(asset, previous, limit);
    }

    /// @notice Rotate the customer-selected attestation key set. Any change
    ///         invalidates signatures prepared for the previous epoch.
    function setAttestor(address attestor, bool active) external onlyOwner {
        if (attestor == address(0) || attestor == owner || attestor == guardian
            || attestors[attestor] == active) revert InvalidAttestor();
        if (active) {
            if (activeAttestorCount == 8) revert InvalidAttestor();
            activeAttestorCount += 1;
        } else {
            activeAttestorCount -= 1;
        }
        attestors[attestor] = active;
        attestorEpoch += 1;
        emit AttestorChanged(attestor, active, attestorEpoch);
    }

    function registerPolicy(
        bytes32 policyId, bytes32 policyHash, address asset, address target,
        uint256 maxAmount, uint64 expiresAt
    ) external onlyOwner {
        _registerPolicy(policyId, policyHash, asset, target, maxAmount, expiresAt);
    }

    /// @notice The strict policy path cannot later be downgraded to legacy
    ///         commitBasket, including after key rotation.
    function registerAttestedPolicy(
        bytes32 policyId, bytes32 policyHash, address asset, address target,
        uint256 maxAmount, uint64 expiresAt
    ) external onlyOwner {
        if (activeAttestorCount < 2) revert InvalidAttestor();
        _registerPolicy(policyId, policyHash, asset, target, maxAmount, expiresAt);
        attestationRequired[policyId] = true;
        emit AttestedPolicyRegistered(policyId);
    }

    function _registerPolicy(
        bytes32 policyId, bytes32 policyHash, address asset, address target,
        uint256 maxAmount, uint64 expiresAt
    ) internal {
        if (paused) revert Paused();
        if (policyId == bytes32(0) || policyHash == bytes32(0)) revert EmptyIdentity();
        if (asset == address(0) || target == address(0) || maxAmount == 0
            || expiresAt <= block.timestamp) revert InvalidPolicy();
        if (!assetBudgets[asset].configured || maxAmount > assetBudgets[asset].limit)
            revert InvalidAssetBudget();
        if (policies[policyId].policyHash != bytes32(0)) revert PolicyAlreadyRegistered();
        policies[policyId] = Policy(policyHash, asset, target, maxAmount, expiresAt, true);
        emit PolicyRegistered(policyId, policyHash, asset, target, maxAmount, expiresAt);
    }

    function revokePolicy(bytes32 policyId) external onlyOwner {
        Policy storage policy = policies[policyId];
        if (!policy.active) revert PolicyNotActive();
        policy.active = false;
        emit PolicyRevoked(policyId);
    }

    /// @notice Domain-separated SHA-256 over fixed-width ABI words.
    /// @dev The caller must independently verify the offchain commitment and
    ///      registered policy. This hash does not attest input truth or execute.
    function computeBasketBinding(
        bytes32 policyId, bytes32 stateRoot, bytes32 basketHash,
        bytes32 offchainCommitmentHash, uint256 amount, uint64 validUntil
    ) public view returns (bytes32) {
        Policy storage policy = policies[policyId];
        return sha256(abi.encode(
            BASKET_DOMAIN, address(this), block.chainid, policyId,
            policy.policyHash, stateRoot, basketHash, offchainCommitmentHash,
            policy.asset, policy.target, amount, validUntil
        ));
    }

    /// @notice Verifiers sign these exact SHA-256 bytes. The binding already
    ///         contains the policy, state, basket, amount, expiry and chain.
    function attestationDigest(bytes32 bindingHash, bytes32 sourceHash)
        public view returns (bytes32)
    {
        return sha256(abi.encode(
            ATTESTATION_DOMAIN, address(this), block.chainid,
            bindingHash, sourceHash, attestorEpoch
        ));
    }

    function _verifyAttestations(bytes32 messageHash, bytes[] calldata signatures)
        internal view
    {
        if (signatures.length != 2 || activeAttestorCount < 2) revert InvalidAttestation();
        address previous = address(0);
        for (uint256 i = 0; i < 2; i++) {
            bytes calldata signature = signatures[i];
            if (signature.length != 65) revert InvalidAttestation();
            bytes32 r;
            bytes32 s;
            uint8 v;
            assembly {
                r := calldataload(signature.offset)
                s := calldataload(add(signature.offset, 32))
                v := byte(0, calldataload(add(signature.offset, 64)))
            }
            if (r == bytes32(0) || s == bytes32(0)
                || uint256(s) > SECP256K1_HALF_ORDER || (v != 27 && v != 28))
                revert InvalidAttestation();
            address signer = ecrecover(messageHash, v, r, s);
            if (!attestors[signer] || signer <= previous) revert InvalidAttestation();
            previous = signer;
        }
    }

    function commitBasket(
        bytes32 policyId, bytes32 stateRoot, bytes32 basketHash,
        bytes32 offchainCommitmentHash, uint256 amount, uint64 validUntil
    ) external onlyOwner returns (bytes32 bindingHash) {
        if (attestationRequired[policyId]) revert AttestationRequired();
        return _commitBasket(policyId, stateRoot, basketHash,
                             offchainCommitmentHash, amount, validUntil);
    }

    /// @notice Only the policy owner registers; two distinct, active verifier
    ///         keys must additionally attest the exact binding and source hash.
    function commitBasketAttested(
        bytes32 policyId, bytes32 stateRoot, bytes32 basketHash,
        bytes32 offchainCommitmentHash, uint256 amount, uint64 validUntil,
        bytes32 authenticatedSourceHash, bytes[] calldata signatures
    ) external onlyOwner returns (bytes32 bindingHash) {
        if (!attestationRequired[policyId]) revert AttestationNotRequired();
        if (authenticatedSourceHash == bytes32(0)) revert EmptyIdentity();
        bindingHash = computeBasketBinding(policyId, stateRoot, basketHash,
                                           offchainCommitmentHash, amount, validUntil);
        _verifyAttestations(attestationDigest(bindingHash, authenticatedSourceHash), signatures);
        bindingHash = _commitBasket(policyId, stateRoot, basketHash,
                                    offchainCommitmentHash, amount, validUntil);
        authenticatedSourceByBinding[bindingHash] = authenticatedSourceHash;
        attestedEpochByBinding[bindingHash] = attestorEpoch;
        emit BasketAttested(bindingHash, authenticatedSourceHash, attestorEpoch);
    }

    function _commitBasket(
        bytes32 policyId, bytes32 stateRoot, bytes32 basketHash,
        bytes32 offchainCommitmentHash, uint256 amount, uint64 validUntil
    ) internal returns (bytes32 bindingHash) {
        if (paused) revert Paused();
        if (stateRoot == bytes32(0) || basketHash == bytes32(0)
            || offchainCommitmentHash == bytes32(0)) revert EmptyIdentity();
        Policy storage policy = policies[policyId];
        if (!policy.active) revert PolicyNotActive();
        if (block.timestamp >= policy.expiresAt) revert PolicyExpired();
        if (amount == 0 || amount > policy.maxAmount) revert AmountOutsidePolicy();
        if (validUntil <= block.timestamp || validUntil > policy.expiresAt) revert InvalidPolicy();
        uint256 used = reservedBasketAmount[policyId] + consumedBasketAmount[policyId];
        if (used > policy.maxAmount || amount > policy.maxAmount - used)
            revert BasketBudgetExceeded();
        AssetBudget storage assetBudget = assetBudgets[policy.asset];
        uint256 assetUsed = assetBudget.reserved + assetBudget.consumed;
        if (!assetBudget.configured || assetUsed > assetBudget.limit
            || amount > assetBudget.limit - assetUsed) revert AssetBudgetExceeded();
        bindingHash = computeBasketBinding(policyId, stateRoot, basketHash,
                                           offchainCommitmentHash, amount, validUntil);
        if (bindingHash == bytes32(0)) revert EmptyIdentity();
        if (baskets[bindingHash].committed) revert BasketAlreadyCommitted();
        if (bindingByCommitment[offchainCommitmentHash] != bytes32(0))
            revert CommitmentAlreadyBound();
        baskets[bindingHash] = BasketRecord(
            policyId, stateRoot, basketHash, offchainCommitmentHash,
            amount, validUntil, true, false, false
        );
        bindingByCommitment[offchainCommitmentHash] = bindingHash;
        reservedBasketAmount[policyId] += amount;
        assetBudget.reserved += amount;
        emit BasketCommitted(policyId, bindingHash, offchainCommitmentHash,
                             stateRoot, basketHash, policy.asset, policy.target,
                             amount, validUntil);
    }

    function revokeBasket(bytes32 bindingHash) external {
        if (msg.sender != owner && msg.sender != guardian) revert OnlyOwnerOrGuardian();
        BasketRecord storage basket = baskets[bindingHash];
        if (!basket.committed || basket.revoked || basket.consumed) revert BasketNotActive();
        basket.revoked = true;
        reservedBasketAmount[basket.policyId] -= basket.amount;
        assetBudgets[policies[basket.policyId].asset].reserved -= basket.amount;
        emit BasketRevoked(bindingHash, msg.sender);
    }

    /// @notice Anyone may release an expired, unconsumed reservation.
    /// @dev The commitment identity stays bound; releasing never permits replay.
    function releaseExpiredBasket(bytes32 bindingHash) external {
        BasketRecord storage basket = baskets[bindingHash];
        if (!basket.committed || basket.revoked || basket.consumed) revert BasketNotActive();
        if (block.timestamp < basket.validUntil) revert BasketNotExpired();
        basket.revoked = true;
        reservedBasketAmount[basket.policyId] -= basket.amount;
        assetBudgets[policies[basket.policyId].asset].reserved -= basket.amount;
        emit BasketExpiredReleased(bindingHash, basket.policyId, basket.amount);
    }

    /// @notice A policy target can consume a basket once in its own transaction.
    /// @dev The target must enforce trade semantics itself. This registry makes
    ///      no external call and moves no asset. If the target's outer
    ///      transaction reverts, the consumed flag also rolls back.
    function consumeBasket(bytes32 bindingHash)
        external returns (address asset, uint256 amount, bytes32 basketHash)
    {
        if (paused) revert Paused();
        BasketRecord storage basket = baskets[bindingHash];
        if (!basket.committed || basket.revoked || basket.consumed) revert BasketNotActive();
        Policy storage policy = policies[basket.policyId];
        if (msg.sender != policy.target) revert OnlyPolicyTarget();
        if (!policy.active || block.timestamp >= policy.expiresAt
            || block.timestamp >= basket.validUntil) revert BasketNotActive();
        basket.consumed = true;
        reservedBasketAmount[basket.policyId] -= basket.amount;
        consumedBasketAmount[basket.policyId] += basket.amount;
        AssetBudget storage assetBudget = assetBudgets[policy.asset];
        assetBudget.reserved -= basket.amount;
        assetBudget.consumed += basket.amount;
        emit BasketConsumed(bindingHash, basket.policyId, policy.target,
                            policy.asset, basket.amount);
        return (policy.asset, basket.amount, basket.basketHash);
    }

    /// @dev Read-only status; it cannot by itself move or reserve assets.
    function activeBasket(bytes32 bindingHash) external view returns (bool) {
        BasketRecord storage basket = baskets[bindingHash];
        Policy storage policy = policies[basket.policyId];
        return !paused && basket.committed && !basket.revoked && !basket.consumed
            && policy.active
            && block.timestamp < basket.validUntil
            && block.timestamp < policy.expiresAt;
    }

    /// @notice Remaining registry budget, not spendable wallet or vault balance.
    function basketBudgetRemaining(bytes32 policyId) external view returns (uint256) {
        Policy storage policy = policies[policyId];
        uint256 used = reservedBasketAmount[policyId] + consumedBasketAmount[policyId];
        if (used >= policy.maxAmount) return 0;
        return policy.maxAmount - used;
    }

    /// @notice Unreserved registry ceiling; it is not spendable capital.
    function assetBudgetRemaining(address asset) external view returns (uint256) {
        AssetBudget storage budget = assetBudgets[asset];
        uint256 used = budget.reserved + budget.consumed;
        if (!budget.configured || used >= budget.limit) return 0;
        return budget.limit - used;
    }
}
