"""Finite TRON resource-window accounting; no implicit daily regeneration.

Energy requests include all caller-paid dynamic energy. Contract sponsorship,
account creation and special transfer fees need separate preflight adapters.
Bandwidth for an existing-account ordinary transaction uses a complete staked
quota, else a complete free quota, else burns for the whole transaction.
"""

from copy import deepcopy
from decimal import Decimal

from .mandate import integer
from .values import MachineError, decstr, ident, require_keys
from .yield_math import exact, quantity, rounded


MAX_RESOURCE = 10**18


def resource_budget(raw):
    require_keys(raw, {'energy_capacity', 'energy_used', 'energy_reserved', 'energy_rented_out',
                       'energy_received', 'bandwidth_staked', 'bandwidth_free'}, 'resource window')
    values = {key: integer(value, key, 0, MAX_RESOURCE) for key, value in raw.items()}
    # Delegated-out energy must come from own capacity, never rented-in energy.
    if values['energy_rented_out'] > values['energy_capacity']:
        raise MachineError('rented energy cannot be rented out again')
    remaining = (values['energy_capacity'] - values['energy_rented_out'] + values['energy_received']
                 - values['energy_used'] - values['energy_reserved'])
    if remaining < 0:
        raise MachineError('energy usage/reservations exceed uncommitted capacity')
    return {'energy': remaining, 'bandwidth_staked': values['bandwidth_staked'],
            'bandwidth_free': values['bandwidth_free']}


@exact
def transaction_costs(window, transactions, prices):
    require_keys(prices, {'sun_per_energy', 'sun_per_byte', 'trx_price_base'}, 'resource prices')
    energy_price = integer(prices['sun_per_energy'], 'energy price', 0, MAX_RESOURCE)
    byte_price = integer(prices['sun_per_byte'], 'bandwidth price', 0, MAX_RESOURCE)
    trx_price = quantity(prices['trx_price_base'])
    if trx_price <= 0 or not isinstance(transactions, list) or not 1 <= len(transactions) <= 64:
        raise MachineError('positive TRX price and bounded transaction costs required')
    left, rows, seen = resource_budget(window), [], set()
    total = 0
    for tx in transactions:
        require_keys(tx, {'id', 'caller_energy', 'bytes', 'max_energy_burn_sun', 'fee_limit_sun'}, 'resource transaction')
        name = ident(tx['id'], 'transaction id')
        if name in seen:
            raise MachineError('duplicate resource transaction')
        seen.add(name)
        energy = integer(tx['caller_energy'], 'caller energy including penalty', 0, MAX_RESOURCE)
        size = integer(tx['bytes'], 'transaction bytes', 1, 1000000)
        limit = integer(tx['max_energy_burn_sun'], 'energy burn budget', 0, 10**30)
        fee_limit = integer(tx['fee_limit_sun'], 'caller Energy fee limit', 0, 10**30)
        if energy * energy_price > fee_limit:
            raise MachineError('caller Energy including stake exceeds fee_limit')
        consumed = min(energy, left['energy'])
        left['energy'] -= consumed
        energy_burn = (energy - consumed) * energy_price
        if energy_burn > limit:
            raise MachineError('energy burn exceeds transaction budget')
        bandwidth_source, bandwidth_burn = 'TRX_BURN', size * byte_price
        for source in ('bandwidth_staked', 'bandwidth_free'):
            if left[source] >= size:
                left[source] -= size
                bandwidth_source, bandwidth_burn = source, 0
                break
        total += energy_burn + bandwidth_burn
        rows.append({'id': name, 'energy_consumed': consumed, 'energy_burn_sun': energy_burn,
                     'bandwidth_source': bandwidth_source, 'bandwidth_burn_sun': bandwidth_burn})
    return {'transactions': rows, 'remaining': deepcopy(left), 'burn_sun': total,
            'burn_trx': decstr(Decimal(total) / 1000000),
            'cost_base': decstr(rounded(Decimal(total) / 1000000 * trx_price, liability=True)),
            'scope': 'EXISTING_ACCOUNT_CALLER_PAYS_RESOURCE_WINDOW'}


@exact
def rental_income(window, *, rented_energy, quoted_base_per_energy, occupied_windows,
                  commission_bps, strx_aggregate=False):
    resource_budget(window)
    if type(strx_aggregate) is not bool or strx_aggregate:
        raise MachineError('sTRX aggregate already includes Energy income')
    rented = integer(rented_energy, 'rented Energy', 0, MAX_RESOURCE)
    windows = integer(occupied_windows, 'quoted rental windows', 0, 366)
    integer(commission_bps, 'rental commission', 0, 10000)
    if rented != window['energy_rented_out']:
        raise MachineError('rental income must match committed Energy capacity')
    revenue = quantity(quoted_base_per_energy) * rented * windows * (10000 - commission_bps) / 10000
    return {'net_base': decstr(rounded(revenue)), 'rented_energy': rented,
            'occupied_windows': windows, 'basis': 'QUOTED_OCCUPANCY_NO_AUTO_RENEWAL'}
