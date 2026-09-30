# Native Stake 2.0 — exact scope of the September 30 extension

The original Nile lending path remains available. This release adds a second executable product family: native TRX staking for Energy followed by a separately approved representative vote. It reuses mandate validation, integer allocation calculations, source hashing, wallet authentication, PostgreSQL compare-and-swap records, the shared spend ledger, receipt recovery, Watch scheduling and the existing interface. It introduces no server signing key.

## What it does

1. Read the Nile account, solid head, chain reward/resource parameters, witness votes and current brokerage percentages. Stale, incomplete or conflicting inputs block a quote.
2. Enumerate native stake/cash allocations in 1% increments, round stakes down to whole TRX, and run the existing constraint engine. Each valid plan must cover the full modeled lifecycle costs and the initial reward activation delay.
3. Select two sufficiently different feasible allocations. Show less committed capital versus more projected voting income. Their amounts and returns come from the calculation, not language-model text.
4. Require explicit Native Stake **and** voting permission. Existing mandates do not gain either permission automatically.
5. Review/sign stake, reconcile it, then review/sign voting. These are two distinct transactions. A stake without a vote is displayed as `STAKED_NOT_VOTED` and Watch asks the user to finish or recover it.
6. Show the original forecast, accrued but unclaimed rewards, claimed rewards, actual fees and net income. Reward claims, unstaking and withdrawal after the chain waiting period each require a fresh review and wallet signature.
7. Watch re-reads current conditions and compares holding with a complete exit. Paid fees remain in accounting but are not charged twice to the incremental decision. Notifications never authorize transactions.

## How income is calculated

For a selected active representative, voting income includes its share of block rewards plus its share of voting rewards, after the representative's commission. Current chain parameters determine the reward amounts and maintenance interval. The forecast accounts for two skipped block slots per maintenance interval and adds the user's proposed votes to both denominators.

```text
annual rewards per vote = blocks per year × (1 − commission) ×
  (voting reward per block / total top-127 votes after this allocation
   + block reward / 27 / selected representative votes after this allocation)
```

This is a **simple variable-rate projection**, not compounded APY. It credits neither hypothetical Energy rental proceeds nor token appreciation. Rate changes, representative rotation and commission changes affect actual results. Current rates are not locked by signing.

The initial model uses one whole maintenance interval without earnings. At capture, Nile's interval was 30 minutes, producing 28,704 expected blocks/day. Mainnet's six-hour interval must not be silently substituted.

## Costs and signing scope

Five native actions are reserved: stake, vote, reward claim, unstake and final withdrawal. Each gets a conservative 400-byte bandwidth bound. With the observed 1,000 sun/byte burn price, this is **0.4 TRX per action, 2 TRX in total**. Free bandwidth can reduce actual cost, but the investment gate does not depend on it. A reward claim is optional; additional claims need new fee budget.

Native system transactions do not execute TVM contracts, so this path does not use the jTRX contract's Energy estimate. They also have **no on-chain `fee_limit`**. The service bounds serialized size and rechecks the resource price and account before sending. A subsequent chain price change cannot be undone. Actual receipt fees, including failed transaction fees, enter the shared ledger.

The codec validates exact protobuf contract type IDs, owner, Energy resource, amount, votes, permission ID, TAPOS, expiry, JSON projection and transaction hash. The browser independently checks official TronWeb serialization before requesting a signature. A mismatched field or oversized payload is rejected.

## Recovery and accounting

Submission intent is persisted before broadcast. Retries follow the same transaction identity; uncertain submission never means “safe to send another.” Reconciliation checks a known Nile anchor, a fresh solid head, exact transaction bytes, block membership, native result, actual fee and expected account changes. Contradictions leave the transaction disputed. An expired request only unlocks after solid-chain time has passed expiry plus a margin and both full-node and solid-node lookups show absence.

These checks use node responses; they are not a locally verified Merkle/light-client proof. Account reads are not an atomic historical snapshot. External changes that contradict expected deltas therefore block attribution.

First-entry support is deliberately limited to wallets without prior stake, outgoing delegated stake, votes, queued unfreezes or unpaid prior rewards. The first release tracks one native position at a time. Existing unrelated positions are not silently merged into its income.

## Verification performed

- Native lifecycle tests cover all five action encodings, official TronWeb serialization, wrong signatures, account/price changes, unresolved retries, durable-before-send ordering, disputed receipts, failed fees, cumulative spending and unstake waiting periods.
- A live Nile RPC built an **unsigned** staking transaction for the reviewed account; the strict codec and official TronWeb transaction serializer accepted the exact bytes.
- Fresh live calculations with 300 TRX, 365 days and a 20% cash floor produced 25 eligible allocations. The selected plans were 165 TRX stake / 133 TRX cash and 237 TRX stake / 61 TRX cash, with 2 TRX reserved. Captured net projections were approximately 0.010920 and 0.888413 TRX. They are dated testnet observations, not fixed UI values or mainnet returns.
- Actual interface components were exercised in an isolated browser harness through comparison, transaction review, approval and wallet-handoff callbacks. The harness cannot sign or broadcast.
- **No real user-signed Native Stake 2.0 transaction has been demonstrated in this release's evidence yet.** Existing live jTRX receipts remain documented separately.

## Remaining scope

This extends executable coverage; it does not complete the original joint multi-product optimizer. Native-enabled conditions currently select the native/cash candidate family. JustLend-only conditions select the existing lending family. Concurrent native+lending portfolio optimization, sTRX execution, provider-bound Energy rental and a live mainnet USDD loop remain outside demonstrated scope. Nile Vault/destination USDD token mismatch remains blocked.

Sources: [reward calculation](https://developers.tron.network/docs/reward-calculation), [consensus](https://developers.tron.network/docs/concensus), [staking APIs](https://developers.tron.network/docs/staking-apis), [protocol definitions](https://github.com/tronprotocol/protocol/tree/master/core/contract).
