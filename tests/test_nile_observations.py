"""Nile source identity, partial failure, ABI and replay trust boundaries."""
import copy
import unittest
from finance_service.nile_observations import NileAssembler, MANIFEST, uint
from economic_machine.tron_sources import address_hex
from economic_machine.values import MachineError, canonical, digest

AT='2026-09-29T09:00:00+00:00'
SCOPE=dict(tenant_id='t',owner_id='o',wallet=address_hex(MANIFEST['token']),network='tron-nile')

class NileObservationsTests(unittest.TestCase):
    def setUp(self):
        self.assembler=NileAssembler();self.failed=set();self.token=MANIFEST['token']
    def request(self,url,body):
        function=body.get('function_selector')
        if function in self.failed:raise TimeoutError()
        if url.endswith('/getcontract'):
            return canonical({'contract_address':address_hex(MANIFEST['market']),'bytecode':'6000'})
        values={'underlying()':int(address_hex(self.token)[2:],16),
            'comptroller()':int(address_hex(MANIFEST['comptroller'])[2:],16),
            'decimals()':6,'supplyRatePerBlock()':269,'getCash()':100000000,'balanceOf(address)':12000000}
        return canonical({'result':{'result':True},'constant_result':[hex(values[function])[2:].rjust(64,'0')+'0'*128]})
    def read(self):return self.assembler.read(SCOPE,lambda:AT,self.request)
    def test_replay_identity_and_non_atomic_read_never_enable_execution(self):
        snapshot=self.read();self.assertEqual(snapshot,self.assembler.verify(snapshot))
        self.assertEqual(snapshot['wallet_token_balance'],'12')
        self.assertFalse(snapshot['execution_enabled'])
        cap=snapshot['products']['justlend.v1.jUSDT']['capability']
        self.assertEqual(cap['stages']['read']['status'],'SUPPORTED')
        self.assertEqual(cap['stages']['execute']['status'],'UNSUPPORTED')
        self.assertTrue(all(not v['state_eligible'] for v in snapshot['facts'].values()))
        forged=copy.deepcopy(snapshot);forged['facts']['justlend.v1.jUSDT.supply_apy']['value']='1'
        with self.assertRaises(MachineError):self.assembler.verify(forged)
        forged=copy.deepcopy(snapshot);forged['scope']['network']='tron-mainnet'
        with self.assertRaises(MachineError):self.assembler.verify(forged)
    def test_rpc_failure_is_missing_not_zero(self):
        self.failed={'getCash()','balanceOf(address)'};snapshot=self.read()
        self.assertIsNone(snapshot['wallet_token_balance'])
        self.assertEqual(snapshot['facts']['justlend.v1.jUSDT.available_cash']['quality'],'MISSING')
        self.assertIsNone(snapshot['facts']['justlend.v1.jUSDT.available_cash']['value'])
        self.assertEqual(snapshot,self.assembler.verify(snapshot))
    def test_wrong_underlying_and_abi_padding_are_rejected(self):
        self.token=MANIFEST['comptroller']
        with self.assertRaisesRegex(MachineError,'identity differs'):self.read()
        with self.assertRaises(MachineError):uint({'result':{'result':True},'constant_result':['0'*191+'1']})
    def test_recommitted_wrong_source_and_stale_capture_are_rejected(self):
        capture=copy.deepcopy(self.read()['captures'][0])
        with self.assertRaisesRegex(MachineError,'skew'):
            self.assembler.assemble(capture,'2026-09-29T09:03:00Z',SCOPE)
        capture['rpc_url']='https://api.trongrid.io';capture.pop('capture_hash');capture['capture_hash']=digest(capture)
        with self.assertRaisesRegex(MachineError,'origin'):
            self.assembler.assemble(capture,AT,SCOPE)
