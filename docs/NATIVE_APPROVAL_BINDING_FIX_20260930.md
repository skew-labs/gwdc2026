# Native approval binding repair — 2026-09-30

## Observed failure

A valid Native Stake review and approval were blocked before preflight with
`Market evidence is stale or belongs to another network. Refresh the plan.`
The browser applied the general allocation guard to native execution. The
comparison referenced the enriched compiler snapshot, while the public evidence
list contained its wallet, lending and voting components. Their hashes were
different by construction. The message incorrectly attributed this to expiry.

## Repair and boundaries

- Project the retained compiler snapshot root with its original expiry; never
  renew evidence during a workspace read.
- Project each durable native step's graph ID, graph hash, step ID and expiry,
  plus its account, network, plan and policy bindings. Existing saved workspaces
  receive these fields through the response projection without rewriting state.
- Validate the independently reviewed current native step before requesting a
  wallet signature. Stake, vote, claim, unstake and withdrawal each retain their
  own fresh review. A later vote or exit does not depend on an expired entry quote.
- Keep ownership, confirmed policy, explicit approval, approval expiry, action,
  workflow cursor and previous receipt checks. Missing bindings fail closed.
- Server account, chain anchor, fee, transaction-byte and signature validation
  remain in place. No approval or signed transaction is synthesized by this fix.

## Verification

- 91 frontend tests passed; TypeScript check and production build passed.
- Native backend suite: 31 passed, one optional PostgreSQL test skipped because
  its dedicated test DSN was not supplied. Three bridge tests also passed.
- A private local regression used the actual saved workspace at its approval
  timestamp: the previous guard reproduced the error, the repaired projection
  and guard passed, and the same approval was still rejected after expiry.
- Durable projection regression confirms GET does not change persisted revision,
  approval or review expiry and does not broadcast.

These checks establish the repaired approval-to-preflight gate. They do not
establish a new user-signed Native Stake or voting receipt. The wallet owner must
refresh the expired step, approve the reviewed action and sign in TronLink.
