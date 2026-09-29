"""Nile jTRX adapter: real RPC quotes, exact wallet bytes, durable submission.

No key is accepted. The only supported call is pinned jTRX mint() with native
TRX. Protocol output is checked after settlement; direct mint has no on-chain
minimum-output argument, which the review explicitly discloses.
"""
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal

from economic_machine.approval import prepare_approval_card, confirm_approval, build_wallet_signature_request
from economic_machine.preflight import compile_preflight
from economic_machine.tx_graph import compile_execution_graph
from economic_machine.signed_tx_validation import _encode_varint, decode_trigger_raw, validate_signed_transaction
from economic_machine.tron_execution import prepare_submission, record_broadcast_attempt, assess_execution_observation
from economic_machine.position_reconciliation import reconcile_position
from economic_machine.tron_consumption_read import read_tron_transaction
from economic_machine.tron_registry_read import _block
from economic_machine.tron_sources import address_base58, parse_raw
from economic_machine.tron_crypto import keccak256
from economic_machine.values import MachineError, digest, decstr

ROOT='https://nile.trongrid.io'
MARKET='4166de0d4dbe8ca9865423ceccacc699c682d4cc94'
CODE_SHA256='edce302a03dff78bd224e68efc9954f34fd01219894313b91b3b748d3f582357'
PRODUCT='justlend.v1.jTRX'
MINT=keccak256(b'Mint(address,uint256,uint256)').hex()
FAILURE=keccak256(b'Failure(uint256,uint256,uint256)').hex()
TRANSFER=keccak256(b'Transfer(address,address,uint256)').hex()
REDEEM=keccak256(b'Redeem(address,uint256,uint256)').hex()
READER={'schema_version':'economic-tron-transaction-reader-1','solidity_url':ROOT,'api_key_env':None,
    'max_block_age_ms':300000,'network_anchor_height':1,
    'network_anchor_block_id':'0000000000000001f1b9db4e1f7a39e5bcb4636382f3b24ee38a718e2bd4ddb0'}
ACK={'accepted':True,'job_id':None}
TERMINAL={'POSITION_RECONCILED','FAILED','DISPUTED'}


def normalize_wallet_signature(tx):
    """Normalize hex notation only; core verification still checks every byte.

    TronWeb ECKeySign appends an uppercase recovery byte (1B/1C). Some
    wallets also prefix signatures with 0x. Neither changes the signature.
    """
    signatures=tx.get('signature')
    if not isinstance(signatures,list) or len(signatures)!=1 or not isinstance(signatures[0],str):
        raise MachineError('Exactly one wallet transaction signature is required. Nothing was broadcast.')
    value=signatures[0]
    if value[:2].lower()=='0x':value=value[2:]
    if re.fullmatch(r'[0-9a-fA-F]{130}',value) is None:
        raise MachineError('Wallet signature must contain exactly 65 bytes. Nothing was broadcast.')
    return {**tx,'signature':[value.lower()]}


def atoms(value):
    d=Decimal(value)*10**6
    if not d.is_finite() or d < 0 or d != int(d): raise MachineError('TRX amount must have at most six decimals.')
    return int(d)


def money(value):
    return {'value':decstr(Decimal(value)/10**6),'symbol':'TRX','decimals':6}


def asset(value, shares=False):
    return {'asset':'jTRX' if shares else 'TRX','token_address':MARKET if shares else None,
        'decimals':8 if shares else 6,'amount_base_units':str(value)}


def verify_native_abi(code):
    if code.get('contract_address') != MARKET or hashlib.sha256(bytes.fromhex(code.get('bytecode',''))).hexdigest()!=CODE_SHA256:
        raise MachineError('Nile jTRX deployed bytecode differs from the verified adapter.')
    entries=code.get('abi',{}).get('entrys',[])
    mint=[e for e in entries if e.get('name')=='mint' and e.get('type')=='Function']
    if len(mint)!=1 or mint[0].get('inputs',[]) or mint[0].get('outputs',[]) or mint[0].get('stateMutability')!='Payable':
        raise MachineError('Nile jTRX payable mint ABI changed.')
    return digest(code['abi'])


def mint_shares(logs, owner, amount):
    relevant=[x for x in logs if x.get('address')==MARKET[2:]]
    if any(x.get('topics',[None])[0]==FAILURE for x in relevant): raise MachineError('JustLend emitted a protocol Failure event.')
    rows=[x for x in relevant if x.get('topics')==[MINT]]
    if len(rows)!=1 or len(rows[0].get('data',''))!=192: raise MachineError('Exactly one complete JustLend Mint event is required.')
    data=rows[0]['data']; wallet='41'+data[24:64]
    if data[:24]!='0'*24 or wallet!=owner or int(data[64:128],16)!=amount: raise MachineError('Mint recipient or input amount differs.')
    shares=int(data[128:192],16)
    transfers=[x for x in relevant if x.get('topics')==[TRANSFER,MARKET[2:].rjust(64,'0'),owner[2:].rjust(64,'0')]]
    if shares<=0 or len(transfers)!=1 or int(transfers[0].get('data','0'),16)!=shares: raise MachineError('Mint and share Transfer events do not agree.')
    return shares


def redeem_amount(logs, owner, shares):
    relevant=[x for x in logs if x.get('address')==MARKET[2:]]
    if any(x.get('topics',[None])[0]==FAILURE for x in relevant):raise MachineError('JustLend emitted a protocol Failure event.')
    rows=[x for x in relevant if x.get('topics')==[REDEEM]]
    if len(rows)!=1 or len(rows[0].get('data',''))!=192:raise MachineError('Exactly one complete Redeem event is required.')
    data=rows[0]['data']
    if data[:24]!='0'*24 or '41'+data[24:64]!=owner or int(data[128:],16)!=shares:raise MachineError('Redemption owner or shares differ from the reviewed amount.')
    proceeds=int(data[64:128],16)
    transfers=[x for x in relevant if x.get('topics')==[TRANSFER,owner[2:].rjust(64,'0'),MARKET[2:].rjust(64,'0')]]
    if proceeds<=0 or len(transfers)!=1 or int(transfers[0].get('data','0'),16)!=shares:raise MachineError('Redeem and share Transfer events do not agree.')
    return proceeds


