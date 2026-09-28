"""Read-only projections of one confirmed mandate into existing core contracts.

Legacy Need, sandbox and BasketPolicy cannot enforce the full mandate. The
projection explicitly carries that gap and grants no execution authority.
PR 04 must replay a plan against all constraints before building any intent.
"""

from decimal import localcontext

from finagent.contracts import Need

from .inference import normalize_inference_scope
from .mandate import assert_effective, draft_hash, hash32, normalize_mandate, policy_hash, reserve_amount
from .values import MachineError, decimal, decstr, digest, ident


def confirmed_mandate(record: dict, at: str) -> dict:
    if record.get("status") != "CONFIRMED":
        raise MachineError("confirmed mandate required")
    mandate = normalize_mandate(record["mandate"])
    if record["draft_hash"] != draft_hash(mandate) or record["policy_hash"] != policy_hash(mandate):
        raise MachineError("stored mandate commitment mismatch")
    assert_effective(mandate, at)
    return mandate


def single_asset_budget(mandate: dict) -> tuple[str, str, str]:
    terms = mandate["terms"]
    if len(terms["capital"]) != 1 or terms["capital"][0]["asset"] != terms["base_asset"]:
        raise MachineError("valued multi-asset snapshot required")
    capital = terms["capital"][0]["amount"]
    return terms["base_asset"], capital, decstr(reserve_amount(terms["immediate_cash"], capital))


def compile_review_projections(record: dict, *, at: str, program_id: str,
                               agent_id: str, state_root: str,
                               allowed_fact_paths: list[str],
                               allowed_products: list[str],
                               capital_fact_path: str, max_plan_age_ms: int) -> dict:
    """Project from a repository-confirmed record, never from raw HTTP/LLM JSON."""
    mandate = confirmed_mandate(record, at)
    terms, scope = mandate["terms"], mandate["scope"]
    asset, capital, reserve = single_asset_budget(mandate)
    products = allowed_products
    if (not isinstance(products, list) or not 1 <= len(products) <= 16
            or any(not isinstance(item, str) for item in products)
            or len(set(products)) != len(products)):
        raise MachineError("allowed products must be a bounded unique list")
    for item in products:
        ident(item, "allowed product")
    if type(max_plan_age_ms) is not int or not 1 <= max_plan_age_ms <= 86400000:
        raise MachineError("invalid plan lifetime")
    protocols = sorted(key for key, cap in terms["protocol_caps_bps"].items() if cap > 0)
    if not protocols:
        raise MachineError("no protocol enabled for legacy sandbox")
    with localcontext() as ctx:
        ctx.prec = 256
        exposure = min(decimal(capital) - decimal(reserve),
                       decimal(terms["limits"]["single_amount"]["amount"]))
    sandbox = {"asset": asset, "capital_limit": capital, "max_exposure": decstr(exposure),
               "max_total_cost": terms["limits"]["fee_amount"]["amount"],
               "max_daily_loss": terms["limits"]["daily_loss"]["amount"],
               "allowed_protocols": protocols, "expires_at": terms["expires_at"]}
    boundary = normalize_inference_scope({"schema_version": "economic-inference-scope-1",
        "request_hash": record["draft_hash"], "state_root": hash32(state_root, "state root"),
        "program_id": program_id, "owner_id": scope["owner_id"], "agent_id": agent_id,
        "network": scope["network"], "sandbox": sandbox, "allowed_fact_paths": allowed_fact_paths})
    basket = {"schema_version": "economic-basket-policy-1",
              "policy_id": digest({"domain": "mandate-basket-id-1", "scope": scope,
                                   "mandate_id": mandate["mandate_id"]}),
              "owner_id": scope["owner_id"], "network": scope["network"], "asset": asset,
              "allowed_products": sorted(products),
              "capital_fact_path": ident(capital_fact_path, "capital fact path"),
              "max_capital": capital, "effective_at": terms["effective_at"],
              "expires_at": terms["expires_at"], "max_plan_age_ms": max_plan_age_ms}
    need_json = None
    if asset in {"USDT", "USDD"} and terms["horizon_seconds"] % 86400 == 0:
        need_json = {"asset": asset, "amount": capital, "liquid_reserve": reserve,
                     "horizon_days": terms["horizon_seconds"] // 86400,
                     "risk": terms["risk_profile"]}
        Need.from_json(need_json)
    return {"schema_version": "economic-mandate-projections-1", "status": "REVIEW_ONLY",
            "scope": scope, "mandate_id": mandate["mandate_id"], "revision": mandate["revision"],
            "mandate_policy_hash": record["policy_hash"], "mandate_draft_hash": record["draft_hash"],
            "constraints": terms, "inference_scope": boundary, "basket_policy": basket,
            "legacy_need": need_json, "legacy_need_status": "READ_ONLY" if need_json else "UNREPRESENTABLE",
            "remaining_verification": ["FULL_MANDATE_PLAN_REPLAY", "PRODUCT_CAPABILITY_BINDING",
                                       "VALUED_WALLET_SNAPSHOT", "TRANSACTION_GRAPH", "WALLET_SIGNATURE"],
            "execution_authority": "NONE", "chain_status": "NOT_SUBMITTED"}
