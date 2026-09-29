# Why these boundaries exist

This is a technical decision record derived from the implementation and observed failures. It records design rationale, tradeoffs and validation evidence.

## 1. Language can propose; typed conditions authorize planning

Conversational answers previously described an allocation as confirmed before the service had confirmed terms. The implementation separates language/intent extraction from canonical mandates and explicit confirmation. A vague acceptance of risk cannot grant borrowing permission. The tradeoff is an explicit review step; it gives the service a concrete version/hash to bind subsequent work to.

## 2. Require the same economics at multiple execution boundaries

A quote can be valid during planning and unattractive before signing. Investment entry rechecks current costs and economics in addition to the user's hard fee limit. Being below a maximum fee does not make a trade worthwhile. A remedial exit uses distinct rules so an uneconomic position can still be closed. The small Nile supply/redemption cycle made this distinction measurable.

## 3. Persist before the network side effect

A browser error or RPC timeout cannot tell us whether the network accepted a signed transaction. Durable identity, reservation and recovery make that ambiguity visible. They also prevent a refresh from silently constructing a second trade. This can temporarily block condition changes, which is preferable to spending the same capital twice. Recovery requires evidence, not an arbitrary timeout.

## 4. Compare bytes with another implementation

Our own encoder and decoder agreeing would not prove compatibility with the wallet. The redemption bug involved a protobuf default value that a self-consistent pair could miss. Golden fixtures produced with TronWeb and wallet-side canonical checks create an independent compatibility boundary. No new custom wallet serializer is assumed correct merely because its own round trip passes.

## 5. Reconcile outcomes independently

The requested action, chain receipt and observed share/cash changes are checked separately. This adds RPC and accounting work. It prevents a mined transaction with missing or contradictory economic effects from being labeled a successful investment. Duplicate receipt observation must be idempotent.

## 6. Monitoring does not expand authority

The user may ask for a daily routine. That authorizes observation and notification, not a daily financial transaction. Watch uses fresh state and compares maintain versus adjust after costs. The decision can open a new review, but execution still needs approval and wallet signing.

## 7. Preserve inconvenient evidence

A new demo period is useful, but deleting the old loss would corrupt performance. The implementation records a new baseline while retaining prior receipts and accounting. Research metrics, replay tests, live protocol transactions and production recovery drills likewise retain separate labels. A negative borrowing spread blocks looping even when leverage would make the gross balance look larger.

## Documentation approach

The concise project entry, runnable starting point and links to deeper technical documents were informed by the public READMEs of [Temporal](https://github.com/temporalio/temporal) and [TigerBeetle](https://github.com/tigerbeetle/tigerbeetle). Their code, performance claims and reliability claims are not attributed to faat. The arguments above are supported by this repository's own modules, regression tests and transaction evidence.
