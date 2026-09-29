"""TRON resource pricing: spendable TRX and protocol fee limits are distinct.

Resource availability is a point-in-time observation. It can be consumed by
another transaction before broadcast, so the full burn bound remains reserved.
Bandwidth uses a whole-transaction bucket; insufficient quota is not a partial
discount. A sequence must consume its resource inventory once, never per step.
"""
from economic_machine.values import MachineError


def nonnegative(value, label):
    if type(value) is not int or value < 0:
        raise MachineError('Invalid resource ' + label)
    return value


def quote_resources(energy, bandwidth, parameters, resources):
    energy = nonnegative(energy, 'energy usage')
    bandwidth = nonnegative(bandwidth, 'bandwidth usage')
    params = {p['key']: p.get('value', 0) for p in parameters.get('chainParameter', [])}
    ep = nonnegative(params.get('getEnergyFee'), 'energy price')
    bp = nonnegative(params.get('getTransactionFee'), 'bandwidth price')
    if not ep or not bp: raise MachineError('Positive live resource prices required.')
    remaining = {}
    for label, limit, used in [('energy', 'EnergyLimit', 'EnergyUsed'),
                             ('staked_bandwidth', 'NetLimit', 'NetUsed'),
                             ('free_bandwidth', 'freeNetLimit', 'freeNetUsed')]:
        remaining[label] = max(0, nonnegative(resources.get(limit, 0), limit) - nonnegative(resources.get(used, 0), used))
    consumed_energy = min(energy, remaining['energy'])
    remaining['energy'] -= consumed_energy
    bandwidth_burn = bandwidth * bp
    for key in ('staked_bandwidth', 'free_bandwidth'):
        if remaining[key] >= bandwidth:
            remaining[key] -= bandwidth
            bandwidth_burn = 0
            break
    return {'energy_units': energy, 'bandwidth_bytes_bound': bandwidth,
        'energy_price_sun': ep, 'bandwidth_price_sun': bp,
        'available_energy_used': consumed_energy,
        'expected_energy_burn_sun': (energy-consumed_energy)*ep,
        'expected_bandwidth_burn_sun': bandwidth_burn,
        'expected_total_burn_sun': (energy-consumed_energy)*ep+bandwidth_burn,
        'full_energy_burn_sun': energy*ep,
        'full_total_burn_bound_sun': energy*ep+bandwidth*bp,
        'remaining': remaining,
        'basis': 'Current wallet resources; full burn remains reserved. Rental payments, activation and other operations require separate quotes.'}


def signed_bandwidth_bound(owner, target, data, value, *, timestamp_ms, fee_limit_sun=15000000000):
    """Serialized raw transaction plus bounded signature/result envelope.

    Use the exact call length for resource availability; the separate workflow
    reservation may retain a larger limit. No wallet signature is produced.
    """
    from .native_execution import unsigned_transaction
    request={'owner_address':owner,'contract_address':target,'data_hex':data,'call_value_sun':str(value),
        'fee_limit_sun':str(fee_limit_sun),'ref_block_bytes':'0001','ref_block_hash':'00'*8,
        'timestamp_ms':timestamp_ms,'expiration_ms':timestamp_ms+180000}
    tx=unsigned_transaction(request,8192)
    return len(bytes.fromhex(tx['raw_data_hex']))+195
