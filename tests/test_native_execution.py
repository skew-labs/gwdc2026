"""Synthetic offline adversarial adapter tests; no live signing or funds."""
import copy,hashlib,json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
from finance_service.machine_bridge import MachineBridge,empty_workspace
from finance_service.operational_repository import OperationalRepository
from finance_service.context import AuthenticatedContext
from finance_service.native_execution import NativeExecution,MARKET,PRODUCT,MINT,TRANSFER,mint_shares,atoms
from economic_machine.values import canonical,digest,MachineError
from economic_machine.capabilities import capability_hash,product_id
from economic_machine.signed_tx_validation import decode_trigger_raw
from economic_machine.tron_execution import assess_execution_observation
import test_economic_tx_graph as f
from test_economic_tron_execution import wallet_for,sign_hash,observation

TEST_KEY=int.from_bytes(b'\x31'*32,'big') # Synthetic deterministic test key only.

class NativeExecutionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.now=f.AT;self.broadcasts=0;self.timeout=False;self.bad_mint=False;self.energy=80894
  self.scope={**f.SCOPE,'network':'tron-nile','wallet':wallet_for(TEST_KEY)}
  self.ctx=AuthenticatedContext(**self.scope,session_id='native-test',trace_id='native-test',issued_at='2026-09-28T12:00:00+00:00',expires_at='2026-09-28T13:00:00+00:00')
  self.repo=OperationalRepository(Path(self.tmp.name)/'db.sqlite');self.bridge=MachineBridge(self.repo,lambda:self.now,None)
  self.native=NativeExecution(self.bridge,self.rpc);self.bridge.native=self.native
  self.addCleanup(patch.stopall);patch('finance_service.native_execution.CODE_SHA256',hashlib.sha256(bytes.fromhex('6001')).hexdigest()).start()
  cap=f.cap(asset='TRX',token=None);cap['network']='tron-nile';cap['contract']=MARKET
  product=f.product(PRODUCT,capability=cap,share_decimals=8);snap={'scope':self.scope,'products':{PRODUCT:product}};snap['snapshot_hash']=digest(snap)
  old=f.SCOPE;f.SCOPE=self.scope
  try:it=f.intent(snap,product,amount='1')
  finally:f.SCOPE=old
  it['legs'][0]['principal_asset']='TRX';it.pop('intent_hash');it['intent_hash']=digest({'domain':it['schema_version'],'payload':it})
  self.intent,self.snapshot=it,snap
  w=empty_workspace('nile');w['mandate']={'hash':'1'*64,'status':'CONFIRMED','terms':{'effective_at':'2026-09-28T12:00:00+00:00','expires_at':'2026-09-28T13:00:00+00:00','allowed_actions':['SUPPLY','REDEEM'],'base_asset':'TRX','borrowing':{'consent':False},'capital':[{'asset':'TRX','amount':'1000'}],
    'immediate_cash':{'kind':'BPS','value':3000},'limits':{'fee_amount':{'asset':'TRX','amount':'15'}}},'constraints':{'max_fee':{'value':'15','symbol':'TRX','decimals':6}}}
  self.state={'workspace':w,'mandates':{},'requests':{},'planning_assumptions':{'entry_cost':'0','exit_cost':'2','conversion_cost':'0','network_cost':'13'}}
  # Synthetic positive economics isolates signing mechanics; no market return claim.
  self.plan={'id':'CONSERVATIVE','hash':it['plan_hash'],'expected_net_return':{'value':'5','symbol':'TRX'},'estimated_fees':{'value':'15','symbol':'TRX'}}
 def logs(self,amount):
  shares=amount*100;word=lambda x:format(x,'064x')
  return [{'address':MARKET[2:],'topics':[MINT],'data':self.scope['wallet'][2:].rjust(64,'0')+word(amount)+word(shares)},
   {'address':MARKET[2:],'topics':[TRANSFER,MARKET[2:].rjust(64,'0'),self.scope['wallet'][2:].rjust(64,'0')],'data':word(shares)}]
 def rpc(self,url,body):
  path=url.split('/')[-1]
  if path=='getcontract':value={'contract_address':MARKET,'bytecode':'6001','abi':{'entrys':[{'name':'mint','type':'Function','stateMutability':'Payable'}]}}
  elif path=='getchainparameters':value={'chainParameter':[{'key':'getEnergyFee','value':100},{'key':'getTransactionFee','value':1000}]}
  elif path=='getaccountresource':value={'EnergyLimit':0,'freeNetLimit':600,'freeNetUsed':0}
  elif path=='getnowblock':value={'blockID':(900).to_bytes(8,'big').hex()+'12'*24,'block_header':{'raw_data':{'number':900,'timestamp':int(datetime.fromisoformat(self.now).timestamp()*1000)-3000}}}
  elif path=='getaccount':value={'address':self.scope['wallet'],'balance':1000*10**6}
  elif path=='triggerconstantcontract':
   selector=body['function_selector']
   if selector=='mint()':value={'result':{'result':True},'constant_result':[''],'energy_used':self.energy,'logs':[] if self.bad_mint else self.logs(body['call_value'])}
   elif selector=='getAccountSnapshot(address)':value={'result':{'result':True},'constant_result':[''.join(format(x,'064x') for x in [0,0,0,10**16])]}
   elif selector=='getCash()':value={'result':{'result':True},'constant_result':[format(10**15,'064x')]}
   else:raise AssertionError(selector)
  elif path=='broadcasttransaction':
   # A committed lock must already exist when the network transport is invoked.
   assert self.bridge.load(self.ctx)[1]['workspace']['execution']['status']=='SUBMISSION_UNKNOWN'
   assert body['signature'][0]==body['signature'][0].lower()
   self.broadcasts+=1
   if self.timeout:raise TimeoutError('synthetic timeout after send')
   value={'result':True}
  elif path in {'gettransactionbyid','gettransactioninfobyid'}:value={}
  else:raise AssertionError(path)
  return canonical(value)
 def reviewed(self):
  self.native.review(self.ctx,self.state,self.intent,self.plan,self.snapshot);self.bridge.commit(self.ctx,0,self.state)
  return self.state['workspace']['graph']
 def prepared(self):
  g=self.reviewed();version,state=self.bridge.load(self.ctx)
  self.native.approve(self.ctx,state,{'graph_id':g['id'],'graph_hash':g['hash'],'plan_hash':g['plan_hash'],'mandate_hash':g['mandate_hash'],'network':'nile','account':g['account']})
  self.bridge.commit(self.ctx,version,state);a=state['workspace']['approval']
  prepared=self.native.prepare(self.ctx,g['id'],{'approval_id':a['id'],'account':g['account'],'step_id':g['steps'][0]['id']})
  tx=prepared['transaction'];tx['signature']=[sign_hash(bytes.fromhex(tx['txID']),TEST_KEY).hex()]
  return {'graph_id':g['id'],'step_id':g['steps'][0]['id'],'approval_id':a['id'],'signed_transaction':tx},prepared
 def test_negative_or_missing_economics_never_creates_graph(self):
  self.plan['expected_net_return']['value']='-8.0894'
  with self.assertRaisesRegex(MachineError,'Keep cash'):self.reviewed()
  self.assertIsNone(self.state['workspace']['graph']);self.assertEqual(self.broadcasts,0)
  self.plan['expected_net_return']['value']='5';self.state.pop('planning_assumptions')
  with self.assertRaisesRegex(MachineError,'cost assumptions'):self.reviewed()
 def test_cost_rise_below_fee_cap_still_blocks_negative_net_before_signing(self):
  g=self.reviewed();version,state=self.bridge.load(self.ctx)
  state['native_execution']['economics']['projected_income']='12'
  self.bridge.commit(self.ctx,version,state)
  self.energy=100000 # 11.024 entry + 2 exit > 12 income; below the 15 cap.
  with self.assertRaisesRegex(MachineError,'Keep cash'):
   self.native.approve(self.ctx,state,{'graph_id':g['id'],'graph_hash':g['hash'],'plan_hash':g['plan_hash'],'mandate_hash':g['mandate_hash'],'network':'nile','account':g['account']})
  self.assertIsNone(state['workspace']['approval']);self.assertEqual(self.broadcasts,0)
 def test_native_graph_has_exact_payable_value_no_token_approve(self):
  payload,prepared=self.prepared();n=self.bridge.load(self.ctx)[1]['native_execution']
  self.assertEqual([x['operation'] for x in n['graph']['steps']],['RESERVE_BALANCE','JUSTLEND_SUPPLY_TRX'])
  raw=decode_trigger_raw(bytes.fromhex(prepared['transaction']['raw_data_hex']))
  self.assertEqual(raw['call_value_sun'],1000000);self.assertEqual(raw['data_hex'],'1249c58b')
  self.assertLess(raw['fee_limit_sun'],15000000)
 def test_tampered_wallet_projection_and_wrong_signer_never_broadcast(self):
  payload,p=self.prepared();bad=copy.deepcopy(payload);bad['signed_transaction']['raw_data']['contract'][0]['parameter']['value']['call_value']=2
  with self.assertRaisesRegex(MachineError,'differs'):self.native.submit(self.ctx,bad)
  bad=copy.deepcopy(payload);bad['signed_transaction']['signature']=[sign_hash(bytes.fromhex(p['transaction']['txID']),TEST_KEY+1).hex()]
  with self.assertRaises(MachineError):self.native.submit(self.ctx,bad)
  self.assertEqual(self.broadcasts,0)
 def test_timeout_persists_before_send_and_restart_never_rebroadcasts(self):
  payload,_=self.prepared();self.timeout=True;self.native.submit(self.ctx,payload)
  bridge=MachineBridge(OperationalRepository(self.repo.path),lambda:self.now,None);native=NativeExecution(bridge,self.rpc)
  self.assertEqual(bridge.load(self.ctx)[1]['workspace']['execution']['status'],'SUBMISSION_UNKNOWN')
  native.submit(self.ctx,payload);self.assertEqual(self.broadcasts,1)
  with self.assertRaisesRegex(MachineError,'Reconcile'):bridge.mutate(self.ctx,'POST','/v1/execution-graphs',{},'new')
 def test_tronweb_uppercase_recovery_byte_and_prefix_are_same_signature(self):
  payload,_=self.prepared();sig=payload['signed_transaction']['signature'][0]
  # TronWeb serializes v+27 using uppercase byte2hexStr: 1B or 1C.
  payload['signed_transaction']['signature']=['0x'+sig[:-2].upper()+format(int(sig[-2:],16)+27,'02X')]
  self.native.submit(self.ctx,payload);self.assertEqual(self.broadcasts,1)
  saved=self.bridge.load(self.ctx)[1]['native_requests'][payload['signed_transaction']['txID']]
  self.assertTrue(saved['broadcast_attempted'])
 def test_bad_signature_encoding_never_broadcasts(self):
  payload,_=self.prepared()
  for sig in [[],['aa'],['0x'+'gg'*65],['00'*65,'00'*65]]:
   payload['signed_transaction']['signature']=sig
   with self.assertRaises(MachineError):self.native.submit(self.ctx,payload)
  self.assertEqual(self.broadcasts,0)
 def absent_observation(self):
  at=int(datetime.fromisoformat(self.now).timestamp()*1000)-3000
  return {'status':'NOT_OBSERVED','solid_tip_before':{'timestamp_ms':at},'solid_tip_after':{'timestamp_ms':at}}
 def test_rejected_signed_pointer_recovers_after_expiry_even_if_graph_changed(self):
  payload,p=self.prepared();bad=copy.deepcopy(payload);bad['signed_transaction']['signature']=['bad']
  with self.assertRaises(MachineError):self.native.submit(self.ctx,bad)
  version,state=self.bridge.load(self.ctx);state['native_execution']={};state['workspace']['graph']=None
  self.bridge.commit(self.ctx,version,state)
  self.now=(datetime.fromtimestamp(p['transaction']['raw_data']['expiration']/1000)+timedelta(minutes=2)).isoformat()+'+00:00'
  self.ctx=type(self.ctx)(**{**vars(self.ctx),'expires_at':(datetime.fromisoformat(self.now)+timedelta(minutes=5)).isoformat()})
  pointer={'txid':payload['signed_transaction']['txID'],'graph_id':payload['graph_id'],'step_id':payload['step_id']}
  with patch('finance_service.native_execution.read_tron_transaction',return_value=self.absent_observation()):
   recovered=self.native.reconcile(self.ctx,pointer)
   self.assertEqual(recovered['resolution']['status'],'EXPIRED_NOT_OBSERVED')
   self.assertEqual(self.native.reconcile(self.ctx,pointer),recovered)
  self.assertEqual(self.broadcasts,0)
 def test_recovery_rejects_unknown_scope_live_expiry_and_node_visibility(self):
  payload,p=self.prepared();pointer={'txid':payload['signed_transaction']['txID'],'graph_id':payload['graph_id'],'step_id':payload['step_id']}
  with self.assertRaisesRegex(MachineError,'matching'):self.native.reconcile(self.ctx,{**pointer,'txid':'f'*64})
  with self.assertRaisesRegex(MachineError,'matching'):self.native.reconcile(self.ctx,{**pointer,'graph_id':'wrong'})
  with self.assertRaisesRegex(MachineError,'expiry'):self.native.reconcile(self.ctx,pointer)
  self.now=(datetime.fromtimestamp(p['transaction']['raw_data']['expiration']/1000)+timedelta(minutes=2)).isoformat()+'+00:00'
  self.ctx=type(self.ctx)(**{**vars(self.ctx),'expires_at':(datetime.fromisoformat(self.now)+timedelta(minutes=5)).isoformat()})
  with patch('finance_service.native_execution.read_tron_transaction',return_value={**self.absent_observation(),'status':'SOLID_BODY_RECEIPT_MISSING'}):
   with self.assertRaisesRegex(MachineError,'visible'):self.native.reconcile(self.ctx,pointer)
  stale=self.absent_observation();stale['solid_tip_before']['timestamp_ms']=p['transaction']['raw_data']['expiration']
  with patch('finance_service.native_execution.read_tron_transaction',return_value=stale):
   with self.assertRaisesRegex(MachineError,'solidified'):self.native.reconcile(self.ctx,pointer)
  self.assertIsNone(self.bridge.load(self.ctx)[1]['native_requests'][pointer['txid']].get('resolution'))
 def test_missing_mint_or_insufficient_fee_cannot_create_review(self):
  self.bad_mint=True
  with self.assertRaisesRegex(MachineError,'Mint'):self.reviewed()
  self.bad_mint=False;self.energy=200000
  with self.assertRaisesRegex(MachineError,'Fee cap'):self.reviewed()
 def test_native_cash_floor_reserves_maximum_fee_in_addition_to_principal(self):
  self.state['workspace']['mandate']['terms']['immediate_cash']['value']=9900
  with self.assertRaisesRegex(MachineError,'cash floor'):self.reviewed()
 def test_receipt_plus_exact_fee_and_share_deltas_reconcile(self):
  payload,_=self.prepared();self.native.submit(self.ctx,payload)
  _,state=self.bridge.load(self.ctx);n=state['native_execution'];e=observation(n['submission']);raw=e['observation']
  raw['details']['logs']=self.logs(1000000);raw.pop('observation_hash');raw['observation_hash']=digest(raw)
  self.now='2026-09-28T12:02:00+00:00';after=copy.deepcopy(n['before']);after['observed_at']=self.now
  after['balances'][0]['amount_base_units']=str(1000*10**6-1000000-1000000)
  after['balances'][1]['amount_base_units']='100000000';after['positions'][0].update(shares_base_units='100000000',underlying_base_units='1000000')
  evidence={'block':raw['details']['block'],'debt':'0'}
  with patch('finance_service.native_execution.read_tron_transaction',return_value=raw),patch.object(self.native,'account',return_value=(after,evidence)):
   self.native.reconcile(self.ctx)
  w=self.bridge.load(self.ctx)[1]['workspace'];self.assertEqual(w['execution']['status'],'POSITION_RECONCILED');self.assertEqual(w['performance']['fees']['value'],'1')
 def test_receipt_success_without_exact_balance_delta_is_disputed(self):
  payload,_=self.prepared();self.native.submit(self.ctx,payload);_,state=self.bridge.load(self.ctx);n=state['native_execution']
  raw=observation(n['submission'])['observation'];raw['details']['logs']=self.logs(1000000);raw.pop('observation_hash');raw['observation_hash']=digest(raw)
  self.now='2026-09-28T12:02:00+00:00';after=copy.deepcopy(n['before']);after['observed_at']=self.now
  after['balances'][0]['amount_base_units']=str(1000*10**6-1000000) # Missing fee debit must fail.
  after['positions'][0].update(shares_base_units='100000000',underlying_base_units='1000000')
  with patch('finance_service.native_execution.read_tron_transaction',return_value=raw),patch.object(self.native,'account',return_value=(after,{'block':raw['details']['block'],'debt':'0'})):
   self.native.reconcile(self.ctx)
  self.assertEqual(self.bridge.load(self.ctx)[1]['workspace']['execution']['status'],'DISPUTED')
 def test_failed_receipt_updates_actual_fees_without_creating_an_investment(self):
  payload,_=self.prepared();self.native.submit(self.ctx,payload)
  n=self.bridge.load(self.ctx)[1]['native_execution']
  raw=observation(n['submission'],status='SOLID_EXECUTION_FAILED')['observation']
  self.now='2026-09-28T12:02:00+00:00'
  with patch('finance_service.native_execution.read_tron_transaction',return_value=raw):
   self.native.reconcile(self.ctx);self.native.reconcile(self.ctx)
  state=self.bridge.load(self.ctx)[1];w=state['workspace']
  self.assertEqual(w['execution']['status'],'FAILED');self.assertEqual(w['positions'],[])
  self.assertEqual(w['performance']['fees']['value'],'1');self.assertEqual(w['performance']['net_income']['value'],'-1')
  self.assertEqual(len(state['native_accounting']['events']),1);self.assertEqual(self.broadcasts,1)

 def test_repeat_preflight_reuses_identical_wallet_bytes(self):
  payload,p=self.prepared();before=self.bridge.load(self.ctx)[1]
  self.now=(datetime.fromisoformat(self.now)+timedelta(seconds=10)).isoformat()
  again=self.native.prepare(self.ctx,payload['graph_id'],{'approval_id':payload['approval_id'],'account':before['workspace']['graph']['account'],'step_id':payload['step_id']})
  self.assertEqual(again['transaction']['txID'],p['transaction']['txID'])
  self.assertEqual(again['transaction']['raw_data_hex'],p['transaction']['raw_data_hex'])
  self.assertEqual(len(self.bridge.load(self.ctx)[1]['native_requests']),1)
  self.assertEqual(self.broadcasts,0)

 def test_server_recovery_survives_lost_browser_and_replaced_active_projection(self):
  payload,p=self.prepared();version,state=self.bridge.load(self.ctx)
  state['native_execution']={};state['workspace']['graph']=None
  self.bridge.commit(self.ctx,version,state)
  w=self.bridge.workspace(self.ctx);pending=w['prepared_transactions'][0]
  self.assertEqual(pending['txid'],p['transaction']['txID']);self.assertNotIn('transaction',pending)
  with self.assertRaisesRegex(MachineError,'previous wallet request'):
   self.bridge.mutate(self.ctx,'POST','/v1/portfolio-adjustments',{},'blocked-other-review')
  self.assertEqual(self.broadcasts,0)

 def test_worker_unlocks_expired_unsubmitted_request_only_after_chain_absence(self):
  from finance_service.machine_worker import recover_expired_requests, job_context
  payload,p=self.prepared();version,state=self.bridge.load(self.ctx)
  recover_expired_requests(self.bridge,self.scope,state)
  self.assertEqual(len(self.bridge.workspace(self.ctx)['prepared_transactions']),1)
  self.now=(datetime.fromtimestamp(p['transaction']['raw_data']['expiration']/1000)+timedelta(minutes=2)).isoformat()+'+00:00'
  with patch('finance_service.native_execution.read_tron_transaction',return_value=self.absent_observation()):
   recover_expired_requests(self.bridge,self.scope,state)
  w=self.bridge.workspace(job_context({'scope':self.scope,'job_id':'verify-recovery'},lambda:self.now))
  self.assertEqual(w['prepared_transactions'],[])
  self.assertEqual(w['transaction_resolutions'][0]['txid'],payload['signed_transaction']['txID'])
  self.assertEqual(self.broadcasts,0)

if __name__=='__main__':unittest.main()
