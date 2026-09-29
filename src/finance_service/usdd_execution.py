"""Durable wallet-signed USDD workflows, bounded by confirmed conditions.

The workflow is not an atomic transaction. The current step is the only action
that can be approved. Receipts and post-state unlock the next step; a timeout or
HTTP success does not. No signing keys are accepted by this service.
"""
from copy import deepcopy
from datetime import datetime,timedelta
from decimal import Decimal
from economic_machine.usdd_workflow import CONFIG, VERSION, units, build_steps, compile_call, advance, loop_cashflow
from economic_machine.values import MachineError,digest,decstr
from economic_machine.tron_sources import address_base58,address_hex,parse_raw
from economic_machine.approval import prepare_approval_card,confirm_approval,build_wallet_signature_request
from economic_machine.preflight import compile_preflight
from economic_machine.signed_tx_validation import validate_signed_transaction
from economic_machine.tron_execution import prepare_submission,record_broadcast_attempt
from economic_machine.tron_consumption_read import read_tron_transaction
from .native_execution import unsigned_transaction,normalize_wallet_signature,money,ACK
from .portfolio_review import confirmed_policy
from .usdd_chain import UsddChain,ANCHORS
from .usdd_market import read_market


def token_money(n):return {'value':decstr(Decimal(n)/10**18),'symbol':'USDD','decimals':18}


def unchanged(a,b):
    # Price/rate/accrual metadata may advance, ownership and spendable state may
    # not silently change underneath an exact approval.
    keys=('owner','network','token','trx_balance','token_balance','shares','market_debt',
        'market_allowance','proxy_allowance','membership','proxy','proxy_owner','cdp_id',
        'cdp_owner','ilk','collateral_wad','vault_debt','entered_markets')
    return all(a.get(k)==b.get(k) for k in keys)