def unsigned_transaction(request, bandwidth_bound=1024):
    if type(bandwidth_bound) is not int or not 256<=bandwidth_bound<=8192:raise MachineError("Invalid reviewed bandwidth bound.")
    def f(n,value):
        if isinstance(value,bytes):return _encode_varint(n*8+2)+_encode_varint(len(value))+value
        return _encode_varint(n*8)+_encode_varint(value)
    owner=bytes.fromhex(request['owner_address']); market=bytes.fromhex(request['contract_address'])
    call_value=int(request['call_value_sun'])
    # Proto3 omits default scalar values. TronWeb/TronLink reconstructs the
    # bytes from JSON before signing; an explicit field 3 = 0 changes the TXID
    # and fails its txCheck even though our semantic decoder accepts it.
    trigger=f(1,owner)+f(2,market)+(f(3,call_value) if call_value else b'')+f(4,bytes.fromhex(request['data_hex']))
    type_url='type.googleapis.com/protocol.TriggerSmartContract'
    contract=f(1,31)+f(2,f(1,type_url.encode())+f(2,trigger))
    raw=f(1,bytes.fromhex(request['ref_block_bytes']))+f(4,bytes.fromhex(request['ref_block_hash']))+f(8,request['expiration_ms'])+f(11,contract)+f(14,request['timestamp_ms'])+f(18,int(request['fee_limit_sun']))
    projection={'ref_block_bytes':request['ref_block_bytes'],'ref_block_hash':request['ref_block_hash'],
        'expiration':request['expiration_ms'],'timestamp':request['timestamp_ms'],'fee_limit':int(request['fee_limit_sun']),
        'contract':[{'type':'TriggerSmartContract','parameter':{'type_url':type_url,'value':{
            'owner_address':request['owner_address'],'contract_address':request['contract_address'],
            **({'call_value':call_value} if call_value else {}),'data':request['data_hex']}}}]}
    decode_trigger_raw(raw)
    if len(raw)+195>bandwidth_bound:raise MachineError('Transaction exceeds the reviewed bandwidth bound.')
    return {'visible':False,'txID':hashlib.sha256(raw).hexdigest(),'raw_data_hex':raw.hex(),'raw_data':projection}


