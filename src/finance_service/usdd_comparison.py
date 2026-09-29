"""Same-budget allocation comparisons compiled through the real USDD executor.

The 10% grid is explicit. Every candidate passes the execution service's policy,
balance, debt and full fee-reserve checks. Selected candidates also simulate the
first step. A later dependent step still requires its own fresh simulation.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR

from economic_machine.values import MachineError, digest, decstr
from economic_machine.usdd_workflow import build_steps
from .portfolio_review import confirmed_policy
from .usdd_market import read_market

D=Decimal


def compare(bridge,context,state):
    svc=bridge.usdd_execution;record,t=svc.policy(context,state)
    at=bridge.clock();network=context.network.removeprefix('tron-');base=t['base_asset']
    out={'id':'usdd-comparison-'+digest({'policy':record['policy_hash'],'at':at})[:24],
        'network':network,'mandate_hash':record['policy_hash'],'snapshot_root':digest({'at':at}),
        'expires_at':min(t['expires_at'],(datetime.fromisoformat(at)+timedelta(minutes=3)).isoformat()),
        'status':'INFEASIBLE','plans':[],'reason':None,'math_version':'usdd-executable-comparison-1',
        'adapter_version':'machine-usdd-workflow-1',
        'search_scope':'10% allocation grid with the same confirmed capital and horizon. No additional recursive borrowing. Both selected candidates pass current account/policy checks and first-step simulation. Each dependent transaction needs fresh approval and simulation.',
        'usdd_vault':{'status':'EXCLUDED','reason':'Owned-USDD plans do not create debt.'}}
    core={'kind':'USDD_WORKFLOW','payloads':{},'candidates':[],'exclusions':[]}
    if network!='mainnet':
        out['reason']='No verified Nile USDD yield-and-execution route is available. No mainnet rates or tokens are substituted.'
        return out,core
    if base not in ('USDD','TRX'):raise MachineError('USDD workflow comparisons require owned USDD or TRX collateral capital.')
    try:
        account=svc.chain(context).snapshot()
        report=bridge.usdd.refresh(context,state)
        market,evidence=read_market(bridge.observations.request,bridge.clock)
        inputs={'account':account,'report':report,'market':market,'market_evidence':evidence}
        core['inputs']=inputs
        out['snapshot_root']=digest(inputs)
        source_time=datetime.fromisoformat(market['market_observed_at'])
        if not 0<=(datetime.fromisoformat(bridge.clock())-source_time).total_seconds()<=300:
            raise MachineError('Current market valuation is older than five minutes. Refresh before comparing executable plans.')
        out['expires_at']=min(out['expires_at'],(source_time+timedelta(minutes=5)).isoformat())
        price=D(market['trx_usd'])/D(market['usdd_usd'])
        capital=D(t['capital'][0]['amount']);base_price=price if base=='TRX' else D(1)
        fee=record['review_inputs']['constraints']['max_fee']
        per=(D(fee['value'])/(price if fee['symbol']=='USDD' else D(1))).quantize(D('.000001'),rounding=ROUND_FLOOR)
        if fee['symbol'] not in ('USDD','TRX') or per<=0:raise MachineError('A positive TRX or USDD transaction fee cap is required.')
        capamount=lambda v:{'value':decstr(D(v)),'symbol':base,'decimals':6 if base=='TRX' else 18}
        tokenamount=lambda v:{'value':decstr(D(v)),'symbol':'USDD','decimals':18}
        mode='OWNED' if base=='USDD' else 'VAULT'
        if mode=='VAULT':out['usdd_vault']={'status':'INCLUDED','reason':'TRX collateral → USDD issuance → JustLend. All debt limits and recovery reserves apply.'}
        eligible=[]
        for weight in range(1000,10000,1000):
            equity=(capital*weight/10000).quantize(D('.000001') if base=='TRX' else D('1e-18'),rounding=ROUND_FLOOR)
            ilk='TRX-C';loan=next(x for x in report['facts']['collaterals'] if x['ilk']==ilk)
            if mode=='VAULT':
                ratio=max(D(loan['liquidation_ratio']),D(t['borrowing']['min_collateral_ratio_bps'])/10000)+D(t['borrowing']['liquidation_buffer_bps'])/10000
                principal=(equity*D(loan['debt_capacity_per_trx'])*D(loan['liquidation_ratio'])/ratio).quantize(D('1e-18'),rounding=ROUND_FLOOR)
            else:principal=equity
            steps=build_steps(amount=int(principal*10**18),collateral_sun=int(equity*10**6) if mode=='VAULT' else 0,
                ilk=ilk,loops=0,borrow_bps=0,has_proxy=bool(account['proxy']))
            total=per*(len(steps)+3+(3 if mode=='VAULT' else 0))
            payload={'network':network,'mode':mode,'amount_usdd':decstr(principal),'collateral_trx':decstr(equity) if mode=='VAULT' else '0',
                'loops':0,'borrow_bps':0,'ilk':ilk,'per_step_fee_cap_trx':decstr(per),'total_fee_cap_trx':decstr(total)}
            trial=deepcopy(state)
            try:
                svc.create(context,trial,payload,inputs=inputs,review_first=False)
                wf=trial['usdd_execution']
                eligible.append((equity,weight,payload,trial))
                core['candidates'].append({'weight_bps':weight,'eligible':True,'outcome':wf['outcome']})
            except MachineError as exc:
                core['exclusions'].append({'weight_bps':weight,'reason':str(exc)})
        if len(eligible)<2:
            reasons=list(dict.fromkeys(x['reason'] for x in core['exclusions']))
            out['reason']='Two distinct allocations do not pass your current conditions and complete cost budget. '+ ' '.join(reasons[:4])
            return out,core
        # Conservative = least deployed principal; growth = greatest net income.
        low=min(eligible,key=lambda x:x[0]);high=max(eligible,key=lambda x:D(x[3]['usdd_execution']['outcome']['net_income_usdd']))
        if high[1]==low[1]:high=max((x for x in eligible if x[1]!=low[1]),key=lambda x:D(x[3]['usdd_execution']['outcome']['net_income_usdd']))
        for name,chosen in (('CONSERVATIVE',low),('GROWTH',high)):
            equity,weight,payload,trial=chosen;wf=trial['usdd_execution']
            svc.next(context,trial)  # Verify the actual first executable call on each selected allocation.
            outcome=wf['outcome'];cost=D(outcome['total_costs_usdd'])/base_price
            cash=capital-equity-cost
            supplied=D(outcome['supplied_usdd']);days=D(t['horizon_seconds'])/86400
            reward=supplied*D(market['reward_apr'])*days/365
            base_income=D(outcome['gross_income_usdd'])-reward
            commitment=digest({'payload':payload,'policy':record['policy_hash'],'snapshot':out['snapshot_root'],'outcome':outcome})
            core['payloads'][name]={'payload':payload,'hash':commitment}
            plan={'id':name,'hash':commitment,'title':'More cash available' if name=='CONSERVATIVE' else 'Higher projected net income',
                'summary':'Same capital and horizon. Full transaction caps are reserved; forecast rewards are variable and not guaranteed.',
                'allocations':[{'product':'USDD Vault '+ilk+' → JustLend USDD' if mode=='VAULT' else 'justlend.v1.jUSDD','protocol':'USDD + JustLend' if mode=='VAULT' else 'JustLend',
                    'amount':capamount(equity),'share_bps':weight,'kind':'VAULT' if mode=='VAULT' else 'SUPPLY',
                    'exit_description':'Redeem shares to USDD after checking liquidity; repay Vault debt before releasing collateral.' if mode=='VAULT' else 'Redeem shares to USDD after a fresh liquidity, fee and wallet approval check.',
                    'participation_terms':'Compatible mainnet USDD, confirmed wallet holdings and fee reserves. Reward eligibility and duration may change.',
                    'base_yield':tokenamount(base_income),'incentive_rewards':tokenamount(reward),
                    'costs':[{'label':'Complete entry / claim / recovery fee reserve','amount':capamount(cost)},
                             {'label':'Projected debt interest','amount':tokenamount(outcome['debt_interest_usdd'])}]},
                    {'product':'Unallocated cash budget','protocol':'Wallet','amount':capamount(cash),'share_bps':max(0,int(cash/capital*10000)),
                     'kind':'CASH','exit_description':'Available without a protocol redemption. Fee reserves are excluded.',
                     'participation_terms':'Within the confirmed allocation budget; separate TRX resources must be present in the wallet.',
                     'base_yield':None,'incentive_rewards':None,'costs':[]}],
                'expected_net_return':capamount(D(outcome['net_income_usdd'])/base_price),'estimated_fees':capamount(cost),
                'immediate_cash':capamount(cash),'recoverable_cash':[{'days':0,'amount':capamount(cash),'evidence':'Confirmed budget less deployed equity and full fee reserve.'}],
                'risks':['Variable supply rates, stablecoin depeg, smart-contract and exit-liquidity risk.',
                    'Incentives use the current daily budget, without guaranteed duration or automatic compounding.',
                    'TRX collateral adds price and liquidation risk.' if mode=='VAULT' else 'No debt or recursive borrowing is used.',
                    'Only the first dependent transaction has been simulated now. Every later step is rechecked before approval.'],
                'eligible':True,'violations':[]}
            out['plans'].append(plan)
            core.setdefault('first_step_simulations',{})[name]=deepcopy(wf['steps'][0]['live'])
        out['status']='READY'
        w=state['workspace'];w['balances']=[{'value':decstr(D(account['trx_balance'])/10**6),'symbol':'TRX','decimals':6},tokenamount(D(account['token_balance'])/10**18)]
        w['snapshots']=[*w['snapshots'],{'id':out['id'],'network':network,'observed_at':at,'expires_at':out['expires_at'],
            'source':'Current USDD/JustLend chain observations and official reward API','source_url':market.get('api_url'),
            'block':str(account.get('block_to',{}).get('number')),'root':out['snapshot_root'],'status':'VALID'}][-20:]
        if datetime.fromisoformat(bridge.clock())>=datetime.fromisoformat(out['expires_at']):raise MachineError('Comparison inputs expired while checking the two plans. Refresh.')
    except (MachineError,OSError,ValueError,KeyError,TypeError) as exc:
        out.update(status='INFEASIBLE',plans=[],reason=str(exc));core['payloads']={}
    return out,core
