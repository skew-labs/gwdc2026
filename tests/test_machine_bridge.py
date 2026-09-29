import copy
import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finance_service.machine_bridge import MachineBridge, router_for
from finance_service.operational_repository import OperationalRepository
from finance_service.context import AuthenticatedContext
from economic_machine.values import MachineError

ROOT = Path(__file__).resolve().parents[1]
AT = '2026-09-28T12:01:00+00:00'
class MachineBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = OperationalRepository(Path(self.temp.name) / 'workspace.sqlite')
        self.bridge = MachineBridge(self.repo, lambda: AT, None)
        self.raw = json.loads((ROOT / 'cases/economic_mandate_demo.json').read_text())
        self.ctx = AuthenticatedContext(**self.raw['scope'], session_id='session', trace_id='trace', issued_at='2026-09-28T12:00:00Z', expires_at='2026-09-28T13:00:00Z')
        self.payload = {'network':'nile','source_text':'Test user form','previous_id':None,'terms':self.raw['terms'],'constraints':{'capital':{'value':'10000','symbol':'USDT','decimals':6},'horizon_days':30,'min_cash_bps':3000,'max_trx_exposure_bps':0,'allow_debt':False,'allowed_protocols':['JustLend','USDD'],'max_fee':{'value':'20','symbol':'TRX','decimals':6}}}
    def test_real_core_confirmation_survives_restart_and_is_tenant_scoped(self):
        self.bridge.mutate(self.ctx,'POST','/v1/mandates',self.payload,'create-1')
        draft = self.bridge.workspace(self.ctx)['mandate']
        self.assertEqual(draft['status'],'DRAFT')
        with self.assertRaises(MachineError):
            self.bridge.mutate(self.ctx,'POST','/v1/mandates/'+draft['id']+'/confirm',{'version':draft['version'],'hash':'f'*64},'bad-confirm')
        self.bridge.mutate(self.ctx,'POST','/v1/mandates/'+draft['id']+'/confirm',{'version':draft['version'],'hash':draft['hash']},'good-confirm')
        restarted = MachineBridge(OperationalRepository(self.repo.path),lambda:AT,None)
        confirmed = restarted.workspace(self.ctx)['mandate']
        self.assertEqual(confirmed['status'],'CONFIRMED')
        self.assertNotEqual(confirmed['hash'],draft['hash'])
        self.assertIsNone(restarted.workspace(replace(self.ctx,owner_id='other'))['mandate'])
        self.assertIsNone(restarted.workspace(replace(self.ctx,network='tron-mainnet'))['mandate'])
    def test_idempotency_and_false_display_do_not_mutate(self):
        self.bridge.mutate(self.ctx,'POST','/v1/mandates',self.payload,'create')
        before=self.bridge.workspace(self.ctx)
        self.bridge.mutate(self.ctx,'POST','/v1/mandates',self.payload,'create')
        self.assertEqual(before,self.bridge.workspace(self.ctx))
        bad=copy.deepcopy(self.payload);bad['constraints']['min_cash_bps']=0
        with self.assertRaises(MachineError):self.bridge.mutate(self.ctx,'POST','/v1/mandates',bad,'new')
        self.assertEqual(before,self.bridge.workspace(self.ctx))
        with self.assertRaises(MachineError):self.bridge.mutate(self.ctx,'POST','/v1/mandates',bad,'create')
    def test_gateway_cannot_be_spoofed_by_wallet_header(self):
        app=FastAPI();app.include_router(router_for(self.bridge,'a'*40));client=TestClient(app)
        self.assertEqual(client.get('/v1/machine/workspace?network=nile',headers={'x-verified-wallet':self.ctx.wallet}).status_code,401)
        headers={'Authorization':'Bearer '+'a'*40,'x-machine-workspace':'12345678-abcd-efgh','x-verified-wallet':self.ctx.wallet}
        self.assertEqual(client.get('/v1/machine/workspace?network=nile',headers=headers).status_code,200)
        headers.pop('x-verified-wallet')
        self.assertEqual(client.post('/v1/machine/mandates',json=self.payload,headers=headers).status_code,401)
        self.assertEqual(client.get('/v1/machine/workspace?network=invalid',headers=headers).status_code,409)

if __name__=='__main__':unittest.main()
