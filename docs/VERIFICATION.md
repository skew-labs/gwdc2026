# Verified scope — 2026-09-30

## Live Nile cycle

Supply: `877d3dcb132ff55b37f0cb24286c9ce3fce4e0966c21bdc4b0924f6d7652a7c2`.

Redemption: `b83d55426b98f73ff458e61d003f062a58d2588bbcad21b3d02ebf72f1f0d628`.

The user signed both through TronLink. The native route supplied 1 TRX and later redeemed 89.46435527 jTRX for 1 TRX. Recorded fees were 8.0894 and 7.2569 TRX, respectively (80,894 and 72,569 Energy). Round-trip income before fees was 0 TRX; net P&L was −15.3463 TRX. The service reconciled the resulting position as closed.

Public RPC receipt captures in `evidence/nile/` can be matched to those transaction IDs. Receipt confirmation and local service reconciliation are distinct evidence: the chain records establish the transactions; the source/tests establish the reconciliation rules. No private workspace dump is needed to claim chain inclusion.

## Regression evidence

`evidence/verification/` contains the publication verification summary. It identifies the commands and test counts actually run for this source. Synthetic tests cover malformed signatures, cost increases, restart/timeout recovery, receipt conflicts, debt consent and measurement-period attribution. Read assertions in the linked tests; counts alone do not prove correctness.

The repository also contains contract compilation/runtime verification code, PostgreSQL isolation and recovery drills, source-data validators and research evaluation modules. They require their relevant toolchains/environments. A check's presence is not a claim that it ran successfully in this publication session.

## Verified product capabilities

Real Qwen3-32B requests through Kiln, persistent conversations, condition editing/confirmation, authenticated financial integration, plan review, native supply/redemption signing, durable reconciliation, Watch and expected/actual accounting have been exercised during development. Historical records retain the state they observed; current scope is summarized here and in the root README.

## Earlier internal flow check — before Native Stake and automatic planning

- Extended local suite: 597 backend tests discovered, 549 passed, 48 skipped; all 52 frontend tests passed. Type checking and production build passed. Skipped tests require dedicated PostgreSQL test databases or pinned Solidity compilers and are not counted as passing.
- Live Qwen3 extracted an isolated 300 TRX / 365 day / 20% cash / no-borrowing request with no execution authority. The current Nile comparison enumerated 101 candidates and returned `NO_TWO_VIABLE_PLANS`. The observed jTRX annual yield fraction was `0.000002652496477862`; the test explicitly reserved 15 TRX entry and 15 TRX exit costs. This is a rejection example, not a successful two-plan investment demonstration.
- An actual production Watch queue job fetched fresh Nile state and completed with `HOLD`. The existing schedule and financial conditions were preserved. A separate synthetic test exercised an `ADJUST` notification and its UI path to a new portfolio review; it is not evidence of a live adjustment opportunity.
- The actual UI components displayed captured portfolio accounting and the live-data rejection. Isolated component scenarios exercised two-plan selection, expired withdrawal refresh, approval/wallet-handoff callbacks and account alerts. These callbacks did not open a real wallet or submit a transaction.
- A sparse-policy editor defect was fixed: omitted USDD/protocol caps now reopen as zero, consistent with the core's deny-by-default interpretation. Explicit caps remain unchanged; a new policy still requires user input. Three regression tests cover this boundary.

These earlier checks did not establish a continuous live two-plan → new transaction → performance → adjustment recording. Later live calculations below establish viable candidates; a new wallet-signed native lifecycle is still pending.

## Native staking extension — September 30

The new native route was checked with live market reads and an unsigned node-built transaction, official TronWeb serialization and synthetic five-action lifecycle/recovery tests. See [Native staking scope](NATIVE_STAKING.md). No new native user signature or on-chain native lifecycle receipt is claimed.

## Latest automatic-planning check — September 30, 01:17 UTC

- **61 frontend tests in 11 files**, TypeScript and production build passed after automatic-planning changes. The prior extended backend run recorded **576 passed / 48 skipped / 624 total**. These are separate runs; skipped cases remain unverified in that environment.
- A real gateway HTTP confirmation, Python allocation engine and fresh Nile RPC produced **25 eligible allocations out of 101** under an isolated 300 TRX / 365 day policy with a 20% cash floor and explicit Native Stake/voting permissions.
- One confirmation posted two comparison cards in about 10 seconds; no follow-up chat prompt was sent. There were **zero signatures and zero broadcasts**, and production user conditions were unchanged.
- Plan A: 165 stake + 133 cash + 2 lifecycle cost reserve = 300 TRX; 365-day projected net income 0.010920 TRX. Plan B: 237 + 61 + 2 = 300 TRX; projected net 0.888413 TRX. These are variable Nile forecasts within the same product family.
- The test explicitly allowed full-capital loss through 300 TRX spending/loss caps and 100% loss scenarios. Such settings are test inputs, not recommended limits.
- Browser verification exercised Edit conditions → Save → Confirm & compare → working status → persistent Plan A / Plan B. An isolated authentication fixture was used; it does not establish a new wallet signature.
- Production Qwen3 extraction recovered the user's 300 TRX request into an editable draft. Deployment verified runtime file hashes and service health. User mandate confirmation and transaction execution were not performed by the verification.

[Full verification and invariants](AUTOMATIC_PLANNING.md) · [Machine-readable result](../artifacts/automatic_planning/verification.json) · [Recorded backend result](../evidence/verification/native-summary.json).

## Conversation routing regression — September 30, 01:45 UTC

The subsequent 65-test frontend run, production build and isolated live calculation verify recovery of omitted conversation context, previously misrouted results, option-status questions and selection after reload. See [routing recovery](AUTOMATIC_PLANNING.md#conversation-routing-recovery--september-30-0145-utc). The previous test counts above remain records of their respective runs.

## Accounting comparisons

| Measure | Meaning | Captured JustLend cycle |
| --- | --- | ---: |
| Expected net to date | Forecast accrued to the same observation time, less estimated costs | −16.3703 TRX |
| Actual gross income | Observed income before actual transaction fees | 0 TRX |
| Actual fees | Supply plus redemption fees | 15.3463 TRX |
| Actual net P&L | Gross income minus actual fees | −15.3463 TRX |
| Actual minus expected to date | Variance on a matching period | +1.024 TRX |

The positive variance is a fee saving relative to the estimate. It is not positive investment income. The original full-horizon forecast is a different time basis and is not used for this variance. A period reset preserves these historical events.

## Remaining limits

- Mainnet USDD full live lifecycle has not been verified end to end. Guarded execution and regression coverage are not a substitute for that evidence.
- Nile Vault USDD and Nile JustLend token incompatibility blocks the corresponding route.
- Current constraints and market costs can produce `INFEASIBLE`. The system must not invent two successful plans just to fill the comparison UI.
- A short test cycle cannot validate annualized realized yield. Testnet fees/rates are not a mainnet return promise.
- Isolated PostgreSQL recovery drills do not establish production off-host RPO/RTO.
- Earlier PR01–PR12 evidence is historical, including `docs/history/pr12-evidence-manifest.json`, which predates the live native cycle.

## Publication provenance

This is a publication of the team's project source and accumulated implementation, including the integrated faat frontend. Commit and document dates are preserved as history where available; file modification times are not used as proof of original authorship. Publication does not rewrite earlier work as a new build or claim competition eligibility beyond the organizer's rules.
