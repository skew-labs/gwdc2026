"""Same-wallet comparisons use synthetic chain and market fixtures, never live funds."""
import copy, unittest
from unittest.mock import patch
import test_usdd_execution as fixture
from finance_service.usdd_comparison import compare


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.UsddExecutionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.market={'trx_usd':'0.3','usdd_usd':'1','reward_apr':'0.1','market_observed_at':self.f.now,'api_url':'https://example.invalid/market'}
        self.addCleanup(patch.stopall)
        patch('finance_service.usdd_comparison.read_market',lambda *args:(copy.deepcopy(self.market),{})).start()
    def test_two_viable_plans_same_budget_and_no_workflow_created(self):
        before=copy.deepcopy(self.f.state)
        out,core=compare(self.f.bridge,self.f.ctx,self.f.state)
        self.assertEqual(out['status'],'READY',out['reason']);self.assertEqual(len(out['plans']),2)
        a,b=out['plans'];self.assertNotEqual(a['allocations'][0]['amount'],b['allocations'][0]['amount'])
        from decimal import Decimal as D
        for p in out['plans']:
            self.assertGreater(D(p['expected_net_return']['value']),0)
            self.assertEqual(sum(D(x['amount']['value']) for x in p['allocations'])+D(p['estimated_fees']['value']),1000)
            self.assertGreater(D(p['allocations'][0]['incentive_rewards']['value']),0)
        self.assertNotIn('usdd_execution',self.f.state);self.assertIsNone(self.f.state['workspace']['graph'])
        self.assertEqual(self.f.state['mandates'],before['mandates']);self.assertEqual(self.f.chain.broadcasts,0)
        # Selection goes through the production constructor, retaining exact principal and fee caps.
        self.f.svc.create(self.f.ctx,self.f.state,core['payloads'][b['id']]['payload'])
        self.assertEqual(self.f.state['workspace']['graph']['review_kind'],'USDD_WORKFLOW')
    def test_unfunded_wallet_cannot_be_displayed_as_executable(self):
        self.f.chain.current['token_balance']='0'
        out,_=compare(self.f.bridge,self.f.ctx,self.f.state)
        self.assertEqual(out['status'],'INFEASIBLE');self.assertEqual(out['plans'],[])
    def test_stale_inputs_and_negative_net_do_not_manufacture_two_plans(self):
        self.market['market_observed_at']='2026-09-29T11:00:00+00:00'
        out,_=compare(self.f.bridge,self.f.ctx,self.f.state)
        self.assertEqual(out['status'],'INFEASIBLE');self.assertIn('older',out['reason'])
        self.market['market_observed_at']=self.f.now
        self.f.state['mandates']['m']['revisions'][0]['mandate']['terms']['horizon_seconds']=86400
        out,_=compare(self.f.bridge,self.f.ctx,self.f.state)
        self.assertEqual(out['status'],'INFEASIBLE');self.assertEqual(out['plans'],[])
    def test_unapproved_risk_or_cash_limits_remain_binding(self):
        terms=self.f.state['mandates']['m']['revisions'][0]['mandate']['terms']
        terms['limits']['stress_loss']['amount']='1'
        out,_=compare(self.f.bridge,self.f.ctx,self.f.state)
        self.assertEqual(out['status'],'INFEASIBLE');self.assertIn('stress loss',out['reason'])
