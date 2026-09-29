"""Synthetic supply → redemption → receipt → accounting lifecycle.

Production transaction construction, signature recovery and persistent locks
run unchanged. RPC responses/receipts are fixtures, not live execution proof.
"""
import copy, unittest
from unittest.mock import patch
from decimal import Decimal
from finance_service.native_adjustments import create_adjustment, review_adjustment, option_hash
from finance_service.native_execution import MARKET, REDEEM, TRANSFER
from economic_machine.values import canonical, digest, MachineError
from economic_machine.signed_tx_validation import decode_trigger_raw
import test_native_execution as fixture
TEST_KEY=fixture.TEST_KEY
from test_economic_tron_execution import observation, sign_hash
from test_portfolio_review import policy


class AdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.NativeExecutionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.state['workspace']['mandate']['terms']['horizon_seconds']=86400
        self.f.plan['allocations']=[{'kind':'SUPPLY','amount':{'value':'1','symbol':'TRX'}}]
        self.f.state['workspace']['comparison']={'plans':[self.f.plan]}
        self.f.test_receipt_plus_exact_fee_and_share_deltas_reconcile()
        self.version,self.state=self.f.bridge.load(self.f.ctx)
        n=self.state['native_execution'];self.after=copy.deepcopy(n['post_state']['account'])
        self.p=policy();t=self.p['mandate']['terms'];t.update(effective_at='2026-09-28T12:00:00+00:00',expires_at='2026-09-29T12:00:00+00:00',capital=[{'asset':'TRX','amount':'1000'}])
        t['limits']['fee_amount']['amount']='100';self.p['policy_hash']='1'*64
        self.state['workspace']['mandate'].update(terms=t,hash=self.p['policy_hash'],status='CONFIRMED')
        self.state['mandates']={'m':{'revisions':[self.p]}}
        self.state['confirmed_review_inputs']={self.p['policy_hash']:copy.deepcopy(self.p['review_inputs'])}
        self.state['observation']={'snapshot':self.f.snapshot}
        self.state['workspace']['execution']['status']='POSITION_RECONCILED'
        self.candidate={'action':'REDEEM','delta':'1','position':'0','eligible':True,'reasons':[],
                        'change_cost':'8','exit_reserve':'0','net_improvement':'0','benefit_over_hold':'0','additional_cost':'8'}
        self.review={'id':'review-1','snapshot_hash':self.f.snapshot['snapshot_hash'],'policy_hash':self.p['policy_hash'],
            'paid_fees':'1','cash_floor':'200','remaining_seconds':86400,'expires_at':'2026-09-28T12:05:00+00:00','status':'HOLD','alternatives':[self.candidate]}
        self.state['workspace']['portfolio_review']=self.review
        original=self.f.rpc
        def rpc(url,body):
            name=url.split('/')[-1]
            if name=='getcontract':
                import json
                result=json.loads(original(url,body));result['abi']['entrys'].append({'name':'redeem','type':'Function','inputs':[{'type':'uint256'}],'outputs':[{'type':'uint256'}]})
                return canonical(result)
            if name=='getaccount':return canonical({'address':self.f.ctx.wallet,'balance':int(self.after['balances'][0]['amount_base_units'])})
            if name=='triggerconstantcontract' and body['function_selector']=='getAccountSnapshot(address)':
                return canonical({'result':{'result':True},'constant_result':[''.join(format(x,'064x') for x in [0,int(self.after['positions'][0]['shares_base_units']),0,10**16])]})
            if name=='triggerconstantcontract' and body['function_selector']=='redeem(uint256)':
                shares=int(body['parameter'],16)
                return canonical({'result':{'result':True},'constant_result':['0'*64],'energy_used':80894,'logs':self.logs(shares)})
            return original(url,body)
        self.f.native.request=rpc
    def logs(self,shares):
        owner=self.f.ctx.wallet[2:].rjust(64,'0');market=MARKET[2:].rjust(64,'0')
        return [{'address':MARKET[2:],'topics':[REDEEM],'data':owner+format(shares//100,'064x')+format(shares,'064x')},
                {'address':MARKET[2:],'topics':[TRANSFER,owner,market],'data':format(shares,'064x')}]
    def reviewed(self):
        create_adjustment(self.f.bridge,self.f.ctx,self.state,self.p,self.review,self.candidate)
        self.f.bridge.commit(self.f.ctx,self.version,self.state)
        return self.state['workspace']['graph']
    def prepared(self):
        g=self.reviewed();version,state=self.f.bridge.load(self.f.ctx)
        self.f.native.approve(self.f.ctx,state,{'graph_id':g['id'],'graph_hash':g['hash'],'plan_hash':g['plan_hash'],'mandate_hash':g['mandate_hash'],'network':'nile','account':g['account']})
        self.f.bridge.commit(self.f.ctx,version,state);a=state['workspace']['approval']
        p=self.f.native.prepare(self.f.ctx,g['id'],{'approval_id':a['id'],'step_id':g['steps'][0]['id'],'account':g['account']})
        tx=p['transaction'];tx['signature']=[sign_hash(bytes.fromhex(tx['txID']),TEST_KEY).hex()]
        return {'graph_id':g['id'],'step_id':g['steps'][0]['id'],'approval_id':a['id'],'signed_transaction':tx},p
    def test_exact_redeem_encoding_receipt_and_realized_accounting(self):
        payload,p=self.prepared();raw=decode_trigger_raw(bytes.fromhex(p['transaction']['raw_data_hex']))
        self.assertEqual(raw['call_value_sun'],0);self.assertEqual(int(raw['data_hex'][8:],16),100000000)
        self.f.native.submit(self.f.ctx,payload)
        _,state=self.f.bridge.load(self.f.ctx);n=state['native_execution']
        observed=observation(n['submission'])['observation'];observed['details']['logs']=self.logs(100000000)
        from datetime import datetime
        observed['details']['block']['timestamp_ms']=int(datetime.fromisoformat(self.f.now).timestamp()*1000)+1000
        observed.pop('observation_hash');observed['observation_hash']=digest(observed)
        self.f.now='2026-09-28T12:03:00+00:00';after=copy.deepcopy(n['before']);after['observed_at']=self.f.now
        after['balances'][0]['amount_base_units']=str(int(after['balances'][0]['amount_base_units'])+1000000-1000000)
        after['balances'][1]['amount_base_units']='0';after['positions'][0].update(shares_base_units='0',underlying_base_units='0')
        evidence={'block':observed['details']['block'],'debt':'0'}
        with patch('finance_service.native_execution.read_tron_transaction',return_value=observed),patch.object(self.f.native,'account',return_value=(after,evidence)):
            self.f.native.reconcile(self.f.ctx);self.f.native.reconcile(self.f.ctx)
        state=self.f.bridge.load(self.f.ctx)[1];w=state['workspace']
        self.assertEqual(w['execution']['status'],'POSITION_RECONCILED');self.assertEqual(w['positions'],[])
        self.assertEqual(w['performance']['realized']['value'],'0');self.assertEqual(w['performance']['accrued']['value'],'0')
        self.assertEqual(w['performance']['fees']['value'],'2');self.assertEqual(w['performance']['net_income']['value'],'-2')
        self.assertIsNotNone(w['performance']['expected_return']);self.assertEqual(len(state['native_accounting']['events']),2)
        self.assertEqual(self.f.broadcasts,2)
    def test_tampered_shares_are_rejected_before_broadcast(self):
        payload,p=self.prepared();payload['signed_transaction']['raw_data']['contract'][0]['parameter']['value']['data']='aa'
        with self.assertRaises(MachineError):self.f.native.submit(self.f.ctx,payload)
        self.assertEqual(self.f.broadcasts,1)  # only the synthetic initial supply
    def test_output_does_not_cover_gas_upfront_and_policy_fee_budget_stays_binding(self):
        self.after['balances'][0]['amount_base_units']='1'
        with self.assertRaisesRegex(MachineError,'fee reserve'):self.reviewed()
        self.after['balances'][0]['amount_base_units']='998000000';self.p['mandate']['terms']['limits']['fee_amount']['amount']='10'
        with self.assertRaisesRegex(MachineError,'fee permission'):self.reviewed()
    def test_stale_or_replaced_review_cannot_create_an_approval(self):
        with self.assertRaisesRegex(MachineError,'changed'):
            review_adjustment(self.f.bridge,self.f.ctx,self.state,{'review_id':'old','option_hash':option_hash(self.candidate)})
        self.review['expires_at']='2026-09-28T11:00:00+00:00'
        with self.assertRaisesRegex(MachineError,'expired'):
            review_adjustment(self.f.bridge,self.f.ctx,self.state,{'review_id':'review-1','option_hash':option_hash(self.candidate)})
    def test_prepared_signature_cannot_be_replaced_by_new_conditions(self):
        self.prepared()
        self.assertIsNotNone(self.f.bridge.workspace(self.f.ctx)['graph'])
        for path in ('/v1/mandates','/v1/plan-comparisons','/v1/portfolio-adjustments','/v1/usdd-workflows'):
            with self.assertRaisesRegex(MachineError,'previous wallet request'):
                self.f.bridge.mutate(self.f.ctx,'POST',path,{},'replace-'+path)
    def test_remaining_position_exit_reserve_must_fit_wallet_cash(self):
        self.after['balances'][0]['amount_base_units']='215000000'
        self.candidate['exit_reserve']='5'
        with self.assertRaisesRegex(MachineError,'cash floor'):self.reviewed()
    def test_pending_draft_cannot_block_withdrawal_or_expand_its_permission(self):
        before=copy.deepcopy(self.state['workspace']['mandate'])
        self.state['workspace']['mandate'].update(id='m',status='DRAFT',hash='d'*64)
        self.state['workspace']['mandate']['terms']=copy.deepcopy(before['terms'])
        self.state['workspace']['mandate']['terms']['limits']['fee_amount']['amount']='99999'
        self.p['status']='SUPERSEDED'
        self.state['mandates']['m']['revisions'].append({'status':'DRAFT','draft_hash':'d'*64})
        self.prepared()
        self.assertEqual(self.f.bridge.load(self.f.ctx)[1]['workspace']['mandate']['status'],'DRAFT')
        projection=self.f.bridge.workspace(self.f.ctx)
        self.assertIsNone(projection['active_mandate'])
        self.assertEqual(projection['withdrawal_policy']['hash'],self.p['policy_hash'])
        self.assertEqual(projection['withdrawal_policy']['status'],'DRAFT')
        self.assertEqual(self.f.bridge.load(self.f.ctx)[1]['mandates']['m']['revisions'][0]['status'],'SUPERSEDED')
        v,state=self.f.bridge.load(self.f.ctx)
        state['mandates']['m']['revisions'][-1]['status']='REVOKED'
        with self.assertRaisesRegex(MachineError,'changed'):
            self.f.native.check_current_policy(self.f.ctx,state,state['native_execution'])
