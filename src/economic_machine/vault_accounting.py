"""Collateral + deployed USDD - debt; forecasts never certify on-chain debt."""

from decimal import Decimal

from .mandate import integer
from .values import MachineError, decstr, require_keys
from .yield_math import YEAR, exact, growth, quantity, rounded


@exact
def vault_cashflow(*, collateral_base, deployed_usdd, stored_debt_usdd,
                   fee_rate, fee_age_seconds, horizon_seconds, destination_rate,
                   usdd_price_base, min_ratio, buffer_bps, shocks):
    collateral, deployed = quantity(collateral_base), quantity(deployed_usdd)
    stored, price = quantity(stored_debt_usdd), quantity(usdd_price_base)
    minimum = quantity(min_ratio)
    integer(buffer_bps, 'collateral safety buffer', 0, 1000000)
    integer(horizon_seconds, 'vault horizon', 1, YEAR)
    if collateral <= 0 or stored <= 0 or price <= 0 or minimum < 1 or deployed > stored:
        raise MachineError('invalid collateral, deployed principal, debt, price or minimum ratio')
    if deployed != rounded(deployed, 18):
        raise MachineError('deployed USDD contains a fractional token atom')
    if quantity(fee_rate['annual_fraction'], signed=True) < 0:
        raise MachineError('negative stability fee unsupported')
    # The API annual rate is an explicit modeling choice. This is not an exact
    # Jug/Vat drip or repayment quote; those require per-second on-chain state.
    current = rounded(stored * growth(fee_rate, fee_age_seconds), 18, liability=True)
    future = rounded(current * growth(fee_rate, horizon_seconds), 18, liability=True)
    assets = rounded(deployed * growth(destination_rate, horizon_seconds), 18)
    initial_nav = collateral + (deployed - current) * price
    final_nav = collateral + (assets - future) * price
    safety = minimum + Decimal(buffer_bps) / 10000
    current_ratio, future_ratio = collateral / (current * price), collateral / (future * price)
    reasons = []
    if min(current_ratio, future_ratio) <= safety:
        reasons.append('VAULT_COLLATERAL_BUFFER')
    if not isinstance(shocks, list) or not 1 <= len(shocks) <= 32:
        raise MachineError('bounded nonempty vault stress scenarios required')
    scenarios, seen = [], set()
    for shock in shocks:
        require_keys(shock, {'name', 'collateral_change_bps', 'usdd_change_bps', 'deployment_loss_bps'}, 'vault stress')
        from .values import ident
        name = ident(shock['name'], 'stress scenario')
        if name in seen:
            raise MachineError('duplicate vault stress scenario')
        seen.add(name)
        c = integer(shock['collateral_change_bps'], 'collateral shock', -10000, 100000)
        d = integer(shock['usdd_change_bps'], 'USDD price shock', -9999, 100000)
        loss = integer(shock['deployment_loss_bps'], 'deployment loss', 0, 10000)
        c_value = collateral * (10000 + c) / 10000
        p_value = price * (10000 + d) / 10000
        a_value = assets * (10000 - loss) / 10000
        ratio = c_value / (future * p_value)
        safe = ratio > safety
        if not safe:
            reasons.append('VAULT_STRESS_COLLATERAL:' + name)
        nav = c_value + (a_value - future) * p_value
        scenarios.append({'name': name, 'collateral_ratio': decstr(rounded(ratio, 18)),
                          'nav_base': decstr(rounded(nav)),
                          'loss_base': decstr(rounded(max(Decimal(0), initial_nav-nav), liability=True)),
                          'above_buffer': safe})
    return {'stored_debt_usdd': decstr(stored), 'modeled_current_debt_usdd': decstr(current),
            'modeled_future_debt_usdd': decstr(future), 'deployed_usdd': decstr(deployed),
            'destination_end_usdd': decstr(assets),
            'unaccrued_fee_estimate_usdd': decstr(current-stored),
            'horizon_fee_usdd': decstr(future-current),
            'initial_nav_base': decstr(rounded(initial_nav)), 'final_nav_base': decstr(rounded(final_nav)),
            'net_income_base': decstr(rounded(final_nav-initial_nav)),
            'current_collateral_ratio': decstr(rounded(current_ratio, 18)),
            'horizon_collateral_ratio': decstr(rounded(future_ratio, 18)),
            'repay_shortfall_usdd': decstr(max(Decimal(0), future-assets)),
            'reason_codes': reasons, 'scenarios': scenarios,
            'debt_basis': 'ANNUAL_RATE_ESTIMATE_NOT_LIVE_REPAYMENT_QUOTE'}
