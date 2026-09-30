# Automatic planning, failure isolation and evidence

## User flow

1. Chat extracts an editable draft. A rejected model response opens the condition editor; it does not turn into a financial-service outage or an unrelated USDD recommendation.
2. Review all fields, including carried-forward spending/loss limits and Native Stake permissions. Saving a draft does not confirm it.
3. Click **Confirm & compare** once. The gateway durably queues the exact draft hash/version, network, originating agent and verified wallet. It returns a job acknowledgement immediately.
4. The worker confirms that exact draft, fetches the confirmed policy, requests a comparison from current observations and writes a persistent Plan A / Plan B message. The page polls the task status. A browser reload does not interrupt the task.
5. Select an eligible plan, review its exact actions, then separately approve and sign in the wallet. The planning worker never calls graph approval, signing or broadcasting endpoints.

## Invariants

- The same workspace/wallet/network/draft/agent tuple has one planning task. Repeated clicks do not create parallel confirmations.
- Confirmation and comparison use different stable finance idempotency keys. A running task resumes after gateway restart. Its confirmed policy hash is persisted before comparison.
- A changed draft hash/version cannot be confirmed. A changed confirmed policy cannot inherit another task's result.
- The worker validates the returned network, policy and comparison binding before publishing the message.
- Invalid model output (`intent: null`) is a valid failure response. It is handled independently of workspace reads and portfolio refreshes.
- Failed optional operations do not discard healthy workspace data. Failed refreshes do not present an old hold recommendation as freshly checked.
- Zero or one eligible candidate produces a blocker result, never a fabricated second recommendation. Existing loss and spending limits are not silently widened.
- The job database migration is additive. Deployment backs up SQLite and changed runtime files first.

## Verification on 2026-09-30

Frontend suite: **61 tests across 11 files**, including gateway HTTP integration, source ownership, transaction signing guards, durable messages and new planning cases. TypeScript and production build pass.

New regression coverage: nullable extraction, extraction transport failure, one-click automatic comparison without any user chat message, duplicate confirmation clicks, restart during comparison, current-wallet scope, unverified callers, infeasible candidates, quote failure/retry and stale drafts. Every recommendation test asserts absence of graph approval and execution.

Live integration: isolated local gateway and finance repository, actual Nile RPC observations, the application's real calculation engine and actual HTTP confirmation path. Authentication was an isolated test fixture; it does **not** prove a new user wallet signature. Transaction endpoints were disabled. Production conditions were unchanged.

Test conditions: 300 TRX, 365 days, at least 20% immediate cash, no borrowing, explicit Stake 2.0/voting permission. Single, cumulative, daily-loss and stress-loss caps were each 300 TRX; loss scenarios were 100%. This deliberately permits loss of the full test capital and is not a suggested risk setting. Total cost budget was 30 TRX; transaction cap 15 TRX. Native costs came from chain parameters rather than the old lending cost assumptions.

At 01:17 UTC, 101 grid candidates were checked; 25 were eligible. Two options appeared automatically in approximately 10 seconds, with **zero chat prompts, signatures or broadcasts**:

| Computed option | Staked | Available cash | Entry/exit reserve | Full-horizon projected net income |
|---|---:|---:|---:|---:|
| More cash available | 165 TRX | 133 TRX | 2 TRX | 0.010920 TRX |
| Higher voting income | 237 TRX | 61 TRX | 2 TRX | 0.888413 TRX |

These are timestamped variable Nile forecasts, not realized income, guaranteed returns or mainnet rates. They are distinct allocations within Native Stake plus cash, not two different protocol products. The current rollout still does not optimize mixed Native Stake/JustLend portfolios or make the incompatible Nile USDD loop executable.

The production workspace had retained 2 TRX single/cumulative limits and a 32 TRX loss cap with a 100% daily-loss assumption. Entering a larger capital amount alone cannot override those conditions. The editor now exposes this conflict and offers an explicit draft-only spending-limit update. The user must review and confirm any change.

Machine-readable evidence: [artifacts/automatic_planning/verification.json](../artifacts/automatic_planning/verification.json). Live deployment on OVH verified 13 file hashes and service health at 01:21 UTC.

## Conversation routing recovery — September 30, 01:45 UTC

An already-open client could omit `agent_id` on confirmation. The gateway then sent the result to the workspace's first agent, while the user stayed in the conversation that contained the reviewed draft. Each new edit changed the shared policy hash, leaving only obsolete references visible in that conversation.

- Missing conversation context now resolves from the exact draft card and authenticated workspace/network. An ambiguous destination is rejected rather than guessed.
- The current policy-bound comparison is exposed in its originating conversation even for previously misrouted jobs. Historical records and conditions remain intact.
- Korean option/investment-plan status questions use calculated results without condition extraction. Amount-bearing change requests still go through extraction and review.
- Infeasible comparison cards show retained investment/loss limits and Native Stake permission. A Native Stake alternative opens an editable draft, explicitly discloses proposed full-budget loss limits and still requires Save plus Confirm. It does not grant transaction authority.
- Confirmation's generated API contract includes optional `agent_id`.

Verification: **64 frontend tests passed**, including an old-client confirmation with multiple agents, a simulated historically misrouted job, reload recovery and the exact Korean missing-options question. TypeScript and production build passed. The real engine and live Nile inputs again produced 25 eligible allocations from 101; the second agent received both options after one confirmation in 9.46 seconds. The first agent received no message. Browser selection of Plan B survived reload. This was an isolated authentication fixture, with no signatures, broadcasts or changes to production conditions.

Existing restrictive policies remain restrictive: a 300 TRX budget does not override a 2 TRX investment cap or enable Native Stake. An unavailable second option is not fabricated.
