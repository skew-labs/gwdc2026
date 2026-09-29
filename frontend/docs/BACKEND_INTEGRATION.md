> Earlier integration checklist / API contract. Current verified scope: [faat README](../../README.md) and [verification](../../docs/VERIFICATION.md).

# Backend integration

> **2026-09-29 OVH update:** Live Qwen3 → confirmed mandate → Nile planning and PostgreSQL Watch jobs have now been verified. Investment execution remains blocked. The current evidence and remaining requirements are in [the integration report](../../docs/VERIFICATION.md). Earlier tables below describe the full target contract, not completed live execution.

The included Node server implements the agent workspace, Kiln chat, durable messages/jobs, usage, sessions and TronLink ownership proof. The financial service remains your backend integration boundary.

## Connect your financial service

1. Use Node 24.19+ and `pnpm install`.
2. Copy `.env.server.example` to `.env.server.local` and set `KILN_API_KEY`.
3. Set `FINANCE_API_URL=https://your-financial-service` and `FINANCE_SERVICE_TOKEN` in that server environment file. Loopback HTTP is also accepted for a co-located service.
4. Run `pnpm dev:all`, open `http://127.0.0.1:5173`. The frontend calls the included Node service through Vite. In deployment the Node service serves both `dist/` and `/v1` behind HTTPS.

### Trust boundary

The Node gateway derives identity from its session and forwards `Authorization: Bearer <FINANCE_SERVICE_TOKEN>`, `X-Machine-Workspace` and `X-Verified-Wallet`. The financial backend must authenticate this service token **before trusting either identity header**, scope all resources to that workspace, and verify the wallet/account on every action. Never accept these headers directly from a browser. The proxy only forwards financial resource routes and preserves idempotency keys.

An unconfigured service returns an empty financial snapshot for display and explicit `FINANCE_NOT_CONNECTED` errors on financial operations. A configured but unavailable/incompatible service surfaces an error; it never falls back to sample values.

### Implemented workspace API

- `GET /v1/session`, wallet challenge/verify/logout.
- `GET/POST /v1/agents`; `PATCH/DELETE /v1/agents/{id}`; `POST /v1/agents/{id}/restore`; `GET /v1/agents?archived=true`.
- `POST /v1/messages` with `{agent_id, role, text, network}`; `POST /v1/jobs/{id}/cancel`.
- `GET /v1/workspace?network=nile&agent_id=<id>` merges financial context with persisted agent messages/jobs.
- `GET /v1/usage` contains provider-reported call metrics. Cancellation before usage arrives leaves unknown values null.
- `GET /v1/status` reports configuration presence, not downstream health.

Financial services return the aggregate `Workspace` from `GET /v1/workspace?network=...`, and handle the financial mutations below. JSON schemas are in `openapi.json`. The API contract is generated from the same Zod schemas that validate responses in the application.

### Kiln

Server-only `KILN_API_KEY`; model `qwen3-32b`; endpoint `https://api.bricksum.com/v1/chat/completions`. An authenticated model list and actual streamed Qwen3 response were verified. No GPT fallback is configured. Qwen explains and clarifies; deterministic financial operations are performed only by explicit controls and financial backend requests. User instructions cannot grant signing authority.

## Boundary and data conventions

- Bare JSON bodies: no `{data: ...}` envelope. Schema is OpenAPI 3.1, generated from the same Zod schemas used to validate every response.
- Amounts are decimal **strings**, including zero. Convert to/from minimal-unit integers on the backend. Never send floats for money. Preserve token decimals and symbols. Unknown yield/energy/cost is `null`, not zero.
- Dates: UTC ISO-8601. `raw_data.expiration` follows TronWeb milliseconds. Browser clock is advisory; server time controls validity.
- Lists in workspace are authoritative bounded snapshots, not append-only client logs. Include the active mandate/comparison/graph/approval/execution and recent messages/jobs. Keep historical records server-side and supply exported run inputs in `EvidenceRun.inputs`.
- `mode: LIVE | SIMULATION` labels the workspace. Positions, performance and runs additionally carry `LIVE | SIMULATION | REPLAY`. Do not combine Mainnet market observations into a Nile allocation execution.
- IDs/hashes are opaque. Each mutation includes a fresh idempotency key, reused on a retry of the exact same payload. Financial submission uses `signed-${txID}`. Deduplicate by tenant, route and key; reject key reuse with another payload.
- All requests derive tenant/owner from the authenticated session. Never accept identity from a body. Check wallet ownership, network and resource ownership on every operation.
- Errors: `{ "error": { "code": "POLICY_CHANGED", "message": "Review the updated mandate.", "request_id": "..." } }`. `401` session expired; `409` stale/conflicting state; `422` invalid condition; `429` throttled. No automatic mutation retry.

## Resource flow

