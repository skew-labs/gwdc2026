"""Conversational edits are drafts, not financial authorization."""
import json
import unittest
from copy import deepcopy
from finance_service.agent_intent import AgentIntentService
from finance_service.model_usage import OperationalModelUsageStore
import test_machine_bridge
from test_finance_wallet_auth import FakeProvider

class ConversationConditionsTests(unittest.TestCase):
    def setUp(self):
        test_machine_bridge.MachineBridgeTests.setUp(self)
        self.output = {'schema_version':'financial-intent-draft-1','intent':'REVISE','patch':{},'evidence':{},'missing':[],'reason_codes':[]}
        owner=self
        class Provider:
            def complete(self, messages):
                owner.prompt = messages
                return {**FakeProvider().complete(messages), 'content':json.dumps(owner.output)}
        self.agent=AgentIntentService(Provider(),OperationalModelUsageStore(self.repo),lambda:'2026-09-28T12:01:00+00:00')
        self.bridge.intents=self.agent

    def test_high_risk_generic_yes_does_not_allow_borrowing_or_lose_capital(self):
        self.output.update(patch={'capital':[{'asset':'TRX','amount':'100'}],'borrowing_consent':True}, evidence={'capital':'100trx','borrowing_consent':'하이리스크 오케이'},missing=['max_debt'])
        result=self.agent.propose(self.ctx,'100trx, 어 하이리스크 오케이, 3은 괜찮아 다')
        self.assertEqual(result['patch'],{'capital':[{'asset':'TRX','amount':'100'}]})
        self.assertIn('EXPLICIT_BORROWING_REQUIRED',result['reason_codes'])
        self.assertEqual(result['execution_authority'],'NONE')

    def test_explicit_borrowing_is_still_only_a_bounded_draft(self):
        self.output.update(patch={'borrowing_consent':True,'max_debt':{'asset':'TRX','amount':'5'}},evidence={'borrowing_consent':'Allow borrowing','max_debt':'5 TRX'})
        result=self.agent.propose(self.ctx,'Allow borrowing up to 5 TRX')
        self.assertEqual(result['status'],'DRAFT_READY')
        self.assertEqual(result['patch']['max_debt']['amount'],'5')
        self.assertEqual(result['signature_status'],'NOT_REQUESTED')

    def test_invalid_json_shape_is_a_rejected_model_result(self):
        for index,invalid in enumerate([[],None,{'patch':None,'evidence':{}},{'patch':{},'evidence':[]}]):
            self.output=invalid
            self.assertEqual(self.agent.propose(self.ctx,f'review {index}')['status'],'MODEL_OUTPUT_REJECTED')

    def test_followup_keeps_known_fields_and_read_only_does_not_replace_draft(self):
        self.bridge.mutate(self.ctx,'POST','/v1/mandates',self.payload,'create')
        draft=self.bridge.workspace(self.ctx)['mandate']
        self.bridge.mutate(self.ctx,'POST',f"/v1/mandates/{draft['id']}/confirm",{'hash':draft['hash'],'version':draft['version']},'confirm')
        confirmed=deepcopy(self.bridge.workspace(self.ctx)['mandate'])
        self.output.update(patch={'horizon_seconds':604800,'immediate_cash':{'kind':'BPS','value':2000}},evidence={'horizon_seconds':'7 days','immediate_cash':'20% cash'})
        first=self.bridge.mutate(self.ctx,'POST','/v1/agent-intent',{'message':'7 days, 20% cash','agent_id':'agent-one','message_id':'message-one'},'edit')
        self.assertEqual(first['questions'],[])
        self.assertEqual(first['known']['capital'],self.payload['terms']['capital'])
        self.assertEqual(first['known']['borrowing_consent'],False)
        self.assertEqual(first['trigger_message_id'],'message-one')
        self.output.update(intent='REVIEW',patch={},evidence={})
        result=self.bridge.mutate(self.ctx,'POST','/v1/agent-intent',{'message':'Show my portfolio','message_id':'read-only'},'read')
        self.assertTrue(result['pending_proposal'])
        self.assertEqual(self.bridge.workspace(self.ctx)['intent'],first)
        self.assertEqual(self.bridge.workspace(self.ctx)['mandate'],confirmed)
        self.assertIsNone(self.bridge.workspace(self.ctx)['approval'])

if __name__=='__main__': unittest.main()
