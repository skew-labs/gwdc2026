"""Receipt-backed jTRX accounting; no wallet cash is treated as investment income.

FIFO share lots retain the original forecast. Unknown share transfers invalidate
income attribution, rather than inventing a deposit or silently assigning zero.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR

from economic_machine.values import MachineError, digest, decstr

D = Decimal
SUN = D(10**6)


def amount(atoms):
    return {'value': decstr(D(atoms).to_integral_value(rounding=ROUND_FLOOR)/SUN), 'symbol': 'TRX', 'decimals': 6}


def record_failure(state,n):
    result=n.get('execution_result') or {}
    if result.get('status')!='SOLID_EXECUTION_FAILED':return
    fee=(result.get('resource_receipt') or {}).get('total_fee_sun')
    if fee is None:raise MachineError('Failed transaction fee is not available yet.')
    txid=n['submission']['txid'];shares=n['before']['positions'][0]['shares_base_units']
    event={'txid':txid,'action':'FEE','at':n['submission']['prepared_at'],'shares':'0','principal_sun':'0',
        'fee_sun':str(fee),'before_shares':shares,'after_shares':shares,'forecast':None,
        'entry_estimate_sun':str(n.get('expected_fee_sun',int(D(n['estimated_fee']['value'])*SUN))),
        'reconciliation_hash':digest(result)}
    state.setdefault('native_accounting',{'version':1,'events':{}})['events'].setdefault(txid,event)


def forecast_for(n, inputs):
    if n.get('performance_forecast'):
        return deepcopy(n['performance_forecast'])
    plans = (inputs.get('comparison') or {}).get('plans', [])
    plan = next((p for p in plans if p['hash'] == n['graph']['plan_hash']), None)
    terms = (inputs.get('mandate') or {}).get('terms') or {}
    if not plan or not terms.get('horizon_seconds'):
        return None
    projected, fees = plan.get('expected_net_return'), plan.get('estimated_fees')
    if not projected or not fees or projected['symbol'] != 'TRX' or fees['symbol'] != 'TRX':
        return None
    principal = sum((D(a['amount']['value']) for a in plan['allocations']
                     if a['kind'] == 'SUPPLY' and a['amount']['symbol'] == 'TRX'), D(0))
    if int(principal*SUN) != n['amount']:
        return None
    entry = n.get('estimated_fee')
    return {'plan_hash': plan['hash'], 'horizon_seconds': terms['horizon_seconds'],
            'net_total_sun': str(int(D(projected['value'])*SUN)),
            'gross_total_sun': decstr((D(projected['value'])+D(fees['value']))*SUN),
            'entry_estimate_sun': str(int(D(entry['value'])*SUN)) if entry else None,
            'basis': 'Original approved plan; forecast income prorated over its stated horizon.'}


def record(state, n, inputs=None):
    """Idempotently import only independently reconciled receipts."""
    if (n.get('reconciliation') or {}).get('status') != 'RECONCILED':
        return
    result = n.get('execution_result') or {}
    txid = n['submission']['txid']
    fee = (result.get('resource_receipt') or {}).get('total_fee_sun')
    if fee is None:
        raise MachineError('A reconciled receipt is missing its actual fee.')
    before = n['before']['positions'][0]
    after = n['post_state']['account']['positions'][0]
    change = int(after['shares_base_units'])-int(before['shares_base_units'])
    redeem = n.get('operation') == 'REDEEM'
    if (redeem and change >= 0) or (not redeem and change <= 0):
        raise MachineError('Receipt share direction is inconsistent with its action.')
    raw = n.get('post_state_raw') or {}
    block = raw.get('block') or {}
    at = n['submission']['prepared_at']
    # Prefer the transaction's block time to a later account observation time.
    receipt_block = (n.get('execution_observation') or {}).get('details', {}).get('block', {})
    if receipt_block.get('timestamp_ms'):
        at = datetime.fromtimestamp(receipt_block['timestamp_ms']/1000,
              tz=datetime.fromisoformat(at).tzinfo).isoformat()
    event = {'txid': txid, 'action': 'REDEEM' if redeem else 'SUPPLY', 'at': at,
             'shares': str(abs(change)), 'fee_sun': str(fee),
             'principal_sun': str(n.get('actual_received', 0) if redeem else n['amount']),
             'before_shares': before['shares_base_units'], 'after_shares': after['shares_base_units'],
             'policy_hash': n.get('policy_hash') or (inputs or {}).get('mandate', {}).get('hash'),
             'forecast': None if redeem else forecast_for(n, inputs or {}),
             'entry_estimate_sun': str(n.get('expected_fee_sun', int(D(n['estimated_fee']['value'])*SUN))) if n.get('estimated_fee') else None,
             'reconciliation_hash': n['reconciliation']['reconciliation_hash']}
    events = state.setdefault('native_accounting', {'version': 1, 'events': {}})['events']
    if txid in events:
        # A new observation must never rewrite the original time/forecast.
        for key in ('action', 'shares', 'fee_sun', 'principal_sun', 'reconciliation_hash'):
            if event[key] != events[txid][key]:
                raise MachineError('A previously recorded receipt changed.')
        return
    events[txid] = event


def refresh(state, account, raw, at):
    """Populate performance from scoped receipts and the freshly read position."""
    for e in state['workspace'].get('evidence', []):
        n = (e.get('calculation') or {}).get('native_execution')
        if n:
            record(state, n, e.get('inputs') or {})
            record_failure(state,n)
    n = state.get('native_execution')
    if n:
        record(state, n, state['workspace'])
        record_failure(state,n)
    accounting = state.get('native_accounting', {})
    period = accounting.get('active_period')
    excluded = set(period['excluded_txids']) if period else set()
    events = sorted((e for e in accounting.get('events', {}).values() if e['txid'] not in excluded), key=lambda x:(x['at'], x['txid']))
    if not events and not period:
        return
    lots, expected_gross, expected_fees = [], D(0), D(0)
    deposited = returned = fees = realized = 0
    complete = not period or all(datetime.fromisoformat(e['at']) >= datetime.fromisoformat(period['started_at']) for e in events)
    forecasts = []; notes = []
    previous_shares = 0
    for event in events:
        shares, principal, fee = (int(event[k]) for k in ('shares', 'principal_sun', 'fee_sun'))
        if int(event['before_shares']) != previous_shares:
            complete = False
        previous_shares = int(event['after_shares'])
        fees += fee
        if event.get('entry_estimate_sun') is None:
            expected_fees = None
        elif expected_fees is not None:
            expected_fees += D(event['entry_estimate_sun'])
        if event['action'] == 'SUPPLY':
            deposited += principal
            lots.append({'shares': shares, 'cost': principal, 'original_shares': shares,
                         'at': event['at'], 'forecast': event['forecast']})
            forecasts.append(event['forecast'])
        else:
            returned += principal
            left, basis = shares, 0
            for lot in lots:
                take = min(left, lot['shares'])
                if not take: continue
                cost = lot['cost'] if take == lot['shares'] else lot['cost']*take//lot['shares']
                basis += cost; left -= take; lot['shares'] -= take; lot['cost'] -= cost
                expected_gross += accrued_forecast(lot, take, event['at'])
            if left: complete = False
            realized += principal-basis
    for lot in lots:
        expected_gross += accrued_forecast(lot, lot['shares'], at)
    shares_now = int(account['positions'][0]['shares_base_units'])
    open_shares = sum(l['shares'] for l in lots)
    complete = complete and shares_now == open_shares and int(raw.get('debt', '0')) == 0
    cost_basis = sum(l['cost'] for l in lots)
    value = int(account['positions'][0]['underlying_base_units'])
    accrued = value-cost_basis
    forecast_complete = bool(forecasts) and all(f and f.get('entry_estimate_sun') is not None for f in forecasts)
    expected = sum((D(f['net_total_sun']) for f in forecasts if f), D(0)) if forecast_complete else None
    expected_now = expected_gross-expected_fees if forecast_complete and expected_fees is not None else None
    net = realized+accrued-fees
    if not complete:
        notes.append('Share history does not reconcile with the current position. External transfers or untracked activity need reconciliation before income can be attributed.')
    if not forecast_complete and events:
        notes.append('An original forecast is missing. No forecast is reconstructed from current rates.')
    if period:
        notes.append('This performance period excludes earlier receipts. Historical transactions and policy spending limits are retained.')
    notes.append('TRX-denominated position income includes underlying conversion rounding. Rewards and external wallet transfers are excluded; fees are shown separately.')
    notes.append('Original forecast covers its full horizon. Expected to date prorates the original gross forecast and deducts estimates only for actions already executed; future exit costs remain in the full-horizon forecast.')
    p = {'network': 'nile', 'provenance': 'LIVE', 'as_of': at,
         'status': 'RECONCILED' if complete else 'INCOMPLETE',
         'period_start': period['started_at'] if period else events[0]['at'],
         **({'period_id': period['id'], 'period_empty': not events} if period else {}),
         'expected_return': amount(expected) if expected is not None else None,
         'expected_to_date': amount(expected_now) if expected_now is not None and complete else None,
         'accrued': amount(accrued) if complete else None, 'realized': amount(realized) if complete else None,
         'net_income': amount(net) if complete else None,
         'supplied_capital': amount(deposited),
         'net_return_pct': str((D(net)*100/deposited).quantize(D('0.000001'))) if complete and deposited else None,
         'expected_return_pct': str((expected_now*100/deposited).quantize(D('0.000001'))) if complete and deposited and expected_now is not None else None,
         'variance': amount(D(net)-expected_now) if expected_now is not None and complete else None,
         'rewards': None, 'price_pnl': None, 'debt_cost': amount(0) if complete else None,
         'fees': amount(fees), 'net_deposits': amount(deposited-returned),
         'open_cost_basis': amount(cost_basis) if complete else None,
         'withdrawn': amount(returned), 'txids': [e['txid'] for e in events],
         'basis': notes, 'ledger_hash': digest(events), 'observed_shares': str(shares_now)}
    state['workspace']['performance'] = p
    return p


def start_period(bridge, context, state, payload):
    """Begin a scoped display period only after all tracked investments are closed.

    Receipts, approvals and policy spend are immutable across this boundary.
    The enclosing bridge CAS rejects a concurrently changed workspace.
    """
    from .native_recovery import pending_requests
    if payload.get('confirm_new_period') is not True:
        raise MachineError('Confirm starting a new performance period.')
    if context.network != 'tron-nile' or bridge.native is None:
        raise MachineError('A new performance period currently supports the Nile TRX account only.')
    w = state['workspace']
    if pending_requests(state) or (w.get('execution') and w['execution']['status'] not in ('POSITION_RECONCILED', 'FAILED')):
        raise MachineError('Reconcile the pending transaction before starting a new performance period.')
    if state.get('usdd_execution', {}).get('status') not in (None, 'CLOSED', 'CANCELLED'):
        raise MachineError('Close the USDD workflow before starting a new performance period.')
    if w.get('positions'):
        raise MachineError('Refresh and close all positions before starting a new performance period.')
    at = bridge.clock()
    expiry = (datetime.fromisoformat(at) + timedelta(minutes=5)).isoformat()
    account, raw = bridge.native.account(context.scope, digest({'scope': context.scope, 'at': at}), expiry)
    if any(int(p['shares_base_units']) or int(p['underlying_base_units']) for p in account['positions']) or int(raw['debt']):
        raise MachineError('Close the live position and repay debt before starting a new performance period.')
    at = bridge.clock()
    previous = refresh(state, account, raw, at)
    if previous and previous['status'] != 'RECONCILED':
        raise MachineError('Reconcile the accounting history before starting a new performance period.')
    accounting = state.setdefault('native_accounting', {'version': 1, 'events': {}})
    archived = {'period': deepcopy(accounting.get('active_period')), 'performance': deepcopy(previous), 'ended_at': at}
    accounting.setdefault('period_history', []).append(archived)
    period = {'started_at': at, 'excluded_txids': sorted(accounting['events']),
              'starting_wallet_balance': amount(raw['wallet'].get('balance', 0)),
              'account_evidence_hash': digest(raw), 'previous_performance_hash': digest(previous)}
    period['id'] = 'period-' + digest(period)[:24]
    accounting['active_period'] = period
    if state.get('stake_position',{}).get('status')=='CLOSED':
        state.setdefault('stake_period_history',[]).append({'position':deepcopy(state.pop('stake_position')),'performance':deepcopy(w.get('stake_position')),'ended_at':at})
        w['stake_position']=None
    w['balances'] = [period['starting_wallet_balance'], *[a for a in w['balances'] if a['symbol'] != 'TRX']]
    state.setdefault('performance_period_evidence', {})[period['id']] = raw
    refresh(state, account, raw, at)
    return {'accepted': True, 'job_id': None, 'period_id': period['id'], 'started_at': at}


def accrued_forecast(lot, shares, at):
    f = lot['forecast']
    if not f or not shares:
        return D(0)
    elapsed = max(0, min(f['horizon_seconds'], int((datetime.fromisoformat(at)-datetime.fromisoformat(lot['at'])).total_seconds())))
    return D(f['gross_total_sun'])*D(shares)/lot['original_shares']*elapsed/f['horizon_seconds']
