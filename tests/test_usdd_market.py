import copy,unittest
from datetime import datetime
from finance_service.usdd_market import normalize_market,MARKET,TOKEN
from finance_service.usdd_chain import UsddChain
from economic_machine.usdd_workflow import word,addr,CONFIG
from economic_machine.tron_sources import address_hex
from economic_machine.values import MachineError

AT='2026-09-30T00:00:00+00:00'
class MarketTests(unittest.TestCase):
    def setUp(self):
        self.entry={'jtokenAddress':MARKET,'collateralAddress':TOKEN,'collateralDecimal':18,'isValid':1,'lastBlocktimestamp':int(datetime.fromisoformat(AT).timestamp()*1000)}
        self.markets={'code':0,'data':{'jtokenList':[self.entry],'trxPrice':str(3*10**17)}}
        self.details={'code':0,'data':{**self.entry,'depositedUSD':'1000000','farmRewardUSD24h':'100','farmRewardUsddAmount24h':'100','farmRewardTrxAmount24h':'0','priceUSD':'1'}}
    def test_daily_budget_is_apr_without_guaranteed_duration_or_compounding(self):
        r=normalize_market(self.markets,self.details,AT)
        self.assertEqual(r['reward_apr'],'0.0365');self.assertIsNone(r['reward_end_at'])
        self.assertFalse(r['reward_auto_compounded']);self.assertFalse(r['reward_guaranteed'])
    def test_binding_staleness_and_reward_currency_mismatch_fail_closed(self):
        for field,value in [('collateralAddress',CONFIG['tron-nile']['token']),('farmRewardUsddAmount24h','1000'),('depositedUSD','0')]:
            d=copy.deepcopy(self.details);d['data'][field]=value
            with self.assertRaises(MachineError):normalize_market(self.markets,d,AT)
        self.entry['lastBlocktimestamp']-=3600001
        with self.assertRaises(MachineError):normalize_market(self.markets,self.details,AT)
    def test_complete_entered_market_array_is_bounded_and_canonical(self):
        c=UsddChain('tron-mainnet','41'+'12'*20,None,lambda:AT)
        c.rpc=lambda *a:{'result':{'result':True},'constant_result':[word(32)+word(1)+addr(MARKET)]}
        self.assertEqual(c.entered_markets(),[address_hex(MARKET)])
        for raw in (word(32)+word(2)+addr(MARKET),word(0)+word(0),word(32)+word(1)+word(2**200)):
            c.rpc=lambda *a:{'result':{'result':True},'constant_result':[raw]}
            with self.assertRaises(MachineError):c.entered_markets()
