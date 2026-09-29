> Historical PR12 checklist. Current scope: [verification](../VERIFICATION.md).

# GWDC 2026 dual-track evidence map

2026-09-29. This document maps product claims to checked-in evidence. It does not declare either track accepted. The public workspace is a historical replay made from a saved public TRON snapshot and synthetic user constraints.

## TRON Challenge B

| Requirement | Current artifact | Status |
| --- | --- | --- |
| User amount, unit, time and withdrawal conditions | `ProductWorkspaceStoryV1.mandate`, PR01 confirmed mandate | VERIFIED_REPLAY |
| At least two allocation plans | PR04 complete enumeration, story plans | VERIFIED_REPLAY |
| JustLend and USDD | jUSDT, jUSDD and USDD Vault collateral/debt cards | VERIFIED_REPLAY |
| APY composition, cost, exit and risk | PR03 cashflows and story product/plan cards | VERIFIED_REPLAY_WITH_ASSUMPTIONS |
| User approval before wallet | PR07 exact approval contract; PR10 replay keeps approval unavailable | CODE_VERIFIED_NO_CUSTOMER_SIGNATURE |
| Deposit, redemption and rebalance | PR06 transaction graph and PR08 reconciliation | REPLAY_ONLY |
| Expected versus actual performance | PR08 ledger; PR10 actual value remains null | NOT_MET_NO_LIVE_POSITION |

USDD Vault is represented as collateral, issued debt, stability fee, liquidation exposure and deployment of issued USDD. It is not shown as a simple deposit APY product. Direct TRX staking/Energy rental and JustLend sTRX remain separate alternatives so their returns are not added twice.

## Furiosa Challenge A

| Requirement | Current artifact | Status |
| --- | --- | --- |
| Qwen3 32B model binding | PR05 strict provider/model boundary | CODE_VERIFIED |
| Actual Kiln Qwen trace | `model_usage.status=NOT_RUN` | NOT_MET |
| Condition change alters plan | Same snapshot run A/B in `evidence-manifest.json` | VERIFIED_DETERMINISTIC_REPLAY |
| Prompt/completion token count and latency | Null in story and manifest | NOT_MEASURED |
| Energy measurement | Null in story and manifest | NOT_MEASURED |
| Matching blockchain receipt/history | PR07/08 replay contracts only | NOT_MET_NO_TESTNET_TRANSACTION |

## Reproduce on Cherry

Use the checked-in PR10 branch in the authorized Cherry worktree. Run the story builder against the saved snapshot whose hash is recorded in `evidence-manifest.json`, then run `tests.test_finance_product_workspace` and the direct PR09 connection tests. Start the service with the checked-in story and web root, open the local HTTP endpoint, and verify the conversation, products, two plans, locked approval, null usage, employee statuses, and refresh restoration.

The service must not be presented as deployed from this procedure. Production PostgreSQL deployment and off-host retention, wallet assertion verification, actual Qwen/Kiln inference, TronLink signature, broadcast, testnet transaction, live position, and realized performance remain external acceptance gates.

## Hosted service reliability

| Requirement | Current artifact | Status |
| --- | --- | --- |
| PostgreSQL role isolation, RLS and restart persistence | PR11 implementation and Cherry tests | VERIFIED_ISOLATED_CHERRY |
| Bounded pool, timeout, metrics and alert checks | PR12 runtime, `/healthz`, `/metrics`, supervisor evidence | VERIFIED_ISOLATED_CHERRY |
| TLS hostname and CA verification | `artifacts/pr12/postgres-tls-drill.json` | VERIFIED_ISOLATED_CLONE |
| Physical backup, PITR and standby promotion | PR12 drill and backup implementation | VERIFIED_ISOLATED_CLONE |
| Actual encrypted off-host retained backup | destination attestation required by code | NOT_MET_NO_DESTINATION |
| Production RPO/RTO and public deployment | no production incident-domain test | NOT_MEASURED |

PostgreSQL apply, PITR, failover, load and service secret rotation now have isolated Cherry evidence. They do not prove a production deployment or replace the missing Qwen, wallet and TRON execution evidence above.
