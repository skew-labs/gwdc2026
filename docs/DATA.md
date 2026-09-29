# Data, provenance and research

## What is versioned

| Material | Path | Meaning |
| --- | --- | --- |
| Official source registry | `config/sources.json` | Endpoints, source kinds, network labels, freshness bounds and documentation links |
| Product registry | `config/tron_product_registry.json` | Explicit product/adapter identities consumed by the economic path |
| Data contracts | `contracts/*.schema.json` | Decision, observation, episode and evidence structure |
| Synthetic scenarios | `cases/`, test fixtures | Reproducible inputs for development/replay; not observed customer returns |
| Public captured multicall | `cases/tron_public_multicall_20260928.json` | Dated mainnet read regression with raw response and SHA-256; not a current executable quote |
| Chain evidence | `evidence/nile/` | Public transaction/receipt records for the integration cycle; independent from private account state |
| Research and generation code | `research/fdc/` | Synthetic cases, formal-optimum/oracle checks, temporal experiments and evaluation |

## Capture to checked observation

`collect.py` retrieves allowed JSON sources into `Store`; `normalize.py` maps source fields to typed facts; `quality.py` checks required facts, source age and consistency. `history.py` exports observation history, and `release.py` builds source manifests. Raw source association and timestamps remain relevant after normalization.

The registry covers JustLend markets and rewards, contract metadata, USDD earn APY, overview and TRON collateral. Those data families are not interchangeable: incentive APY must not silently replace base supply yield, and a multi-chain observation must not be used as a Nile executable market quote.

## Research is an explicit lane

`research/fdc/auto_cases.py` mines source changes. `formal_optima.py` and `validate_formal_optima.py` generate/check bounded mathematical allocations under stated objectives. `allocation_oracle.py` rechecks proposal constraints and evidence. Training and evaluation modules retain synthetic/observed distinctions and dataset restrictions.

Historical research results are in `research/` and `docs/history/DATASET_CARD.md`. Their synthetic metrics are not a claim about live trading performance. Source redistribution/training permission is not inferred merely from a successful API response. Large raw collections, private runtime stores and model weights are not committed; the code, schemas, explicit fixtures and reproduction paths are.

## No customer database is a dataset fixture

The publication excludes account sessions, scoped workspace exports and SQL/SQLite production stores. Public testnet transaction records are sufficient to identify the demonstrated on-chain cycle. The service must read fresh balances and positions for a new user instead of seeding those observations into their workspace.
