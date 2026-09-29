"""Independent accounting invariants; all positions below are synthetic."""
import copy, unittest
from finance_service.native_performance import refresh

AT='2026-09-29T12:00:00+00:00'
LATER='2026-09-30T12:00:00+00:00'

def event(tx,action,shares,principal,fee,before,after,at=AT):
    return {'txid':tx,'action':action,'shares':str(shares),'principal_sun':str(principal),
        'fee_sun':str(fee),'before_shares':str(before),'after_shares':str(after),'at':at,
        'entry_estimate_sun':'200000','reconciliation_hash':'h',
        'forecast':{'horizon_seconds':86400,'gross_total_sun':'1000000','net_total_sun':'500000','entry_estimate_sun':'200000'} if action=='SUPPLY' else None}

class PerformanceTests(unittest.TestCase):
    def evaluate(self,events,shares,value):
        state={'workspace':{'evidence':[]},'native_accounting':{'version':1,'events':{e['txid']:e for e in events}}}
        account={'positions':[{'shares_base_units':str(shares),'underlying_base_units':str(value)}]}
        return refresh(state,account,{'debt':'0'},LATER)
    def test_unrealized_and_realized_do_not_count_deposits_or_wallet_cash_as_income(self):
        a=event('a','SUPPLY',100,10_000_000,100_000,0,100)
        p=self.evaluate([a],100,11_000_000)
        self.assertEqual(p['accrued']['value'],'1');self.assertEqual(p['realized']['value'],'0')
        self.assertEqual(p['expected_return']['value'],'0.5');self.assertEqual(p['expected_to_date']['value'],'0.8')
        self.assertEqual(p['net_income']['value'],'0.9');self.assertEqual(p['variance']['value'],'0.1')
    def test_partial_then_full_redemption_conserves_cost_basis(self):
        a=event('a','SUPPLY',3,10_000_000,100_000,0,3)
        b=event('b','REDEEM',1,4_000_000,100_000,3,2,at=LATER)
        p=self.evaluate([a,b],2,8_000_000)
        self.assertEqual(p['open_cost_basis']['value'],'6.666667')
        self.assertEqual(p['realized']['value'],'0.666667')
        self.assertEqual(p['net_income']['value'],'1.8')
        c=event('c','REDEEM',2,8_000_000,100_000,2,0,at='2026-09-30T12:00:01+00:00')
        p=self.evaluate([a,b,c],0,0)
        self.assertEqual(p['realized']['value'],'2');self.assertEqual(p['accrued']['value'],'0')
        self.assertEqual(p['net_income']['value'],'1.7');self.assertEqual(p['open_cost_basis']['value'],'0')
    def test_external_share_transfer_does_not_fabricate_income(self):
        a=event('a','SUPPLY',100,10_000_000,100_000,0,100)
        p=self.evaluate([a],200,20_000_000)
        self.assertEqual(p['status'],'INCOMPLETE');self.assertIsNone(p['accrued']);self.assertIsNone(p['net_income'])
        self.assertEqual(p['fees']['value'],'0.1')
    def test_missing_forecast_does_not_hide_known_actual_results(self):
        a=event('a','SUPPLY',100,10_000_000,100_000,0,100);a['forecast']=None
        p=self.evaluate([a],100,11_000_000)
        self.assertIsNone(p['expected_return']);self.assertEqual(p['net_income']['value'],'0.9')
    def test_display_keeps_a_one_sun_loss_instead_of_inventing_yield(self):
        a=event('a','SUPPLY',100,1_000_000,8_089_400,0,100)
        p=self.evaluate([a],100,999999)
        self.assertEqual(p['accrued']['value'],'-0.000001');self.assertEqual(p['net_income']['value'],'-8.089401')
    def test_failed_entry_fee_is_a_loss_without_a_fabricated_original_forecast(self):
        a=event('a','FEE',0,0,800000,0,0)
        p=self.evaluate([a],0,0)
        self.assertEqual(p['fees']['value'],'0.8');self.assertEqual(p['net_income']['value'],'-0.8')
        self.assertEqual(p['realized']['value'],'0');self.assertIsNone(p['expected_return'])
    def test_return_denominator_remains_supplied_capital_after_full_withdrawal(self):
        a=event('a','SUPPLY',100,10_000_000,100_000,0,100)
        b=event('b','REDEEM',100,11_000_000,100_000,100,0,at=LATER)
        p=self.evaluate([a,b],0,0)
        self.assertEqual(p['supplied_capital']['value'],'10')
        self.assertEqual(p['net_return_pct'],'8.000000')
        self.assertEqual(p['expected_return_pct'],'6.000000')
    def test_return_is_unknown_without_principal_or_with_incomplete_history(self):
        a=event('a','FEE',0,0,800000,0,0)
        self.assertIsNone(self.evaluate([a],0,0)['net_return_pct'])
        a=event('a','SUPPLY',100,1_000_000,8_089_400,0,100)
        self.assertIsNone(self.evaluate([a],200,2_000_000)['net_return_pct'])
        self.assertEqual(self.evaluate([a],100,1_000_000)['net_return_pct'],'-808.940000')
