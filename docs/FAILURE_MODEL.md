# Failure model and executable evidence

Each row is a claim reviewers can inspect. Tests named here are deterministic regressions unless explicitly described as external integration tests.

| Failure or adversarial input | Required behavior | Implementation | Test |
| --- | --- | --- | --- |
| Model output sounds like approval | Keep execution authority separate from language | `agent_intent.py`, `mandate_service.py`, `approval.py` | `test_finance_intent_service.py`, `test_economic_approval.py` |
| Conditions produce a loss after current costs | Reject the new investment candidate | `native_execution.py`, `usdd_comparison.py` | `test_native_execution.py`: negative/missing economics and cost-rise cases |
| High risk is interpreted as debt consent | Require explicit debt and collateral constraints | `usdd_workflow.py`, `usdd_execution.py` | `test_usdd_execution.py`: debt-consent/loss gate |
| Wallet or destination changes after review | Refuse approval/signing/submission | `signed_tx_validation.py`, `usdd_execution.py` | `test_native_execution.py`: wrong signer/tampered wallet; `test_usdd_execution.py`: changed wallet/contract |
| Zero-value protobuf encoding differs from TronWeb | Match independent canonical wallet serialization | `native_execution.py`, frontend wallet validation | `test_wallet_encoding.py`, `frontend/tests/wallet.test.ts` |
| RPC times out after a possible broadcast | Preserve ID and reservation; reconcile without blind rebroadcast | `native_execution.py`, `native_recovery.py` | `test_native_execution.py`: timeout/restart case |
| Prepared transaction expires | Require evidence of absence from the relevant chain before unlocking | `native_recovery.py`, `usdd_execution.py` | `test_usdd_execution.py`: expired prepared transaction / solid absence |
| Receipt succeeds but protocol post-state disagrees | Keep execution unresolved/disputed; do not invent a position | `position_reconciliation.py`, native/USDD execution | `test_native_execution.py`, `test_usdd_execution.py`: unexpected solid receipt |
| Same receipt is processed again | Do not charge a second fee or unlock a duplicate action | native/USDD accounting | `test_usdd_execution.py`: solid receipt / duplicate fee |
| Worker crashes, two workers race, or lease expires | Recover durable work without claiming the same job concurrently | `worker.py`, `postgres_repository.py` | `test_finance_postgres_runtime.py` (requires PostgreSQL) |
| Workspace changes during a scheduled review | Reject stale version and retry a fresh review | `machine_worker.py` | `test_machine_worker.py` |
| Cached zero exposure is used to reset performance | Re-read chain state and reject positions/debt/unresolved work | `native_performance.py` | `test_performance_periods.py` |
| Old receipt arrives after a new period begins | Retain historical attribution | `native_performance.py` | `test_performance_periods.py`: late old receipt |
| Applied migration changes or API receives worker credentials | Reject startup/migration | `postgres_runtime.py`, migration runner | `test_finance_postgres_runtime.py` |

Paths without a prefix are under `src/finance_service/`, `src/economic_machine/`, or `tests/` as listed in the root module map. This table is a review guide, not an exhaustive proof. Run the tests and inspect the assertions. Live Nile receipt evidence is in [VERIFICATION.md](VERIFICATION.md).
