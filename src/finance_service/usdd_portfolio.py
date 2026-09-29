"""Fresh read-only monitoring of the isolated USDD workflow.

Compares forward carry with debt reduction using the approved full exit budget.
It never represents that budget as a live quote for all dependent transactions.
Each actual recovery step still requires simulation and a wallet signature.
"""
from copy import deepcopy
from datetime import datetime,timedelta
from decimal import Decimal as D,localcontext
from economic_machine.values import MachineError,digest,decstr
from economic_machine.mandate import reserve_amount
from .usdd_market import read_market
from .usdd_execution import token_money
from .native_execution import money


def refresh_usdd(reviews,context,state,policy,source,notify):
    b=reviews.bridge;w=state['workspace'];wf=state['usdd_execution'];at=b.clock()
    r=dict(id='review-'+digest({'scope':context.scope,'at':at})[:24],network=w['network'],policy_hash=policy['policy_hash'] if policy else None,
        observed_at=at,expires_at=(datetime.fromisoformat(at)+timedelta(minutes=5)).isoformat(),source=source,
        status='DATA_UNAVAILABLE',reason='',checks=[],hold=None,alternatives=[],suggested=None,execution_authority='NONE',
        denomination='USDD',snapshot_hash=None,block=None,limitations=[
          'Only this isolated USDD workflow is valued; TRX amounts use the current provider price in USDD.',
          'Rates and reward budgets are variable. Rewards are not automatically compounded or guaranteed through the horizon.',
          'Keep versus exit uses the previously approved complete exit budget as a cost bound, not an exact quote for dependent future calls.',
          'No new borrowing or purchase is proposed by this monitor. Recovery needs fresh per-step simulation and your wallet signature.',
          'Collateral price gains and losses are excluded from forward carry. Liquidation and debt limits are checked separately.'])
    try:
        if not policy:raise MachineError('Confirmed conditions are needed to monitor this USDD position.')
        t=policy['mandate']['terms'];observed=b.usdd_execution.chain(context).snapshot(wf)
        route=b.usdd.refresh(context,state);facts=route['facts'];market,raw=read_market(b.observations.request,b.clock)
        if 'LIVE_INPUT_UNAVAILABLE' in route['blockers'] or not facts['token_match']:raise MachineError('Complete compatible USDD market inputs are required for monitoring.')
        if not 0<=datetime.fromisoformat(b.clock()).timestamp()-datetime.fromisoformat(market['market_observed_at']).timestamp()<=300:raise MachineError('USDD market valuation is stale.')
        if t['base_asset'] not in ('USDD','TRX') or len(t['capital'])!=1:raise MachineError('Single-asset USDD or TRX policy required for this valuation.')
        with localcontext() as ctx:
            ctx.prec=80
            price=D(market['trx_usd'])/D(market['usdd_usd']);base_price=price if t['base_asset']=='TRX' else D(1)
            capital=D(t['capital'][0]['amount'])*base_price;floor=reserve_amount(t['immediate_cash'],t['capital'][0]['amount'])*base_price
            value=D(observed['shares'])*D(observed.get('current_exchange_rate',observed['exchange_rate']))/10**36
            debt=D(observed.get('accrued_market_debt',observed['market_debt']))/10**18
            vault_debt=D(observed.get('accrued_vault_debt',observed['vault_debt']))/10**18
            collateral=D(observed['collateral_wad'])/10**18*price
            cash=D(observed['trx_balance'])/10**6*price if t['base_asset']=='TRX' else D(observed['token_balance'])/10**18
            fees=D(wf['spent_fees'])/10**6*price;exit_cost=D(wf['exit_fee_reserve'])/10**6*price
            end=min(datetime.fromisoformat(t['expires_at']),datetime.fromisoformat(wf['created_at'])+timedelta(seconds=wf['horizon_seconds']))
            remaining=max(0,int((end-datetime.fromisoformat(at)).total_seconds()));years=D(min(remaining,31536000))/31536000
            loan=next(x for x in facts['collaterals'] if x['ilk']==wf['ilk'])
            income=value*((1+D(facts['supply_apy']))**years-1+D(market['reward_apr'])*years)
            interest=debt*((1+D(facts['borrow_apy']))**years-1)+vault_debt*((1+D(loan['stability_apy']))**years-1)
            carry=income-interest;checks=[]
            if debt+vault_debt>D(t['borrowing']['max_debt']['amount'])*base_price or (debt+vault_debt>0 and not t['borrowing']['consent']):checks.append('DEBT_LIMIT_BREACH')
            if cash<floor:checks.append('CASH_FLOOR_BREACH')
            if value>capital*D(t['protocol_caps_bps'].get('justlend',0))/10000:checks.append('PROTOCOL_CAP_BREACH')
            if value+D(observed['token_balance'])/10**18>capital*D(t['price_exposure_caps_bps'].get('USDD',0))/10000:checks.append('USDD_EXPOSURE_BREACH')
            if collateral>capital*D(t['price_exposure_caps_bps'].get('TRX',0))/10000:checks.append('TRX_EXPOSURE_BREACH')
            if value>D(facts['market_cash_usdd']):checks.append('EXIT_LIQUIDITY_SHORTFALL')
            if fees+exit_cost>D(t['limits']['fee_amount']['amount'])*base_price:checks.append('TOTAL_COST_LIMIT')
            if int(wf['total_fee_cap'])-int(wf['spent_fees'])<int(wf['exit_fee_reserve']) or int(observed['trx_balance'])<int(wf['exit_fee_reserve']):checks.append('EXIT_FEE_RESERVE_SHORTFALL')
            buffer=D(t['borrowing']['liquidation_buffer_bps'])/10000
            oracle_collateral=D(observed['collateral_wad'])/10**18*D(loan['debt_capacity_per_trx'])*D(loan['liquidation_ratio'])
            if vault_debt and oracle_collateral<vault_debt*(max(D(loan['liquidation_ratio']),D(t['borrowing']['min_collateral_ratio_bps'])/10000)+buffer):checks.append('VAULT_COLLATERAL_BUFFER_BREACH')
            if debt and value<debt*(max(1/D(facts['collateral_factor']),D(t['borrowing']['min_collateral_ratio_bps'])/10000)+buffer):checks.append('LENDING_COLLATERAL_BUFFER_BREACH')
            assumptions=(policy.get('review_inputs') or {}).get('assumptions')
            if not assumptions:raise MachineError('Confirmed stress and daily-loss assumptions are missing.')
            for assumption,limit,code in [('daily_loss_bps','daily_loss','DAILY_LOSS_LIMIT_BREACH'),('stress_loss_bps','stress_loss','STRESS_LOSS_LIMIT_BREACH')]:
                if (value+collateral)*D(assumptions[assumption])/10000>D(t['limits'][limit]['amount'])*base_price:checks.append(code)
            r.update(capital=decstr(capital),paid_fees=decstr(fees),remaining_budget=decstr(max(D(0),capital-fees)),cash_floor=decstr(floor),remaining_seconds=remaining,checks=checks,
                block=str(observed['block_to']['number']),snapshot_hash=digest({'account':observed,'market':market,'route':route.get('hash',digest(route))}))
            hold=dict(action='HOLD',position=decstr(value),wallet_cash=decstr(cash),gross_income=decstr(carry),change_cost='0',exit_reserve=decstr(exit_cost),net_income=decstr(carry-exit_cost),eligible=not checks,reasons=checks)
            alternative=dict(action='REDEEM',position='0',wallet_cash=decstr(cash),gross_income='0',change_cost=decstr(exit_cost),exit_reserve='0',net_income=decstr(-exit_cost),eligible=False,reasons=['SEPARATE_DEBT_RECOVERY_REVIEW_REQUIRED'],net_improvement=decstr(-carry))
            r.update(hold=hold,alternatives=[alternative],status='POLICY_BREACH' if checks else 'ADJUST' if carry<0 else 'HOLD',
                reason='Your USDD position breaches confirmed limits. Review repayment and exit in USDD strategies.' if checks else 'Projected debt costs exceed income. Review repayment and exit under the full reserved cost budget.' if carry<0 else 'Projected carry is positive under the current rates and reserved exit budget. Keep the position and check again on your routine.')
            if remaining==0 or datetime.fromisoformat(at)<datetime.fromisoformat(t['effective_at']):r.update(status='POLICY_INACTIVE',reason='Your USDD investment horizon needs renewal or a repayment and exit review.')
            if wf['status'] in ('SUBMISSION_UNKNOWN','SUBMITTED','DISPUTED'):r.update(status='PENDING_EXECUTION',reason='Reconcile the outstanding USDD transaction before adjusting the position.')
            key='usdd-'+context.network;keys=(key,key+'-vault')
            other=[p for p in w['positions'] if p['id'] not in keys]
            w['positions']=list(other)
            for position_id,product,protocol,principal,current,d in [(key,'justlend.v1.jUSDD','JustLend',token_money(int(wf['amount'])),token_money(int(value*10**18)),token_money(int(debt*10**18))),(key+'-vault','usdd.vault.'+wf['ilk'],'USDD',money(int(wf['collateral_sun'])),money(int(observed['collateral_wad'])//10**12),token_money(int(vault_debt*10**18)))]:
                if D(current['value']) or D(d['value']):w['positions'].append(dict(id=position_id,product=product,protocol=protocol,network=w['network'],provenance='LIVE',principal=principal,current_value=current,debt=d,exit_status='Review repayment and exit in USDD strategies.',receipt_txid=wf['confirmed_txids'][-1] if wf['confirmed_txids'] else None))
            w['balances']=[x for x in w['balances'] if x['symbol'] not in ('TRX','USDD')]+[money(int(observed['trx_balance'])),token_money(int(observed['token_balance']))]
            state['usdd_monitor_evidence']={'observed_at':at,'account':observed,'market':raw,'route':route,'carry_usdd':decstr(carry),'debt_interest_usdd':decstr(interest)}
            if other:raise MachineError('Other recorded positions need a consolidated valuation; USDD monitoring alone cannot describe the whole portfolio.')
    except (MachineError,KeyError,ValueError,TypeError,OSError) as exc:r.update(status='DATA_UNAVAILABLE',reason=str(exc),suggested=None)
    if datetime.fromisoformat(b.clock())>=datetime.fromisoformat(r['expires_at']):r.update(status='DATA_UNAVAILABLE',reason='USDD review inputs expired during calculation.')
    w['portfolio_review']=r;state['portfolio_reviews']=(state.get('portfolio_reviews',[])+[deepcopy(r)])[-50:]
    if notify:reviews.notifications(w,r,b.clock())
    return r
