# Documentation map

Current submission: **faat · Finance AI Agent Tron · September 30, 2026**.

## Follow the product

| Question | Read |
| --- | --- |
| What does the user do, and what is working? | [Root overview](../README.md) |
| How does confirmation automatically create two options? | [Automatic planning](AUTOMATIC_PLANNING.md) |
| What happens when the constraints cannot be met? | [Failure model](FAILURE_MODEL.md), [planning invariants](AUTOMATIC_PLANNING.md#invariants) |
| How are market data, calculations and signing separated? | [Architecture](ARCHITECTURE.md) |
| Which product families are supported? | [Current capability table](../README.md#what-exists-today), [Native Stake](NATIVE_STAKING.md) |
| What has actually run on-chain? | [Verification](VERIFICATION.md), [captured receipts](../evidence/nile/) |
| How do I run or verify it? | [Setup](SETUP.md), [CI workflow](../.github/workflows/verify.yml) |
| Where do the datasets and synthetic examples come from? | [Data and provenance](DATA.md) |

## Read the code in execution order

1. [`frontend/server/conversation.ts`](../frontend/server/conversation.ts): typed chat-to-finance boundary.
2. [`frontend/server/index.ts`](../frontend/server/index.ts): wallet scope, durable planning task and publication of comparison cards.
3. [`machine_bridge.py`](../src/finance_service/machine_bridge.py): financial workspace and orchestration.
4. [`stake_comparison.py`](../src/finance_service/stake_comparison.py), [`plan_compiler.py`](../src/economic_machine/plan_compiler.py): current observations and deterministic eligibility.
5. [`approval.py`](../src/economic_machine/approval.py), [`signed_tx_validation.py`](../src/economic_machine/signed_tx_validation.py): exact authority and payload checks.
6. [`native_execution.py`](../src/finance_service/native_execution.py), [`stake_execution.py`](../src/finance_service/stake_execution.py), [`usdd_execution.py`](../src/finance_service/usdd_execution.py): route-specific execution and recovery.
7. [`native_performance.py`](../src/finance_service/native_performance.py), [`machine_worker.py`](../src/finance_service/machine_worker.py): accounting and recurring review.

## Evidence levels

- **Signed and reconciled:** public Nile JustLend supply/redemption receipts.
- **Live read / unsigned:** Native Stake calculations, node-built transaction and TronWeb encoding.
- **Synthetic regression:** controlled failure, recovery and notification scenarios.
- **Pending:** first user-signed native lifecycle, funded mainnet USDD lifecycle and joint staking/lending optimization.

A screenshot proves what the interface displayed. Its linked inputs and receipts establish the stronger underlying claim. See [verification details](VERIFICATION.md).

## Historical material

[`history/`](history/) contains original plans and PR01–PR12 implementation records. Their dates and claims are preserved as history. Current behavior is described by this index, the root README and current source. [`DECISIONS.md`](DECISIONS.md) explains implementation tradeoffs; [`operations/postgres-resilience.md`](operations/postgres-resilience.md) covers environment-dependent recovery drills.
