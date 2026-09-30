# Architecture — current submission

## Runtime boundaries

The browser owns interaction state and asks the wallet to sign. The Node gateway owns HTTP sessions, CSRF, persistent conversations and wallet-ownership proofs. The Python service owns financial state, constraints, graph compilation, approval checks, submission and reconciliation. PostgreSQL owns durable records and worker coordination. Protocol RPC responses are observations whose network, age and meaning must be checked.

Alpha, Vault and Watch are user-facing roles within one workspace. They share a scoped financial state through the service. They are not independent signers or three wallets. A role name is not an authorization boundary; authenticated context and the transaction approval are.

## Conversation and conditions

`frontend/server/index.ts` authenticates the session and talks to Kiln using the server-only key. `conversation.ts` connects financial replies to typed service results. `agent_intent.py` and `intent_service.py` separate extracted language from confirmed conditions. The user edits and confirms a versioned mandate before dependent financial work becomes eligible.

Contract validation lives in `frontend/src/api/contracts.ts` and the Python service. `frontend/docs/openapi.json` is generated from the frontend contracts. Amounts cross the API as decimal strings. The economic core uses explicit integer/minimal-unit values and deterministic canonical hashing; display formatting must not change an executable amount.

## Durable automatic comparison

`Confirm & compare` creates a `jobs` / `plan_jobs` record in the gateway SQLite store. It binds the originating workspace, verified wallet, network, agent and exact draft hash/version. The worker confirms that draft through the finance API, persists its confirmed policy hash, reads a fresh workspace and computes a comparison. The resulting cards are stored in the originating conversation.

Confirmation and comparison use separate stable idempotency keys. Repeated clicks share the existing job. Interrupted running tasks are requeued on gateway startup. A changed draft or confirmed policy invalidates the dependent calculation; zero or one eligible candidate yields a blocker message. The worker never requests transaction approval or broadcast. This queue is separate from the PostgreSQL Watch scheduler.

Extraction failures are typed results. A model rejection with `intent: null` cannot turn a healthy financial workspace into a service outage. Optional portfolio/intent requests fail independently. See [automatic planning invariants and recorded evidence](AUTOMATIC_PLANNING.md).

## Observation and planning

`machine_observations.py`, `nile_observations.py`, `usdd_market.py` and `usdd_chain.py` fetch the state needed by each supported route. `snapshot_assembly.py`, product registries and chain-binding modules retain network/product dependencies. A missing or stale observation cannot silently become a zero rate or a healthy balance.

`plan_service.py`, `plan_compiler.py`, `stake_comparison.py` and `usdd_comparison.py` build and compare candidates. Cash availability, withdrawals, explicit debt bounds, collateral, costs and the horizon constrain eligibility. An allocation comparison is invalid if its alternatives quietly use different capital or time assumptions. Calculated candidate rates are projections, not observed performance.

### Product families and the current selection boundary

| Family | Observations and calculation | Execution / recovery |
| --- | --- | --- |
| JustLend TRX + cash | `nile_observations.py`, core plan compilation | `native_execution.py`, `native_adjustments.py`, `native_recovery.py` |
| Native Stake + cash | `stake_market.py`, `stake_comparison.py` | `stake_codec.py`, `stake_execution.py`, `stake_receipt.py`, `stake_watch.py` |
| USDD supply / Vault / bounded loops | `usdd_market.py`, `usdd_chain.py`, `usdd_comparison.py`, `usdd_workflow.py` | `usdd_execution.py`, `usdd_review.py` |

Native-enabled conditions currently use the Native Stake/cash family; JustLend-only conditions use lending/cash. Joint mixed-product optimization remains planned. Two allocations can belong to one family. Their availability depends on the exact policy and fresh market inputs. A listed adapter is not proof of a funded live lifecycle; [verification](VERIFICATION.md) records those separately.

## Execution lifecycle

```text
confirmed conditions + current observations
                  ↓
             eligible plan
                  ↓
        dependency-bound transaction graph
                  ↓
     explicit approval of the exact review
                  ↓
             fresh preflight
                  ↓
       unsigned canonical TRON transaction
                  ↓
           TronLink user signature
                  ↓
     server verifies bytes, signer and bounds
                  ↓
       durable submission record / reservation
                  ↓
               broadcast
                  ↓
    receipt + independent post-state checks
                  ↓
        reconciled position and accounting
```

This is a conceptual flow; concrete status enums and transitions are defined in the source. A transport failure branches into unresolved submission/reconciliation. An inconsistent receipt or post-state can become disputed. Financial work is not declared complete merely because a request returned HTTP 200.

`native_execution.py` handles direct TRX supply into JustLend jTRX (the historical module name uses “native” for the asset). `stake_execution.py` handles the distinct native system-contract Stake 2.0/voting path. `native_adjustments.py` prepares redemption. `native_recovery.py` keeps prepared/submitted work locked until it has evidence for resolution. `usdd_execution.py` coordinates the multi-step USDD path; dependencies must be confirmed before subsequent steps receive approval. The shared economic modules validate hashes, capabilities, signed payloads and evidence.

`contracts/` contains policy/vault/guard/adaptor code and compile manifests. Its presence does not imply every Solidity component was deployed in the live native demonstration. The observed native route calls the reviewed protocol contract and verifies its result. Contract-runtime tests and live protocol transaction evidence are separate verification levels.

## Watch and adjustment

`machine_worker.py` consumes durable PostgreSQL work through `FinanceWorker`. A scheduled run reloads the scoped workspace, checks that the routine dependency still matches, obtains a new review, saves the result and computes the next due time. `portfolio_review.py`, `usdd_review.py` and `rebalance_gate.py` compare the current position with eligible adjustments, including costs and cooldown conditions.

The user sees an account notification for an actionable result. The service can propose a reviewable transaction; the user still approves and signs it. The Watch process does not hold a wallet private key. The local SQLite development profile does not replace the hosted PostgreSQL scheduler.

## Accounting

`native_performance.py` records transaction costs, position changes and performance periods. Expected income over the complete original horizon, expected income up to the observation time, accrued/realized income and net P&L have distinct meanings. Fee-estimate variance is not investment yield.

A period reset creates a new measurement baseline. Historical receipts, spend reservations and accounting remain. It rejects unresolved transactions and existing exposure and uses fresh state to prevent a cached zero position from permitting an invalid reset.

## Persistence and operations

The gateway uses SQLite for sessions, agent records, messages and durable automatic-planning jobs. Financial production state uses PostgreSQL with API/worker role separation, scoped records, row-level controls, optimistic versions, account serialization, worker leases and a hash-linked journal. Migration contents are hash-pinned. The API refuses a worker DSN. Secret files must be owned by the process with restricted permissions; hosted DSNs require verified TLS.

Operational tests and recovery drills are in `tests/test_finance_postgres_runtime.py`, `scripts/postgres_resilience_drill.py`, `scripts/postgres_tls_drill.py` and `scripts/postgres_physical_backup.py`. Those drills need a configured PostgreSQL environment; ordinary unit-test success does not establish production RPO/RTO.

## Research lane

`src/finagent` captures and normalizes observations. `research/fdc` contains synthetic generation, formal allocation oracles, learned-model experiments and validation. These remain distinguishable from the live financial service. Learned outputs cannot skip mandate confirmation, constraints, exact approval or chain validation. See [DATA.md](DATA.md).
