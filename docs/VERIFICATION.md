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

## Remaining limits

- Mainnet USDD full live lifecycle has not been verified end to end. Guarded execution and regression coverage are not a substitute for that evidence.
- Nile Vault USDD and Nile JustLend token incompatibility blocks the corresponding route.
- Current constraints and market costs can produce `INFEASIBLE`. The system must not invent two successful plans just to fill the comparison UI.
- A short test cycle cannot validate annualized realized yield. Testnet fees/rates are not a mainnet return promise.
- Isolated PostgreSQL recovery drills do not establish production off-host RPO/RTO.
- Earlier PR01–PR12 evidence is historical, including `docs/history/pr12-evidence-manifest.json`, which predates the live native cycle.

## Publication provenance

This is a publication of the team's project source and accumulated implementation, including the integrated faat frontend. Commit and document dates are preserved as history where available; file modification times are not used as proof of original authorship. Publication does not rewrite earlier work as a new build or claim competition eligibility beyond the organizer's rules.