class NativeExecution:
    def __init__(self, bridge, request):
        self.bridge=bridge;self.clock=bridge.clock;self.request=request

    def rpc(self,path,body=None):
        data=parse_raw(self.request(ROOT+'/'+path,body or {}),max_bytes=8388608 if path.rsplit('/',1)[-1] in {'getblock','getblockbynum','getblockbyid','getnowblock'} else 1048576)
        if not isinstance(data,dict) or data.get('Error') or data.get('error'):raise MachineError('Nile RPC failed: '+path)
        return data

    def call(self,owner,selector,parameter='',solid=True):
        from .nile_observations import uint
        return uint(self.rpc(('walletsolidity' if solid else 'wallet')+'/triggerconstantcontract',{
            'owner_address':owner,'contract_address':MARKET,'function_selector':selector,'parameter':parameter,'visible':False}))

    def account(self,scope,snapshot_hash,expiry):
        # Accept a multi-call set only while the solidified head stays identical.
        for _ in range(6):
            before=_block(self.rpc('walletsolidity/getnowblock'))
            owner=scope['wallet'];arg=owner[2:].rjust(64,'0')
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures=[pool.submit(self.rpc,'walletsolidity/getaccount',{'address':owner,'visible':False}),
                    pool.submit(self.rpc,'walletsolidity/triggerconstantcontract',{'owner_address':owner,'contract_address':MARKET,'function_selector':'getAccountSnapshot(address)','parameter':arg,'visible':False}),
                    pool.submit(self.call,owner,'getCash()')]
                wallet,position,cash=[f.result() for f in futures]
            values=position.get('constant_result')
            if position.get('result',{}).get('result') is not True or not isinstance(values,list) or len(values)!=1 or len(values[0])!=256:
                raise MachineError('Complete jTRX getAccountSnapshot output required.')
            error,shares,debt,rate=[int(values[0][i:i+64],16) for i in range(0,256,64)]
            if error:raise MachineError('jTRX account snapshot returned a protocol error.')
            after=_block(self.rpc('walletsolidity/getnowblock'))
            if before['block_id']==after['block_id']:break
        else:raise MachineError('Nile solidified state moved during account reads; retry preflight.')
        if wallet.get('address')!=owner or type(wallet.get('balance',0)) is not int or wallet.get('balance',0)<0 or rate<=0:
            raise MachineError('Verified native wallet balance and exchange rate required.')
        at=self.clock()
        if not 0 <= datetime.fromisoformat(at).timestamp()*1000-after['timestamp_ms'] <= 300000:
            raise MachineError('Nile solidified account block is stale.')
        evidence={'block':after,'wallet':wallet,'shares':str(shares),'exchange_rate':str(rate),'debt':str(debt),'cash':str(cash),'observed_at':at,'position_response':position,'read_consistency':'Independent wallet and market calls bracketed by an unchanged solidified head; node-response evidence, not a cryptographic state proof.'}
        account={'schema_version':'economic-account-snapshot-1','scope':scope,'snapshot_hash':snapshot_hash,'observed_at':at,'valid_until':expiry,
            'balances':[asset(wallet.get('balance',0)),asset(shares,True)],'allowances':[],
            'positions':[{'product_id':PRODUCT,'shares_base_units':str(shares),'underlying_base_units':str(shares*rate//10**18)}],
            'vaults':[],'reservations':[],'market_liquidity':[{'product_id':PRODUCT,'amount_base_units':str(cash)}]}
        from economic_machine.tx_graph import normalize_account_snapshot
        return normalize_account_snapshot(account),evidence

    def simulate(self,owner,amount):
        with ThreadPoolExecutor(max_workers=3) as pool:
            fs=[pool.submit(self.rpc,'wallet/getcontract',{'value':MARKET,'visible':False}),
                pool.submit(self.rpc,'wallet/triggerconstantcontract',{'owner_address':owner,'contract_address':MARKET,'function_selector':'mint()','parameter':'','call_value':amount,'visible':False}),
                pool.submit(self.rpc,'wallet/getchainparameters')]
            code,simulation,parameters=[f.result() for f in fs]
        abi=verify_native_abi(code)
        if simulation.get('result',{}).get('result') is not True or simulation.get('constant_result')!=[''] or type(simulation.get('energy_used')) is not int:
            raise MachineError('Live payable jTRX mint simulation failed.')
        shares=mint_shares(simulation.get('logs',[]),owner,amount)
        params={x['key']:x.get('value',0) for x in parameters.get('chainParameter',[])}
        energy_price=params.get('getEnergyFee');bandwidth_price=params.get('getTransactionFee')
        if type(energy_price) is not int or type(bandwidth_price) is not int or energy_price<=0 or bandwidth_price<=0:
            raise MachineError('Current energy and bandwidth prices required.')
        from .resource_costs import quote_resources,signed_bandwidth_bound
        resources=self.rpc('wallet/getaccountresource',{'address':owner,'visible':False})
        size=signed_bandwidth_bound(owner,MARKET,'1249c58b',amount,timestamp_ms=int(datetime.fromisoformat(self.clock()).timestamp()*1000))
        costs=quote_resources(simulation['energy_used'],size,parameters,resources)
        evidence={'code':code,'simulation':simulation,'parameters':parameters,'resources':resources,'resource_costs':costs,'observed_at':self.clock()}
        return {'shares':shares,'energy_burn':simulation['energy_used']*energy_price,'bandwidth_bound':1024*bandwidth_price,
            'expected_burn':costs['expected_total_burn_sun'],'resource_costs':costs,
            'abi_hash':abi,'hash':digest(evidence),'evidence':evidence}

    def preflight_manifest(self,native,account,live):
        redeem=native.get('operation')=='REDEEM'
        if not redeem:self.economic_gate(native.get('economics'), live)
        graph=native['graph'];step=next(x for x in graph['steps'] if x['kind']!='OFFCHAIN_RESERVATION')
        minimum=int(step['expected_outputs'][0]['amount_base_units'])
        conditions=[]
        for c in step['postconditions']:
            if c['kind']=='MIN_OUTPUT':value=live['received'] if redeem else live['shares'];passed=value>=minimum
            elif c['kind']=='INPUT_DECREASE':value=native['amount'];passed=True # Mint event amount was decoded and matched.
            elif c['kind']=='POSITION_SHARES_AT_LEAST':value=int(account['positions'][0]['shares_base_units']);passed=value>=int(c['amount_base_units'])
            elif c['kind']=='MARKET_LIQUIDITY_AT_LEAST':value=int(account['market_liquidity'][0]['amount_base_units']);passed=value>=int(c['amount_base_units'])
            else:raise MachineError('Native adapter cannot verify an unexpected postcondition.')
            conditions.append({'kind':c['kind'],'passed':passed,'observed':str(value)})
        sim={'schema_version':'economic-step-simulation-1','step_id':step['step_id'],'action_hash':step['action']['action_hash'],
            'status':'SUCCESS','decoded_result':'0' if redeem else None,'fee_estimate_sun':str(live['energy_burn']),'projected_outputs':[asset(live['received']) if redeem else asset(live['shares'],True)],
            'postcondition_results':conditions,'evidence_hash':live['hash'],'recorded_at':live['evidence']['observed_at']}
        if live['abi_hash']!=native['abi_hash'] or live['bandwidth_bound']!=native['bandwidth_bound']:
            raise MachineError('Contract ABI or bandwidth price changed; review again.')
        if live['energy_burn']+live['bandwidth_bound']>native['total_fee_cap']:
            raise MachineError('Live fee estimate exceeds the approved total fee cap.')
        self.budget(native,account)
        full=self.rpc('wallet/getaccount',{'address':account['scope']['wallet'],'visible':False})
        if full.get('address')!=account['scope']['wallet'] or full.get('balance',0)!=int(account['balances'][0]['amount_base_units']):
            raise MachineError('Unsettled wallet balance changes detected. Wait for solidification before signing.')
        manifest=compile_preflight(graph,account,[sim],at=self.clock())
        if manifest['status']!='PREFLIGHT_PASSED_UNSIGNED':raise MachineError('Native preflight blocked: '+','.join(manifest['blockers']))
        return manifest

    @staticmethod
    def economic_gate(economics, live):
        if not economics:
            raise MachineError('Compare fresh plans again: this entry has no cost-adjusted return review.')
        gross=Decimal(economics['projected_income'])
        other=Decimal(economics['other_costs'])
        entry=Decimal(live.get('expected_burn',live['energy_burn']+live['bandwidth_bound']))/10**6
        if not gross.is_finite() or not other.is_finite() or other<0 or gross<=entry+other:
            raise MachineError('Keep cash: projected income does not cover the live entry cost and reviewed remaining costs. No transaction is authorized.')

    @staticmethod
    def budget(native,account):
        if native.get('operation')=='REDEEM':
            if int(account['balances'][0]['amount_base_units'])<native['total_fee_cap']:raise MachineError('Wallet must hold the fee reserve before redemption.')
            remaining=int(account['balances'][0]['amount_base_units'])+native['minimum_received']-native['total_fee_cap']
        else:remaining=int(account['balances'][0]['amount_base_units'])-native['amount']-native['total_fee_cap']
        reserve=atoms(native.get('adjustment_candidate',{}).get('exit_reserve','0'))
        if remaining<native['cash_floor']+reserve:
            raise MachineError('Native capital, maximum fees and confirmed wallet cash floor exceed the observed balance.')

    def simulate_action(self,owner,native):
        return self.simulate_redeem(owner,native['shares']) if native.get('operation')=='REDEEM' else self.simulate(owner,native['amount'])

    def check_current_policy(self,context,state,native):
        m=state['workspace'].get('mandate');g=state['workspace'].get('graph')
        withdrawal=native.get('operation')=='REDEEM' and bool(native.get('adjustment_candidate'))
        if withdrawal:
            from .portfolio_review import withdrawal_policy
            m=withdrawal_policy(state)
        if not m or (m.get('status')!='CONFIRMED' and not (withdrawal and m.get('status')=='DRAFT')) or m['hash']!=g['mandate_hash'] or m['hash']!=native.get('policy_hash',g['mandate_hash']):
            raise MachineError('Confirmed conditions changed. Review the transaction again.')
        t=m['terms'];now=datetime.fromisoformat(self.clock())
        if not datetime.fromisoformat(t['effective_at'])<=now<datetime.fromisoformat(t['expires_at']):raise MachineError('Confirmed conditions expired. Review them before signing.')
        action=native.get('operation','SUPPLY')
        if action not in t['allowed_actions']:raise MachineError('This action is outside the confirmed permission.')
        if native['graph']['scope']!=context.scope:raise MachineError('Transaction belongs to a different wallet or network.')
        if native['total_fee_cap']>atoms(m['constraints']['max_fee']['value']):raise MachineError('Transaction exceeds the current fee permission.')
        ledger=[e for e in state.get('review_spend_ledger',{}).values() if e['policy_hash']==m['hash']]
        if any('fee' not in e for e in ledger):raise MachineError('Recorded fees must be reconciled first.')
        paid=sum(int(e['fee']) for e in ledger)
        reserve=atoms(native.get('adjustment_candidate',{}).get('exit_reserve','0'))
        if paid+native['total_fee_cap']+reserve>atoms(t['limits']['fee_amount']['amount']):raise MachineError('Transaction and exit reserves exceed remaining total fee permission.')

    def simulate_redeem(self,owner,shares):
        code=self.rpc('wallet/getcontract',{'value':MARKET,'visible':False});abi=verify_native_abi(code)
        entry=[e for e in code.get('abi',{}).get('entrys',[]) if e.get('name')=='redeem' and e.get('type')=='Function']
        if len(entry)!=1 or [i.get('type') for i in entry[0].get('inputs',[])]!=['uint256'] or [i.get('type') for i in entry[0].get('outputs',[])]!=['uint256']:
            raise MachineError('Pinned redemption ABI unavailable.')
        sim=self.rpc('wallet/triggerconstantcontract',{'owner_address':owner,'contract_address':MARKET,'function_selector':'redeem(uint256)','parameter':format(shares,'064x'),'visible':False})
        if sim.get('result',{}).get('result') is not True or sim.get('constant_result')!=['0'*64] or type(sim.get('energy_used')) is not int or sim['energy_used']<=0:raise MachineError('Live redemption simulation failed.')
        received=redeem_amount(sim.get('logs',[]),owner,shares)
        params=self.rpc('wallet/getchainparameters');resources=self.rpc('wallet/getaccountresource',{'address':owner,'visible':False})
        from .resource_costs import quote_resources,signed_bandwidth_bound
        data=keccak256(b'redeem(uint256)').hex()[:8]+format(shares,'064x')
        costs=quote_resources(sim['energy_used'],signed_bandwidth_bound(owner,MARKET,data,0,timestamp_ms=int(datetime.fromisoformat(self.clock()).timestamp()*1000)),params,resources)
        evidence={'code':code,'simulation':sim,'parameters':params,'resources':resources,'resource_costs':costs,'observed_at':self.clock()}
        return {'received':received,'energy_burn':sim['energy_used']*costs['energy_price_sun'],'bandwidth_bound':1024*costs['bandwidth_price_sun'],
                'expected_burn':costs['expected_total_burn_sun'],'resource_costs':costs,'abi_hash':abi,'hash':digest(evidence),'evidence':evidence}

    def review(self,context,state,intent,plan,snapshot):
        if context.network!='tron-nile' or len(intent['legs'])!=1 or intent['legs'][0]['product_id']!=PRODUCT:
            raise MachineError('Live direct execution currently supports one Nile native jTRX allocation.')
        terms=state['workspace']['mandate']['terms'];constraints=state['workspace']['mandate']['constraints']
        if terms['base_asset']!='TRX' or terms['borrowing']['consent']:raise MachineError('Native jTRX requires a TRX policy without borrowing.')
        amount=atoms(intent['legs'][0]['principal_amount']);cap=atoms(constraints['max_fee']['value'])
        if not amount or not cap or constraints['max_fee']['symbol']!='TRX' or cap>atoms(terms['limits']['fee_amount']['amount']):
            raise MachineError('Positive TRX principal and fee cap within the policy cost limit required.')
        # A direct mint has no minimum-output calldata. Keep a conservative 1% monitoring floor; disclose it.
        live=self.simulate(context.wallet,amount);minimum=live['shares']*99//100
        if cap<=live['bandwidth_bound'] or live['energy_burn']+live['bandwidth_bound']>cap:
            raise MachineError('Fee cap is below the live estimate '+money(live['energy_burn']+live['bandwidth_bound'])['value']+' TRX.')
        assumptions=state.get('planning_assumptions')
        if not assumptions or any((plan.get(k) or {}).get('symbol')!='TRX' for k in ('expected_net_return','estimated_fees')):
            raise MachineError('A bound TRX return calculation and reviewed cost assumptions are required.')
        projected_net=Decimal(plan['expected_net_return']['value'])
        if not projected_net.is_finite() or projected_net<=0:
            raise MachineError('Keep cash: this plan has no positive projected return after costs.')
        economics={'projected_income':decstr(projected_net+Decimal(plan['estimated_fees']['value'])),
            'other_costs':decstr(sum((Decimal(assumptions[k]) for k in ('entry_cost','exit_cost','conversion_cost')),Decimal(0))),
            'plan_hash':plan['hash']}
        self.economic_gate(economics,live)
        energy_cap=cap-live['bandwidth_bound'];expiry=intent['valid_until']
        account,account_evidence=self.account(context.scope,snapshot['snapshot_hash'],expiry)
        product=snapshot['products'][PRODUCT]
        binding={'schema_version':'tron-action-binding-1','operation':'JUSTLEND_SUPPLY_TRX','network':context.network,'target_address':MARKET,
            'capability_hash':product['capability_hash'],'abi_evidence_hash':live['abi_hash'],'contract_code_hash':CODE_SHA256,
            'status':'VERIFIED','source_url':'https://docs.justlend.org/developers/common_pitfalls/'}
        at=self.clock()
        quote={'schema_version':'economic-execution-quote-1','product_id':PRODUCT,'intent_hash':intent['intent_hash'],'snapshot_hash':snapshot['snapshot_hash'],
            'input':asset(amount),'minimum_output':asset(minimum,True),'fee_limit_sun':str(energy_cap),'quoted_at':live['evidence']['observed_at'],
            'expires_at':expiry,'simulation_status':'SIMULATED','simulation_evidence_hash':live['hash']}
        graph=compile_execution_graph(intent,snapshot,account,[quote],[],[binding],at=at,fee_budget_sun=str(cap))
        if graph['status']=='BLOCKED':raise MachineError('Native graph blocked: '+','.join(graph['blockers']))
        capital=atoms(terms['capital'][0]['amount']);floor=(capital*terms['immediate_cash']['value']+9999)//10000
        native={'graph':graph,'amount':amount,'total_fee_cap':cap,'bandwidth_bound':live['bandwidth_bound'],'cash_floor':floor,
            'policy_hash':state['workspace']['mandate']['hash'],'expected_fee_sun':live['expected_burn'],
            'economics':economics,
            'abi_hash':live['abi_hash'],'quote':quote,'binding':binding,'account_evidence':account_evidence,'review_simulation':live['evidence'],
            'estimated_fee':money(live['energy_burn']+live['bandwidth_bound']), 'submission':None}
        self.budget(native,account)
        state['native_execution']=native
        from .native_performance import forecast_for
        native['performance_forecast']=forecast_for(native,state['workspace'])
        step=next(x for x in graph['steps'] if x['kind']!='OFFCHAIN_RESERVATION')
        projection={'id':'native-'+graph['graph_hash'][:24],'hash':graph['graph_hash'],'plan_id':plan['id'],'plan_hash':plan['hash'],
            'mandate_hash':state['workspace']['mandate']['hash'],'network':'nile','account':address_base58(context.wallet),'expires_at':expiry,
            'enforcement_scope':'DIRECT_PROTOCOL','steps':[{'id':step['step_id'],'title':'Supply native TRX to JustLend','action':'mint() · payable TRX',
                'amount':money(amount),'recipient':address_base58(MARKET),'depends_on':[],'status':'READY','txid':None,'fee_cap':money(cap),
                'allowance_remaining':None,'error':None}],
            'disclosure':'Expected payment uses currently available resources; the full burn cap stays reserved and resource changes can raise the final fee. Native mint has no on-chain minimum-share guarantee. The service checks a 1% share-output tolerance after settlement; a mismatch is disputed, not reversed.',
            'estimated_fee':native['estimated_fee'],'expected_fee':money(live['expected_burn']),'resource_costs':live['resource_costs'],'minimum_shares':{'value':decstr(Decimal(minimum)/10**8),'symbol':'jTRX','decimals':8}}
        state['workspace']['graph']=projection;state['workspace']['approval']=None;state['workspace']['execution']=None

    def approve(self,context,state,payload):
        w=state['workspace'];g=w['graph'];native=state.get('native_execution')
        if not native or native.get('submission'):raise MachineError('Native execution review is unavailable or already submitted.')
        if any(payload.get(k)!=v for k,v in {'graph_id':g['id'],'graph_hash':g['hash'],'plan_hash':g['plan_hash'],'mandate_hash':g['mandate_hash'],'account':g['account'],'network':'nile'}.items()):
            raise MachineError('Approval scope differs from the reviewed graph.')
        self.check_current_policy(context,state,native)
        account,raw=self.account(context.scope,native['graph']['snapshot_hash'],g['expires_at']);live=self.simulate_action(context.wallet,native)
        manifest=self.preflight_manifest(native,account,live);at=self.clock()
        session={'schema_version':'economic-wallet-session-1','session_id':context.session_id,'scope':context.scope,
            'auth_context_hash':digest({'session':context.session_id,'scope':context.scope}),'issued_at':at,'expires_at':g['expires_at'],'status':'AUTHENTICATED'}
        card=prepare_approval_card(native['graph'],manifest,session,g['steps'][0]['id'],execution_path='DIRECT_WALLET',nonce='0',prepared_at=at,expires_at=g['expires_at'])
        approved=confirm_approval(card,session,{'approval_hash':card['approval_hash'],'session_id':session['session_id'],'auth_context_hash':session['auth_context_hash'],
            'wallet':context.wallet,'network':context.network,'decision':'APPROVE','confirmed_at':at})
        native.update(approved=approved,session=session,before=account,before_evidence=raw,approved_simulation=live['evidence'],manifest=manifest)
        w['approval']={'id':approved['approval_hash'],'graph_hash':g['hash'],'plan_hash':g['plan_hash'],'mandate_hash':g['mandate_hash'],
            'account':g['account'],'network':'nile','expires_at':g['expires_at'],'status':'APPROVED','consented_at':at}

    def prepare(self,context,graph_id,payload):
        version,state=self.bridge.load(context);w=state['workspace'];n=state.get('native_execution');g=w['graph'];a=w['approval']
        if not n or not a or n.get('submission') or graph_id!=g['id'] or payload.get('approval_id')!=a['id'] or payload.get('step_id')!=g['steps'][0]['id'] or payload.get('account')!=g['account']:
            raise MachineError('Approved native step changed or has already been submitted.')
        if n['session']['session_id']!=context.session_id or n['session']['scope']!=context.scope:raise MachineError('Wallet session changed.')
        fresh,raw=self.account(context.scope,n['graph']['snapshot_hash'],g['expires_at'])
        # Same approved balances and positions; never silently rebind an approval to changed capital.
        for key in ['balances','allowances','positions','vaults','reservations']:
            if fresh[key]!=n['before'][key]:raise MachineError('Approved account state changed. Review and approve a new graph.')
        self.check_current_policy(context,state,n)
        live=self.simulate_action(context.wallet,n);manifest=self.preflight_manifest(n,fresh,live)
        from .native_recovery import pending_requests
        pending = pending_requests(state)
        if pending:
            # Retrying the wallet prompt must reuse the exact transaction, not
            # create another independently broadcastable transaction.
            if len(pending) != 1 or pending[0][0] != (n.get('transaction') or {}).get('txID') or pending[0][1]['approval_id'] != a['id']:
                raise MachineError('A previous wallet request needs chain verification before preparing another transaction.')
            transaction = n['transaction']
            if transaction['raw_data']['expiration'] <= int(datetime.fromisoformat(self.clock()).timestamp()*1000):
                raise MachineError('The wallet request expired. It will unlock automatically after chain verification.')
            return self.prepared_projection(g,a,n,transaction,live)
        block=_block(self.rpc('wallet/getnowblock'));at=self.clock()
        anchor={'schema_version':'economic-tron-reference-block-1','network':context.network,'block_number':str(block['number']),'block_id':block['block_id'],
            'ref_block_bytes':block['number'].to_bytes(8,'big')[6:8].hex(),'ref_block_hash':bytes.fromhex(block['block_id'])[8:16].hex(),
            'observed_at':at,'valid_until':g['expires_at']}
        req=build_wallet_signature_request(n['approved'],n['session'],anchor,issued_at=at);transaction=unsigned_transaction(req)
        n.update(wallet_request=req,transaction=transaction,last_preflight=manifest,last_simulation=live['evidence'],last_account_evidence=raw)
        state.setdefault('native_requests',{})[transaction['txID']]={
            'graph_id':g['id'],'step_id':g['steps'][0]['id'],'approval_id':a['id'],
            'transaction':deepcopy(transaction),'prepared_at':at,'broadcast_attempted':False}
        self.bridge.commit(context,version,state)
        return self.prepared_projection(g,a,n,transaction,live)

    def prepared_projection(self,g,a,n,transaction,live):
        return {'graph_hash':g['hash'],'approval_id':a['id'],'step_id':g['steps'][0]['id'],'network':'nile','account':g['account'],'expires_at':g['expires_at'],
            'transaction':deepcopy(transaction),'simulation':False,'checks':[
                {'name':'Exact protocol call','status':'PASS','detail':'Pinned Nile jTRX bytecode and ABI; reviewed supply amount or redemption shares; no token approval.'},
                {'name':'Balance and reserve','status':'PASS','detail':'Approved wallet state and minimum cash floor verified at a stable solidified head.'},
                {'name':'Live simulation','status':'PASS','detail':'Protocol and share Transfer events agree; projected output meets the reviewed monitoring floor.'},
                {'name':'Maximum fee','status':'PASS','detail':'Estimated upper burn '+money(live['energy_burn']+live['bandwidth_bound'])['value']+' TRX; total cap '+money(n['total_fee_cap'])['value']+' TRX.'}]}

    def submit(self,context,payload):
        version,state=self.bridge.load(context);n=state.get('native_execution');w=state['workspace'];g=w.get('graph')
        if not n or not g or not n.get('wallet_request'):raise MachineError('Fresh native preflight is required.')
        tx=payload.get('signed_transaction',{})
        if n.get('submission'):
            if tx.get('txID')==n['submission']['txid']:return ACK
            raise MachineError('Reconcile the previous native transaction before signing another.')
        if payload.get('graph_id')!=g['id'] or payload.get('step_id')!=g['steps'][0]['id'] or payload.get('approval_id')!=w['approval']['id']:
            raise MachineError('Signed submission context changed.')
        if tx.get('raw_data')!=n['transaction']['raw_data'] or tx.get('raw_data_hex')!=n['transaction']['raw_data_hex'] or tx.get('txID')!=n['transaction']['txID']:
            raise MachineError('Displayed or signed transaction differs from the prepared bytes.')
        saved=state.get('native_requests',{}).get(tx['txID'],{})
        if saved.get('resolution'):raise MachineError('This prepared transaction is closed. Review and approve a fresh plan.')
        signed=normalize_wallet_signature({k:tx.get(k) for k in ['visible','txID','raw_data_hex','signature']})
        validation=validate_signed_transaction(signed,n['wallet_request'],n['session'],[],at=self.clock())
        fresh,_=self.account(context.scope,n['graph']['snapshot_hash'],n['approved']['expires_at'])
        if any(fresh[k]!=n['before'][k] for k in ['balances','positions','allowances','vaults','reservations']):
            raise MachineError('Wallet state changed after approval; no broadcast was attempted.')
        self.check_current_policy(context,state,n)
        live=self.simulate_action(context.wallet,n);self.preflight_manifest(n,fresh,live)
        n['broadcast_preflight']=live['evidence']
        record=prepare_submission(validation,at=self.clock());attempted=self.clock()
        attempt={'schema_version':'economic-tron-submission-attempt-1','attempt_id':'send-'+tx['txID'][:24],'txid':record['txid'],
            'signed_payload_hash':record['signed_payload_hash'],'attempted_at':attempted,'transport_status':'AFTER_SEND_UNKNOWN','node_result':'UNKNOWN',
            'response_hash':digest({'status':'DURABLE_BEFORE_SEND'}),'error_code':'AWAITING_NODE_RESPONSE'}
        record=record_broadcast_attempt(record,attempt);n['submission']=record
        if saved:saved['broadcast_attempted']=True
        self.project(state,'SUBMISSION_UNKNOWN','Signed transaction recorded. Waiting for node and solidified-chain evidence.')
        self.bridge.commit(context,version,state) # CAS lock is durable before any network side effect.
        try:response=self.rpc('wallet/broadcasttransaction',{**n['transaction'],'signature':signed['signature']})
        except Exception:return ACK # Unknown state survives network failure and process restart.
        version,state=self.bridge.load(context);n=state['native_execution']
        if n['submission']['txid']!=record['txid']:raise MachineError('Submission record changed unexpectedly.')
        attempt={**attempt,'attempt_id':'response-'+tx['txID'][:24],'response_hash':digest(response),'transport_status':'NODE_RESPONSE',
            'node_result':'ACCEPTED' if response.get('result') is True else 'REJECTED','error_code':None}
        n['node_response']=response;n['submission']=record_broadcast_attempt(n['submission'],attempt)
        self.project(state,'SUBMITTED' if response.get('result') is True else 'SUBMISSION_UNKNOWN',
            'Node accepted the transaction; awaiting solidified receipt and position.' if response.get('result') is True else 'Node rejected the request. The original transaction remains locked until chain reconciliation establishes its outcome.')
        self.bridge.commit(context,version,state)
        return ACK

    def project(self,state,status,message):
        w=state['workspace'];n=state['native_execution'];txid=n['submission']['txid'];at=self.clock()
        w['graph']['steps'][0].update(status=status if status!='DISPUTED' else 'BLOCKED',txid=txid,error=message if status=='DISPUTED' else None)
        w['execution']={'id':txid,'graph_id':w['graph']['id'],'status':status,'network':'nile',
            'started_at':n['submission']['prepared_at'],'updated_at':at,'message':message}

    def reconcile_unsubmitted(self,context,version,state,payload):
        """Close only a known prepared transaction past solidified expiry.

        This also recovers a local browser pointer after pre-broadcast validation
        failed or a later review replaced the active graph. Absence at one head
        while a signed transaction is still valid is never sufficient.
        """
        txid=(payload or {}).get('txid')
        row=state.get('native_requests',{}).get(txid)
        if not row or any(payload.get(k)!=row[k] for k in ['graph_id','step_id']):
            raise MachineError('No matching prepared transaction is recorded for this wallet. Recovery needs its original preflight record.')
        if row.get('resolution'):return {**ACK,'resolution':deepcopy(row['resolution'])}
        if row.get('broadcast_attempted'):
            raise MachineError('A broadcast attempt is recorded. Receipt reconciliation is required.')
        transaction=row['transaction'];decoded=decode_trigger_raw(bytes.fromhex(transaction['raw_data_hex']))
        if hashlib.sha256(bytes.fromhex(transaction['raw_data_hex'])).hexdigest()!=txid or decoded['owner_address']!=context.wallet:
            raise MachineError('Prepared recovery record does not match the authenticated wallet.')
        expires=decoded['expiration_ms'];now=int(datetime.fromisoformat(self.clock()).timestamp()*1000)
        if now<=expires+60000:raise MachineError('The signed transaction is still within its expiry safety window. Reconcile again after it expires.')
        captured=[]
        def transport(kind,path,body):
            result=self.rpc(path.lstrip('/'),body);captured.append({'path':path,'request':body,'response':result});return result
        observed=read_tron_transaction(txid,READER,now_ms=now,transport=transport)
        full_tx=self.rpc('wallet/gettransactionbyid',{'value':txid})
        full_receipt=self.rpc('wallet/gettransactioninfobyid',{'value':txid})
        if observed['status']!='NOT_OBSERVED' or full_tx or full_receipt:
            raise MachineError('The original transaction is visible to a node. Its recovery remains locked for receipt verification.')
        if min(observed['solid_tip_before']['timestamp_ms'],observed['solid_tip_after']['timestamp_ms'])<=expires+60000:
            raise MachineError('Wait for the solidified chain to pass this transaction expiry before recovering it.')
        resolution={'txid':txid,'graph_id':row['graph_id'],'step_id':row['step_id'],
            'status':'EXPIRED_NOT_OBSERVED','message':'The prepared transaction expired without a broadcast record or a full/solid node transaction. Review a fresh plan before signing again.'}
        row.update(resolution=resolution,recovered_at=self.clock(),recovery_evidence={
            'observation':observed,'reads':captured,'full_transaction':full_tx,'full_receipt':full_receipt})
        # Fences any concurrently validating submit: its stale CAS cannot commit
        # the durable broadcast lock, so it must not reach the network call.
        self.bridge.commit(context,version,state)
        return {**ACK,'resolution':deepcopy(resolution)}

    def reconcile(self,context,payload=None):
        version,state=self.bridge.load(context);n=state.get('native_execution');w=state['workspace']
        if not n or not n.get('submission') or (payload and payload.get('txid')!=n['submission']['txid']):
            return self.reconcile_unsubmitted(context,version,state,payload)
        txid=n['submission']['txid']
        if payload and (payload.get('txid')!=txid or payload.get('graph_id')!=w['graph']['id'] or payload.get('step_id')!=w['graph']['steps'][0]['id']):
            raise MachineError('Reconciliation pointer differs from the recorded transaction.')
        if w.get('execution',{}).get('status') in TERMINAL:return ACK
        captured=[]
        def transport(kind,path,body):
            result=self.rpc(path.lstrip('/'),body);captured.append({'path':path,'request':body,'response':result});return result
        observation=read_tron_transaction(txid,READER,now_ms=int(datetime.fromisoformat(self.clock()).timestamp()*1000),transport=transport)
        evidence={'schema_version':'economic-tron-execution-observation-evidence-1','network':context.network,'reader_config_hash':digest(READER),'source_id':'nile-solid-node','observation':observation}
        result=assess_execution_observation(n['submission'],evidence);n['execution_result']=result;n['execution_observation']=observation;n['receipt_reads']=captured;n['last_reconciled_at']=self.clock()
        if result['status']=='SOLID_EXECUTED_PENDING_POST_STATE':
            redeem=n.get('operation')=='REDEEM'
            try:actual=redeem_amount(observation['details']['logs'],context.wallet,n['shares']) if redeem else mint_shares(observation['details']['logs'],context.wallet,n['amount'])
            except MachineError as exc:
                self.project(state,'DISPUTED',str(exc))
                self.bridge.commit(context,version,state)
                return ACK
            expiry=(datetime.fromisoformat(self.clock())+timedelta(minutes=5)).isoformat()
            after,raw=self.account(context.scope,n['graph']['snapshot_hash'],expiry)
            post={'schema_version':'economic-post-state-evidence-1','network':context.network,'txid':txid,'block_number':raw['block']['number'],
                'block_id':raw['block']['block_id'],'observed_at':after['observed_at'],'source_hash':digest(raw),'completeness':'REQUIRED_FIELDS_COMPLETE','account':after}
            if result['resource_receipt']['total_fee_sun'] is None:raise MachineError('Actual receipt fee is unavailable; position remains pending.')
            reconciliation=reconcile_position(n['graph'],n['approved'],result,n['before'],post,at=self.clock())
            share_delta=int(after['positions'][0]['shares_base_units'])-int(n['before']['positions'][0]['shares_base_units'])
            fee=int(result['resource_receipt']['total_fee_sun'])
            n.update(post_state=post,post_state_raw=raw,reconciliation=reconciliation)
            exact_delta=(share_delta==-n['shares'] and int(after['balances'][0]['amount_base_units'])-int(n['before']['balances'][0]['amount_base_units'])+fee==actual) if redeem else share_delta==actual
            if reconciliation['status']=='RECONCILED' and exact_delta and fee<=n['total_fee_cap']:
                self.project(state,'POSITION_RECONCILED','Solidified transaction, Mint event, exact TRX debit including fees, and independent jTRX share balance agree.')
                if redeem:n['actual_received']=actual;w['execution']['message']='Solidified redemption, exact share decrease, TRX proceeds and actual fee agree.'
                state.setdefault('review_spend_ledger',{})[txid]={'policy_hash':w['graph']['mandate_hash'],'amount':'0' if redeem else str(n['amount']),'fee':str(fee),'action':'REDEEM' if redeem else 'SUPPLY'}
                principal=int(after['positions'][0]['underlying_base_units'])
                existing=next((p for p in w['positions'] if p['id']=='nile-jtrx'),None)
                prior_principal=atoms(existing['principal']['value']) if existing else 0
                w['positions']=[p for p in w['positions'] if p['id']!='nile-jtrx']+[{'id':'nile-jtrx','product':PRODUCT,'protocol':'JustLend','network':'nile','provenance':'LIVE',
                    'principal':None if redeem else money(prior_principal+n['amount']),'current_value':money(principal),'debt':money(int(raw['debt'])),
                    'exit_status':'Redeem requires a separately reviewed transaction. Market liquidity may change.','receipt_txid':txid}]
                w['balances']=[money(int(after['balances'][0]['amount_base_units']))]
                w['performance']={'network':'nile','provenance':'LIVE','as_of':self.clock(),'expected_return':None,'accrued':None,'realized':None,
                    'rewards':None,'price_pnl':None,'debt_cost':None,'fees':money(sum(int(x['fee']) for x in state['review_spend_ledger'].values())) if all('fee' in x for x in state['review_spend_ledger'].values()) else None,'net_deposits':money(sum(int(x['amount']) for x in state['review_spend_ledger'].values()))}
                from .native_performance import refresh as refresh_performance
                performance=refresh_performance(state,after,raw,self.clock())
                if performance and performance['status']=='RECONCILED':
                    for position in w['positions']:
                        if position['id']=='nile-jtrx':position['principal']=performance['open_cost_basis']
                if not int(after['positions'][0]['shares_base_units']) and not int(raw['debt']):w['positions']=[p for p in w['positions'] if p['id']!='nile-jtrx']
            else:self.project(state,'DISPUTED','Receipt and post-state checks differ. Further execution is blocked; inspect the evidence.')
        elif result['status']=='SOLID_EXECUTION_FAILED':
            self.project(state,'FAILED','A solidified failed execution was observed; its actual fee is retained in performance.')
            from .native_performance import record_failure, refresh as refresh_performance
            record_failure(state,n)
            fee=result['resource_receipt']['total_fee_sun']
            state.setdefault('review_spend_ledger',{})[txid]={'policy_hash':w['graph']['mandate_hash'],'amount':'0','fee':str(fee),'action':'FAILED'}
            expiry=(datetime.fromisoformat(self.clock())+timedelta(minutes=5)).isoformat()
            after,raw=self.account(context.scope,n['graph']['snapshot_hash'],expiry)
            refresh_performance(state,after,raw,self.clock())
        else:
            # Do not infer non-submission from a timeout or one empty node response.
            self.project(state,w['execution']['status'],'Waiting for solidified receipt. The original transaction remains reserved.')
        if w['execution']['status'] in TERMINAL:
            w['evidence'].append({'id':'native-'+txid,'network':'nile','provenance':'LIVE','mandate_hash':w['graph']['mandate_hash'],
                'snapshot_root':n['graph']['snapshot_hash'],'plan_hash':n['graph']['plan_hash'],'graph_hash':n['graph']['graph_hash'],
                'approval_id':n['approved']['approval_hash'],'txids':[txid],'outcome':w['execution']['message'],
                'model_id':None,'model_approval_evidence':None,'flows':[],
                'energy':{'status':'UNAVAILABLE','wh':None,'basis':'On-chain Energy units are not measured electricity consumption.'},
                'inputs':{'mandate':deepcopy(n.get('policy_projection') or w['mandate']),'comparison':deepcopy(w['comparison']),'graph':deepcopy(w['graph']),'approval':deepcopy(w['approval']),'performance':deepcopy(w['performance'])},
                'calculation':{'native_execution':deepcopy(n)}})
            w['evidence']=w['evidence'][-20:]
        self.bridge.commit(context,version,state)
        return ACK
