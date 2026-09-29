import copy
import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
from finance_service.portfolio_review import evaluate, confirmed_policy, PortfolioReviews, PRODUCT
from finance_service.machine_bridge import MachineBridge, empty_workspace
from finance_service.machine_worker import observe_routine
from finance_service.operational_repository import OperationalRepository
from finance_service.context import AuthenticatedContext
from economic_machine.values import MachineError, digest

AT='2026-09-29T12:00:00+00:00'
ROOT=Path(__file__).resolve().parents[1]

def policy():
    t=json.loads((ROOT/'cases/economic_mandate_demo.json').read_text())['terms']
    t.update(base_asset='TRX',capital=[{'asset':'TRX','amount':'100'}],horizon_seconds=604800,
        immediate_cash={'kind':'BPS','value':2000},withdrawals=[],effective_at=AT,expires_at='2026-10-06T12:00:00+00:00',
        protocol_caps_bps={'justlend':8000},price_exposure_caps_bps={'TRX':10000},allowed_actions=['HOLD','SUPPLY','REDEEM'])
    t['borrowing'].update(consent=False,max_debt={'asset':'TRX','amount':'0'})
    t['limits']={k:{'asset':'TRX','amount':v} for k,v in {'single_amount':'100','cumulative_amount':'100','fee_amount':'30','daily_loss':'100','stress_loss':'100'}.items()}
    return {'policy_hash':'a'*64,'confirmed_at':AT,'mandate':{'terms':t},'status':'CONFIRMED',
        'review_inputs':{'assumptions':assumptions(),'constraints':{'max_fee':{'value':'15','symbol':'TRX','decimals':6}}}}

def assumptions():return {'exit_cost':'1','daily_loss_bps':10000,'stress_loss_bps':10000}
def live():return {'position':'1','wallet':'99','debt':'0','pool_cash':'1000000','apy':'0.05'}
def quote(cost):return {'cost':cost,'hash':'c'*64}

class EvaluationTests(unittest.TestCase):
    def run_review(self,p=None,l=None,supply='8',redeem='1'):
        return evaluate(p or policy(),assumptions(),l or live(),AT,lambda d:quote(supply),lambda *_:quote(redeem))
    def test_hold_does_not_recharge_sunk_entry_fee_and_costs_block_churn(self):
        r=self.run_review()
        self.assertEqual(r['status'],'HOLD')
        self.assertEqual(r['hold']['change_cost'],'0')
        self.assertEqual(r['hold']['exit_reserve'],'1')
    def test_cash_reserve_is_not_counted_as_expected_exit_fee(self):
        p=policy();a=assumptions();a['exit_cost']='15'
        l={**live(),'apy':'0'}
        r=evaluate(p,a,l,AT,lambda d:quote('7'),lambda *_:quote('8'))
        self.assertEqual(r['status'],'HOLD')
        self.assertEqual(r['hold']['exit_reserve'],'15')
        self.assertEqual(r['hold']['future_exit_estimate'],'8')
        self.assertEqual(r['hold']['net_income'],'-8')
        self.assertEqual(next(c for c in r['alternatives'] if c['action']=='REDEEM')['net_improvement'],'0')
    def test_profitable_permitted_adjustment_and_liquidity_buffer(self):
        p=policy();p['mandate']['terms']['protocol_caps_bps']['justlend']=7000
        r=self.run_review(p,supply='0.01',redeem='0.01')
        self.assertEqual(r['status'],'ADJUST')
        self.assertGreater(float(r['suggested']['benefit_over_hold']),float(r['suggested']['additional_cost']))
    def test_maximum_target_is_reduced_to_pay_fees_without_breaking_cash_floor(self):
        r=self.run_review(supply='0.01',redeem='0.01')
        self.assertEqual(r['status'],'ADJUST')
        self.assertEqual(r['suggested']['position'],'78.99')
        self.assertEqual(r['suggested']['wallet_cash'],'21')
    def test_cash_floor_protocol_and_debt_breaches_are_not_a_yield_suggestion(self):
        p=policy();p['mandate']['terms']['protocol_caps_bps']['justlend']=100
        l={**live(),'position':'3','wallet':'10','debt':'1'}
        r=self.run_review(p,l)
        self.assertEqual(r['status'],'POLICY_BREACH')
        for x in ['CASH_FLOOR_BREACH','PROTOCOL_CAP_BREACH','DEBT_LIMIT_BREACH']:self.assertIn(x,r['checks'])
    def test_partial_reduction_is_quoted_when_protocol_cap_is_breached(self):
        p=policy();t=p['mandate']['terms'];t['capital'][0]['amount']='1000'
        t.update(protocol_caps_bps={'justlend':5000},horizon_seconds=31536000,expires_at='2027-09-29T12:00:00+00:00')
        for k in ['single_amount','cumulative_amount','daily_loss','stress_loss']:t['limits'][k]['amount']='1000'
        l={**live(),'wallet':'200','position':'800','apy':'0.5'}
        quoted=[]
        def redeem(amount):quoted.append(amount);return quote('1')
        r=evaluate(p,assumptions(),l,AT,lambda _:quote('1'),redeem)
        self.assertEqual(r['status'],'POLICY_BREACH')
        self.assertEqual(r['suggested']['action'],'REDEEM')
        self.assertEqual(r['suggested']['position'],'500')
        self.assertEqual([str(x) for x in quoted],['800','300.000000'])
    def test_expiry_requires_new_confirmation(self):
        p=policy();p['mandate']['terms']['expires_at']='2026-09-29T11:59:59+00:00'
        self.assertEqual(self.run_review(p)['status'],'POLICY_INACTIVE')
    def test_missing_redeem_quote_never_becomes_hold(self):
        def fail(*_):raise MachineError('network')
        r=evaluate(policy(),assumptions(),live(),AT,lambda d:quote('1'),fail)
        self.assertEqual(r['status'],'DATA_UNAVAILABLE')
    def test_wallet_cash_is_not_all_counted_against_small_mandate(self):
        r=self.run_review(l={**live(),'wallet':'990'})
        self.assertEqual(r['hold']['wallet_cash'],'99')
    def test_total_fee_limit_includes_future_exit_reserve(self):
        p=policy();p['mandate']['terms']['limits']['fee_amount']['amount']='1'
        r=self.run_review(p,supply='0.01',redeem='1')
        self.assertIn('TOTAL_COST_LIMIT',next(c for c in r['alternatives'] if c['action']=='SUPPLY')['reasons'])
    def test_paid_fees_reduce_budget_and_remaining_permission_but_not_incremental_return(self):
        from decimal import Decimal
        r=evaluate(policy(),assumptions(),live(),AT,lambda *_:quote('8'),lambda *_:quote('1'),paid_fees=Decimal('8'))
        self.assertEqual(r['hold']['wallet_cash'],'91')
        self.assertEqual(r['hold']['change_cost'],'0')
        self.assertEqual(r['remaining_budget'],'92')
        p=policy();p['mandate']['terms']['limits']['fee_amount']['amount']='10'
        r=evaluate(p,assumptions(),live(),AT,lambda *_:quote('8'),lambda *_:quote('1'),paid_fees=Decimal('8'))
        self.assertIn('TOTAL_COST_LIMIT',next(c for c in r['alternatives'] if c['action']=='SUPPLY')['reasons'])
    def test_cumulative_spend_is_included(self):
        from decimal import Decimal
        p=policy();p['mandate']['terms']['limits']['cumulative_amount']['amount']='1'
        r=evaluate(p,assumptions(),live(),AT,lambda d:quote('0.01'),lambda *_:quote('1'),Decimal('1'))
        self.assertFalse(any(c['action']=='SUPPLY' for c in r['alternatives']))

