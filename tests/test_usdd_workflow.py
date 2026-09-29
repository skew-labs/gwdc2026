import copy
import unittest
from decimal import Decimal
from economic_machine.usdd_workflow import *
from finance_service.resource_costs import quote_resources

OWNER='41'+'12'*20
PROXY='41'+'34'*20
TOKEN=address_hex(CONFIG['tron-mainnet']['token'])
PARAMS={'chainParameter':[{'key':'getEnergyFee','value':100},{'key':'getTransactionFee','value':1000}]}

class UsddWorkflowTests(unittest.TestCase):
    def test_precise_loop_600_and_rounding(self):
        s=build_steps(amount=600*10**18,loops=2,borrow_bps=5000)
        self.assertEqual([x['amount'] for x in s if x['operation']=='BORROW'],[str(300*10**18),str(150*10**18)])
        self.assertEqual([x['operation'] for x in s[:3]],['APPROVE_MARKET','SUPPLY','ENTER_MARKET'])
        with self.assertRaises(MachineError):build_steps(amount=1,loops=1,borrow_bps=5000)
        with self.assertRaises(MachineError):build_steps(amount=600,loops=5,borrow_bps=5000)
        with self.assertRaises(MachineError):units('0.0000000000000000001')

    def test_vault_composition_pins_every_contract_and_owner(self):
        c=CONFIG['tron-mainnet'];s={'operation':'VAULT_DRAW','amount':str(600*10**18),'collateral_sun':'3000000000','ilk':'TRX-C'}
        state={'proxy':PROXY,'proxy_owner':OWNER,'cdp_id':'42','cdp_owner':PROXY,'ilk':'TRX-C','token':TOKEN}
        call=compile_call('tron-mainnet',OWNER,s,state)
        self.assertEqual(call['value_sun'],'3000000000')
        self.assertEqual(call['target'],PROXY)
        raw=call['data'][8:]
        self.assertEqual(raw[:64],addr(c['actions']))
        self.assertEqual(int(raw[64:128],16),64)
        inner=raw[192:192+int(raw[128:192],16)*2]
        expected=encode('lockTRXAndDraw(address,address,address,address,uint256,uint256)',[c['manager'],c['jug'],c['joins']['TRX-C'],c['join'],42,600*10**18])
        self.assertEqual(inner,expected)
        for field in ('proxy_owner','cdp_owner'):
            broken={**state,field:'41'+'99'*20}
            with self.assertRaises(MachineError):compile_call('tron-mainnet',OWNER,s,broken)
        with self.assertRaises(MachineError):encode('transfer(address,uint256)',[OWNER,1])

    def test_vat_collateral_18_decimals_is_released_in_trx_sun(self):
        state={'proxy':PROXY,'proxy_owner':OWNER,'cdp_id':'42','cdp_owner':PROXY,'ilk':'TRX-C','token':TOKEN,'vault_debt':'0'}
        s={'operation':'VAULT_FREE','amount':str(3*10**18),'ilk':'TRX-C'}
        call=compile_call('tron-mainnet',OWNER,s,state)
        raw=call['data'][8:];inner=raw[192:192+int(raw[128:192],16)*2]
        self.assertEqual(int(inner[-64:],16),3*10**6)
        with self.assertRaises(MachineError):compile_call('tron-mainnet',OWNER,s,{**state,'vault_debt':'1'})
        with self.assertRaises(MachineError):compile_call('tron-mainnet',OWNER,{**s,'amount':'1'},state)

    def test_nile_addresses_never_inherit_mainnet_and_allowance_is_exact(self):
        s={'operation':'APPROVE_MARKET','amount':str(600*10**18)}
        o={'token':address_hex('TZ78R2E6ejfFhxq8hxrmuqT6hGBxjHQbo4')}
        call=compile_call('tron-nile',OWNER,s,o)
        self.assertEqual(call['target'],o['token'])
        self.assertEqual(call['data'][8:72],addr(CONFIG['tron-nile']['market']))
        self.assertEqual(int(call['data'][72:],16),600*10**18)
        self.assertNotIn(addr(CONFIG['tron-mainnet']['market']),call['data'])

    def test_resources_are_consumed_once_and_partial_bandwidth_not_discounted(self):
        q=quote_resources(80894,300,PARAMS,{'EnergyLimit':100000,'EnergyUsed':30000,'freeNetLimit':600,'freeNetUsed':400})
        self.assertEqual(q['expected_total_burn_sun'],1389400)
        self.assertEqual(q['full_total_burn_bound_sun'],8389400)
        self.assertEqual(q['remaining']['energy'],0)
        q2=quote_resources(80894,300,PARAMS,{'EnergyLimit':q['remaining']['energy'],'freeNetLimit':q['remaining']['free_bandwidth']})
        self.assertEqual(q2['expected_total_burn_sun'],8389400)
        with self.assertRaises(MachineError):quote_resources(80894,300,PARAMS,{'EnergyUsed':-1})

    def test_borrowing_600_is_not_600_equity(self):
        own=loop_cashflow('600','.000008542','.04005','.07252','0',days='365',loops=0,borrow_bps=0,costs=['5'],equity='600')
        debt=loop_cashflow('600','.000008542','.04005','.07252','.005',days='365',loops=0,borrow_bps=0,costs=['5'],equity='900')
        self.assertEqual(Decimal(own['net_income_usdd'])-Decimal(debt['net_income_usdd']),Decimal(3))
        self.assertLess(Decimal(debt['holding_return']),Decimal(own['holding_return']))
        loop=loop_cashflow('600','.000008542','.04005','.07252','0',days='365',loops=2,borrow_bps=5000,costs=['10'],equity='600')
        self.assertLess(Decimal(loop['net_income_usdd']),Decimal(own['net_income_usdd']))
        self.assertLess(Decimal(loop['marginal_loop_return']),0)
        short=loop_cashflow('600','.000008542','.04005','.07252','0',days='7',loops=0,borrow_bps=0,costs=['5'],equity='600')
        self.assertLess(Decimal(short['net_income_usdd']),0)

    def test_false_receipt_does_not_unlock_and_confirmation_is_idempotent(self):
        wf={'network':'tron-mainnet','owner':OWNER,'cursor':0,'status':'SUBMISSION_UNKNOWN','confirmed_txids':[],
          'steps':[{'id':'s1','operation':'SUPPLY','amount':'100','minimum_shares':'9','status':'SUBMISSION_UNKNOWN','txid':'a'*64,
              'before':{'token_balance':'100','shares':'0'}}, {'id':'s2','status':'WAITING'}]}
        after={'token_balance':'0','shares':'10'}
        proof={'txid':'a'*64,'solidified':True,'success':True,'fee_sun':'0','logs':[]}
        with self.assertRaises(MachineError):advance(wf,'a'*64,{**proof,'solidified':False},after)
        broken=advance(wf,'a'*64,proof,{**after,'shares':'0'})
        self.assertEqual(broken['status'],'DISPUTED');self.assertEqual(broken['cursor'],0)
        failed=advance(wf,'a'*64,{**proof,'logs':[{'address':address_hex(CONFIG['tron-mainnet']['market'])[2:],'topics':[FAILURE]}]},after)
        self.assertEqual(failed['status'],'NEEDS_RECOVERY');self.assertEqual(failed['cursor'],0)
        done=advance(wf,'a'*64,proof,after)
        self.assertEqual(done['cursor'],1);self.assertEqual(done['status'],'AWAITING_NEXT_REVIEW')
        self.assertEqual(advance(done,'a'*64,proof,after),done)
        self.assertEqual(wf['cursor'],0)

if __name__=='__main__':unittest.main()
