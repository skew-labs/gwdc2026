"""Raw snapshot -> confirmed conditions -> financial calculation and exclusions."""

import json
import unittest
from decimal import Decimal, localcontext

from economic_machine.snapshot_assembly import SnapshotAssembler
from economic_machine.tron_cashflow import VERSION, calculate_tron_cashflows, verify_tron_cashflows, rate
from economic_machine.tron_yield import plan_tron_yield
from economic_machine.values import MachineError
from test_economic_mandate import fixture as mandate_fixture, confirmed
from test_economic_tron_sources import AT, CONFIG, ADDRESSES, capture, fixtures, modify, rpc_fixture
from test_economic_cashflow_math import window, tx


def sources():
    captures = fixtures()
    modify(captures, 'justlend_usdd_rewards_v1', lambda p: p['data'].update(
        {contract: {'USDD':'0.03'} for _,_,contract,_,_ in ADDRESSES}))
    captures.append(capture('tron_chain_parameters', {'chainParameter':[
        {'key':'getUnfreezeDelayDays','value':14}, {'key':'getEnergyFee','value':100},
        {'key':'getTransactionFee','value':1000}]}))
    return captures


def mandate():
    raw = mandate_fixture()
    raw['scope']['network']='tron-mainnet'
    raw['terms']['withdrawals']=[]
    return raw


def quote(product='justlend.v1.jUSDT'):
    return {'product_id': product, 'principal':'1000', 'prices_base':{'USDT':'1','USDD':'0.99','TRX':'0.25'},
            'costs_base':dict(entry='1',exit='1',conversion='0',network='1'), 'redemption_seconds':30,
            'reward_claim_seconds':0,'reward_haircut_bps':0,'exit_tranches':None,
            'native':None,'vault':None,'resources':None}


def vault_quote():
    q=quote('usdd.vault.TRX-A');q['principal']='16000'
    q['vault']={'deployed_usdd':'1000','stored_debt_usdd':'1000','fee_age_seconds':0,
        'fee_convention':'EFFECTIVE_APY','destination_id':'justlend.v1.jUSDD',
        'destination_redemption_seconds':30,'collateral_release_seconds':10,
        'shocks':[{'name':'fall','collateral_change_bps':-100,'usdd_change_bps':100,'deployment_loss_bps':500}]}
    return q


