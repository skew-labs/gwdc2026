"""Turn a fresh Watch alternative into one explicitly reviewed protocol call."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal

from economic_machine.tron_actions import compile_action
from economic_machine.tron_sources import address_base58
from economic_machine.tron_crypto import keccak256
from economic_machine.values import MachineError, digest, decstr
from .native_execution import PRODUCT, MARKET, CODE_SHA256, asset, money, atoms
from .portfolio_review import confirmed_policy, active_mandate, withdrawal_policy


def option_hash(option):
    return digest({k:v for k,v in option.items() if k != 'hash'})


def review_adjustment(bridge, context, state, payload):
    w=state['workspace']; previous=w.get('portfolio_review')
    if not previous or payload.get('review_id')!=previous['id']:
        raise MachineError('Portfolio review changed. Check current holdings before choosing an adjustment.')
    selected=next((a for a in previous['alternatives'] if option_hash(a)==payload.get('option_hash')),None)
    if not selected or not selected['eligible']:
        raise MachineError('Choose a permitted adjustment from the current portfolio review.')
    if context.network!='tron-nile':
        raise MachineError('Use the USDD recovery workflow for a mainnet position.')
    if datetime.fromisoformat(bridge.clock())>=datetime.fromisoformat(previous['expires_at']):
        raise MachineError('Portfolio review expired. Check now before selecting an adjustment.')
    p=confirmed_policy(state); m=w.get('mandate')
    if not p or (selected['action']=='REDEEM' and not withdrawal_policy(state)) or (selected['action']!='REDEEM' and p.get('status')!='CONFIRMED'):
        raise MachineError('Confirm your investment conditions before reviewing a transaction.')
    if selected['action']!='REDEEM' and (not m or m['status']!='CONFIRMED' or m['hash']!=p['policy_hash']):
        raise MachineError('Confirm your draft before increasing an investment. Existing holdings can still be withdrawn under your active conditions.')
    r=bridge.reviews.refresh(context,state,notify=False)
    if r['status'] in ('DATA_UNAVAILABLE','POLICY_INACTIVE','PENDING_EXECUTION'):
        raise MachineError(r['reason'])
    # A new source read may change price, amount, liquidity or fee eligibility.
    candidate=next((a for a in r['alternatives'] if a['action']==selected['action'] and a['position']==selected['position'] and a['eligible']),None)
    if candidate is None:
        raise MachineError('The selected adjustment no longer satisfies current conditions. Refresh and review the new options.')
    create_adjustment(bridge,context,state,p,r,candidate)


def create_adjustment(bridge,context,state,policy,review,candidate):
    n=bridge.native;w=state['workspace'];t=policy['mandate']['terms'];at=bridge.clock()
    action=candidate['action'];redeem=action=='REDEEM'
    if action not in ('SUPPLY','REDEEM') or action not in t['allowed_actions']:
        raise MachineError('Adjustment action is not authorized by the confirmed conditions.')
    expiry=min(review['expires_at'], t['expires_at'],(datetime.fromisoformat(at)+timedelta(minutes=3)).isoformat())
    account,raw=n.account(context.scope,review['snapshot_hash'],expiry)
    if int(raw['debt']):raise MachineError('Debt must receive a separate repayment review before jTRX adjustments.')
    delta=atoms(candidate['delta']);shares=int(raw['shares']) if candidate['position']=='0' else (delta*10**18+int(raw['exchange_rate'])-1)//int(raw['exchange_rate'])
    if redeem and (shares<=0 or shares>int(raw['shares'])):raise MachineError('Current shares cannot cover this redemption.')
    live=n.simulate_redeem(context.wallet,shares) if redeem else n.simulate(context.wallet,delta)
    cap=atoms(policy['review_inputs']['constraints']['max_fee']['value'])
    upper=live['energy_burn']+live['bandwidth_bound']
    if cap<upper or cap<=live['bandwidth_bound']:raise MachineError('The current fee bound exceeds your confirmed per-transaction limit.')
    # Reserve the entire authorization, not just the current simulated burn.
    paid=atoms(review['paid_fees']);exit_reserve=atoms(candidate['exit_reserve'])
    if paid+cap+exit_reserve>atoms(t['limits']['fee_amount']['amount']):
        raise MachineError('Paid fees, this transaction cap and the remaining exit reserve exceed your total fee permission.')
    if delta>atoms(t['limits']['single_amount']['amount']):raise MachineError('Adjustment exceeds the confirmed single-transaction amount.')
    if redeem and live['received']>atoms(t['limits']['single_amount']['amount']):raise MachineError('Fresh redemption proceeds exceed the confirmed single-transaction amount.')
    operation='JUSTLEND_REDEEM_SHARES' if redeem else 'JUSTLEND_SUPPLY_TRX'
    product=state['observation']['snapshot']['products'][PRODUCT]
    binding={'schema_version':'tron-action-binding-1','operation':operation,'network':context.network,'target_address':MARKET,
        'capability_hash':product['capability_hash'],'abi_evidence_hash':live['abi_hash'],'contract_code_hash':CODE_SHA256,
        'status':'VERIFIED','source_url':'https://docs.justlend.org/developers/common_pitfalls/'}
    compiled=compile_action(binding,[str(shares)] if redeem else [],call_value_sun='0' if redeem else str(delta),fee_limit_sun=str(cap-live['bandwidth_bound']),expires_at=expiry)
    if compiled.get('blockers'):raise MachineError('Adjustment call binding failed: '+','.join(compiled['blockers']))
    minimum=(live['received'] if redeem else live['shares'])*99//100
    if minimum<=0:raise MachineError('Adjustment output is below one token unit.')
    conditions=[{'kind':'MIN_OUTPUT','asset':'TRX' if redeem else 'jTRX','amount_base_units':str(minimum)}]
    if redeem:
        conditions.extend([{'kind':'POSITION_SHARES_AT_LEAST','amount_base_units':str(shares)},
                           {'kind':'MARKET_LIQUIDITY_AT_LEAST','amount_base_units':str(live['received'])}])
    else:conditions.append({'kind':'INPUT_DECREASE','asset':'TRX','amount_base_units':str(delta)})
    step={'step_id':'step-001','kind':'ONCHAIN_CALL','operation':operation,'depends_on':[],'product_id':PRODUCT,
        'inputs':[{**(asset(shares,True) if redeem else asset(delta)),'availability':'OBSERVED_ACCOUNT_STATE'}],
        'expected_outputs':[{**(asset(minimum) if redeem else asset(minimum,True)),'availability':'UNCONFIRMED'}],
        'action':compiled,'postconditions':conditions,'status':'PLANNED','blockers':[],'output_availability':'UNCONFIRMED'}
    step['step_hash']=digest({'domain':'economic-execution-step-1','step':step})
    plan_hash=digest({'review':review['id'],'candidate':candidate,'policy':policy['policy_hash']})
    graph={'schema_version':'economic-execution-graph-1','status':'READY_FOR_SIMULATION','execution_semantics':'ORDERED_MULTI_TRANSACTION_NON_ATOMIC',
        'scope':context.scope,'plan_hash':plan_hash,'snapshot_hash':review['snapshot_hash'],'created_at':account['observed_at'],'valid_until':expiry,
        'fee_budget_sun':str(cap),'maximum_fee_limits_sun':str(cap-live['bandwidth_bound']),'steps':[step],'blockers':[],
        'signature_status':'NOT_REQUESTED','chain_status':'NOT_SUBMITTED','execution_authority':'NONE'}
    graph['graph_hash']=digest({'domain':graph['schema_version'],'graph':graph})
    native={'graph':graph,'operation':action,'policy_hash':policy['policy_hash'],'amount':delta,'shares':shares if redeem else None,
        'policy_projection':withdrawal_policy(state) if redeem else active_mandate(state),
        'authority_basis':'EXPLICIT_EXACT_WITHDRAWAL_APPROVAL' if redeem else 'CONFIRMED_INVESTMENT_POLICY',
        'minimum_received':minimum if redeem else 0,'total_fee_cap':cap,'bandwidth_bound':live['bandwidth_bound'],
        'cash_floor':atoms(review['cash_floor']),'abi_hash':live['abi_hash'],'binding':binding,'submission':None,
        'account_evidence':raw,'review_simulation':live['evidence'],'estimated_fee':money(upper),'expected_fee_sun':live['expected_burn'],
        'adjustment_review':deepcopy(review),'adjustment_candidate':deepcopy(candidate),
        'economics':None if redeem else {'projected_income':candidate['benefit_over_hold'],
            'other_costs':decstr(max(Decimal(0),Decimal(candidate['additional_cost'])-Decimal(candidate['change_cost']))),'plan_hash':plan_hash}}
    if not redeem:
        native['performance_forecast']={'plan_hash':plan_hash,'horizon_seconds':review['remaining_seconds'],
            'net_total_sun':str(int((Decimal(candidate['benefit_over_hold'])-Decimal(candidate['additional_cost']))*10**6)),
            'gross_total_sun':str(int(Decimal(candidate['benefit_over_hold'])*10**6)),
            'entry_estimate_sun':str(live['expected_burn']),'basis':'Incremental forecast from the approved portfolio adjustment.'}
    n.preflight_manifest(native,account,live)
    state['native_execution']=native
    w['graph']={'id':'native-'+graph['graph_hash'][:24],'hash':graph['graph_hash'],'plan_id':'adjustment-'+action.lower(),'plan_hash':plan_hash,
        'mandate_hash':policy['policy_hash'],'network':'nile','account':address_base58(context.wallet),'expires_at':expiry,
        'enforcement_scope':'DIRECT_PROTOCOL','review_kind':'ADJUSTMENT','review_id':review['id'],'snapshot_root':review['snapshot_hash'],
        'estimated_fee':money(upper),'expected_fee':money(live['expected_burn']),
        'minimum_received':money(minimum) if redeem else None,
        'disclosure':'Withdrawals can cost more than their proceeds. The exact share amount and fee cap require your approval. Output floors are checked after settlement; direct protocol calls cannot reverse a completed transaction.',
        'steps':[{'id':step['step_id'],'title':'Withdraw JustLend to TRX' if redeem else 'Increase JustLend supply',
            'action':'redeem(uint256)' if redeem else 'mint()','amount':money(live['received']) if redeem else money(delta),
            'call_data':keccak256(compiled['function_selector'].encode()).hex()[:8]+compiled['parameter_hex'],
            'call_value_sun':compiled['call_value_sun'],
            'input_amount':{'value':decstr(Decimal(shares)/10**8),'symbol':'jTRX','decimals':8} if redeem else money(delta),
            'recipient':address_base58(MARKET),'depends_on':[],'status':'READY','txid':None,'fee_cap':money(cap),'allowance_remaining':None,'error':None}]}
    w['approval']=None;w['execution']=None
