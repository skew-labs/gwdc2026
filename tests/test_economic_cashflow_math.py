"""Independent monetary vectors and adversarial TRON accounting cases."""

import unittest
from decimal import Decimal, ROUND_UP, localcontext
from fractions import Fraction

from economic_machine.liquidity import (available_at, check_schedule, lending_exit,
    native_exit, net_recovery, recovery_curve)
from economic_machine.resource_cost import rental_income, resource_budget, transaction_costs
from economic_machine.values import MachineError
from economic_machine.vault_accounting import vault_cashflow
from economic_machine.yield_math import YEAR, net_cashflow, period_yield, reward_income


def rate(value='0.1', convention='SIMPLE_APR'):
    return {'annual_fraction': value, 'convention': convention, 'day_count': 'ACT_365F'}


def window():
    return dict(energy_capacity=100, energy_used=10, energy_reserved=10, energy_rented_out=60,
                energy_received=0, bandwidth_staked=100, bandwidth_free=200)


def tx(name='step-a', energy=30, size=150):
    return {'id': name, 'caller_energy': energy, 'bytes': size,
            'max_energy_burn_sun': 100000, 'fee_limit_sun': 100000}


def vault(**edits):
    args = dict(collateral_base='1000', deployed_usdd='400', stored_debt_usdd='400',
        fee_rate=rate('0.1'), fee_age_seconds=0, horizon_seconds=YEAR,
        destination_rate=rate('0.2'), usdd_price_base='1', min_ratio='1.5', buffer_bps=0,
        shocks=[{'name': 'fall', 'collateral_change_bps': -1000,
                 'usdd_change_bps': 0, 'deployment_loss_bps': 0}])
    args.update(edits)
    return vault_cashflow(**args)


