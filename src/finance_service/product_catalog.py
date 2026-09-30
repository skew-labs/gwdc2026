"""Deployment capabilities, never an assertion of live market readiness."""


def catalog(network):
    nile = network == "tron-nile"
    return [
        {
            "id": "native",
            "name": "Native Stake + voting",
            "status": "Enabled" if nile else "Not enabled",
            "reason": (
                "Nile TRX: live chain rewards, commission and votes; full bandwidth reserve. Separate stake/vote/exit signatures. Existing external stakes and votes are blocked for exact attribution."
                if nile
                else "Native system transaction execution is limited to Nile in this release."
            ),
        },
        {
            "id": "jtrx",
            "name": "JustLend TRX supply",
            "status": "Enabled" if nile else "Read only",
            "reason": "Nile mint and redeem receipts verified. Plans require positive income after complete costs; current market rates may fail this check. Use JustLend-only conditions for this route.",
        },
        {
            "id": "strx",
            "name": "JustLend sTRX",
            "status": "Excluded",
            "reason": "A product definition and deployed contract alone do not establish current complete yield, entry and exit economics. No unverified rental yield is credited.",
        },
        {
            "id": "usdd",
            "name": "USDD supply / collateral workflow",
            "status": "Blocked on Nile" if nile else "Conditional",
            "reason": (
                "Nile Vault and destination USDD token identities do not match."
                if nile
                else "Requires owned USDD or eligible collateral, route identity, profitable spread, limits and exact per-step approval. Full mainnet lifecycle has not been demonstrated."
            ),
        },
        {
            "id": "rental",
            "name": "Energy rental",
            "status": "Not enabled",
            "reason": "Requires a provider-bound rental quote, delegation limits, expiry and recovery verification. Resource capacity is not treated as cash income.",
        },
    ]
