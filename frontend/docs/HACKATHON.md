> Earlier integration checklist / API contract. Current verified scope: [faat README](../../README.md) and [verification](../../docs/VERIFICATION.md).

# GWDC 2026 Korea — implementation mapping

> **2026-09-29 OVH update:** Live Qwen3 → confirmed mandate → Nile planning and PostgreSQL Watch jobs have now been verified. Native jTRX supply now passes live unsigned preflight; actual wallet-signed investment and matched position evidence remain pending. Other investment adapters remain blocked. The current evidence and remaining requirements are in [the integration report](../../machine-submission/README.md). Earlier tables below describe the full target contract, not completed live execution.

Reviewed 2026-09-28 against the event's linked TRON PDF (all 3 pages), FuriosaAI × Bricksum brief (both challenges), event schedule and Kiln model documentation. This document distinguishes UI readiness from verified live integration.

## Target tracks

**TRON Challenge B**: AI Asset Allocation and Yield Planning Assistant.
**FuriosaAI × Bricksum Challenge A**: Financial Service Powered by AI Agents and Blockchain.

The user's latest instruction confirms the event switched to **Qwen3**. Use `qwen3-32b` through Kiln. The publicly linked Furiosa document retrieved on 2026-09-28 still names `gpt-oss-120b`; preserve that source-version discrepancy in submission notes. Do not switch the implementation back or offer a GPT option. The public Kiln catalog lists Qwen3 32B as available; authenticated model discovery and an actual Qwen3 request were verified during development.

## TRON B mapping

| Required evidence | Frontend implementation | Live work still required |
|---|---|---|
| Conversational needs, clarification and confirmation | Alpha messages, missing fields, editable versioned mandate and explicit confirmation | Qwen response → validated mandate, source references and clarification jobs |
| JustLend and USDD coverage with source times/terms | Source panel, allocation terms and explicit USDD Vault comparison/exclusion | Both real product adapters and verified source snapshots; exclusion UI alone is not proof of USDD integration |
| Two viable alternatives with allocation, yields, costs, exits and risks | Plan cards separate base yield, incentive rewards and costs; show infeasibility without relaxing limits | Deterministic calculations using current authenticated inputs |
| Execution after consent, with amounts/fees/risks/scope and results | Consent, preflight, exact transaction signature, step status and receipts | ABI compilation, signature verification, broadcasting and protocol state reconciliation |
| Original assumptions and expected/actual review | Position provenance, performance breakdown, original run inputs and JSON export | Real position accounting; labeled replay/simulation is allowed for the review demonstration |

The project-specific requirement to consider **USDD Vault** comes from the user's architecture. The official brief requires JustLend and USDD coverage; it does not require every user to borrow. A no-debt mandate must show why the Vault path is excluded.

## Furiosa A mapping

| Required evidence | Frontend implementation | Live work still required |
|---|---|---|
| Declared user function and complete journey | Function sentence in README; chat → conditions → allocation → approval → receipt | Run the connected service end to end |
| Kiln call influencing decisions; tokens by flow | Evidence model ID, flow-level calls/tokens/latency | Qwen3 chat calls and actual usage are implemented; attach their job IDs to financial runs in the financial backend |
| Devnet/testnet transaction with matching history | Network-bound txIDs, explorer links, graph/approval/receipt trace | At least one real testnet transaction with reconciled receipt |
| Two runs with changed user conditions and inspectable outcomes | Mandate version edits, original inputs per run, same-snapshot field and exports | Capture both actual end-to-end runs; validate allowed/blocked effects |
| Efficiency explanation with supported energy evidence | Missing metrics remain null; measured/estimated/unavailable energy states | Document measurement scope or assumptions; do not infer hardware energy from API access |

The runtime contains no seeded agents, conversations, financial positions or simulated execution. A real browser-originated Qwen3 call succeeded, and the service recorded provider-reported input/output tokens and elapsed time. On-chain testnet evidence still requires the financial backend and a user-controlled wallet.

## Suggested presentation sequence

1. Declare the asset-management function and show the user request.
2. Confirm 10,000 USDT, 90 days, at least 30% immediate cash, no TRX exposure and no debt.
3. Show both JustLend and USDD source evidence. Explain the Vault exclusion if debt is prohibited.
4. Compare two eligible plans and inspect cost/yield/exit assumptions.
5. Approve a plan, run preflight, sign each exact testnet transaction and show the matching protocol receipt.
6. Review position/performance separately from predicted return.
7. Change the cash floor to 10%, retain the same snapshot for a controlled comparison, reconfirm and repeat. Preserve both histories. If an action violates constraints, show the recorded rejection.
8. Export both run bundles and show Qwen3 usage per flow. Do not label a frontend demo as actual Kiln or chain execution.

## Schedule and sources

The organizer currently lists **September 30, 2026, 12:00 KST** as the submission deadline. Demo Day is 15:00–17:00 KST. Registration pages identify mandatory conference and hackathon Luma registration. The linked Telegram Q&A could not be retrieved in this session, so additional eligibility, reuse, judging, licensing or cross-track submission rules are not asserted here.

- [Official event and linked briefs](https://luma.com/be2le0l0)
- [GWDC hackathon schedule](https://wap.gwdc.net/hackathon.html)
- [TRON challenge brief, page 2](https://drive.google.com/file/d/1HXLhu1_vCoMD5aN3APEYEjDrh9oVCj7g/view)
- [FuriosaAI × Bricksum challenge brief](https://docs.google.com/document/d/13qh7oePGl7Flrl-Zh_A6hfr02L266PvS/edit)
- [Organizer Q&A link](https://t.me/GWDC_Global/392/742)
- [Kiln served models and capabilities](https://kiln.bricksum.com/docs/en/models)