class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=OperationalRepository(Path(self.tmp.name)/'state.sqlite')
        raw=json.loads((ROOT/'cases/economic_mandate_demo.json').read_text())
        self.ctx=AuthenticatedContext(**{**raw['scope'],'network':'tron-nile'},session_id='session',trace_id='trace',issued_at=AT,expires_at='2026-09-29T13:00:00+00:00')
        self.bridge=MachineBridge(self.repo,lambda:AT,None)
        p=policy();w=empty_workspace('nile')
        w['mandate']={'status':'CONFIRMED','hash':p['policy_hash'],'terms':p['mandate']['terms'],'constraints':p['review_inputs']['constraints']}
        self.state={'workspace':w,'mandates':{'one':{'revisions':[p]}},'requests':{},'planning_assumptions':assumptions()}
    def test_confirmed_inputs_survive_pending_draft(self):
        p=confirmed_policy(self.state)
        self.state['workspace']['mandate']={'status':'DRAFT','hash':'b'*64,'terms':{}}
        self.state['planning_assumptions']={'exit_cost':'999'}
        self.assertEqual(confirmed_policy(self.state)['review_inputs']['assumptions']['exit_cost'],'1')
    def test_historical_evidence_backfills_only_exact_confirmed_policy(self):
        p=policy();w=self.state['workspace']
        w['evidence']=[{'mandate_hash':p['policy_hash'],'inputs':{'mandate':copy.deepcopy(w['mandate'])},'calculation':{'assumptions':assumptions()}}]
        w['mandate']={'status':'DRAFT','hash':'other'}
        self.state['planning_assumptions']={'exit_cost':'999'}
        self.assertEqual(confirmed_policy(self.state)['review_inputs']['assumptions']['exit_cost'],'1')
    def test_notifications_persist_dedupe_read_and_scope(self):
        w=self.state['workspace'];r={'id':'r1','policy_hash':'a'*64,'status':'POLICY_BREACH','checks':['CASH_FLOOR_BREACH'],'reason':'cash too low','suggested':None}
        PortfolioReviews.notifications(w,r,AT)
        PortfolioReviews.notifications(w,{**r,'id':'r2'},AT)
        self.assertEqual(len(w['notifications']),1)
        self.bridge.commit(self.ctx,0,self.state)
        nid=w['notifications'][0]['id']
        self.bridge.mutate(self.ctx,'POST','/v1/notifications/'+nid+'/read',{},'read')
        restarted=MachineBridge(self.repo,lambda:AT,None)
        self.assertEqual(restarted.workspace(self.ctx)['notifications'][0]['read_at'],AT)
        with self.assertRaises(MachineError):restarted.mutate(replace(self.ctx,owner_id='someone-else'),'POST','/v1/notifications/'+nid+'/read',{},'read')
        PortfolioReviews.notifications(w,{**r,'status':'HOLD'},AT)
        self.assertIsNotNone(w['notifications'][0]['resolved_at'])
    def test_worker_calls_fresh_review_and_commits_result(self):
        routine={'id':'daily','name':'Daily','time':'09:00','timezone':'Asia/Seoul','enabled':True,'notify_on':['Portfolio review'],'last_success_at':None}
        self.state['workspace']['routines']=[routine]
        self.bridge.commit(self.ctx,0,self.state)
        def review(context,state,**kw):
            self.assertEqual(kw,{'source':'ROUTINE','notify':True})
            result={'id':'fresh','status':'HOLD','reason':'After fresh reads, costs outweigh benefit.','expires_at':'2026-09-29T12:05:00+00:00'}
            state['workspace']['portfolio_review']=result
            return result
        self.bridge.reviews.refresh=review
        job={'scope':self.ctx.scope,'job_id':'job1','routine_id':'daily','dependency_hash':digest(routine)}
        self.assertEqual(observe_routine(self.bridge,job)['status'],'HOLD')
        w=self.bridge.workspace(self.ctx)
        self.assertEqual(w['routines'][0]['last_result'],'HOLD')
        self.assertEqual(w['routines'][0]['last_review_id'],'fresh')
        self.assertEqual(observe_routine(self.bridge,job)['status'],'CANCELLED')
    def test_new_live_holdings_change_decision_and_preserve_historical_principal(self):
        from types import SimpleNamespace
        from finance_service.native_execution import money
        values={'position':1000000,'wallet':99000000}
        def account(*args):
            raw={'shares':'100000000','wallet':{'balance':values['wallet']},'debt':'0','cash':'1000000000000','block':{'number':123},'observed_at':AT}
            return {'positions':[{'underlying_base_units':str(values['position'])}]},raw
        self.bridge.observations=SimpleNamespace(read=lambda *args:{'balances':[money(values['wallet'])],'snapshots':[],
            'snapshot':{'facts':{PRODUCT+'.supply_apy':{'availability':'AVAILABLE','quality':'VALID','value':'0.05'}}}})
        self.bridge.native=SimpleNamespace(account=account,simulate=lambda *args:{'energy_burn':8000000,'bandwidth_bound':1000000,'hash':'d'*64})
        self.state['workspace']['positions']=[{'id':'nile-jtrx','product':PRODUCT,'principal':money(1000000),'current_value':money(1000000),'receipt_txid':'original'}]
        self.bridge.commit(self.ctx,0,self.state)
        with patch('finance_service.portfolio_review.redeem_quote',return_value=quote('1')):
            self.bridge.mutate(self.ctx,'POST','/v1/portfolio-reviews',{},'one')
            first=self.bridge.workspace(self.ctx)['portfolio_review']
            values.update(position=3000000,wallet=10000000)
            self.bridge.mutate(self.ctx,'POST','/v1/portfolio-reviews',{},'two')
        w=self.bridge.workspace(self.ctx)
        self.assertNotEqual(w['portfolio_review']['id'],first['id'])
        self.assertEqual(w['portfolio_review']['status'],'POLICY_BREACH')
        self.assertIn('CASH_FLOOR_BREACH',w['portfolio_review']['checks'])
        self.assertEqual(w['positions'][0]['current_value']['value'],'3')
        self.assertEqual(w['positions'][0]['principal']['value'],'1')
        self.assertEqual(w['positions'][0]['receipt_txid'],'original')
        self.assertEqual(w['notifications'][0]['kind'],'POLICY_BREACH')
    def test_read_failure_does_not_resolve_a_known_breach(self):
        w=self.state['workspace']
        r={'id':'r1','policy_hash':'a'*64,'status':'POLICY_BREACH','checks':['CASH_FLOOR_BREACH'],'reason':'cash too low','suggested':None}
        PortfolioReviews.notifications(w,r,AT)
        PortfolioReviews.notifications(w,{**r,'id':'r2','status':'DATA_UNAVAILABLE','reason':'RPC unavailable'},AT)
        self.assertIsNone(w['notifications'][0]['resolved_at'])
    def test_failed_read_has_no_fake_hold_or_trade_authority(self):
        r=self.bridge.reviews.refresh(self.ctx,self.state)
        self.assertEqual(r['status'],'DATA_UNAVAILABLE')
        self.assertIsNone(r['hold'])
        self.assertEqual(r['execution_authority'],'NONE')
        self.assertEqual(len(self.state['workspace']['notifications']),1)

if __name__=='__main__':unittest.main()
