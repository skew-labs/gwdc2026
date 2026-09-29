"""Offline adversarial USDD service tests. Synthetic rates, balances and keys.

These tests exercise real approval/unsigned encoding/signature verification and
CAS persistence. RPC state transitions are synthetic, never live-chain proof.
"""
import copy,json,tempfile,unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from economic_machine.values import MachineError,digest,canonical
from economic_machine.usdd_workflow import CONFIG,addr,word
from economic_machine.tron_sources import address_hex
from economic_machine.tron_crypto import keccak256
from finance_service.machine_bridge import MachineBridge,empty_workspace
from finance_service.operational_repository import OperationalRepository
from finance_service.context import AuthenticatedContext
from finance_service.usdd_execution import UsddExecution
from test_portfolio_review import policy,AT
from test_economic_tron_execution import wallet_for,sign_hash
KEY=int.from_bytes(b'\x39'*32,'big')

class FakeChain:
    def __init__(self,test):
        self.test=test;self.network='tron-mainnet';self.root='https://example.invalid';self.broadcasts=0;self.timeout=False
        self.current={'network':'tron-mainnet','owner':test.ctx.wallet,'token':address_hex(CONFIG['tron-mainnet']['token']),
            'join_token':address_hex(CONFIG['tron-mainnet']['token']),'trx_balance':'1000000000','token_balance':str(1000*10**18),
            'shares':'0','market_debt':'0','market_allowance':'0','proxy_allowance':'0','membership':False,
            'proxy':None,'proxy_owner':None,'cdp_id':None,'cdp_owner':None,'ilk':None,'vault_debt':'0','collateral_wad':'0',
            'exchange_rate':str(10**26),'market_cash':str(1000000*10**18),'observed_at':AT}
    def snapshot(self,wf=None):return copy.deepcopy({**self.current,'observed_at':self.test.now,'block_to':{'number':900}})
    def head(self,solid=True):return {'number':900,'block_id':word(900)[:0]+format(900,'016x')+'12'*24,'timestamp_ms':int(datetime.fromisoformat(self.test.now).timestamp()*1000)-3000}
    def simulate(self,call,cap):
        owner=addr(self.test.ctx.wallet);a=int(call['amount']);m=address_hex(CONFIG['tron-mainnet']['market'])[2:];logs=[]
        def event(signature,values):return {'address':m,'topics':[keccak256(signature.encode()).hex()],'data':owner+''.join(word(v) for v in values)}
        if call['operation']=='SUPPLY':logs=[event('Mint(address,uint256,uint256)',[a,a//10**8])]
        if call['operation']=='BORROW':logs=[event('Borrow(address,uint256,uint256,uint256)',[a,int(self.current['market_debt'])+a,10**24])]
        if call['operation']=='REPAY_ALL':logs=[{'address':m,'topics':[keccak256(b'RepayBorrow(address,address,uint256,uint256,uint256)').hex()],
            'data':owner+owner+word(int(self.current.get('accrued_market_debt',self.current['market_debt'])))+word(0)+word(10**24)}]
        if call['operation']=='REDEEM_SHARES':logs=[event('Redeem(address,uint256,uint256)',[a*10**8,a])]
        return {'codes':{call['target']:{'abi_hash':'a'*64,'bytecode_hash':'b'*64}},'observed_at':self.test.now,
            'simulation':{'logs':logs},'costs':{'full_total_burn_bound_sun':10000,'full_energy_burn_sun':1000,
                'expected_total_burn_sun':1000,'bandwidth_bytes_bound':1024,'bandwidth_price_sun':1}}
    def rpc(self,path,body=None):
        if path.endswith('broadcasttransaction'):
            state=self.test.bridge.load(self.test.ctx)[1]
            self.test.assertEqual(state['workspace']['execution']['status'],'SUBMISSION_UNKNOWN')
            self.broadcasts+=1
            if self.timeout:raise TimeoutError('Synthetic lost response')
            return {'result':True}
        return {}

class UsddExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.addCleanup(patch.stopall)
        self.now=AT;self.ctx=AuthenticatedContext(tenant_id='test',owner_id='owner',wallet=wallet_for(KEY),network='tron-mainnet',
            session_id='usdd-test',trace_id='test',issued_at=AT,expires_at='2026-10-01T00:00:00+00:00')
        self.bridge=MachineBridge(OperationalRepository(Path(self.tmp.name)/'db.sqlite'),lambda:self.now,None)
        self.svc=self.bridge.usdd_execution;self.chain=FakeChain(self);self.svc.chain=lambda _:self.chain
        self.bridge.observations=SimpleNamespace(request=lambda *a:None)
        facts={'token_match':True,'supply_apy':'0.02','borrow_apy':'0.05','collateral_factor':'0.85','market_cash_usdd':'1000000',
            'collaterals':[{'ilk':'TRX-C','stability_apy':'0.005','liquidation_ratio':'1.3','minimum_debt_usdd':'500','debt_ceiling_remaining_usdd':'1000000','debt_capacity_per_trx':'0.25'}]}
        self.bridge.usdd=SimpleNamespace(refresh=lambda *args:{'facts':copy.deepcopy(facts),'blockers':[]})
        patch('finance_service.usdd_execution.read_market',lambda *args:({'trx_usd':'0.3','usdd_usd':'1','reward_apr':'0.1','market_observed_at':self.now},{})).start()
        p=policy();t=p['mandate']['terms'];p['mandate']['scope']=self.ctx.scope
        t.update(base_asset='USDD',capital=[{'asset':'USDD','amount':'1000'}],horizon_seconds=31536000,expires_at='2027-09-29T12:00:00+00:00',price_exposure_caps_bps={'USDD':10000,'TRX':10000})
        t['limits']={k:{'asset':'USDD','amount':v} for k,v in {'single_amount':'1000','cumulative_amount':'1000','fee_amount':'100','daily_loss':'1000','stress_loss':'1000'}.items()}
        t['borrowing']['max_debt']={'asset':'USDD','amount':'0'}
        t['allowed_actions']+=['CLAIM','REPAY','WITHDRAW_COLLATERAL']
        w=empty_workspace('mainnet');w['mandate']={'hash':p['policy_hash'],'status':'CONFIRMED','terms':t,'constraints':p['review_inputs']['constraints']}
        self.state={'workspace':w,'mandates':{'m':{'revisions':[p]}},'requests':{},'confirmed_review_inputs':{p['policy_hash']:p['review_inputs']}}
        self.payload={'mode':'OWNED','amount_usdd':'600','loops':0,'borrow_bps':0,'per_step_fee_cap_trx':'1','total_fee_cap_trx':'20','network':'mainnet'}
    def reviewed(self):
        self.svc.create(self.ctx,self.state,self.payload);self.bridge.commit(self.ctx,0,self.state)
        return self.state['workspace']['graph']
    def prepared(self):
        g=self.reviewed();version,state=self.bridge.load(self.ctx)
        self.svc.approve(self.ctx,state,{k:v for k,v in {'graph_id':g['id'],'graph_hash':g['hash'],'plan_hash':g['plan_hash'],'mandate_hash':g['mandate_hash'],'account':g['account'],'network':'mainnet'}.items()})
        self.bridge.commit(self.ctx,version,state);a=state['workspace']['approval']
        result=self.svc.prepare(self.ctx,g['id'],{'approval_id':a['id'],'step_id':g['steps'][0]['id'],'account':g['account']})
        tx=copy.deepcopy(result['transaction']);tx['signature']=[sign_hash(bytes.fromhex(tx['txID']),KEY).hex()]
        return {'graph_id':g['id'],'step_id':g['steps'][0]['id'],'approval_id':a['id'],'signed_transaction':tx},result
    def test_exact_approve_amount_is_signed_and_tampering_never_broadcasts(self):
        p,r=self.prepared();tx=r['transaction'];value=tx['raw_data']['contract'][0]['parameter']['value']
        self.assertEqual(value['contract_address'],address_hex(CONFIG['tron-mainnet']['token']))
        self.assertEqual(int(value['data'][-64:],16),600*10**18)
        bad=copy.deepcopy(p);bad['signed_transaction']['raw_data']['contract'][0]['parameter']['value']['call_value']=1
        with self.assertRaises(MachineError):self.svc.submit(self.ctx,bad)
        bad=copy.deepcopy(p);bad['signed_transaction']['signature']=[sign_hash(bytes.fromhex(tx['txID']),KEY+1).hex()]
        with self.assertRaises(MachineError):self.svc.submit(self.ctx,bad)
        self.assertEqual(self.chain.broadcasts,0)

    def test_watch_uses_fresh_usdd_debt_and_deduplicates_breach_alerts(self):
        from finance_service.portfolio_review import PortfolioReviews
        self.reviewed();_,state=self.bridge.load(self.ctx);wf=state['usdd_execution']
        wf.update(status='COMPLETE',cursor=2,confirmed_txids=['c'*64])
        self.chain.current.update(shares=str(600*10**10),token_balance=str(400*10**18))
        with patch('finance_service.usdd_portfolio.read_market',lambda *a:({'trx_usd':'0.3','usdd_usd':'1','reward_apr':'0.1','market_observed_at':self.now},{})):
            reviews=PortfolioReviews(self.bridge);r=reviews.refresh(self.ctx,state,source='ROUTINE')
            self.assertEqual(r['status'],'HOLD',r['reason'])
            self.assertEqual(r['denomination'],'USDD');self.assertEqual(r['hold']['position'],'600')
            self.chain.current['accrued_market_debt']=str(2*10**18)
            r=reviews.refresh(self.ctx,state,source='ROUTINE')
            self.assertEqual(r['status'],'POLICY_BREACH');self.assertIn('DEBT_LIMIT_BREACH',r['checks'])
            reviews.refresh(self.ctx,state,source='ROUTINE')
            self.assertEqual(len([n for n in state['workspace']['notifications'] if n['kind']=='POLICY_BREACH']),1)
        self.assertEqual(self.chain.broadcasts,0)
    def test_changed_wallet_or_contract_blocks_approval(self):
        g=self.reviewed();_,state=self.bridge.load(self.ctx);self.chain.current['token_balance']='1'
        with self.assertRaisesRegex(MachineError,'changed'):self.svc.approve(self.ctx,state,{})
        self.assertIsNone(state['workspace']['approval']);self.assertEqual(self.chain.broadcasts,0)
    def test_write_ahead_timeout_and_replay_survive_restart(self):
        p,_=self.prepared();self.chain.timeout=True;self.svc.submit(self.ctx,p)
        restarted=MachineBridge(OperationalRepository(self.bridge.repository.path),lambda:self.now,None)
        restarted.usdd_execution.chain=lambda _:self.chain
        restarted.usdd_execution.submit(self.ctx,p)
        self.assertEqual(self.chain.broadcasts,1)
        state=restarted.load(self.ctx)[1]
        self.assertEqual(state['usdd_execution']['cursor'],0)
        with self.assertRaises(MachineError):restarted.usdd_execution.next(self.ctx,state)
        with self.assertRaises(MachineError):restarted.usdd_execution.cancel(self.ctx,state)
    def test_unconfirmed_success_does_not_unlock_next_step(self):
        p,_=self.prepared();self.svc.submit(self.ctx,p)
        with patch('finance_service.usdd_execution.read_tron_transaction',return_value={'status':'NOT_OBSERVED'}):
            self.svc.reconcile(self.ctx,{k:p[k] for k in ('graph_id','step_id')}|{'txid':p['signed_transaction']['txID']})
        state=self.bridge.load(self.ctx)[1]
        self.assertEqual(state['usdd_execution']['cursor'],0)
        self.assertEqual(state['usdd_execution']['steps'][1]['status'],'WAITING')
    def test_solid_receipt_unlocks_new_approval_without_duplicate_fee(self):
        p,_=self.prepared();self.svc.submit(self.ctx,p);self.chain.current['market_allowance']=str(600*10**18)
        self.chain.current['trx_balance']=str(1000000000-1000)
        tx=p['signed_transaction']['txID'];pointer={k:p[k] for k in ('graph_id','step_id')}|{'txid':tx}
        def read(*a,**kw):
            receipt={'receipt':{'result':'SUCCESS'},'fee':1000,'log':[]}
            original=self.chain.rpc;self.chain.rpc=lambda path,body=None:receipt if path.endswith('gettransactioninfobyid') else original(path,body)
            kw['transport']('solidity','/walletsolidity/gettransactioninfobyid',{'value':tx})
            return {'status':'SOLID_EXECUTED','details':{'call_target_address':address_hex(CONFIG['tron-mainnet']['token'])}}
        with patch('finance_service.usdd_execution.read_tron_transaction',side_effect=read):self.svc.reconcile(self.ctx,pointer)
        self.svc.reconcile(self.ctx,pointer)
        version,state=self.bridge.load(self.ctx);self.assertEqual(state['usdd_execution']['spent_fees'],'1000')
        self.assertEqual(state['usdd_execution']['cursor'],1)
        self.svc.next(self.ctx,state)
        self.assertEqual(state['workspace']['graph']['steps'][0]['action'],'mint(uint256)')
        self.assertIsNone(state['workspace']['approval'])
    def test_debt_consent_and_loss_gate_prevent_any_graph_or_network_write(self):
        self.payload.update(loops=1,borrow_bps=5000)
        with self.assertRaisesRegex(MachineError,'permit'):self.svc.create(self.ctx,self.state,self.payload)
        self.assertIsNone(self.state['workspace']['graph'])
        self.payload.update(loops=0,borrow_bps=0)
        self.state['mandates']['m']['revisions'][0]['mandate']['terms']['horizon_seconds']=86400
        with self.assertRaisesRegex(MachineError,'Keep cash'):self.svc.create(self.ctx,self.state,self.payload)
        self.assertEqual(self.chain.broadcasts,0)
    def test_unsubmitted_review_can_cancel_but_prepared_tx_cannot(self):
        self.reviewed();version,state=self.bridge.load(self.ctx);self.svc.cancel(self.ctx,state)
        self.assertEqual(state['usdd_execution']['status'],'CANCELLED')
        self.assertIsNone(state['workspace']['graph'])
    def test_expired_prepared_transaction_needs_solid_absence_evidence(self):
        p,r=self.prepared();pointer={k:p[k] for k in ('graph_id','step_id')}|{'txid':r['transaction']['txID']}
        with self.assertRaisesRegex(MachineError,'remains valid'):self.svc.reconcile(self.ctx,pointer)
        self.now=(datetime.fromtimestamp(r['transaction']['raw_data']['expiration']/1000,datetime.fromisoformat(AT).tzinfo)+timedelta(minutes=2)).isoformat()
        result=self.svc.reconcile(self.ctx,pointer)
        self.assertEqual(result['resolution']['status'],'EXPIRED_NOT_OBSERVED')
        self.assertEqual(self.bridge.load(self.ctx)[1]['usdd_execution']['status'],'AWAITING_NEXT_REVIEW')

    def test_recovery_binds_accrued_debt_and_never_resumes_borrowing(self):
        self.reviewed();_,state=self.bridge.load(self.ctx);wf=state['usdd_execution']
        wf.update(status='NEEDS_RECOVERY',confirmed_txids=['c'*64])
        self.chain.current.update(market_debt=str(100*10**18),accrued_market_debt=str(101*10**18),shares=str(1000*10**8))
        self.svc.recover(self.ctx,state,{})
        wf=state['usdd_execution'];steps=wf['steps'][wf['cursor']:]
        self.assertEqual([s['operation'] for s in steps],['APPROVE_MARKET','REPAY_ALL'])
        self.assertGreater(int(steps[0]['amount']),101*10**18)
        self.assertEqual(wf['phase'],'RECOVERY')
        self.assertIsNone(state['workspace']['approval'])
        self.assertEqual(self.chain.broadcasts,0)

    def test_expired_conditions_require_new_confirmation_then_allow_debt_reduction(self):
        self.reviewed();_,state=self.bridge.load(self.ctx)
        state['usdd_execution'].update(status='COMPLETE',cursor=2,confirmed_txids=['c'*64])
        self.chain.current.update(shares=str(600*10**8),token_balance=str(400*10**18))
        record=state['mandates']['m']['revisions'][0];old_hash=record['policy_hash']
        record['mandate']['terms']['expires_at']=AT
        with self.assertRaisesRegex(MachineError,'expired'):self.svc.recover(self.ctx,state,{})
        record['mandate']['terms']['expires_at']='2027-09-29T12:00:00+00:00'
        record['policy_hash']='d'*64;state['workspace']['mandate']['hash']=record['policy_hash']
        self.svc.recover(self.ctx,state,{})
        self.assertNotEqual(state['usdd_execution']['policy_hash'],old_hash)
        self.assertEqual(state['usdd_execution']['steps'][-1]['operation'],'REDEEM_SHARES')
        self.assertEqual(self.chain.broadcasts,0)

    def test_live_debt_limit_is_checked_again_before_supply(self):
        self.reviewed();_,state=self.bridge.load(self.ctx);wf=state['usdd_execution']
        self.chain.current.update(market_debt=str(10**18),accrued_market_debt=str(2*10**18))
        with self.assertRaisesRegex(MachineError,'debt limit'):
            self.svc.live_entry_check(self.ctx,state,wf,{'operation':'SUPPLY','amount':str(600*10**18)},self.chain.snapshot())

    def test_unexpected_solid_receipt_is_persisted_as_disputed(self):
        p,_=self.prepared();self.svc.submit(self.ctx,p)
        tx=p['signed_transaction']['txID'];pointer={k:p[k] for k in ('graph_id','step_id')}|{'txid':tx}
        original=self.chain.rpc
        self.chain.rpc=lambda path,body=None:{'fee':1000,'log':[]} if path.endswith('gettransactioninfobyid') else original(path,body)
        def read(*args,**kw):
            kw['transport']('solidity','/walletsolidity/gettransactioninfobyid',{'value':tx})
            return {'status':'SOLID_EXECUTED','details':{'call_target_address':address_hex(CONFIG['tron-mainnet']['token'])}}
        with patch('finance_service.usdd_execution.read_tron_transaction',side_effect=read),patch.object(self.svc,'check_simulation',side_effect=MachineError('Unexpected receipt event')):
            self.svc.reconcile(self.ctx,pointer)
        self.svc.reconcile(self.ctx,pointer)
        _,state=self.bridge.load(self.ctx)
        self.assertEqual(state['usdd_execution']['status'],'DISPUTED')
        self.assertEqual(state['usdd_execution']['spent_fees'],'1000')
        self.assertEqual(state['usdd_execution']['cursor'],0)
        self.assertEqual(state['workspace']['graph']['steps'][0]['status'],'BLOCKED')
        with self.assertRaises(MachineError):self.svc.recover(self.ctx,state,{})

if __name__=='__main__':unittest.main()
