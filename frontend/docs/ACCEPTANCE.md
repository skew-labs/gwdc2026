> Earlier integration checklist / API contract. Current verified scope: [faat README](../../README.md) and [verification](../../docs/VERIFICATION.md).

# Verification

> **2026-09-29 OVH update:** Live Qwen3 → confirmed mandate → Nile planning and PostgreSQL Watch jobs have now been verified. Native jTRX supply now passes live unsigned preflight; actual wallet-signed investment and matched position evidence remain pending. Other investment adapters remain blocked. The current evidence and remaining requirements are in [the integration report](../../machine-submission/README.md). Earlier tables below describe the full target contract, not completed live execution.

## Verified in development

- Empty initial database has no agents, conversations, balances or plans.
- Create, rename, edit, archive and restore real persisted agents.
- An actual browser request reached Kiln `qwen3-32b` and produced a saved assistant reply. The provider reported 161 input / 341 output tokens for the first verification call; elapsed time was 5.5 seconds. These are observations of that call, not hardcoded product values.
- Reload restored the conversation. A separate live generation was stopped through the UI and displayed its cancellation state.
- Server tests restart a process against the same database and verify persisted agent state.
- Session isolation, origin/CSRF rejection, duplicate request handling, altered idempotency payload rejection, archive recovery, unavailable providers and a real cryptographic wallet ownership proof/nonce replay rejection.
- Signing guards reject account, network, policy, plan, snapshot, expiry and payload changes.
- 14 automated tests pass. TypeScript checking and production asset build pass.

## Financial integration acceptance

These checks require the connected financial backend and a user-controlled Nile wallet. The included server does not claim chain execution or reconciled positions.

- [ ] Source timestamps, network, protocol terms and raw evidence are valid.
- [ ] Qwen clarification is mapped to a validated draft; the user explicitly confirms it.
- [ ] Two eligible plans share one confirmed mandate and snapshot, or show explicit infeasibility.
- [ ] JustLend and USDD adapters return actual facts. Base yield, incentives, costs, cash and exit liquidity remain separate.
- [ ] Preflight binds exact graph/approval/step/account/network, verifies all invariants and returns the exact unsigned transaction.
- [ ] User signs each transaction in TronLink; backend independently verifies serialized bytes, authority, calldata, fee and amount limits before broadcasting.
- [ ] Unknown submission retains the same txID and reservation across reload and is reconciled without another signature.
- [ ] Allowance success followed by supply failure remains a partial execution.
- [ ] Chain confirmation and protocol position reconciliation are separate recorded events.
- [ ] Expected/actual accounting excludes deposits from returns and preserves original assumptions.
- [ ] Routines have a real worker heartbeat and observation timestamp.
- [ ] Two changed-condition runs retain receipts, hashes and usage linked to their actual model jobs.