| Step | Endpoint | Frontend behavior |
|---|---|---|
| Bootstrap | `GET /v1/session` | Always responds, including unauthenticated. Supplies CSRF token. |
| Read | `GET /v1/workspace?network=nile` | Aggregate BFF: messages, mandates, plan comparison, graph, approvals, execution, balances, positions, performance, routines, market sources, evidence and jobs. Polls every 4s, or 700ms while an agent job is active; reload only reads. |
| Ownership challenge | `POST /v1/auth/challenge` | Domain + address + network. Server creates expiring, single-use nonce and canonical sign-in message. |
| Verify | `POST /v1/auth/verify` | Server validates signature, domain/nonce/expiry/chain binding, sets Secure HttpOnly session cookie, rotates CSRF. No transaction authority is granted. |
| Chat | `POST /v1/messages` | `{agent_id,role,text,network}`; persist a queued job and real provider response. Use the mandate editor for structured mutations. |
| Structured edit | `POST /v1/mandates` | New draft version; source text + constraints. Invalidate old unsigned plans/approvals. Debt permission alone does not satisfy missing debt/collateral limits; expose required confirmations in `missing_fields`. |
| Confirm | `POST /v1/mandates/{id}/confirm` | Compare version/hash and activate only the reviewed draft. |
| Plans | `POST /v1/plan-comparisons` | Same confirmed mandate and snapshot for all plans; at least two eligible alternatives or explicit infeasibility. Include USDD status/reason. |
| Graph | `POST /v1/execution-graphs` | Verified, eligible plan → transaction DAG. Protocol capabilities and funding/exit steps resolved server-side. Refuse overlapping unresolved executions. |
| Plan consent | `POST /v1/approvals` | Bind graph/plan/mandate/account/network/amounts/fees/expiry. Does not sign anything. |
| Preflight | `POST /v1/execution-graphs/{id}/preflight` | Validate exact step and dependencies; return all checks and unsigned transaction. Called for review and repeated immediately before wallet signing. |
| Submit | `POST /v1/executions` | Receive signed transaction, compare exact payload and authority, persist submission intent/txID and reserved capital, then broadcast. |
| Recovery | `POST /v1/executions/reconcile` | Query the same txID; never rebuild/rebroadcast blindly. Persist whether it was submitted, confirmed, failed, or remains unknown. |
| Routines | `POST /v1/routines`, `PATCH /v1/routines/{id}` | Persist schedule and meaningful-change notification preferences to hosted worker. No client timer pretends to monitor markets. |
| Export | `GET /v1/evidence?network=nile` | Same `EvidenceRun` contract; UI exports loaded run records from workspace. |

Existing `/v1/positions` and `/v1/performance` services can remain independent internally. Compose them in `/v1/workspace`. No frontend-side financial calculations or duplicate service truth is needed.

## Signing requirements

The frontend supports modern `window.tron` / TIP-6963 and legacy TronLink. It connects, signs a separate ownership challenge, observes account/network changes, checks the exact account/network again at signing and submits **only** the wallet-returned signature. It never holds keys or broadcasts itself.

Preflight must bind `graph_hash`, `approval_id`, `step_id`, `account`, `network`, `expires_at`, checks, and transaction. Refuse missing/unknown checks. The frontend checks `SHA256(raw_data_hex) == txID`, owner, expiration, unchanged signed raw fields and nonempty signatures. The backend MUST independently re-encode and compare `raw_data` to `raw_data_hex`, verify signatures, owner/permission, recipient, token amount, calldata, call value, fee limit, reference block and expiry against the graph. Frontend checks do not replace that boundary.

Sequential steps require prior step confirmation/reconciliation. An approval-only success is not a deposit. Return remaining allowance if supply fails. Use statuses from the schema, including `PARTIALLY_COMPLETED`, `WAITING_EXIT`, `DISPUTED`, `SUBMISSION_UNKNOWN`. Chain inclusion and protocol position reconciliation are separate.

### Unknown submissions and reloads

Before submission, the browser saves a minimal pointer (txID, graph/step/approval, account, network) in sessionStorage. It does **not** save a signed payload. If transport fails, retain that pointer and disable another signature. Reload restores the pointer. `Reconcile submission` must be called using the original wallet and network. The backend must durably acknowledge signed transactions only after saving enough information to resume. If the request never reached the backend, keep it unresolved until the server can prove expiry/non-submission; provide an explicit resolution in workspace. Do not tell the user that a network timeout means a failed transaction.

## Authentication and expiration

User sessions and API authorization are backend responsibilities. Sign-in challenges should be rate-limited and same-origin; include a human-readable domain/address/network/nonce/expiration. The UI checks returned domain/address/network/expiry before requesting the message signature. Session must refresh after ownership/account changes. Every job, routine, evidence record and query needs tenant filtering.

Market snapshot, policy revision, adapter revision or graph expiration invalidates unsigned approval. Signed or potentially submitted transactions remain reserved and reconciled independently. `GET /v1/workspace` must never create an execution.

## Testing the connected service

Start with Nile and a test wallet. Exercise the checklist in `ACCEPTANCE.md`. Actual Kiln inference was verified. Tests do not prove correct financial ABI construction, RPC preflight or on-chain reconciliation; those remain financial backend integration gates.

Official references: [TronLink requests](https://docs.tronlink.org/plugin-wallet/active-requests/), [TronLink events](https://docs.tronlink.org/plugin-wallet/passive-messages/), [TRON signing flow](https://developers.tron.network/docs/api-signature-and-broadcast-flow), [Kiln models](https://kiln.bricksum.com/docs/en/models).
