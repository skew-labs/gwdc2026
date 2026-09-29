# faat

**Finance AI Agent Tron**

Describe an allocation. Compare feasible plans. Sign the exact transaction. Account for what actually happened.

faat is a conversational TRON allocation workspace built for **GWDC 2026 · TRON B**. Alpha helps express an investment objective, Vault turns confirmed conditions into transaction reviews, and Watch checks whether an existing position still fits those conditions. Users create their agents and keep control of their wallet.

[Open faat on Nile](https://machine.148-113-153-116.nip.io/?network=nile) · [Run locally](docs/SETUP.md) · [Architecture](docs/ARCHITECTURE.md) · [Failure cases](docs/FAILURE_MODEL.md) · [Verification](docs/VERIFICATION.md)

[![Verify faat](https://github.com/skew-labs/gwdc2026/actions/workflows/verify.yml/badge.svg)](https://github.com/skew-labs/gwdc2026/actions/workflows/verify.yml)

## Start with a receipt

The integrated application completed a real Nile JustLend supply and full redemption, signed by the user through TronLink and reconciled against chain receipts and positions.

| Operation | Principal / output | Actual fee | Chain evidence |
| --- | --- | --- | --- |
| Supply TRX | 1 TRX supplied | 8.0894 TRX | [Supply transaction](https://nile.tronscan.org/#/transaction/877d3dcb132ff55b37f0cb24286c9ce3fce4e0966c21bdc4b0924f6d7652a7c2) |
| Redeem jTRX | 89.46435527 jTRX → 1 TRX | 7.2569 TRX | [Redemption transaction](https://nile.tronscan.org/#/transaction/b83d55426b98f73ff458e61d003f062a58d2588bbcad21b3d02ebf72f1f0d628) |

That tiny integration test produced **0 TRX income before fees and −15.3463 TRX net P&L**. We display that loss. An RPC success, a minted share token, and a profitable investment are three different claims. The current investment-entry gate rejects candidates whose projected income does not cover modeled costs; a remedial exit has separate rules.

Nile is a test network. These transactions establish an integration path, not future yield or mainnet economics. [Exact scope and remaining gaps](docs/VERIFICATION.md).

## The product loop

1. **Explain the objective.** Chat captures capital, base asset, horizon, immediate cash, withdrawals and risk constraints. Missing terms remain missing. General risk appetite does not authorize debt.
2. **Confirm conditions.** A versioned mandate records the terms the user reviewed. Editing conditions invalidates dependent unsigned work; it cannot erase an unresolved signed transaction.
3. **Compare eligible plans.** Alternatives share capital, horizon and the relevant snapshot. The breakdown separates base income, incentives, borrowing interest, entry/exit costs, liquidity and risk. Infeasible conditions produce reasons rather than fabricated alternatives.
4. **Review and sign.** The selected plan becomes a transaction graph. Approval binds the account, network, conditions, graph, amounts, fee bounds and expiry. TronLink signs each reviewed step.
5. **Reconcile.** The service records submission intent before broadcasting, checks receipts and post-state, and accounts for actual costs. A timeout stays unresolved until evidence resolves it.
6. **Watch.** A scheduled or requested review fetches fresh state and compares maintaining the position with adjusting it, including recovery and reinvestment costs. Actionable changes create account notifications. A notification does not authorize execution.

## Architecture: every boundary has a different job

```text
USER
  │ conversation / condition confirmation / plan choice
  ▼
React workspace ──────────────── TronLink
  │                              │ user signature only
  ▼                              │
Node gateway ◀───────────────────┘
  ├─ sessions, wallet proof, CSRF, persistent chat, agent workspace
  ├─ Kiln / Qwen3-32B: language and structured intent
  └─ authenticated finance requests
       ▼
Python finance service
  ├─ conditions → snapshot → eligible comparison → transaction graph
  ├─ approval → fresh preflight → exact signed-payload validation
  ├─ durable submission → solid receipt → position reconciliation
  └─ performance ledger / Watch reviews / account notifications
       │                  │                         │
       ▼                  ▼                         ▼
Economic Machine     PostgreSQL               TRON / protocols
integer amounts      scoped records           network-bound reads
constraints          version checks           JustLend / USDD
execution guards     leases + journal         receipts + post-state

Research/data lane: source capture → normalization → quality checks
                    → explicit replay/research release → oracle checks
```

The gateway's chat store and the finance ledger are separate. The financial service authenticates the gateway before trusting workspace or verified-wallet headers. The model cannot manufacture signing authority. [Detailed module and state map](docs/ARCHITECTURE.md).

## Where to read the code

| Concern | Entry points |
| --- | --- |
| Agent experience and conversation cards | [`frontend/src/App.tsx`](frontend/src/App.tsx), [`frontend/server/conversation.ts`](frontend/server/conversation.ts) |
| Durable chat, ownership proof and gateway | [`frontend/server/index.ts`](frontend/server/index.ts), [`frontend/server/store.ts`](frontend/server/store.ts) |
| Financial integration boundary | [`machine_bridge.py`](src/finance_service/machine_bridge.py), [`entrypoint.py`](src/finance_service/entrypoint.py) |
| Arithmetic, constraints and plans | [`values.py`](src/economic_machine/values.py), [`mandate.py`](src/economic_machine/mandate.py), [`plan_compiler.py`](src/economic_machine/plan_compiler.py) |
| Graph, approval and signed bytes | [`tx_graph.py`](src/economic_machine/tx_graph.py), [`approval.py`](src/economic_machine/approval.py), [`signed_tx_validation.py`](src/economic_machine/signed_tx_validation.py) |
| Native TRX supply, redemption and recovery | [`native_execution.py`](src/finance_service/native_execution.py), [`native_adjustments.py`](src/finance_service/native_adjustments.py), [`native_recovery.py`](src/finance_service/native_recovery.py) |
| USDD collateral, issuance, looping and unwind | [`usdd_workflow.py`](src/economic_machine/usdd_workflow.py), [`usdd_execution.py`](src/finance_service/usdd_execution.py), [`usdd_review.py`](src/finance_service/usdd_review.py) |
| Watch, costs and portfolio decisions | [`machine_worker.py`](src/finance_service/machine_worker.py), [`portfolio_review.py`](src/finance_service/portfolio_review.py), [`rebalance_gate.py`](src/finance_service/rebalance_gate.py) |
| Expected versus actual performance | [`native_performance.py`](src/finance_service/native_performance.py), [`performance.py`](src/economic_machine/performance.py) |
| Persistence, concurrency and integrity | [`postgres_repository.py`](src/finance_service/postgres_repository.py), [`postgres_runtime.py`](src/finance_service/postgres_runtime.py), [`db/migrations/`](db/migrations) |
| On-chain guards and adapters | [`contracts/`](contracts), [`scripts/verify_execution_guard.py`](scripts/verify_execution_guard.py), [`scripts/verify_economic_vault.py`](scripts/verify_economic_vault.py) |
| Source capture and dataset provenance | [`src/finagent/`](src/finagent), [`config/sources.json`](config/sources.json), [`cases/`](cases), [`research/fdc/`](research/fdc) |

## Engineering decisions that came from failures

**One omitted zero broke wallet signing.** A zero-value redemption encoded `call_value = 0` explicitly, while the canonical protobuf serializer omits that default. We now compare server bytes against official TronWeb fixtures and check the transaction before opening the signing prompt. See [`test_wallet_encoding.py`](tests/test_wallet_encoding.py) and [`wallet.test.ts`](frontend/tests/wallet.test.ts).

**A timeout is not proof of failure.** The service saves transaction identity and reserves capital before broadcast. Browser reloads recover an unresolved transaction; a retry follows the same ID. Expired prepared transactions require chain absence evidence before the reservation can be released. See [`native_recovery.py`](src/finance_service/native_recovery.py) and the restart/timeout cases in [`test_usdd_execution.py`](tests/test_usdd_execution.py).

**A successful transaction is not yet a reconciled position.** Execution checks receipts and independent position/cash changes. Contradictory evidence remains disputed. Fee accounting must not run twice when the same receipt is observed twice. See [`position_reconciliation.py`](src/economic_machine/position_reconciliation.py) and [`test_native_execution.py`](tests/test_native_execution.py).

**Resetting a chart must not delete a loss.** A new measurement period preserves old accounting and receipts. Open positions, debt and unresolved transactions block a reset; late old receipts do not become income in the new period. See [`test_performance_periods.py`](tests/test_performance_periods.py).

**Leverage amplifies a negative spread too.** Borrow/resupply cycles require explicit debt terms, bounded exposure, current collateral checks and viable net economics. The source includes the USDD execution/recovery path; mainnet end-to-end live verification remains outstanding. See [`test_usdd_workflow.py`](tests/test_usdd_workflow.py) and [`test_usdd_execution.py`](tests/test_usdd_execution.py).

The [failure model](docs/FAILURE_MODEL.md) maps each claim to its code and test. The [decision record](docs/DECISIONS.md) explains the tradeoffs and the evidence that led to them.

## Run and verify

Requirements: Python **3.11+**, Node **24.19+**, pnpm **11.19.0**. Real chat needs your Kiln key; wallet operations need TronLink and the appropriate testnet funds. Complete wiring is in [SETUP.md](docs/SETUP.md).

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[service,contracts,validation]'
cd frontend
pnpm install --frozen-lockfile
cp .env.server.example .env.server.local
# Configure the gateway and finance service using docs/SETUP.md.
pnpm dev:all
```

Run the product regressions from the repository root:

```bash
PYTHONPATH=src:tests python -m unittest \
  test_native_execution test_native_adjustments test_native_performance \
  test_performance_periods test_wallet_encoding test_machine_worker \
  test_usdd_workflow test_usdd_execution test_usdd_comparison
cd frontend
pnpm test
pnpm build
```

Tests with synthetic receipts demonstrate rejection/recovery logic; they do not constitute a live mainnet transaction. PostgreSQL resilience drills and research training have additional environment requirements. [Verification guide](docs/VERIFICATION.md).

GitHub Actions runs these offline product checks and the frontend build. The extended check passed **549 backend tests and 52 frontend tests**, with **48 backend tests skipped** because their dedicated PostgreSQL environment or pinned compiler was unavailable; exact commands and results are in [`evidence/verification/`](evidence/verification). Re-read the two demonstrated Nile receipts with `python scripts/verify_nile_receipts.py --refresh`, or omit `--refresh` to validate the checked-in captures without a network request. This script never signs or broadcasts.

## Data and research

The repository includes collection, normalization, source hashing, quality gates, explicit synthetic cases, schemas and research code. Public observations, synthetic labels and live customer positions have different provenance. Research model outputs do not gain execution authority. Source-license uncertainty is retained in the data pipeline rather than silently promoting data into training. [Data map](docs/DATA.md).

## Scope and history

The public product is **faat**. Internal `machine` route, cookie and storage identifiers remain for compatibility. `frontend/` is the current interface; `web/` is the older reference client. Earlier implementation and design documents are preserved under [`docs/history/`](docs/history) and [`research/`](research); their dated statements are historical, not current completion claims.

Nile native supply and redemption are live-verified. Mainnet USDD full lifecycle is not. Nile's Vault and JustLend USDD incompatibility blocks that route. No maximum-APY, zero-bug or autonomous-profit claim is made.

This publication includes the project source and reviewed evidence. Pitch decks, private customer databases, credentials, signing material and dependency caches are maintained outside the repository.
