"""A new display period retains financial evidence and survives repository reloads."""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from economic_machine.values import MachineError
from finance_service.context import AuthenticatedContext
from finance_service.machine_bridge import MachineBridge, empty_workspace
from finance_service.operational_repository import OperationalRepository
from finance_service.native_performance import refresh
from test_native_performance import event

AT = '2026-09-30T12:00:00+00:00'


class PeriodTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = OperationalRepository(Path(self.tmp.name)/'state.sqlite')
        raw = json.loads((Path(__file__).resolve().parents[1]/'cases/economic_mandate_demo.json').read_text())
        self.ctx = AuthenticatedContext(**{**raw['scope'], 'network': 'tron-nile'}, session_id='s', trace_id='t', issued_at=AT, expires_at='2026-09-30T13:00:00+00:00')
        self.bridge = MachineBridge(self.repo, lambda: AT, None)
        self.account = {'positions': [{'shares_base_units': '0', 'underlying_base_units': '0'}]}
        self.raw = {'debt': '0', 'wallet': {'balance': 984653700}}
        self.native = SimpleNamespace(account=Mock(side_effect=lambda *_: (copy.deepcopy(self.account), copy.deepcopy(self.raw))))
        self.bridge.native = self.native
        old = [event('supply', 'SUPPLY', 100, 1000000, 8089400, 0, 100),
               event('redeem', 'REDEEM', 100, 1000000, 7256900, 100, 0, at='2026-09-29T13:00:00+00:00')]
        self.state = {'workspace': empty_workspace('nile'), 'mandates': {}, 'requests': {},
                      'native_accounting': {'version': 1, 'events': {e['txid']: e for e in old}},
                      'review_spend_ledger': {'preserved': {'fee': '15346300', 'amount': '1000000'}}}
        refresh(self.state, self.account, self.raw, AT)

    def start(self):
        return self.bridge.mutate(self.ctx, 'POST', '/v1/performance/periods', {'confirm_new_period': True}, 'one-period')

    def test_reset_restart_refresh_and_next_investment_preserve_evidence(self):
        original = copy.deepcopy(self.state)
        self.bridge.commit(self.ctx, 0, self.state)
        ack = self.start()
        restarted = MachineBridge(self.repo, lambda: AT, None)
        version, state = restarted.load(self.ctx)
        p = refresh(state, self.account, self.raw, AT)
        self.assertTrue(p['period_empty']); self.assertIsNone(p['net_return_pct'])
        self.assertIsNone(p['expected_to_date'])
        for key in ('net_income', 'realized', 'accrued', 'fees', 'supplied_capital'):
            self.assertEqual(p[key]['value'], '0')
        self.assertEqual(state['native_accounting']['events'], original['native_accounting']['events'])
        self.assertEqual(state['review_spend_ledger'], original['review_spend_ledger'])
        self.assertEqual(state['native_accounting']['period_history'][0]['performance']['net_income']['value'], '-15.3463')
        self.assertEqual(ack, self.start())
        self.assertEqual(self.native.account.call_count, 1)  # retry does not open another period
        next_event = event('new', 'SUPPLY', 200, 10000000, 100000, 0, 200, at=AT)
        state['native_accounting']['events']['new'] = next_event
        current = {'positions': [{'shares_base_units': '200', 'underlying_base_units': '11000000'}]}
        p = refresh(state, current, self.raw, AT)
        self.assertFalse(p['period_empty']); self.assertEqual(p['net_income']['value'], '0.9')
        self.assertEqual(p['net_return_pct'], '9.000000'); self.assertEqual(p['txids'], ['new'])
        self.assertEqual(p['period_id'], ack['period_id'])
        restarted.commit(self.ctx, version, state)
        self.assertIsNone(restarted.workspace(replace(self.ctx, owner_id='another-owner'))['performance'])

    def test_unresolved_transactions_and_open_positions_cannot_be_reset(self):
        changes = [
            {'native_requests': {'pending': {}}},
            {'usdd_execution': {'status': 'AWAITING_SIGNATURE', 'steps': [{}], 'cursor': 0}},
            {'workspace': {**self.state['workspace'], 'execution': {'status': 'SUBMITTED'}}},
            {'workspace': {**self.state['workspace'], 'positions': [{'id': 'open'}]}},
        ]
        for change in changes:
            with self.subTest(change=change):
                state = {**copy.deepcopy(self.state), **change}
                version, _ = self.bridge.load(self.ctx)
                self.bridge.commit(self.ctx, version, state)
                with self.assertRaises(MachineError): self.start()
                self.assertNotIn('active_period', self.bridge.load(self.ctx)[1]['native_accounting'])

    def test_fresh_chain_read_blocks_stale_zero_position_and_debt(self):
        self.bridge.commit(self.ctx, 0, self.state)
        self.account['positions'][0]['shares_base_units'] = '1'
        with self.assertRaisesRegex(MachineError, 'live position'): self.start()
        self.account['positions'][0]['shares_base_units'] = '0'; self.raw['debt'] = '1'
        with self.assertRaisesRegex(MachineError, 'repay debt'): self.start()

    def test_late_old_receipt_is_not_attributed_to_new_period(self):
        self.bridge.commit(self.ctx, 0, self.state); self.start()
        _, state = self.bridge.load(self.ctx)
        state['native_accounting']['events']['late'] = event('late', 'FEE', 0, 0, 100000, 0, 0)
        p = refresh(state, self.account, self.raw, AT)
        self.assertEqual(p['status'], 'INCOMPLETE'); self.assertIsNone(p['net_income'])