class TronCashflowTests(unittest.TestCase):
    def setUp(self):
        self.assembler=SnapshotAssembler(CONFIG)
        self.captures=sources();self.raw=mandate();self.q=quote()

    def evaluate(self, q=None, raw=None, captures=None, snapshot=None, at=AT):
        s=snapshot or self.assembler.assemble(captures or self.captures, as_of=AT)
        a={'schema_version':VERSION,'snapshot_hash':s['snapshot_hash'],'quotes':[q or self.q]}
        return calculate_tron_cashflows(confirmed(raw or self.raw),s,a,assembler=self.assembler,at=at)

    def enable_vault(self):
        self.q=vault_quote()
        t=self.raw['terms']
        t['borrowing'].update(consent=True,max_debt={'asset':'USDT','amount':'2000'})
        t['allowed_actions']+=['MINT_USDD']
        t['protocol_caps_bps']['usdd']=5000
        t['price_exposure_caps_bps']['TRX']=5000

    def test_real_unit_pipeline_preserves_assumptions_and_source_hashes(self):
        result=self.evaluate();row=result['quotes'][0]
        self.assertEqual(row['status'],'CALCULATED_ASSUMPTIONS')
        self.assertEqual(result['mode'],'FIXTURE')
        self.assertEqual(result['execution_authority'],'NONE')
        self.assertEqual(row['accounting']['required_budget_base'],'1003')
        self.assertFalse(row['inputs']['justlend.v1.jUSDT.supply_apy']['state_eligible'])
        self.assertIn('SOURCE_TIME_UNKNOWN',row['inputs']['justlend.v1.jUSDT.supply_apy']['withheld_reasons'])
        self.assertEqual(self.evaluate(),result)

    def test_snapshot_raw_replay_rejects_rate_forgery(self):
        s=self.assembler.assemble(self.captures,as_of=AT)
        s['facts']['justlend.v1.jUSDT.supply_apy']['value']='0.99'
        with self.assertRaisesRegex(MachineError,'raw replay'):
            self.evaluate(snapshot=s)

    def test_receipt_expiry_checked_at_calculation_not_just_snapshot_time(self):
        s=self.assembler.assemble(self.captures,as_of='2026-09-28T12:14:00Z')
        row=self.evaluate(snapshot=s,at='2026-09-28T12:16:00Z')['quotes'][0]
        self.assertEqual(row['status'],'WITHHELD')
        self.assertIn('INPUT_RECEIPT_EXPIRED',row['reason_codes'][0])

    def test_stale_future_snapshot_and_cross_network_are_rejected(self):
        for at in ('2026-09-28T12:16:00Z', '2026-09-28T11:59:59Z'):
            with self.assertRaises(MachineError):self.evaluate(at=at)
        self.raw['scope']['network']='tron-nile'
        with self.assertRaisesRegex(MachineError,'scope'):
            self.evaluate()

    def test_missing_reward_is_not_silently_zero_income(self):
        modify(self.captures,'justlend_usdd_rewards_v1',lambda p:p['data'].clear())
        row=self.evaluate()['quotes'][0]
        self.assertEqual(row['status'],'WITHHELD')
        self.assertIn('reward_apy',row['reason_codes'][0])

    def test_fee_increase_excludes_candidate_that_used_all_available_budget(self):
        self.q['principal']='6999';self.raw['terms']['limits']['single_amount']['amount']='7000'
        row=self.evaluate()['quotes'][0]
        self.assertIn('CAPITAL_WITH_FEES_AND_RESERVES_EXCEEDED',row['reason_codes'])

    def test_zero_exposure_rejects_strx_and_native_and_vault_collateral(self):
        q=quote('justlend.strx');q['exit_tranches']=[{'amount':'1000','after_seconds':30}]
        self.assertIn('PRICE_EXPOSURE_LIMIT:TRX',self.evaluate(q=q)['quotes'][0]['reason_codes'])
        q=quote('tron.native.stake')
        q['native']=dict(voting_rate=rate('0.04','SIMPLE_APR'),lock_remaining_seconds=1,
                         resource_recovery_seconds=0,window=None,rental=None)
        self.assertIn('PRICE_EXPOSURE_LIMIT:TRX',self.evaluate(q=q)['quotes'][0]['reason_codes'])
        self.enable_vault();self.raw['terms']['price_exposure_caps_bps']['TRX']=0
        self.assertIn('PRICE_EXPOSURE_LIMIT:TRX',self.evaluate()['quotes'][0]['reason_codes'])

    def test_vault_without_consent_stays_in_comparison_with_reason(self):
        row=self.evaluate(q=vault_quote())['quotes'][0]
        self.assertEqual(row['status'],'WITHHELD')
        self.assertEqual(row['reason_codes'],['BORROWING_NOT_CONSENTED'])

    def test_vault_destination_change_changes_income_and_all_commitments(self):
        self.enable_vault()
        first=self.evaluate();a=first['quotes'][0]
        self.assertEqual(a['status'],'CALCULATED_ASSUMPTIONS')
        modify(self.captures,'justlend_markets_v1',lambda p:p['data']['tokenList'][1].update(supplyRate='0.1'))
        second=self.evaluate();b=second['quotes'][0]
        self.assertNotEqual(a['accounting']['net_income_base'],b['accounting']['net_income_base'])
        self.assertNotEqual(first['snapshot_hash'],second['snapshot_hash'])
        self.assertNotEqual(first['calculation_hash'],second['calculation_hash'])
        self.assertEqual(b['detail']['destination_binding']['rate']['annual_fraction'],'0.1')
        self.q['vault']['destination_apy_bps']=9999
        with self.assertRaisesRegex(MachineError,'vault quote'):
            self.evaluate()

    def test_vault_destination_cannot_point_at_borrow_rate_or_unknown_product(self):
        self.enable_vault();self.q['vault']['destination_id']='justlend.v1.jUSDD.borrow_apy'
        self.assertEqual(self.evaluate()['quotes'][0]['reason_codes'],['VAULT_DESTINATION_UNSUPPORTED'])

    def test_vault_exit_waits_for_destination_and_collateral_release(self):
        self.enable_vault();row=self.evaluate()['quotes'][0]
        self.assertEqual(row['liquidity']['checkpoints'][0]['after_seconds'],40)
        self.assertGreater(Decimal(row['repayment_reserve_base']),0)
        modify(self.captures,'justlend_markets_v1',lambda p:p['data']['tokenList'][1].update(cash='999'))
        row=self.evaluate()['quotes'][0]
        self.assertEqual(row['liquidity']['unknown_amount'],'4000')

    def test_vault_debt_cap_stress_and_protocol_capacity_exclusions(self):
        self.enable_vault()
        self.raw['terms']['borrowing']['max_debt']['amount']='990'
        self.raw['terms']['protocol_caps_bps']['justlend']=0
        self.q['vault']['shocks'][0]['deployment_loss_bps']=10000
        reasons=self.evaluate()['quotes'][0]['reason_codes']
        self.assertIn('VAULT_DEBT_LIMIT',reasons)
        self.assertIn('DESTINATION_PROTOCOL_LIMIT',reasons)
        self.assertIn('VAULT_STRESS_LOSS_LIMIT',reasons)

    def test_legacy_market_and_strx_unknown_queue_withhold(self):
        self.assertIn('LEGACY',self.evaluate(q=quote('justlend.v1.jUSDDOLD'))['quotes'][0]['reason_codes'][0])
        self.assertEqual(self.evaluate(q=quote('justlend.strx'))['quotes'][0]['reason_codes'],['STRX_EXIT_QUEUE_UNKNOWN'])

    def test_strx_zero_yield_is_valid_and_cannot_add_rental_component(self):
        modify(self.captures,'justlend_strx_v1',lambda p:p['data']['stakeInfo'].update(supplyRate='0'))
        q=quote('justlend.strx');q['exit_tranches']=[{'amount':'1000','after_seconds':30}]
        self.assertEqual(self.evaluate(q=q)['quotes'][0]['detail']['base']['income'],'0')
        q['native']={'voting_rate':rate('0.1')}
        with self.assertRaises(MachineError):self.evaluate(q=q)

    def test_negative_yield_underlying_math_is_supported(self):
        modify(self.captures,'justlend_markets_v1',lambda p:p['data']['tokenList'][0].update(supplyRate='-0.01'))
        row=self.evaluate()['quotes'][0]
        self.assertTrue(row['detail']['base']['income'].startswith('-'))
        modify(self.captures,'justlend_markets_v1',lambda p:p['data']['tokenList'][0].update(cash='-1'))
        with self.assertRaises(MachineError):self.evaluate()

    def test_negative_base_yield_reduces_liquidity_despite_claimable_rewards(self):
        modify(self.captures,'justlend_markets_v1',lambda p:p['data']['tokenList'][0].update(supplyRate='-0.5'))
        self.raw['terms']['withdrawals']=[{'after_seconds':30,'minimum':{'kind':'AMOUNT','asset':'USDT','amount':'9997'}}]
        row=self.evaluate()['quotes'][0]
        self.assertNotEqual(row['principal_loss_reserve_base'],'0')
        self.assertIn('LIQUIDITY_REQUIREMENT',row['reason_codes'])
        self.assertLess(Decimal(row['liquidity']['checkpoints'][0]['available']),1000)

    def test_reward_claim_after_horizon_is_not_spendable(self):
        a=self.evaluate()['quotes'][0]
        self.q['reward_claim_seconds']=2592001
        b=self.evaluate()['quotes'][0]
        self.assertEqual(b['detail']['reward']['claimable_income'],'0')
        self.assertNotEqual(a['accounting']['net_income_base'],b['accounting']['net_income_base'])

    def test_source_conflict_is_not_bypassed_by_api_preference(self):
        modify(self.captures,'justlend_markets_v1',lambda p:p['data']['tokenList'][0].update(cash='1001'))
        products=self.assembler.assemble(self.captures,as_of=AT)['products']
        # Existing RPC fixture has different API cash; conflict must be checked
        # on both the RPC and the API dependency, not only the RPC fact flag.
        rpc=rpc_fixture(products=products)
        s=self.assembler.assemble(self.captures,as_of=AT,rpc=rpc)
        row=self.evaluate(snapshot=s)['quotes'][0]
        self.assertEqual(row['status'],'WITHHELD')
        self.assertIn('INPUT_CONFLICT',row['reason_codes'][0])

    def test_snapshot_binding_and_duplicate_quote_rejected(self):
        s=self.assembler.assemble(self.captures,as_of=AT)
        a={'schema_version':VERSION,'snapshot_hash':'a'*64,'quotes':[self.q]}
        with self.assertRaisesRegex(MachineError,'binding'):
            calculate_tron_cashflows(confirmed(self.raw),s,a,assembler=self.assembler,at=AT)
        a.update(snapshot_hash=s['snapshot_hash'],quotes=[self.q,self.q])
        with self.assertRaisesRegex(MachineError,'duplicate'):
            calculate_tron_cashflows(confirmed(self.raw),s,a,assembler=self.assembler,at=AT)

    def test_global_decimal_context_cannot_change_hash(self):
        expected=self.evaluate()
        with localcontext() as ctx:
            ctx.prec=6
            self.assertEqual(self.evaluate(),expected)

    def test_resource_cost_uses_observed_chain_fee_and_budget(self):
        self.q['costs_base']['network']='0'
        self.q['resources']={'window':window(),'transactions':[tx()]}
        a=self.evaluate()['quotes'][0]
        self.assertEqual(a['accounting']['costs_base']['network'],'0.00025')
        modify(self.captures,'tron_chain_parameters',lambda p:p['chainParameter'][1].update(value=200))
        b=self.evaluate()['quotes'][0]
        self.assertEqual(b['accounting']['costs_base']['network'],'0.0005')
        self.q['costs_base']['network']='1'
        with self.assertRaisesRegex(MachineError,'both'):
            self.evaluate()

    def test_rental_and_costs_cannot_use_different_energy_windows(self):
        q=quote('tron.native.stake');q['costs_base']['network']='0'
        q['resources']={'window':window(),'transactions':[tx()]}
        q['native']=dict(voting_rate=rate('0.04','SIMPLE_APR'),lock_remaining_seconds=1,
            resource_recovery_seconds=0,window=window(),rental=dict(rented_energy=60,
            quoted_base_per_energy='0.001',occupied_windows=1,commission_bps=0,window_seconds=86400))
        row=self.evaluate(q=q)['quotes'][0]
        self.assertEqual(row['detail']['rental']['net_base'],'0.06')
        q['resources']['window']['energy_rented_out']=0
        with self.assertRaisesRegex(MachineError,'share one resource window'):
            self.evaluate(q=q)

    def test_unknown_or_fractional_underlying_token_amounts_rejected(self):
        self.q['principal']='1.0000001'
        with self.assertRaisesRegex(MachineError,'token atom'):
            self.evaluate()
        self.q['principal']='1000';self.q['prices_base']['USDT']='0.99'
        with self.assertRaisesRegex(MachineError,'base price'):
            self.evaluate()

    def test_calculation_replay_rejects_forged_result_and_changed_conditions(self):
        s=self.assembler.assemble(self.captures,as_of=AT)
        a={'schema_version':VERSION,'snapshot_hash':s['snapshot_hash'],'quotes':[self.q]}
        record=confirmed(self.raw)
        result=calculate_tron_cashflows(record,s,a,assembler=self.assembler,at=AT)
        self.assertTrue(verify_tron_cashflows(result,record,s,a,assembler=self.assembler,at=AT))
        result['quotes'][0]['accounting']['net_income_base']='99999'
        self.assertFalse(verify_tron_cashflows(result,record,s,a,assembler=self.assembler,at=AT))

    def test_draft_or_foreign_wallet_scope_cannot_calculate(self):
        s=self.assembler.assemble(self.captures,as_of=AT)
        a={'schema_version':VERSION,'snapshot_hash':s['snapshot_hash'],'quotes':[self.q]}
        record=confirmed(self.raw);record['status']='DRAFT'
        with self.assertRaisesRegex(MachineError,'confirmed'):
            calculate_tron_cashflows(record,s,a,assembler=self.assembler,at=AT)
        # Add scoped RPC fixture using its own owner context, different from
        # this confirmed mandate. The raw scope must remain bound on replay.
        rpc=rpc_fixture(s['products'],wallet=True)
        from test_economic_tron_sources import SCOPE
        scoped=self.assembler.assemble(self.captures,as_of=AT,scope=SCOPE,rpc=rpc)
        with self.assertRaisesRegex(MachineError,'scope'):
            self.evaluate(snapshot=scoped)

    def test_zero_legacy_strx_aggregate_was_not_an_error(self):
        from pathlib import Path
        req=json.loads((Path(__file__).resolve().parents[1]/'cases/economic_tron_yield_demo.json').read_text())
        for item in req['opportunities']:
            if item['kind']=='JUSTLEND_STRX':item['aggregate_reported_bps']=0
        result=plan_tron_yield(req)
        self.assertEqual(next(r for r in result['opportunity_details'] if r['kind']=='JUSTLEND_STRX')['annualized_rate_bps'],0)


if __name__ == '__main__':
    unittest.main()
