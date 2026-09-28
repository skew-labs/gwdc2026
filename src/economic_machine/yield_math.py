"""Bounded ACT/365F forecasts; decimal inputs, explicit convention and rounding.

Rates are assumptions held constant over a horizon, not realized yields. Each
entrypoint installs its own context so an upstream Decimal setting cannot alter
money or a replay hash. Revenue rounds down, liabilities/costs round up.
"""

from decimal import Context, Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from functools import wraps

from .mandate import integer
from .values import MachineError, decimal, decstr, require_keys


YEAR = 365 * 86400
CONTEXT = Context(prec=96)


def exact(function):
    @wraps(function)
    def run(*args, **kwargs):
        with localcontext(CONTEXT):
            return function(*args, **kwargs)
    return run


def quantity(value, *, signed=False):
    result = decimal(value, signed=signed)
    if abs(result) > Decimal('1e30') or result.as_tuple().exponent < -36:
        raise MachineError('quantity exceeds financial arithmetic bounds')
    return result


@exact
def rounded(value, decimals=6, *, liability=False):
    integer(decimals, 'asset decimals', 0, 36)
    return value.quantize(Decimal(1).scaleb(-decimals),
                          rounding=ROUND_CEILING if liability else ROUND_FLOOR)


@exact
def growth(rate, seconds):
    require_keys(rate, {'annual_fraction', 'convention', 'day_count'}, 'yield rate')
    integer(seconds, 'accrual seconds', 0, YEAR)
    annual = quantity(rate['annual_fraction'], signed=True)
    if not -1 < annual <= 10 or rate['day_count'] != 'ACT_365F':
        raise MachineError('unsupported rate range or day count')
    if rate['convention'] == 'SIMPLE_APR':
        return Decimal(1) + annual * seconds / YEAR
    if rate['convention'] == 'EFFECTIVE_APY':
        return (Decimal(1) + annual) ** (Decimal(seconds) / YEAR)
    raise MachineError('explicit APR or APY convention required')


@exact
def period_yield(principal, rate, seconds, *, decimals=6, liability=False):
    amount = quantity(principal)
    factor = growth(rate, seconds)
    result = rounded(amount * (factor - 1), decimals, liability=liability)
    return {'principal': decstr(amount), 'period_seconds': seconds,
            'income': decstr(result), 'end_amount': decstr(amount + result),
            'rate': dict(rate), 'rounding': 'CEILING' if liability else 'FLOOR',
            'forecast': 'CONSTANT_RATE_ASSUMPTION'}


@exact
def reward_income(principal, rate, seconds, *, claim_after_seconds, haircut_bps, decimals=6):
    if claim_after_seconds is not None:
        integer(claim_after_seconds, 'reward claim seconds', 0, 2 * YEAR)
    integer(haircut_bps, 'reward haircut', 0, 10000)
    if quantity(rate['annual_fraction'], signed=True) < 0:
        raise MachineError('negative reward rate')
    gross = quantity(period_yield(principal, rate, seconds, decimals=decimals)['income'])
    marked = rounded(gross * (10000 - haircut_bps) / 10000, decimals)
    claimable = claim_after_seconds is not None and claim_after_seconds <= seconds
    return {'gross_accrued': decstr(gross), 'discounted_accrued': decstr(marked),
            'claimable_income': decstr(marked if claimable else Decimal(0)),
            'unclaimable_income': decstr(Decimal(0) if claimable else marked),
            'claim_after_seconds': claim_after_seconds, 'haircut_bps': haircut_bps}


@exact
def net_cashflow(principal_base, income_base, costs):
    require_keys(costs, {'entry', 'exit', 'conversion', 'network'}, 'round trip base costs')
    principal = quantity(principal_base)
    income = quantity(income_base, signed=True)
    charged = {key: rounded(quantity(value), liability=True) for key, value in costs.items()}
    total = sum(charged.values(), Decimal(0))
    # Reserve every round-trip cost up front; never fund exit fees from a
    # forecast that may not be earned or may still be unclaimable.
    return {'principal_base': decstr(principal), 'income_base': decstr(rounded(income)),
            'costs_base': {k: decstr(v) for k, v in charged.items()},
            'total_cost_base': decstr(total), 'required_budget_base': decstr(principal + total),
            'net_income_base': decstr(rounded(income - total))}
