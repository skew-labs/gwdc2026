"""Read-only, scoped portfolio reviews. No trade/approval/broadcast capability.

The supported live review universe is Nile wallet TRX + JustLend jTRX.
Unpriced assets are disclosed; unsupported recorded positions fail closed.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from concurrent.futures import ThreadPoolExecutor

from economic_machine.values import MachineError, digest, decstr
from economic_machine.mandate import reserve_amount
from economic_machine.yield_math import exact, period_yield
from .rebalance_gate import rebalance_reasons
from .native_execution import PRODUCT, MARKET, FAILURE, money, verify_native_abi

D = Decimal
SUN = D(10**6)
TERMINAL = {'POSITION_RECONCILED', 'PERFORMANCE_TRACKED', 'FAILED'}


def confirmed_policy(state):
    """Draft edits must not change the policy or assumptions used by Watch."""
    rows = [r for a in state.get('mandates', {}).values() for r in a['revisions']
            if r.get('confirmed_at') and r.get('policy_hash')]
    if not rows:
        return None
    row = max(rows, key=lambda r: r['confirmed_at'])
    policy = deepcopy(row)
    saved = state.get('confirmed_review_inputs', {}).get(row['policy_hash'])
    # Backfill only from an actually confirmed projection, never from a draft.
    w = state['workspace']; m = w.get('mandate') or {}
    if saved is None and m.get('status') == 'CONFIRMED' and m.get('hash') == row['policy_hash']:
        saved = {'assumptions': state.get('planning_assumptions'), 'constraints': m['constraints']}
        state.setdefault('confirmed_review_inputs', {})[row['policy_hash']] = deepcopy(saved)
    if saved is None:
        # Historical releases kept reviewed inputs in scoped calculation evidence.
        # Recover only an exact confirmed policy match, never the current draft.
        for event in reversed(w.get('evidence', [])):
            m0=(event.get('inputs') or {}).get('mandate') or {}
            a0=(event.get('calculation') or {}).get('assumptions')
            if event.get('mandate_hash')==row['policy_hash'] and m0.get('hash')==row['policy_hash'] and m0.get('status')=='CONFIRMED' and a0:
                saved={'assumptions':a0,'constraints':m0['constraints']}
                state.setdefault('confirmed_review_inputs', {})[row['policy_hash']]=deepcopy(saved)
                break
    policy['review_inputs'] = saved
    return policy


def policy_projection(state,p,status):
    current=state['workspace'].get('mandate') or {}
    if status=='CONFIRMED' and current.get('status')=='CONFIRMED' and current.get('hash')==p['policy_hash']:
        return deepcopy(current)
    raw=p['mandate']
    return {'id':raw.get('mandate_id','active-'+p['policy_hash'][:24]),'version':raw.get('revision',1),
            'hash':p['policy_hash'],'network':state['workspace']['network'],'status':status,
            'constraints':deepcopy(p['review_inputs']['constraints']),'terms':deepcopy(raw['terms']),
            'source_text':'Previously confirmed limits proposed for this exact withdrawal only. New explicit transaction approval is required. Pending investment draft edits are not applied.',
            'missing_fields':[],'confirmed_at':p['confirmed_at']}


def active_mandate(state):
    p=confirmed_policy(state)
    return policy_projection(state,p,'CONFIRMED') if p and p.get('status')=='CONFIRMED' and p.get('review_inputs') else None


def withdrawal_policy(state):
    """A draft may retire a policy. Reapprove only an exact withdrawal, never
    reactivate that policy or grant supply/debt permission from a retired record.
    Revoked policies and policies replaced by a confirmed revision cannot be used.
    """
    active=active_mandate(state)
    if active:return active
    p=confirmed_policy(state);w=state['workspace'];m=w.get('mandate') or {}
    if not p or p.get('status')!='SUPERSEDED' or not p.get('review_inputs') or m.get('status')!='DRAFT':return None
    aggregate=state.get('mandates',{}).get(m.get('id'))
    if not aggregate or aggregate['revisions'][-1].get('status')!='DRAFT' or aggregate['revisions'][-1].get('draft_hash')!=m.get('hash'):return None
    if not any(r.get('policy_hash')==p['policy_hash'] for r in aggregate['revisions']):return None
    return policy_projection(state,p,'DRAFT')


def redeem_quote(native, owner, shares):
    if shares <= 0:
        raise MachineError('No shares available for a redemption cost simulation.')
    with ThreadPoolExecutor(max_workers=3) as pool:
        fs = [pool.submit(native.rpc, 'wallet/getcontract', {'value': MARKET, 'visible': False}),
              pool.submit(native.rpc, 'wallet/triggerconstantcontract', {'owner_address': owner,
                  'contract_address': MARKET, 'function_selector': 'redeem(uint256)',
                  'parameter': hex(shares)[2:].rjust(64, '0'), 'visible': False}),
              pool.submit(native.rpc, 'wallet/getchainparameters')]
        code, sim, params = [f.result() for f in fs]
    verify_native_abi(code)
    out = sim.get('constant_result')
    if (sim.get('result', {}).get('result') is not True or not isinstance(out, list) or len(out) != 1
            or len(out[0]) != 64 or int(out[0], 16) != 0 or type(sim.get('energy_used')) is not int
            or sim['energy_used'] <= 0 or any(x.get('topics', [None])[0] == FAILURE for x in sim.get('logs', []))):
        raise MachineError('Current jTRX redemption simulation did not succeed.')
    prices = {x['key']: x.get('value', 0) for x in params.get('chainParameter', [])}
    energy, bandwidth = prices.get('getEnergyFee'), prices.get('getTransactionFee')
    if type(energy) is not int or type(bandwidth) is not int or min(energy, bandwidth) <= 0:
        raise MachineError('Current resource prices are unavailable.')
    evidence={'code':code,'simulation':sim,'parameters':params}
    return {'cost': decstr(D(sim['energy_used'] * energy + 1024 * bandwidth) / SUN),
            'evidence':evidence,
            'hash': digest({'code': code, 'simulation': sim, 'parameters': params}),
            'observed_at': native.clock(), 'basis': 'Live redeem simulation; full Energy burn and 1,024-byte bandwidth bound. Resource credits excluded.'}


def spent_under_policy(state, policy_hash):
    """Count reconciled supply amounts once per TXID; never count forecasts."""
    txs = {tx:D(v['amount'])/SUN for tx,v in state.get('review_spend_ledger',{}).items() if v['policy_hash']==policy_hash}
    for event in state['workspace'].get('evidence', []):
        n = (event.get('calculation') or {}).get('native_execution') or {}
        if event.get('mandate_hash') != policy_hash or (n.get('reconciliation') or {}).get('status') != 'RECONCILED':
            continue
        for tx in event.get('txids', []):
            txs[tx] = D(0) if n.get('operation')=='REDEEM' else D(n['amount']) / SUN
            fee=(n.get('execution_result') or {}).get('resource_receipt',{}).get('total_fee_sun')
            entry={'policy_hash':policy_hash,'amount':str(int(txs[tx]*SUN)),'action':n.get('operation','SUPPLY')}
            if fee is not None: entry['fee']=str(fee)
            state.setdefault('review_spend_ledger',{}).setdefault(tx,{}).update(entry)
    return sum(txs.values(), D(0))


@exact
def evaluate(policy, assumptions, live, at, quote_supply, quote_redeem, spent= D(0), paid_fees=D(0)):
    """Compare holding with bounded same-asset adjustments over remaining horizon."""
    t = policy['mandate']['terms']; capital = D(t['capital'][0]['amount'])
    now = datetime.fromisoformat(at); start = datetime.fromisoformat(t['effective_at'])
    end = min(datetime.fromisoformat(t['expires_at']), start + timedelta(seconds=t['horizon_seconds']))
    seconds = max(0, int((end-now).total_seconds()))
    current, wallet, debt, pool = [D(live[k]) for k in ('position', 'wallet', 'debt', 'pool_cash')]
    budget=max(D(0),capital-paid_fees)
    cash = min(wallet, max(D(0), budget-current))
    floor = reserve_amount(t['immediate_cash'], str(capital))
    for w in t['withdrawals']:
        # Reserve scheduled withdrawals in cash; avoids assuming guaranteed liquidity.
        floor = max(floor, reserve_amount(w['minimum'], str(capital)))
    limits = {k: D(v['amount']) for k,v in t['limits'].items()}
    rate = {'annual_fraction': live['apy'], 'convention': 'EFFECTIVE_APY', 'day_count': 'ACT_365F'}
    income = lambda principal: D(period_yield(decstr(principal), rate, seconds)['income'])
    daily, stress = D(assumptions['daily_loss_bps']) / 10000, D(assumptions['stress_loss_bps']) / 10000
    if not 0 <= daily <= 1 or not 0 <= stress <= 1:
        raise MachineError('Reviewed risk assumptions are invalid.')
    cap = capital * t['protocol_caps_bps'].get('justlend', 0) / 10000
    violations = []
    if paid_fees > limits['fee_amount']: violations.append('PAID_FEE_LIMIT_BREACH')
    if cash < floor: violations.append('CASH_FLOOR_BREACH')
    if current > cap: violations.append('PROTOCOL_CAP_BREACH')
    if current * daily > limits['daily_loss']: violations.append('DAILY_LOSS_LIMIT_BREACH')
    if current * stress > limits['stress_loss']: violations.append('STRESS_LOSS_LIMIT_BREACH')
    if debt > D(t['borrowing']['max_debt']['amount']) or (debt > 0 and t['borrowing']['consent'] is not True): violations.append('DEBT_LIMIT_BREACH')
    # TRX cash also carries TRX price exposure; supplying it does not remove exposure.
    if min(capital, wallet+current) > capital*t['price_exposure_caps_bps'].get('TRX',0)/10000:
        violations.append('TRX_EXPOSURE_BREACH')
    if current > pool: violations.append('EXIT_LIQUIDITY_SHORTFALL')
    exit_reserve = D(assumptions['exit_cost'])
    if exit_reserve < 0: raise MachineError('Invalid reviewed exit reserve.')
    redeem, quote_errors = None, []
    if current > 0:
        try:
            redeem = quote_redeem(current)
            exit_reserve = max(exit_reserve, D(redeem['cost']))
        except (MachineError, OSError, ValueError, KeyError) as exc:
            quote_errors.append('REDEMPTION_QUOTE_UNAVAILABLE')
    exit_estimate = D(redeem['cost']) if redeem else exit_reserve
    hold = {'action': 'HOLD', 'position': decstr(current), 'wallet_cash': decstr(cash),
            'gross_income': decstr(income(current)), 'change_cost': '0',
            'exit_reserve': decstr(exit_reserve if current else D(0)),
            'future_exit_estimate': decstr(exit_estimate if current else D(0)),
            'net_income': decstr(income(current)-(exit_estimate if current else D(0))),
            'eligible': not violations, 'reasons': violations, 'quote_hash': None}
    candidates = []
    upper = min(cap, budget-floor, current+limits['single_amount'], current+max(D(0), limits['cumulative_amount']-spent))
    if daily: upper = min(upper, limits['daily_loss']/daily)
    if stress: upper = min(upper, limits['stress_loss']/stress)
    upper = max(D(0), upper.quantize(D('0.000001'), rounding=ROUND_FLOOR))
    # Same-asset interest is monotone; evaluate the maximum bounded position and a full exit.
    targets = sorted({upper, D(0)} - {current})
    for target in targets:
        action = 'SUPPLY' if target > current else 'REDEEM'
        delta = abs(target-current); reasons = []
        if action not in t['allowed_actions']: reasons.append('ACTION_NOT_ALLOWED')
        if delta > limits['single_amount']: reasons.append('SINGLE_AMOUNT_LIMIT')
        if action == 'SUPPLY' and spent+delta > limits['cumulative_amount']: reasons.append('CUMULATIVE_AMOUNT_LIMIT')
        if action == 'REDEEM' and delta > pool: reasons.append('EXIT_NOT_VERIFIED')
        if debt > 0: reasons.append('DEBT_REPAYMENT_REQUIRES_SEPARATE_REVIEW')
        if 'TRX_EXPOSURE_BREACH' in violations: reasons.append('TRX_EXPOSURE_BREACH')
        quote = None
        if not reasons:
            try:
                quote = quote_supply(delta) if action == 'SUPPLY' else (redeem if target == 0 else quote_redeem(delta))
                if quote is None: raise MachineError('Quote unavailable')
                # Fees consume the same policy cash budget. Re-quote a smaller
                # amount instead of rejecting every useful allocation at the cap.
                if action == 'SUPPLY':
                    for _ in range(3):
                        affordable = (min(budget,wallet+current)-floor-D(quote['cost'])-exit_reserve).quantize(D('0.000001'),rounding=ROUND_FLOOR)
                        if affordable >= target or affordable <= current: break
                        target=affordable; delta=target-current
                        quote=quote_supply(delta)

            except (MachineError, OSError, ValueError, KeyError):
                reasons.append(action + '_QUOTE_UNAVAILABLE')
        cost = D(quote['cost']) if quote else None
        if cost is not None and (not cost.is_finite() or cost < 0):
            raise MachineError('Invalid transaction cost quote.')
        future_exit = exit_reserve if target else D(0)
        projected_cash = min(wallet+current-target-(cost or D(0)), max(D(0), budget-target-(cost or D(0))))
        if cost is not None:
            if cost > D(policy['review_inputs']['constraints']['max_fee']['value']): reasons.append('FEE_LIMIT')
            if paid_fees+cost+future_exit > limits['fee_amount']: reasons.append('TOTAL_COST_LIMIT')
            if projected_cash-future_exit < floor: reasons.append('CASH_AND_EXIT_RESERVE_LIMIT')
        if target > cap: reasons.append('PROTOCOL_CAP_BREACH')
        if target*daily > limits['daily_loss'] or target*stress > limits['stress_loss']: reasons.append('LOSS_LIMIT')
        # A liquidity reserve is not an expected fee. Use the current live exit
        # estimate for both future exits; reserve extra cash separately.
        future_estimate = exit_estimate if target else D(0)
        net = income(target)-(cost or D(0))-future_estimate
        benefit = income(target)-income(current)
        exit_saving=max(D(0),D(hold['future_exit_estimate'])-future_estimate)
        additional_cost = (cost or D(0))+max(D(0), future_estimate-D(hold['future_exit_estimate']))
        candidates.append({'action': action, 'position': decstr(target), 'delta': decstr(delta),
            'wallet_cash': decstr(max(D(0), projected_cash)), 'gross_income': decstr(income(target)),
            'change_cost': decstr(cost) if cost is not None else None, 'exit_reserve': decstr(future_exit),
            'future_exit_estimate':decstr(future_estimate),
            'net_income': decstr(net) if cost is not None else None,
            'exit_cost_saving':decstr(exit_saving), 'net_improvement':decstr(net-D(hold['net_income'])) if cost is not None else None,
            'benefit_over_hold': decstr(benefit), 'additional_cost': decstr(additional_cost) if cost is not None else None,
            'eligible': not reasons, 'reasons': reasons, 'quote_hash': quote['hash'] if quote else None})
    eligible = [c for c in candidates if c['eligible']]
    best = max(eligible, key=lambda c:D(c['net_improvement']), default=None)
    status = 'HOLD'; reason = 'No verified adjustment improves projected income after additional costs.'
    if now < start or seconds == 0:
        status, reason = 'POLICY_INACTIVE', 'Confirmed conditions are not currently effective. Review and confirm a new investment horizon.'
    elif violations:
        status, reason = 'POLICY_BREACH', 'Current holdings are outside confirmed conditions. Review the failed checks before any transaction.'
    elif quote_errors or any('QUOTE_UNAVAILABLE' in r for c in candidates for r in c['reasons']):
        status, reason = 'DATA_UNAVAILABLE', 'Holdings were refreshed, but complete live transaction costs could not be verified.'
    elif best and not rebalance_reasons(str(int(max(D(0),D(best['benefit_over_hold'])+D(best['exit_cost_saving']))*SUN)), str(int(D(best['additional_cost'])*SUN)), None, 0, at):
        status, reason = 'ADJUST', 'A permitted adjustment has higher projected income after incremental costs.'
    return dict(status=status, reason=reason, checks=violations, hold=hold, alternatives=candidates,
        suggested=best if status in {'ADJUST','POLICY_BREACH'} else None, remaining_seconds=seconds,
        capital=decstr(capital), paid_fees=decstr(paid_fees), remaining_budget=decstr(budget), cash_floor=decstr(floor), rate=rate, quote_errors=quote_errors)


class PortfolioReviews:
    def __init__(self, bridge): self.bridge = bridge

    def refresh(self, context, state, *, source='ON_DEMAND', notify=True):
        b = self.bridge; w = state['workspace']; at = b.clock()
        p = confirmed_policy(state)
        if context.network=='tron-mainnet' and state.get('usdd_execution',{}).get('status') not in (None,'CLOSED','CANCELLED'):
            from .usdd_portfolio import refresh_usdd
            return refresh_usdd(self,context,state,p,source,notify)
        review = dict(id='review-'+digest({'scope':context.scope,'at':at})[:24], network=w['network'],
            policy_hash=p['policy_hash'] if p else None, observed_at=at,
            expires_at=(datetime.fromisoformat(at)+timedelta(minutes=5)).isoformat(),
            source=source, status='DATA_UNAVAILABLE', reason='', checks=[], hold=None, alternatives=[],
            suggested=None, execution_authority='NONE', limitations=[
                'Nile TRX wallet and JustLend jTRX only; other assets are outside this valuation.',
                'Forecasts hold the current APY constant; incentives and price gains are excluded.',
                'Wallet and position reads are bracketed by an unchanged solidified head. Market and simulation calls are separate live reads, not a cryptographic state proof.',
                'Live change costs use full Energy burn plus a bandwidth bound, before resource credits.',
                'Future exit estimates reuse the current redemption simulation, assuming unchanged fees. The separate cash reserve is the greater of that estimate and your reviewed reserve; future fees may change.',
                'This is a review. Any transaction requires a fresh execution review and your wallet signature.'],
            snapshot_hash=None, block=None)
        state['portfolio_quote_evidence']={}
        try:
            if b.observations is None: raise MachineError('Live observations are unavailable.')
            observation = b.observations.read(context, p['mandate']['terms']['base_asset'] if p else 'TRX')
            if not observation['balances']: raise MachineError('Wallet observation unavailable.')
            w.update(balances=observation['balances'], snapshots=observation['snapshots'])
            state['observation'] = observation
            if context.network != 'tron-nile': raise MachineError('Complete position and cost reviews currently support Nile TRX only.')
            account, raw = b.native.account(context.scope, digest(observation), review['expires_at'])
            if raw['block'].get('timestamp_ms'):
                solid_expiry=datetime.fromtimestamp(raw['block']['timestamp_ms']/1000, tz=datetime.fromisoformat(at).tzinfo)+timedelta(minutes=5)
                review['expires_at']=min(datetime.fromisoformat(review['expires_at']),solid_expiry).isoformat()
            current = D(account['positions'][0]['underlying_base_units'])/SUN
            w['balances'] = [money(int(raw['wallet'].get('balance',0))), *[a for a in w['balances'] if a['symbol']!='TRX']]
            old = next((x for x in w['positions'] if x['product']==PRODUCT), None)
            other = [x for x in w['positions'] if x['product']!=PRODUCT]
            position = {**(old or {'id':'nile-jtrx','product':PRODUCT,'protocol':'JustLend','network':'nile','provenance':'LIVE',
                'principal':None,'receipt_txid':None,'exit_status':'Separate redemption review required.'}),
                'current_value': money(int(account['positions'][0]['underlying_base_units'])),
                'debt':money(int(raw['debt'])), 'observed_at':raw['observed_at'], 'shares_base_units':raw['shares']}
            w['positions'] = other + ([position] if int(raw['shares']) or int(raw['debt']) else [])
            state['portfolio_account_evidence'] = raw
            from .native_performance import refresh as refresh_performance
            performance=refresh_performance(state,account,raw,b.clock())
            if performance and performance['status']=='RECONCILED':position['principal']=performance['open_cost_basis']
            review.update(snapshot_hash=digest({'market':observation,'account':raw}), block=str(raw['block']['number']))
            if int(raw['debt']) and p and p['mandate']['terms']['borrowing']['consent'] is True:
                raise MachineError('Debt valuation and repayment forecasts are not available in this review adapter.')
            if other: raise MachineError('A recorded position has no verified valuation adapter. A complete portfolio comparison is unavailable.')
            if not p: raise MachineError('Confirm investment conditions before monitoring compliance or comparing adjustments.')
            t=p['mandate']['terms']
            if t['base_asset']!='TRX' or len(t['capital'])!=1 or t['capital'][0]['asset']!='TRX':
                raise MachineError('A verified cross-asset review adapter is required for these conditions.')
            if not p['review_inputs'] or not p['review_inputs'].get('assumptions'):
                raise MachineError('Confirmed cost and risk assumptions are missing. Review conditions before enabling comparison.')
            fact=(observation.get('snapshot') or {}).get('facts',{}).get(PRODUCT+'.supply_apy',{})
            if fact.get('availability')!='AVAILABLE' or fact.get('quality') not in {'VALID','VALID_ZERO'}:
                raise MachineError('Current JustLend APY is unavailable.')
            live=dict(position=decstr(current),wallet=decstr(D(raw['wallet'].get('balance',0))/SUN),debt=decstr(D(raw['debt'])/SUN),
                pool_cash=decstr(D(raw['cash'])/SUN),apy=fact['value'])
            def supply(amount):
                q=b.native.simulate(context.wallet,int(amount*SUN))
                if q.get('evidence'): state['portfolio_quote_evidence'][q['hash']]=q['evidence']
                return {'cost':decstr(D(q['energy_burn']+q['bandwidth_bound'])/SUN),'hash':q['hash']}
            redemption_cache={}
            def redemption(amount):
                shares=int(raw['shares']) if amount >= current else min(int(raw['shares']), (int(amount*SUN)*10**18+int(raw['exchange_rate'])-1)//int(raw['exchange_rate']))
                if shares in redemption_cache: return redemption_cache[shares]
                q=redeem_quote(b.native,context.wallet,shares)
                redemption_cache[shares]=q
                if q.get('evidence'): state['portfolio_quote_evidence'][q['hash']]=q['evidence']
                return q
            spent=spent_under_policy(state,p['policy_hash'])
            ledger=[x for x in state.get('review_spend_ledger',{}).values() if x['policy_hash']==p['policy_hash']]
            if any('fee' not in x for x in ledger): raise MachineError('Actual fees for a recorded policy transaction are unavailable.')
            paid_fees=sum((D(x['fee'])/SUN for x in ledger),D(0))
            review.update(evaluate(p,p['review_inputs']['assumptions'],live,b.clock(),supply,redemption,spent,paid_fees))
            review['holdings']=live
            execution=w.get('execution')
            if execution and execution['status'] not in TERMINAL:
                review.update(status='PENDING_EXECUTION',reason='An execution is unresolved. Reconcile it before considering another transaction.',suggested=None)
        except (MachineError, OSError, ValueError, KeyError, TypeError) as exc:
            review.update(status='DATA_UNAVAILABLE', reason=str(exc), suggested=None)
        # Bound total freshness, including time spent fetching and simulating.
        if datetime.fromisoformat(b.clock()) >= datetime.fromisoformat(review['expires_at']):
            review.update(status='DATA_UNAVAILABLE',reason='Review inputs expired during calculation. Retry with fresh state.',suggested=None)
        review['id']='review-'+digest({'scope':context.scope,'at':at,'snapshot':review['snapshot_hash'],'policy':review['policy_hash'],'status':review['status'],'hold':review['hold'],'alternatives':review['alternatives']})[:24]
        from .native_adjustments import option_hash
        for option in review['alternatives']:option['hash']=option_hash(option)
        w['portfolio_review']=review
        state['portfolio_reviews']=(state.get('portfolio_reviews',[])+[deepcopy(review)])[-50:]
        if notify: self.notifications(w,review, b.clock())
        return review

    @staticmethod
    def notifications(w, review, at):
        rows=w.setdefault('notifications',[])
        actionable=review['status'] in {'ADJUST','POLICY_BREACH','POLICY_INACTIVE','DATA_UNAVAILABLE','PENDING_EXECUTION'}
        key=digest({'policy':review['policy_hash'],'status':review['status'],'checks':review['checks'],
            'action':(review.get('suggested') or {}).get('action'),
            'failure':review['reason'] if review['status']=='DATA_UNAVAILABLE' else None})
        for n in rows:
            if review['status'] not in {'DATA_UNAVAILABLE','PENDING_EXECUTION'} and n.get('resolved_at') is None and (not actionable or n['incident_key']!=key): n['resolved_at']=at
        if not actionable: return
        existing=next((n for n in rows if n['incident_key']==key and n.get('resolved_at') is None),None)
        if existing:
            existing.update(review_id=review['id'], updated_at=at)
            return
        if review['status']=='ADJUST':
            last=next((n for n in reversed(rows) if n['kind']=='ADJUST'),None)
            if last and 'REBALANCE_COOLDOWN_ACTIVE' in rebalance_reasons('1','0',last['created_at'],21600,at):
                return
        titles={'ADJUST':'An adjustment is worth reviewing','POLICY_BREACH':'Your holdings need a conditions review',
            'POLICY_INACTIVE':'Your confirmed investment horizon needs renewal','DATA_UNAVAILABLE':'Portfolio review could not finish',
            'PENDING_EXECUTION':'Reconcile your pending transaction'}
        rows.append(dict(id='notice-'+review['id'],incident_key=key,kind=review['status'],title=titles[review['status']],
            detail=review['reason'],review_id=review['id'],created_at=at,updated_at=at,read_at=None,resolved_at=None))
        w['notifications']=rows[-100:]