class UsddExecution:
    def __init__(self,bridge):self.bridge=bridge;self.clock=bridge.clock

    def chain(self,context):
        if not self.bridge.observations:raise MachineError('Live USDD execution reads unavailable.')
        return UsddChain(context.network,context.wallet,self.bridge.observations.request,self.clock)

    def policy(self,context,state,workflow=None):
        record=confirmed_policy(state)
        if not record or state['workspace']['mandate']['status']!='CONFIRMED':raise MachineError('Confirm the complete conditions before reviewing USDD execution.')
        t=record['mandate']['terms'];now=datetime.fromisoformat(self.clock())
        if not datetime.fromisoformat(t['effective_at'])<=now<datetime.fromisoformat(t['expires_at']):raise MachineError('Confirmed conditions have expired.')
        if workflow and workflow['policy_hash']!=record['policy_hash']:raise MachineError('Conditions changed; the existing workflow needs recovery review.')
        if record['mandate']['scope']!=context.scope:raise MachineError('Confirmed conditions belong to a different wallet or network.')
        return record,t

    def create(self,context,state,payload,*,inputs=None,review_first=True):
        prior=state.get('usdd_execution')
        if prior and prior['status'] not in ('CLOSED','CANCELLED'):
            raise MachineError('Resume or recover the existing USDD workflow before creating another.')
        record,t=self.policy(context,state)
        mode=payload.get('mode');amount=units(payload.get('amount_usdd'))
        if mode not in ('OWNED','VAULT'):raise MachineError('Choose owned USDD or TRX Vault funding.')
        loops=payload.get('loops',0);bps=payload.get('borrow_bps',0)
        collateral=units(payload.get('collateral_trx'),6) if mode=='VAULT' else 0
        per=units(payload.get('per_step_fee_cap_trx'),6);total=units(payload.get('total_fee_cap_trx'),6)
        if total<per or per>15_000_000_000:raise MachineError('Invalid workflow fee caps.')
        ilk=payload.get('ilk','TRX-C')
        if ilk not in CONFIG[context.network]['joins']:raise MachineError('Unsupported collateral type.')
        needed={'SUPPLY','REDEEM','CLAIM'}|({'OPEN_VAULT','MINT_USDD','REPAY','WITHDRAW_COLLATERAL'} if mode=='VAULT' else set())|({'BORROW','REPAY'} if loops else set())
        if not needed.issubset(t['allowed_actions']):raise MachineError('Confirmed conditions do not permit all requested workflow actions.')
        if (mode=='VAULT' or loops) and t['borrowing']['consent'] is not True:raise MachineError('Explicit borrowing consent is required.')
        if context.network!='tron-mainnet':
            # Nile JustLend USDD is a different deployment from Vault USDD;
            # current incentives are mainnet-only and cannot justify testnet debt.
            report=self.bridge.usdd.refresh(context,state)
            if mode=='VAULT' and report['facts']['token_match'] is False:
                raise MachineError('Nile Vault USDD and Nile JustLend USDD are different tokens. This route cannot be executed on Nile.')
            raise MachineError('No verified Nile USDD reward and cost forecast is available for this route.')
        chain=self.chain(context);observed=deepcopy(inputs['account']) if inputs else chain.snapshot()
        report=deepcopy(inputs['report']) if inputs else self.bridge.usdd.refresh(context,state)
        facts=report['facts']
        if set(report['blockers']) & ({'LIVE_INPUT_UNAVAILABLE','DESTINATION_SUPPLY_PAUSED'} | ({'DESTINATION_BORROW_PAUSED'} if loops else set()) | ({'VAULT_SHUTDOWN'} if mode=='VAULT' else set())) or not facts['token_match']:
            raise MachineError('Complete current Vault and market observations are required.')
        market,market_evidence=(deepcopy(inputs['market']),deepcopy(inputs['market_evidence'])) if inputs else read_market(self.bridge.observations.request,self.clock)
        if not 0<=datetime.fromisoformat(self.clock()).timestamp()-datetime.fromisoformat(market['market_observed_at']).timestamp()<=300:
            raise MachineError('USDD market valuation is older than five minutes; refresh before execution.')
        price=Decimal(market['trx_usd'])/Decimal(market['usdd_usd'])
        fee_condition=(record.get('review_inputs') or {}).get('constraints',{}).get('max_fee')
        if not fee_condition or fee_condition['symbol'] not in ('TRX','USDD'):
            raise MachineError('A confirmed per-transaction fee limit in TRX or USDD is required.')
        if Decimal(per)/10**6*price>Decimal(fee_condition['value'])*(price if fee_condition['symbol']=='TRX' else Decimal(1)):
            raise MachineError('Requested per-transaction fee cap exceeds the confirmed fee condition.')
        equity=Decimal(amount)/10**18 if mode=='OWNED' else Decimal(collateral)/10**6*price
        base=t['base_asset']
        if base not in ('USDD','TRX') or len(t['capital'])!=1 or t['capital'][0]['asset']!=base:
            raise MachineError('This route needs single-asset USDD or TRX confirmed capital.')
        if mode=='OWNED' and base!='USDD' or mode=='VAULT' and base!='TRX':raise MachineError('Funding asset differs from the confirmed capital.')
        base_price=price if base=='TRX' else Decimal(1)
        capital=Decimal(t['capital'][0]['amount'])*base_price
        from economic_machine.mandate import reserve_amount
        reserve=reserve_amount(t['immediate_cash'],t['capital'][0]['amount'])*base_price
        if equity+Decimal(total)/10**6*price>capital-reserve:raise MachineError('Principal and the complete fee reserve would consume the confirmed cash reserve.')
        assumptions=(record.get('review_inputs') or {}).get('assumptions')
        if not assumptions:raise MachineError('Confirmed daily-loss and stress assumptions are required.')
        for field,assumption in (('daily_loss','daily_loss_bps'),('stress_loss','stress_loss_bps')):
            bps_loss=assumptions.get(assumption)
            if type(bps_loss) is not int or not 0<=bps_loss<=10000:raise MachineError('Bounded confirmed loss assumptions are required.')
            if equity*Decimal(bps_loss)/10000>Decimal(t['limits'][field]['amount'])*base_price:raise MachineError('Requested allocation exceeds the confirmed '+field.replace('_',' ')+' limit.')
        loan=next(r for r in facts['collaterals'] if r['ilk']==ilk)
        stability=loan['stability_apy'] if mode=='VAULT' else '0'
        steps=build_steps(amount=amount,collateral_sun=collateral,ilk=ilk,loops=loops,borrow_bps=bps,has_proxy=bool(observed['proxy']))
        outcome=loop_cashflow(str(Decimal(amount)/10**18),facts['supply_apy'],market['reward_apr'],facts['borrow_apy'],stability,
            days=str(Decimal(t['horizon_seconds'])/86400),loops=loops,borrow_bps=bps,
            costs=[str(Decimal(total)/10**6*price)],equity=str(equity))
        # Keep enough bounded transaction budget for approval, repayment and
        # collateral release even when future amounts change through interest.
        exit_steps=3+(3 if mode=='VAULT' else 0)+(3*(loops+1) if loops else 0)
        if total<per*(len(steps)+exit_steps):raise MachineError('The total fee budget must cover every entry and reserved exit step at its stated cap.')
        if Decimal(outcome['net_income_usdd'])<=0:raise MachineError('Keep cash: forecast income does not cover borrowing and the complete reviewed fee budget.')
        if loops and Decimal(outcome['marginal_loop_return'])<=0:raise MachineError('Borrow and resupply reduces returns at current rates, even before additional fees.')
        supplied=Decimal(outcome['supplied_usdd']);debt=Decimal(outcome['recursive_debt_usdd'])+(Decimal(amount)/10**18 if mode=='VAULT' else 0)
        ratio=max(Decimal(t['borrowing']['min_collateral_ratio_bps'])/10000,Decimal(1)/Decimal(facts['collateral_factor']))+Decimal(t['borrowing']['liquidation_buffer_bps'])/10000
        if loops and Decimal(outcome['recursive_debt_usdd'])*ratio>supplied:raise MachineError('Recursive debt exceeds the confirmed collateral buffer.')
        if debt+Decimal(outcome['debt_interest_usdd'])>Decimal(t['borrowing']['max_debt']['amount'])*base_price:raise MachineError('Total Vault and lending debt exceeds the confirmed debt limit.')
        if supplied>capital*Decimal(t['protocol_caps_bps'].get('justlend',0))/10000:raise MachineError('Gross lending exposure exceeds the confirmed protocol cap.')
        if supplied>capital*Decimal(t['price_exposure_caps_bps'].get('USDD',0))/10000:raise MachineError('Gross USDD exposure exceeds the confirmed price exposure cap.')
        if equity>Decimal(t['limits']['single_amount']['amount'])*base_price or equity>Decimal(t['limits']['cumulative_amount']['amount'])*base_price:
            raise MachineError('Workflow principal exceeds the confirmed spending limit.')
        spent=sum(Decimal(r.get('principal_base','0')) for r in state.get('usdd_spend_ledger',{}).values() if r.get('policy_hash')==record['policy_hash'])
        if spent+equity/base_price>Decimal(t['limits']['cumulative_amount']['amount']):raise MachineError('Cumulative USDD workflow budget exhausted.')
        if Decimal(total)/10**6*price>Decimal(t['limits']['fee_amount']['amount'])*base_price:raise MachineError('Workflow fees exceed the confirmed fee limit.')
        if int(observed['trx_balance'])<collateral+total:raise MachineError('Wallet lacks TRX collateral and the complete fee reserve.')
        if base=='TRX' and Decimal(int(observed['trx_balance'])-collateral-total)/10**6*price<reserve:raise MachineError('Collateral and fees would consume the confirmed TRX cash floor.')
        if t['withdrawals']:raise MachineError('Scheduled withdrawals need a dated repayment plan; this bounded USDD workflow does not support them yet.')
        if mode=='OWNED' and int(observed['token_balance'])<amount:raise MachineError('Wallet does not hold the reviewed USDD principal.')
        if mode=='VAULT' and int(observed.get('cdp_count','0')):
            raise MachineError('An existing Vault needs consolidated debt review before creating another. This route currently opens an isolated first Vault.')
        if int(observed['market_debt']) or int(observed['shares']):raise MachineError('This version requires a separate empty USDD lending position; existing positions must first receive a recovery review.')
        if mode=='VAULT':
            ratio=max(Decimal(loan['liquidation_ratio']),Decimal(t['borrowing']['min_collateral_ratio_bps'])/10000)+Decimal(t['borrowing']['liquidation_buffer_bps'])/10000
            capacity=Decimal(collateral)/10**6*Decimal(loan['debt_capacity_per_trx'])*Decimal(loan['liquidation_ratio'])/ratio
            if Decimal(amount)/10**18<Decimal(loan['minimum_debt_usdd']) or Decimal(amount)/10**18>min(capacity,Decimal(loan['debt_ceiling_remaining_usdd'])):
                raise MachineError('Vault debt violates its minimum, remaining ceiling or confirmed collateral buffer.')
        spec={'schema_version':VERSION,'network':context.network,'owner':context.wallet,'policy_hash':record['policy_hash'],
            'mode':mode,'amount':str(amount),'collateral_sun':str(collateral),'ilk':ilk,'loops':loops,'borrow_bps':bps,
            'per_step_fee_cap':str(per),'total_fee_cap':str(total),'exit_fee_reserve':str(exit_steps*per),
            'steps':deepcopy(steps),'created_at':self.clock(),'horizon_seconds':t['horizon_seconds'],
            'cash_floor_trx':str(int(reserve/price*10**6)) if base=='TRX' else '0',
            'cash_floor_usdd':str(int(reserve*10**18)) if base=='USDD' else '0'}
        commitment=digest(spec)
        wf={**spec,'id':'usdd-'+commitment[:24],'hash':commitment,'spec':spec,'cursor':0,'status':'AWAITING_NEXT_REVIEW',
            'confirmed_txids':[],'observed':observed,'cdp_id':None,'spent_fees':'0','outcome':outcome,
            'market_evidence':market_evidence,'market_quote':market,'rate_review':report,'principal_base':decstr(equity/base_price)}
        state['usdd_execution']=wf
        if review_first:self.next(context,state)

    def next(self,context,state):
        wf=state.get('usdd_execution')
        if not wf or wf['status'] not in ('AWAITING_NEXT_REVIEW','AWAITING_SIGNATURE'):
            raise MachineError('Reconcile the current USDD transaction before reviewing another step.')
        self.policy(context,state,wf)
        step=wf['steps'][wf['cursor']]
        if step.get('submission') or step.get('transaction'):raise MachineError('Resolve the prepared transaction before replacing its review.')
        chain=self.chain(context);observed=chain.snapshot(wf)
        if wf['cursor'] and not unchanged(wf['observed'],observed):raise MachineError('USDD account changed after the previous receipt. Recovery review is required.')
        per=int(wf['per_step_fee_cap']);remaining=int(wf['total_fee_cap'])-int(wf['spent_fees'])
        if remaining<per+(0 if wf.get('phase')=='RECOVERY' else int(wf['exit_fee_reserve'])) or int(observed['trx_balance'])<remaining:
            raise MachineError('Remaining entry and exit fee reserve is insufficient.')
        if step['operation']=='SUPPLY':step['minimum_shares']=str(int(step['amount'])*10**18//int(observed['exchange_rate'])*99//100)
        if step['operation']=='BORROW':
            fresh=self.bridge.usdd.refresh(context,state)['facts']
            if fresh['borrow_apy'] is None or fresh['market_cash_usdd'] is None or Decimal(fresh['market_cash_usdd'])<Decimal(step['amount'])/10**18:
                raise MachineError('Live borrowing rate and liquidity required.')
            market,_=read_market(self.bridge.observations.request,self.clock)
            if Decimal(fresh['supply_apy'])+Decimal(market['reward_apr'])<=Decimal(fresh['borrow_apy']):raise MachineError('Current borrow-resupply spread is negative; stop increasing debt.')
        self.live_entry_check(context,state,wf,step,observed)
        call=compile_call(context.network,context.wallet,step,observed)
        self.reserve(wf,step,observed,call)
        live=chain.simulate(call,per)
        self.check_simulation(step,observed,live)
        expiry=(datetime.fromisoformat(self.clock())+timedelta(minutes=3)).isoformat()
        record,t=self.policy(context,state,wf);expiry=min(expiry,t['expires_at'])
        step.update(status='READY',before=observed,call=call,live=live,expires_at=expiry,graph_id=wf['id']+'-'+str(wf['cursor']))
        # Later approvals bind to the full immutable workflow and actual prior receipts.
        action={'schema_version':'tron-action-1','operation':step['operation'],'transport':'TRIGGER_SMART_CONTRACT',
            'target_address':call['target'],'function_selector':call['signature'],'parameter_hex':call['data'][8:],
            'call_value_sun':call['value_sun'],'fee_limit_sun':str(per-live['costs']['bandwidth_bytes_bound']*live['costs']['bandwidth_price_sun']),
            'result_semantics':'BOOL_TRUE' if step['operation'].startswith('APPROVE') else 'UINT_ZERO' if step['operation'] in ('SUPPLY','BORROW','REPAY','REPAY_ALL','REDEEM','REDEEM_SHARES') else 'RECEIPT_SUCCESS','expires_at':expiry,'status':'READY_FOR_SIMULATION',
            'binding':live['codes'],'workflow_hash':wf['hash'],'confirmed_txids':wf['confirmed_txids']}
        action['action_hash']=digest(action)
        inputs=[]
        if int(call['value_sun']):inputs.append({'asset':'TRX','token_address':None,'decimals':6,'amount_base_units':call['value_sun']})
        if step['operation'] in ('SUPPLY','REPAY','REPAY_ALL','VAULT_REPAY','VAULT_REPAY_ALL'):
            inputs.append({'asset':'USDD','token_address':observed['token'],'decimals':18,'amount_base_units':step['amount']})
        if step['operation']=='REDEEM_SHARES':inputs.append({'asset':'jUSDD','token_address':address_hex(CONFIG[context.network]['market']),'decimals':8,'amount_base_units':step['amount']})
        conditions=[{'kind':'BALANCE_AT_LEAST',**i} for i in inputs]
        conditions.append({'kind':'BALANCE_AT_LEAST','asset':'TRX','token_address':None,'decimals':6,'amount_base_units':str(remaining+int(call['value_sun']))})
        core_step={'step_id':step['id'],'kind':'ONCHAIN_CALL','operation':step['operation'],'product_id':'usdd.workflow',
            'depends_on':[],'inputs':inputs,'expected_outputs':[],'postconditions':conditions,'action':action,'blockers':[]}
        core_step['step_hash']=digest({'domain':'economic-execution-step-1','step':core_step})
        core={'schema_version':'economic-execution-graph-1','scope':context.scope,'plan_hash':wf['hash'],
            'snapshot_hash':digest(observed),'created_at':observed['observed_at'],'valid_until':expiry,
            'steps':[core_step],'blockers':[],'fee_budget_sun':str(per),'maximum_fee_limits_sun':action['fee_limit_sun'],
            'execution_semantics':'ORDERED_MULTI_TRANSACTION_NON_ATOMIC','execution_authority':'NONE',
            'signature_status':'NOT_REQUESTED','chain_status':'NOT_SUBMITTED','workflow_hash':wf['hash']}
        core['graph_hash']=digest({'domain':core['schema_version'],'graph':core});step['core_graph']=core
        wf['status']='AWAITING_SIGNATURE'
        w=state['workspace'];w['graph']={'id':wf['id']+'-'+str(wf['cursor']),'hash':core['graph_hash'],
            'review_kind':'USDD_WORKFLOW',
            'plan_id':wf['id'],'plan_hash':wf['hash'],'mandate_hash':wf['policy_hash'],'network':w['network'],
            'account':address_base58(context.wallet),'expires_at':expiry,'enforcement_scope':'DIRECT_PROTOCOL',
            'estimated_fee':money(live['costs']['full_total_burn_bound_sun']),'expected_fee':money(live['costs']['expected_total_burn_sun']),
            'disclosure':'Step '+str(wf['cursor']+1)+' of '+str(len(wf['steps']))+'. Each transaction is separate. A later failure does not reverse earlier transactions. The next step requires a confirmed receipt and new approval.',
            'steps':[{'id':step['id'],'title':step['operation'].replace('_',' ').title(),'action':call.get('inner_signature') or call['signature'],
                'call_data':call['data'],'call_value_sun':call['value_sun'],
                'amount':money(int(call['value_sun'])) if int(call['value_sun']) else money(int(step['amount'])//10**12) if step['operation']=='VAULT_FREE' else {'value':decstr(Decimal(step['amount'])/10**8),'symbol':'jUSDD','decimals':8} if step['operation']=='REDEEM_SHARES' else token_money(int(step['amount'])),
                'recipient':address_base58(call['target']),'depends_on':[],'status':'READY','txid':None,'fee_cap':money(per),
                'allowance_remaining':None,'error':None}]}
        w['approval']=None;w['execution']=None
        self.project(state)

    def recover(self,context,state,payload):
        wf=state.get('usdd_execution')
        if not wf or wf['owner']!=context.wallet or wf['network']!=context.network:raise MachineError('No matching USDD workflow.')
        if wf['status'] in ('SUBMISSION_UNKNOWN','SUBMITTED','DISPUTED'):raise MachineError('Resolve the current transaction before recovery.')
        if wf['cursor']<len(wf['steps']) and wf['steps'][wf['cursor']].get('transaction') and not wf['steps'][wf['cursor']].get('submission'):
            raise MachineError('A prepared wallet transaction must expire and be reconciled before recovery.')
        record,terms=self.policy(context,state)
        chain=self.chain(context);observed=chain.snapshot(wf)
        required=({'CLAIM'} if payload.get('claim_key') else {'REDEEM'})|({'REPAY'} if int(observed['market_debt']) or int(observed['vault_debt']) else set())|({'WITHDRAW_COLLATERAL'} if int(observed['collateral_wad']) else set())
        if not required.issubset(terms['allowed_actions']):raise MachineError('Confirm permission to redeem, repay and release the existing collateral before recovery.')
        market,_=read_market(self.bridge.observations.request,self.clock)
        price=Decimal(market['trx_usd'])/Decimal(market['usdd_usd'])
        base_price=price if terms['base_asset']=='TRX' else Decimal(1)
        if terms['base_asset'] not in ('TRX','USDD') or len(terms['capital'])!=1 or terms['capital'][0]['asset']!=terms['base_asset']:
            raise MachineError('Recovery needs single-asset USDD or TRX confirmed conditions.')
        fee_condition=(record.get('review_inputs') or {}).get('constraints',{}).get('max_fee')
        if not fee_condition or fee_condition['symbol'] not in ('TRX','USDD') or Decimal(wf['per_step_fee_cap'])/10**6*price>Decimal(fee_condition['value'])*(price if fee_condition['symbol']=='TRX' else Decimal(1)):
            raise MachineError('Existing per-transaction fee cap exceeds the confirmed recovery condition.')
        if Decimal(wf['total_fee_cap'])/10**6*price>Decimal(terms['limits']['fee_amount']['amount'])*base_price:
            raise MachineError('Existing workflow fee reserve exceeds the newly confirmed fee limit.')
        from economic_machine.mandate import reserve_amount
        floor=reserve_amount(terms['immediate_cash'],terms['capital'][0]['amount'])
        field='cash_floor_trx' if terms['base_asset']=='TRX' else 'cash_floor_usdd'
        wf.update(cash_floor_trx='0',cash_floor_usdd='0')
        wf[field]=str(int(floor*10**(6 if terms['base_asset']=='TRX' else 18)))
        steps=[];offset=len(wf['steps'])
        def add(op,amount=0,**extra):
            steps.append({'id':'usdd-step-'+str(offset+len(steps)+1),'operation':op,'amount':str(amount),
                'status':'WAITING','txid':None,'receipt':None,**extra})
        debt=int(observed.get('accrued_market_debt',observed['market_debt']));vault_debt=int(observed.get('accrued_vault_debt',observed['vault_debt']));balance=max(0,int(observed['token_balance'])-int(wf.get('cash_floor_usdd','0')));shares=int(observed['shares'])
        if payload.get('claim_key'):
            if context.network!='tron-mainnet':raise MachineError('USDD rewards are not configured for Nile.')
            from .usdd_market import HOST
            data=parse_raw(self.bridge.observations.request(HOST+'/sunProject/getAllUnClaimedAirDrop?addr='+address_base58(context.wallet)))
            entry=(data.get('data') or {}).get(payload['claim_key'])
            if data.get('code')!=0 or not entry or entry.get('tokenAddress')!=CONFIG[context.network]['token']:
                raise MachineError('A current single-token USDD reward claim for this wallet is required.')
            amount=int(entry['amount']);proof=[p.removeprefix('0x').lower() for p in entry.get('merkleProof',entry.get('proof',[]))]
            if amount<=0:raise MachineError('No positive claimable USDD reward.')
            add('CLAIM_USDD',amount,merkle_index=str(entry['merkleIndex']),claim_index=str(entry['index']),proof=proof)
        elif debt:
            cap=(debt*10001+9999)//10000+1
            if balance>=cap:
                add('APPROVE_MARKET',cap);add('REPAY_ALL',cap)
            elif balance:
                amount=min(balance,debt)
                add('APPROVE_MARKET',amount);add('REPAY',amount)
            else:
                report=self.bridge.usdd.refresh(context,state)
                factor=Decimal(report['facts']['collateral_factor'])
                supplied=shares*int(observed['exchange_rate'])//10**18
                # Leave an extra 1% reserve above the protocol borrow boundary;
                # final redemption feasibility is always checked by the node.
                maximum=max(0,supplied-int((Decimal(debt)/factor*Decimal('1.01')).to_integral_value(rounding='ROUND_CEILING')))
                amount=min(maximum,int(observed['market_cash']),debt)
                if amount<=0:raise MachineError('No safe redemption capacity. Repay with additional USDD before releasing collateral.')
                add('REDEEM',amount)
        elif shares:
            add('REDEEM_SHARES',shares)
        elif vault_debt:
            cap=(vault_debt*10001+9999)//10000+1
            if balance<cap:raise MachineError('USDD balance is below accrued Vault debt and the bounded repayment buffer. Claim available rewards or add USDD before repayment.')
            add('APPROVE_PROXY',cap);add('VAULT_REPAY_ALL',cap,repay_cap=str(cap),ilk=wf['ilk'])
        elif int(observed['collateral_wad']):
            add('VAULT_FREE',int(observed['collateral_wad']),ilk=wf['ilk'])
        else:
            wf['status']='CLOSED';self.project(state);return
        if len(wf['steps'])+len(steps)>64:raise MachineError('Workflow recovery requires operator review after 64 transactions.')
        for skipped in wf['steps'][wf['cursor']:]:
            if skipped.get('submission') and skipped['status'] not in ('FAILED','CONFIRMED'):raise MachineError('Unresolved prior submission prevents recovery.')
            if skipped['status'] in ('WAITING','READY'):skipped['status']='CANCELLED'
        wf['hash']=digest({'previous_workflow_hash':wf['hash'],'steps':steps,'confirmed_txids':wf['confirmed_txids'],'policy_hash':record['policy_hash']})
        wf['policy_hash']=record['policy_hash']
        wf['cursor']=len(wf['steps']);wf['steps']+=steps;wf['phase']='RECOVERY';wf['observed']=observed
        wf['status']='AWAITING_NEXT_REVIEW';self.next(context,state)

    def project(self,state):
        wf=state['usdd_execution'];state['workspace']['usdd_workflow']={
            k:deepcopy(wf[k]) for k in ('id','hash','mode','status','cursor','confirmed_txids','outcome','spent_fees','total_fee_cap')}
        state['workspace']['usdd_workflow']['phase']=wf.get('phase','ENTRY')
        state['workspace']['usdd_workflow']['rewards']=wf.get('rewards',[])
        state['workspace']['usdd_workflow']['account']=address_base58(wf['owner'])
        state['workspace']['usdd_workflow']['network']=wf['network'].removeprefix('tron-')
        current=wf['steps'][wf['cursor']] if wf['cursor']<len(wf['steps']) else None
        tx=current.get('transaction') if current else None
        state['workspace']['usdd_workflow']['prepared']={'txid':tx['txID'],'graph_id':current['graph_id'],'step_id':current['id'],'expires_at':datetime.fromtimestamp(tx['raw_data']['expiration']/1000,datetime.fromisoformat(self.clock()).tzinfo).isoformat()} if tx and not current.get('submission') else None
        state['workspace']['usdd_workflow']['steps']=[{k:s.get(k) for k in ('id','operation','amount','status','txid','graph_id')} for s in wf['steps']]

    def verified_live(self,context,state):
        wf=state['usdd_execution'];self.policy(context,state,wf);step=wf['steps'][wf['cursor']]
        if step['status']!='READY' or datetime.fromisoformat(self.clock())>=datetime.fromisoformat(step['expires_at']):raise MachineError('USDD step needs a fresh review.')
        chain=self.chain(context);fresh=chain.snapshot(wf)
        if not unchanged(fresh,step['before']):raise MachineError('USDD account changed after review. Nothing was broadcast.')
        self.live_entry_check(context,state,wf,step,fresh)
        call=compile_call(context.network,context.wallet,step,fresh)
        self.reserve(wf,step,fresh,call)
        if call!=step['call']:raise MachineError('USDD transaction composition changed.')
        live=chain.simulate(call,int(wf['per_step_fee_cap']))
        self.check_simulation(step,fresh,live)
        if live['codes']!=step['live']['codes']:raise MachineError('USDD deployed implementation changed after review.')
        return wf,step,fresh,live

    def live_entry_check(self,context,state,wf,step,observed):
        if wf.get('phase')=='RECOVERY' or step['operation'] not in ('SUPPLY','BORROW','VAULT_DRAW'):return
        _,terms=self.policy(context,state,wf)
        report=self.bridge.usdd.refresh(context,state);f=report['facts']
        blocking={'LIVE_INPUT_UNAVAILABLE','DESTINATION_SUPPLY_PAUSED'}
        if wf['loops']:blocking.add('DESTINATION_BORROW_PAUSED')
        if wf['mode']=='VAULT':blocking.update(('VAULT_SHUTDOWN','VAULT_DESTINATION_TOKEN_MISMATCH'))
        if blocking.intersection(report['blockers']):raise MachineError('Current USDD route state no longer permits entry.')
        market,_=read_market(self.bridge.observations.request,self.clock)
        if not 0<=datetime.fromisoformat(self.clock()).timestamp()-datetime.fromisoformat(market['market_observed_at']).timestamp()<=300:
            raise MachineError('Refresh the USDD market valuation before further entry.')
        loan=next(x for x in f['collaterals'] if x['ilk']==wf['ilk'])
        price=Decimal(market['trx_usd'])/Decimal(market['usdd_usd'])
        remaining=Decimal(wf['horizon_seconds'])-Decimal(str((datetime.fromisoformat(self.clock())-datetime.fromisoformat(wf['created_at'])).total_seconds()))
        if remaining<=0:raise MachineError('Investment horizon has ended; review recovery instead of new entry.')
        equity=Decimal(wf['amount'])/10**18 if wf['mode']=='OWNED' else Decimal(wf['collateral_sun'])/10**6*price
        calculation=loop_cashflow(str(Decimal(wf['amount'])/10**18),f['supply_apy'],market['reward_apr'],f['borrow_apy'],loan['stability_apy'] if wf['mode']=='VAULT' else '0',
            days=str(remaining/86400),loops=wf['loops'],borrow_bps=wf['borrow_bps'],costs=[str(Decimal(wf['total_fee_cap'])/10**6*price)],equity=str(equity))
        if Decimal(calculation['net_income_usdd'])<=0 or (wf['loops'] and Decimal(calculation['marginal_loop_return'])<=0):
            raise MachineError('Fresh rates no longer cover borrowing and the full fee budget. Further entry is blocked.')
        base_price=price if terms['base_asset']=='TRX' else Decimal(1)
        planned_debt=Decimal(calculation['recursive_debt_usdd'])+(Decimal(wf['amount'])/10**18 if wf['mode']=='VAULT' else 0)
        current_debt=(Decimal(observed.get('accrued_market_debt',observed['market_debt']))+Decimal(observed.get('accrued_vault_debt',observed['vault_debt'])))/10**18
        if step['operation'] in ('VAULT_DRAW','BORROW'):current_debt+=Decimal(step['amount'])/10**18
        if max(planned_debt,current_debt)+Decimal(calculation['debt_interest_usdd'])>Decimal(terms['borrowing']['max_debt']['amount'])*base_price:
            raise MachineError('Fresh debt and interest exceed the confirmed debt limit.')
        ratio=max(Decimal(terms['borrowing']['min_collateral_ratio_bps'])/10000,Decimal(loan['liquidation_ratio']) if step['operation']=='VAULT_DRAW' else Decimal(1)/Decimal(f['collateral_factor']))+Decimal(terms['borrowing']['liquidation_buffer_bps'])/10000
        if step['operation']=='VAULT_DRAW':
            debt=Decimal(observed.get('accrued_vault_debt',observed['vault_debt']))/10**18+Decimal(step['amount'])/10**18
            collateral=Decimal(observed['collateral_wad'])/10**18+Decimal(step['collateral_sun'])/10**6
            if collateral*Decimal(loan['debt_capacity_per_trx'])*Decimal(loan['liquidation_ratio'])<debt*ratio:
                raise MachineError('Live Vault collateral no longer covers the confirmed safety buffer.')
        if step['operation']=='BORROW':
            supplied=Decimal(observed['shares'])*Decimal(observed['exchange_rate'])/10**36
            debt=(Decimal(observed.get('accrued_market_debt',observed['market_debt']))+Decimal(step['amount']))/10**18
            if supplied<debt*ratio or Decimal(f['market_cash_usdd'])<Decimal(step['amount'])/10**18:
                raise MachineError('Borrowing exceeds current liquidity or the confirmed collateral buffer.')
        step['economic_review']={'calculation':calculation,'route_hash':report.get('hash',digest(report)),
            'reward_evidence_hash':market.get('hash',digest(market)),'checked_at':self.clock()}

    def rewards(self,context,state):
        wf=state.get('usdd_execution')
        if not wf or context.network!='tron-mainnet':raise MachineError('Open a mainnet USDD workflow before reviewing its rewards.')
        from .usdd_market import HOST
        data=parse_raw(self.bridge.observations.request(HOST+'/sunProject/getAllUnClaimedAirDrop?addr='+address_base58(context.wallet)))
        if data.get('code')!=0 or not isinstance(data.get('data'),dict) or len(data['data'])>512:raise MachineError('Current reward periods are unavailable.')
        entries=[]
        for key,entry in data['data'].items():
            if entry.get('tokenAddress')!=CONFIG[context.network]['token'] or isinstance(entry.get('amount'),list):continue
            amount=int(entry.get('amount','0'))
            if amount>0:entries.append({'key':key,'round':str(entry['merkleIndex']),'amount':decstr(Decimal(amount)/10**18)})
        wf['rewards']=entries[:25];self.project(state)

    @staticmethod
    def reserve(wf,step,observed,call):
        remaining=int(wf['total_fee_cap'])-int(wf['spent_fees'])
        if int(observed['trx_balance'])-int(call['value_sun'])-remaining<int(wf.get('cash_floor_trx','0')):
            raise MachineError('Complete fee reserve and TRX cash floor are not covered.')
        if step['operation'] in ('SUPPLY','REPAY','REPAY_ALL','VAULT_REPAY','VAULT_REPAY_ALL') and int(observed['token_balance'])-int(step['amount'])<int(wf.get('cash_floor_usdd','0')):
            raise MachineError('This step would consume the confirmed USDD cash floor.')

    @staticmethod
    def check_simulation(step,observed,live):
        from economic_machine.tron_crypto import keccak256
        from economic_machine.usdd_workflow import addr
        op=step['operation'];logs=live['simulation'].get('logs',[]);amount=int(step['amount'])
        if op in ('SUPPLY','BORROW','REPAY','REPAY_ALL','REDEEM','REDEEM_SHARES'):
            signatures={'SUPPLY':('Mint(address,uint256,uint256)',3),
                'BORROW':('Borrow(address,uint256,uint256,uint256)',4),
                'REPAY':('RepayBorrow(address,address,uint256,uint256,uint256)',5),
                'REPAY_ALL':('RepayBorrow(address,address,uint256,uint256,uint256)',5),
                'REDEEM':('Redeem(address,uint256,uint256)',3),'REDEEM_SHARES':('Redeem(address,uint256,uint256)',3)}
            signature,n=signatures[op];topic=keccak256(signature.encode()).hex()
            market=address_hex(CONFIG[observed['network']]['market'])[2:]
            found=[x for x in logs if x.get('address')==market and x.get('topics')==[topic]]
            if len(found)!=1 or len(found[0].get('data',''))!=n*64:raise MachineError('Complete protocol simulation event required.')
            chunks=[found[0]['data'][i*64:(i+1)*64] for i in range(n)]
            if chunks[0]!=addr(observed['owner']):raise MachineError('Simulated protocol owner differs.')
            if op.startswith('REPAY'):
                if chunks[1]!=addr(observed['owner']):raise MachineError('Repayment cannot pay another wallet debt.')
                actual=int(chunks[2],16)
                if (op=='REPAY_ALL' and (not 0<actual<=amount or int(chunks[3],16)!=0)) or (op=='REPAY' and actual!=amount):
                    raise MachineError('Simulated repayment differs from its bounded amount.')
            elif op=='REDEEM_SHARES':
                if int(chunks[2],16)!=amount or int(chunks[1],16)<=0:raise MachineError('Simulated redemption share amount differs.')
            else:
                if int(chunks[1],16)!=amount:raise MachineError('Simulated protocol amount differs.')
                if op=='SUPPLY' and int(chunks[2],16)<int(step['minimum_shares']):raise MachineError('Simulated share output is below the reviewed minimum.')
        if op in ('CLAIM_USDD','VAULT_DRAW'):
            topic=keccak256(b'Transfer(address,address,uint256)').hex()
            transfers=[x for x in logs if x.get('address')==observed['token'][2:] and len(x.get('topics',[]))==3 and x['topics'][0]==topic and x['topics'][2]==addr(observed['owner'])]
            if sum(int(x.get('data','0'),16) for x in transfers)!=amount:raise MachineError('Simulated USDD receipt differs from the reviewed amount.')

    def approve(self,context,state,payload):
        wf,step,fresh,live=self.verified_live(context,state);g=state['workspace']['graph']
        if any(payload.get(k)!=v for k,v in {'graph_id':g['id'],'graph_hash':g['hash'],'plan_hash':g['plan_hash'],
                'mandate_hash':g['mandate_hash'],'account':g['account'],'network':g['network']}.items()):raise MachineError('USDD approval differs from the exact reviewed step.')
        at=self.clock();core=step['core_graph'];expiry=step['expires_at']
        account={'schema_version':'economic-account-snapshot-1','scope':context.scope,'snapshot_hash':core['snapshot_hash'],
            'observed_at':at,'valid_until':expiry,'balances':[
                {'asset':'TRX','token_address':None,'decimals':6,'amount_base_units':fresh['trx_balance']},
                {'asset':'USDD','token_address':fresh['token'],'decimals':18,'amount_base_units':fresh['token_balance']},
                {'asset':'jUSDD','token_address':address_hex(CONFIG[context.network]['market']),'decimals':8,'amount_base_units':fresh['shares']}],
            'allowances':[],'positions':[],'vaults':[],'reservations':[],'market_liquidity':[]}
        simulation={'schema_version':'economic-step-simulation-1','step_id':step['id'],'action_hash':core['steps'][0]['action']['action_hash'],
            'status':'SUCCESS','decoded_result':True if step['operation'].startswith('APPROVE') else '0' if step['operation'] in ('SUPPLY','BORROW','REPAY','REPAY_ALL','REDEEM','REDEEM_SHARES') else None,'fee_estimate_sun':str(live['costs']['full_energy_burn_sun']),
            'projected_outputs':[],'postcondition_results':[{'kind':'BALANCE_AT_LEAST','passed':all(
                int(next((b['amount_base_units'] for b in account['balances'] if b['asset']==c['asset'] and b['token_address']==c['token_address']), '0'))>=int(c['amount_base_units'])
                for c in core['steps'][0]['postconditions']), 'observed':digest(fresh)}],'evidence_hash':digest(live),'recorded_at':at}
        manifest=compile_preflight(core,account,[simulation],at=at)
        if manifest['status']!='PREFLIGHT_PASSED_UNSIGNED':raise MachineError('USDD preflight failed.')
        session={'schema_version':'economic-wallet-session-1','session_id':context.session_id,'scope':context.scope,
            'auth_context_hash':digest({'session':context.session_id,'scope':context.scope}),'issued_at':at,'expires_at':expiry,'status':'AUTHENTICATED'}
        card=prepare_approval_card(core,manifest,session,step['id'],execution_path='DIRECT_WALLET',nonce=str(wf['cursor']),prepared_at=at,expires_at=expiry)
        approved=confirm_approval(card,session,{'approval_hash':card['approval_hash'],'session_id':session['session_id'],'auth_context_hash':session['auth_context_hash'],
            'wallet':context.wallet,'network':context.network,'decision':'APPROVE','confirmed_at':at})
        step.update(approved=approved,session=session)
        state['workspace']['approval']={'id':approved['approval_hash'],'graph_hash':g['hash'],'plan_hash':g['plan_hash'],
            'mandate_hash':g['mandate_hash'],'account':g['account'],'network':g['network'],'expires_at':expiry,'status':'APPROVED','consented_at':at}

    def prepare(self,context,graph_id,payload):
        version,state=self.bridge.load(context);wf,step,fresh,live=self.verified_live(context,state)
        w=state['workspace'];g=w['graph'];a=w['approval']
        if not a or graph_id!=g['id'] or payload.get('approval_id')!=a['id'] or payload.get('step_id')!=step['id'] or payload.get('account')!=g['account']:
            raise MachineError('Exact USDD step approval required.')
        if step['session']['session_id']!=context.session_id:raise MachineError('Wallet session changed.')
        head=self.chain(context).head(False);at=self.clock()
        anchor={'schema_version':'economic-tron-reference-block-1','network':context.network,'block_number':str(head['number']),
            'block_id':head['block_id'],'ref_block_bytes':head['number'].to_bytes(8,'big')[6:8].hex(),
            'ref_block_hash':bytes.fromhex(head['block_id'])[8:16].hex(),'observed_at':at,'valid_until':step['expires_at']}
        if not step.get('transaction'):
            req=build_wallet_signature_request(step['approved'],step['session'],anchor,issued_at=at)
            step.update(wallet_request=req,transaction=unsigned_transaction(req,live['costs']['bandwidth_bytes_bound']))
        self.project(state)
        self.bridge.commit(context,version,state)
        return {'graph_hash':g['hash'],'approval_id':a['id'],'step_id':step['id'],'network':g['network'],
            'account':g['account'],'expires_at':step['expires_at'],'transaction':step['transaction'],'simulation':False,
            'checks':[{'name':'Exact USDD step','status':'PASS','detail':'Fixed protocol calldata, owner, token, confirmed conditions and live implementation verified.'},
                {'name':'Live cost and account','status':'PASS','detail':'Wallet state unchanged; live simulation fits the reserved fee cap. Subsequent steps require new signatures.'}]}

    def submit(self,context,payload):
        version,state=self.bridge.load(context);wf=state['usdd_execution'];step=wf['steps'][wf['cursor']];w=state['workspace'];g=w['graph']
        tx=payload.get('signed_transaction',{})
        if step.get('submission'):
            if tx.get('txID')==step['txid']:return ACK
            raise MachineError('Reconcile the current USDD transaction first.')
        wf,step,_,_=self.verified_live(context,state)
        if payload.get('graph_id')!=g['id'] or payload.get('step_id')!=step['id'] or payload.get('approval_id')!=(w.get('approval') or {}).get('id'):
            raise MachineError('USDD signed submission context differs.')
        prepared=step.get('transaction') or {}
        if any(tx.get(k)!=prepared.get(k) for k in ('txID','raw_data','raw_data_hex')) or not prepared:raise MachineError('Signed USDD bytes differ from the prepared transaction.')
        signed=normalize_wallet_signature({k:tx.get(k) for k in ('visible','txID','raw_data_hex','signature')})
        validated=validate_signed_transaction(signed,step['wallet_request'],step['session'],[],at=self.clock())
        if tx['txID'] in wf['confirmed_txids']:raise MachineError('This transaction was already consumed.')
        record=prepare_submission(validated,at=self.clock())
        attempt={'schema_version':'economic-tron-submission-attempt-1','attempt_id':'send-'+tx['txID'][:24],'txid':tx['txID'],
            'signed_payload_hash':record['signed_payload_hash'],'attempted_at':self.clock(),'transport_status':'AFTER_SEND_UNKNOWN',
            'node_result':'UNKNOWN','response_hash':digest({'status':'DURABLE_BEFORE_SEND'}),'error_code':'AWAITING_NODE_RESPONSE'}
        step.update(submission=record_broadcast_attempt(record,attempt),status='SUBMISSION_UNKNOWN',txid=tx['txID'])
        wf['status']='SUBMISSION_UNKNOWN';self.project(state)
        w['execution']={'id':tx['txID'],'graph_id':g['id'],'status':'SUBMISSION_UNKNOWN','network':g['network'],
            'started_at':self.clock(),'updated_at':self.clock(),'message':'USDD signed transaction durably recorded; awaiting chain evidence.'}
        g['steps'][0].update(status='SUBMISSION_UNKNOWN',txid=tx['txID'])
        self.bridge.commit(context,version,state) # write-ahead CAS fence before network effect
        try:response=self.chain(context).rpc('wallet/broadcasttransaction',{**prepared,'signature':signed['signature']})
        except Exception:return ACK
        version,state=self.bridge.load(context);wf=state['usdd_execution'];step=wf['steps'][wf['cursor']]
        if step['txid']!=tx['txID']:raise MachineError('USDD submission record changed.')
        step['node_response']=response
        # Both accepted and rejected RPC responses still need solidified evidence.
        self.project(state);self.bridge.commit(context,version,state);return ACK

    def cancel(self,context,state):
        wf=state.get('usdd_execution')
        if not wf or wf['owner']!=context.wallet or wf['network']!=context.network:raise MachineError('No matching workflow.')
        if wf['confirmed_txids'] or any(s.get('submission') or s.get('transaction') for s in wf['steps']):
            raise MachineError('Prepared or executed transactions require reconciliation and recovery.')
        wf['status']='CANCELLED';self.project(state)
        state['workspace']['graph']=None;state['workspace']['approval']=None

    def recover_prepared(self,context,version,state,payload):
        wf=state['usdd_execution'];step=wf['steps'][wf['cursor']];tx=step.get('transaction')
        if not tx or payload.get('txid')!=tx['txID'] or payload.get('step_id')!=step['id'] or payload.get('graph_id')!=state['workspace']['graph']['id']:
            raise MachineError('No matching prepared USDD transaction.')
        if step.get('submission'):raise MachineError('Submitted USDD transaction requires receipt reconciliation.')
        now=int(datetime.fromisoformat(self.clock()).timestamp()*1000);expiry=tx['raw_data']['expiration']
        if now<=expiry+60000:raise MachineError('Prepared USDD transaction remains valid; wait for expiry before releasing its review.')
        chain=self.chain(context);before=chain.head()
        reads=[chain.rpc(prefix+'/'+method,{'value':tx['txID']}) for prefix in ('wallet','walletsolidity') for method in ('gettransactionbyid','gettransactioninfobyid')]
        after=chain.head()
        if any(reads) or min(before['timestamp_ms'],after['timestamp_ms'])<=expiry+60000:
            raise MachineError('USDD transaction is visible or not yet past solidified expiry. Keep its reservation until receipt review.')
        resolution={'txid':tx['txID'],'graph_id':payload['graph_id'],'step_id':step['id'],
            'status':'EXPIRED_NOT_OBSERVED','message':'Prepared USDD transaction expired with no full or solid node transaction. Review this step again.'}
        wf.setdefault('expired_prepared',[]).append({'transaction':tx,'resolution':resolution,'reads':reads,'heads':[before,after]})
        for key in ('transaction','wallet_request','approved','session'):step.pop(key,None)
        step['status']='WAITING';wf['status']='AWAITING_NEXT_REVIEW';state['workspace']['approval']=None
        self.project(state);self.bridge.commit(context,version,state)
        return {**ACK,'resolution':resolution}

    def reconcile(self,context,payload):
        version,state=self.bridge.load(context);wf=state.get('usdd_execution')
        if not wf:raise MachineError('No USDD workflow is recorded.')
        txid=payload.get('txid')
        if txid in wf['confirmed_txids']:return ACK
        step=wf['steps'][wf['cursor']];g=state['workspace']['graph']
        if not step.get('submission'):return self.recover_prepared(context,version,state,payload)
        if step['status'] in ('FAILED','DISPUTED'):return ACK
        if txid!=step.get('txid') or payload.get('graph_id')!=g['id'] or payload.get('step_id')!=step['id']:
            raise MachineError('USDD reconciliation pointer differs from the durable record.')
        chain=self.chain(context);now=int(datetime.fromisoformat(self.clock()).timestamp()*1000)
        reader={'schema_version':'economic-tron-transaction-reader-1','solidity_url':chain.root,'api_key_env':None,
            'max_block_age_ms':300000,'network_anchor_height':1,'network_anchor_block_id':ANCHORS[context.network]}
        reads=[]
        def transport(kind,path,body):
            result=chain.rpc(path.lstrip('/'),body);reads.append({'path':path,'body':body,'response':result});return result
        observation=read_tron_transaction(txid,reader,now_ms=now,transport=transport)
        step['last_observation']=observation
        if observation['status'] not in ('SOLID_EXECUTED','SOLID_EXECUTION_FAILED'):
            self.bridge.commit(context,version,state);return ACK
        # The transaction reader verifies the transaction body/hash, block
        # inclusion and solidification before post-state can unlock anything.
        receipt=next((r['response'] for r in reads if r['path'].endswith('/gettransactioninfobyid')),None)
        if not receipt:raise MachineError('Solid USDD receipt payload unavailable.')
        fee=receipt.get('fee',0)
        if type(fee) is not int or fee<0:raise MachineError('Invalid actual USDD transaction fee.')
        after=chain.snapshot(wf)
        if observation['details']['call_target_address']!=step['call']['target']:raise MachineError('USDD receipt target differs from prepared call.')
        proof={'txid':txid,'solidified':True,'success':observation['status']=='SOLID_EXECUTED',
            'fee_sun':str(fee),'logs':receipt.get('log',[]),'observation_hash':digest(observation)}
        disagreement=None
        if proof['success']:
            try:self.check_simulation(step,step['before'],{'simulation':{'logs':proof['logs']}})
            except MachineError as exc:disagreement=str(exc)
        if disagreement:
            updated=deepcopy(wf);updated['status']='DISPUTED'
            updated['steps'][updated['cursor']].update(status='DISPUTED',receipt=proof,after=after,error=disagreement)
        else:updated=advance(wf,txid,proof,after)
        updated['spent_fees']=str(int(wf['spent_fees'])+fee)
        state['usdd_execution']=updated
        if updated['status'] not in ('NEEDS_RECOVERY','DISPUTED'):
            if step['operation']=='VAULT_OPEN':updated['cdp_id']=after['cdp_id']
            if step['operation'] in ('SUPPLY','VAULT_DRAW'):
                state.setdefault('usdd_spend_ledger',{}).setdefault(wf['id'],{'policy_hash':wf['policy_hash'],'principal_base':wf['principal_base']})
        w=state['workspace'];terminal='POSITION_RECONCILED' if step['status'] not in ('FAILED','DISPUTED') and updated['status'] not in ('NEEDS_RECOVERY','DISPUTED') else 'FAILED' if updated['status']=='NEEDS_RECOVERY' else 'DISPUTED'
        w['graph']['steps'][0].update(status='BLOCKED' if terminal=='DISPUTED' else terminal,txid=txid)
        w['execution'].update(status=terminal,updated_at=self.clock(),message='USDD receipt and independent position reconciled. Review the next step separately.' if terminal=='POSITION_RECONCILED' else 'USDD transaction needs recovery. Further entry is blocked.')
        if terminal=='POSITION_RECONCILED':
            key='usdd-'+context.network;vault_key=key+'-vault'
            positions=[p for p in w['positions'] if p['id'] not in (key,vault_key)]
            if int(after['shares']) or int(after['market_debt']):
                positions.append({'id':key,'product':'justlend.v1.jUSDD','protocol':'JustLend','network':w['network'],'provenance':'LIVE',
                    'principal':token_money(int(wf['amount'])),'current_value':token_money(int(after['shares'])*int(after['exchange_rate'])//10**18),
                    'debt':token_money(int(after['market_debt'])),'exit_status':'Review redemption and any lending debt repayment separately.','receipt_txid':txid})
            if int(after['collateral_wad']) or int(after['vault_debt']):
                positions.append({'id':vault_key,'product':'usdd.vault.'+wf['ilk'],'protocol':'USDD','network':w['network'],'provenance':'LIVE',
                    'principal':money(int(wf['collateral_sun'])),'current_value':money(int(after['collateral_wad'])//10**12),
                    'debt':token_money(int(after['vault_debt'])),'exit_status':'USDD debt must be repaid before releasing TRX collateral.','receipt_txid':txid})
            w['positions']=positions
            w['balances']=[b for b in w['balances'] if b['symbol'] not in ('USDD','TRX')]+[token_money(int(after['token_balance'])),money(int(after['trx_balance']))]
        self.project(state);self.bridge.commit(context,version,state);return ACK
