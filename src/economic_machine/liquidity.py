"""Cumulative recoverable principal, in seconds; no interpolation or guarantees."""

from decimal import Decimal

from .mandate import integer
from .values import MachineError, decstr, require_keys
from .yield_math import YEAR, exact, quantity, rounded


@exact
def recovery_curve(principal, tranches):
    amount = quantity(principal)
    if not isinstance(tranches, list) or not 1 <= len(tranches) <= 64:
        raise MachineError('bounded exit tranches required')
    known, unknown, total = {}, Decimal(0), Decimal(0)
    for item in tranches:
        require_keys(item, {'amount', 'after_seconds'}, 'exit tranche')
        part = quantity(item['amount'])
        total += part
        when = item['after_seconds']
        if when is None:
            unknown += part
        else:
            integer(when, 'exit seconds', 0, 2 * YEAR)
            known[when] = known.get(when, Decimal(0)) + part
    if total != amount:
        raise MachineError('exit tranches must conserve principal exactly')
    checkpoints, available = [], Decimal(0)
    for when, part in sorted(known.items()):
        available += part
        checkpoints.append({'after_seconds': when, 'available': decstr(available)})
    return {'principal': decstr(amount), 'checkpoints': checkpoints,
            'unknown_amount': decstr(unknown), 'basis': 'EXIT_ASSUMPTIONS_NOT_GUARANTEED'}


@exact
def available_at(curve, seconds):
    integer(seconds, 'liquidity deadline', 0, 2 * YEAR)
    available = Decimal(0)
    for point in curve['checkpoints']:
        if point['after_seconds'] <= seconds:
            available = quantity(point['available'])
    return available


@exact
def lending_exit(principal, cash_available, *, settlement_seconds):
    amount, cash = quantity(principal), quantity(cash_available)
    integer(settlement_seconds, 'redemption settlement seconds', 0, YEAR)
    now = min(amount, cash)
    return recovery_curve(principal, [{'amount': decstr(now), 'after_seconds': settlement_seconds},
                                     {'amount': decstr(amount-now), 'after_seconds': None}])


def native_exit(principal, *, lock_remaining_seconds, resource_recovery_seconds, unstake_seconds):
    for label, value in [('delegation lock', lock_remaining_seconds),
                         ('resource recovery', resource_recovery_seconds), ('unstake wait', unstake_seconds)]:
        integer(value, label, 0, YEAR)
    # Conservative sequential dependencies. Callers must supply the actual
    # residual lock/recovery state; no whole-day truncation is allowed.
    delay = lock_remaining_seconds + resource_recovery_seconds + unstake_seconds
    return recovery_curve(principal, [{'amount': principal, 'after_seconds': delay}])


@exact
def net_recovery(curve, price, *, debt_due='0', exit_cost='0', delay_seconds=0):
    """Value and repay before releasing cash; debt/costs deducted once cumulatively."""
    px, debt, cost = quantity(price), quantity(debt_due), quantity(exit_cost)
    if px <= 0:
        raise MachineError('positive exit price required')
    integer(delay_seconds, 'settlement delay', 0, YEAR)
    rows = []
    for point in curve['checkpoints']:
        when = point['after_seconds'] + delay_seconds
        integer(when, 'net exit seconds', 0, 2 * YEAR)
        rows.append({'after_seconds': when,
                     'available': decstr(rounded(max(Decimal(0), quantity(point['available']) * px - debt - cost)))})
    return {'principal': decstr(rounded(quantity(curve['principal']) * px)),
            'checkpoints': rows, 'unknown_amount': decstr(rounded(quantity(curve['unknown_amount']) * px)),
            'basis': 'CONSTANT_PRICE_EXIT_ASSUMPTION'}


@exact
def check_schedule(curve, cash, requirements):
    liquid = quantity(cash)
    if not isinstance(requirements, list) or len(requirements) > 33:
        raise MachineError('bounded liquidity requirements required')
    previous_time, previous_min = -1, Decimal(0)
    checks = []
    for item in requirements:
        require_keys(item, {'after_seconds', 'minimum'}, 'cash deadline')
        when = integer(item['after_seconds'], 'cash deadline seconds', 0, YEAR)
        minimum = quantity(item['minimum'])
        if when <= previous_time or minimum < previous_min:
            raise MachineError('cash requirements must increase in time and not decrease in amount')
        available = liquid + available_at(curve, when)
        checks.append({'after_seconds': when, 'required': decstr(minimum),
                       'available': decstr(available), 'satisfied': available >= minimum})
        previous_time, previous_min = when, minimum
    return checks