class YieldMathTests(unittest.TestCase):
    def test_apr_matches_independent_fraction_floor(self):
        for annual in ('0', '0.0525', '-0.25'):
            for seconds in (1, 1800, 86400, YEAR):
                with self.subTest(annual=annual, seconds=seconds):
                    value = Fraction(12345) * Fraction(annual) * seconds / YEAR
                    atoms = (value * 10**6).__floor__()
                    result = period_yield('12345', rate(annual), seconds)
                    self.assertEqual(Decimal(result['income']), Decimal(atoms)/10**6)

    def test_apy_half_year_square_root_and_negative_yield(self):
        self.assertEqual(period_yield('1000', rate('0.21', 'EFFECTIVE_APY'), YEAR//2)['income'], '100')
        self.assertEqual(period_yield('1000', rate('-0.19', 'EFFECTIVE_APY'), YEAR//2)['income'], '-100')
        self.assertEqual(period_yield('1000', rate('0.21'), YEAR//2)['income'], '105')

    def test_revenue_floor_and_liability_ceiling_smallest_atom(self):
        self.assertEqual(period_yield('1', rate('0.1'), 1)['income'], '0')
        self.assertEqual(period_yield('1', rate('0.1'), 1, liability=True)['income'], '0.000001')
        self.assertEqual(period_yield('1', rate('-0.1'), 1)['income'], '-0.000001')

    def test_decimal_context_does_not_change_results(self):
        expected = period_yield('123456.78', rate('0.07123', 'EFFECTIVE_APY'), 123456)
        with localcontext() as ctx:
            ctx.prec, ctx.rounding = 5, ROUND_UP
            self.assertEqual(period_yield('123456.78', rate('0.07123', 'EFFECTIVE_APY'), 123456), expected)
            self.assertEqual(vault()['net_income_base'], '40')

    def test_reject_unknown_conventions_invalid_types_and_unbounded_values(self):
        for value in ('-1', '11', 'NaN', '1e2', True, 0.1):
            with self.subTest(value=value), self.assertRaises(MachineError):
                period_yield('100', rate(value), 1)
        for seconds in (-1, True, YEAR+1, 1.5):
            with self.assertRaises(MachineError):
                period_yield('100', rate(), seconds)
        for spec in (rate('0.1','APY'), dict(rate(), day_count='ACT_360')):
            with self.assertRaises(MachineError):
                period_yield('100', spec, 1)
        with self.assertRaises(MachineError):
            period_yield('1' + '0'*31, rate(), 1)

    def test_rewards_unclaimable_then_boundary_and_haircut(self):
        for when, expected in ((None,'0'), (YEAR+1,'0'), (YEAR,'75'), (0,'75')):
            r = reward_income('1000', rate(), YEAR, claim_after_seconds=when, haircut_bps=2500)
            self.assertEqual(r['claimable_income'], expected)
            self.assertEqual(r['gross_accrued'], '100')
        with self.assertRaises(MachineError):
            reward_income('1000', rate('-0.1'), YEAR, claim_after_seconds=0, haircut_bps=0)

    def test_net_costs_reserved_even_when_income_is_negative(self):
        r = net_cashflow('1000', '-2', dict(entry='0.0000001', exit='3', conversion='1', network='2'))
        self.assertEqual(r['total_cost_base'], '6.000001')
        self.assertEqual(r['net_income_base'], '-8.000001')
        self.assertEqual(r['required_budget_base'], '1006.000001')


class LiquidityTests(unittest.TestCase):
    def test_sub_day_locks_are_not_truncated(self):
        curve = native_exit('1000', lock_remaining_seconds=1, resource_recovery_seconds=3600, unstake_seconds=14*86400)
        self.assertEqual(available_at(curve,14*86400),0)
        self.assertEqual(available_at(curve,14*86400+3601),1000)

    def test_cash_shortage_stays_unknown_without_interpolation(self):
        curve = lending_exit('1000','300',settlement_seconds=30)
        self.assertEqual(curve['unknown_amount'],'700')
        self.assertEqual(available_at(curve,29),0)
        self.assertEqual(available_at(curve,YEAR),300)

    def test_tranches_are_cumulative_and_conserve_principal(self):
        curve = recovery_curve('100',[{'amount':'60','after_seconds':10}, {'amount':'40','after_seconds':10}])
        self.assertEqual(curve['checkpoints'],[{'after_seconds':10,'available':'100'}])
        for rows in ([{'amount':'99','after_seconds':0}], [{'amount':'101','after_seconds':0}],
                     [{'amount':'100','after_seconds':True}]):
            with self.assertRaises(MachineError):
                recovery_curve('100',rows)

    def test_debt_and_exit_fee_deducted_once_from_cumulative_cash(self):
        curve = recovery_curve('100',[{'amount':'40','after_seconds':10}, {'amount':'60','after_seconds':20}])
        result = net_recovery(curve,'2',debt_due='50',exit_cost='10',delay_seconds=3)
        self.assertEqual(available_at(result,12),0)
        self.assertEqual(available_at(result,13),20)
        self.assertEqual(available_at(result,23),140)

    def test_requirements_cannot_spend_unknown_exit_or_future_claim(self):
        curve = lending_exit('1000','300',settlement_seconds=30)
        r=check_schedule(curve,'100',[{'after_seconds':0,'minimum':'100'}, {'after_seconds':30,'minimum':'401'}])
        self.assertTrue(r[0]['satisfied']); self.assertFalse(r[1]['satisfied'])
        with self.assertRaises(MachineError):
            check_schedule(curve,'0',[{'after_seconds':2,'minimum':'2'}, {'after_seconds':1,'minimum':'1'}])


class ResourceTests(unittest.TestCase):
    def costs(self, w=None, txs=None):
        return transaction_costs(w or window(), txs or [tx()],
            {'sun_per_energy':100,'sun_per_byte':1000,'trx_price_base':'0.25'})

    def test_rented_out_energy_is_removed_before_transaction_costs(self):
        r=self.costs()
        self.assertEqual(r['transactions'][0]['energy_consumed'],20)
        self.assertEqual(r['burn_sun'],1000)
        self.assertEqual(r['cost_base'],'0.00025')

    def test_rented_in_energy_cannot_be_exported_again(self):
        w=window();w.update(energy_received=100,energy_rented_out=101)
        with self.assertRaisesRegex(MachineError,'rented out again'):
            resource_budget(w)

    def test_no_capacity_double_spend_between_two_transactions(self):
        r=self.costs(txs=[tx(),tx('step-b')])
        self.assertEqual(r['burn_sun'],154000)
        self.assertEqual(r['remaining']['energy'],0)
        self.assertEqual(r['transactions'][1]['bandwidth_source'],'TRX_BURN')

    def test_bandwidth_cannot_combine_partial_quotas(self):
        w=window();w['bandwidth_free']=100
        r=self.costs(w)
        self.assertEqual(r['transactions'][0]['bandwidth_burn_sun'],150000)
        self.assertEqual(r['remaining']['bandwidth_staked'],100)
        self.assertEqual(r['remaining']['bandwidth_free'],100)

    def test_fee_limit_includes_staked_energy_even_without_burn(self):
        item=tx(energy=20);item['fee_limit_sun']=1999
        with self.assertRaisesRegex(MachineError,'fee_limit'):
            self.costs(txs=[item])
        item['fee_limit_sun']=2000
        self.assertEqual(self.costs(txs=[item])['burn_sun'],0)

    def test_energy_burn_budget_and_reservations_fail_closed(self):
        item=tx();item['max_energy_burn_sun']=999
        with self.assertRaisesRegex(MachineError,'burn exceeds'):
            self.costs(txs=[item])
        w=window();w['energy_reserved']=31
        with self.assertRaises(MachineError):
            resource_budget(w)
        with self.assertRaises(MachineError):
            self.costs(txs=[tx(),tx()])

    def test_rental_income_cannot_add_to_strx_or_exceed_allocated_capacity(self):
        kw=dict(rented_energy=60,quoted_base_per_energy='0.001',occupied_windows=10,commission_bps=1000)
        self.assertEqual(rental_income(window(),**kw)['net_base'],'0.54')
        with self.assertRaises(MachineError):
            rental_income(window(),**kw,strx_aggregate=True)
        kw['rented_energy']=61
        with self.assertRaises(MachineError):
            rental_income(window(),**kw)


class VaultTests(unittest.TestCase):
    def test_minted_principal_cancels_debt_and_only_net_income_remains(self):
        r=vault()
        self.assertEqual(r['initial_nav_base'],'1000')
        self.assertEqual(r['final_nav_base'],'1040')
        self.assertEqual(r['horizon_fee_usdd'],'40')
        self.assertEqual(r['destination_end_usdd'],'480')
        self.assertEqual(r['net_income_base'],'40')

    def test_outstanding_fee_is_in_debt_not_added_to_assets(self):
        r=vault(stored_debt_usdd='440')
        self.assertEqual(r['initial_nav_base'],'960')
        self.assertEqual(r['final_nav_base'],'996')
        self.assertEqual(r['net_income_base'],'36')
        self.assertEqual(r['repay_shortfall_usdd'],'4')

    def test_unaccrued_fee_and_horizon_fee_do_not_overlap(self):
        r=vault(fee_age_seconds=YEAR)
        self.assertEqual(r['unaccrued_fee_estimate_usdd'],'40')
        self.assertEqual(r['horizon_fee_usdd'],'44')

    def test_usdd_price_applies_to_assets_and_liability(self):
        r=vault(stored_debt_usdd='440',usdd_price_base='0.5')
        self.assertEqual(r['initial_nav_base'],'980')
        self.assertEqual(r['net_income_base'],'18')

    def test_boundary_ratio_and_stress_buffer_are_strict(self):
        r=vault(collateral_base='660')
        self.assertIn('VAULT_COLLATERAL_BUFFER',r['reason_codes'])
        self.assertFalse(r['scenarios'][0]['above_buffer'])
        self.assertIn('VAULT_STRESS_COLLATERAL:fall',r['reason_codes'])

    def test_deployment_loss_is_not_erased_by_matching_usdd_debt(self):
        r=vault(shocks=[dict(name='default',collateral_change_bps=0,usdd_change_bps=0,deployment_loss_bps=10000)])
        self.assertEqual(r['scenarios'][0]['nav_base'],'560')
        self.assertEqual(r['scenarios'][0]['loss_base'],'440')

    def test_negative_destination_yield_and_debt_increase_are_losses(self):
        self.assertEqual(vault(destination_rate=rate('-0.1'))['net_income_base'],'-80')
        with self.assertRaises(MachineError):
            vault(deployed_usdd='401')
        with self.assertRaisesRegex(MachineError,'token atom'):
            vault(deployed_usdd='399.0000000000000000001')


if __name__ == '__main__':
    unittest.main()
