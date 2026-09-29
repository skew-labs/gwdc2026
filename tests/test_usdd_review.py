import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace

from economic_machine.tron_sources import address_hex
from economic_machine.values import MachineError, digest
from finance_service.usdd_review import UsddReviews, CONFIG, RAY, RAD, words
from finance_service.machine_bridge import MachineBridge, empty_workspace
from finance_service.context import AuthenticatedContext
from finance_service.operational_repository import OperationalRepository
from test_portfolio_review import policy, AT


def result(*args):
    return {'result': {'result': True}, 'constant_result': [''.join(format(v, '064x') for v in args)]}


def addr(value):
    return int(address_hex(value)[2:], 16)


def evidence(network='tron-nile', mismatch=True):
    c=CONFIG[network]
    block={'blockID': format(10,'016x')+'a'*48, 'block_header': {'raw_data': {'number':10,'timestamp':int(datetime.fromisoformat(AT).timestamp()*1000)-30000}}}
    r={'start_block':block, 'end_block':copy.deepcopy(block),
       'energy':{'chainParameter':[{'key':'getEnergyFee','value':100},{'key':'getTransactionFee','value':1000}]},
       'join_token':result(addr(c['usdd'])), 'underlying':result(addr('TZ78R2E6ejfFhxq8hxrmuqT6hGBxjHQbo4') if mismatch else addr(c['usdd'])),
       'market_controller':result(addr(c['comptroller'])), 'market':result(1,9*10**17,0),
       'mint_paused':result(0), 'borrow_paused':result(0), 'vat_live':result(1),
       'supply_rate':result(40000000000,0,0),'borrow_rate':result(41000000000,0,0),
       'cash':result(13*10**18), 'jug_base':result(0)}
    for ilk in ('TRX-A','TRX-B','TRX-C'):
        r['vat:'+ilk]=result(0,RAY,RAY//4,1000000*RAD,600*RAD)
        r['spot:'+ilk]=result(1,13*RAY//10)
        r['jug:'+ilk]=result(1000000000158153903837946257,1)
    return r


def state():
    p=policy()
    return {'workspace':empty_workspace('nile'),'mandates':{'m':{'revisions':[p]}},'requests':{},'confirmed_review_inputs':{p['policy_hash']:p['review_inputs']}}


class UsddReviewTests(unittest.TestCase):
    def test_live_token_mismatch_and_negative_recursive_spread_are_independent(self):
        s=state(); before=copy.deepcopy(s)
        r=UsddReviews.assess('tron-nile',evidence(),{},s,AT)
        self.assertIn('VAULT_DESTINATION_TOKEN_MISMATCH',r['blockers'])
        self.assertIn('RECURSIVE_SPREAD_NOT_POSITIVE',r['blockers'])
        self.assertIn('VAULT_MINIMUM_DEBT',r['blockers'])
        self.assertIn('BORROWING_NOT_CONSENTED',r['blockers'])
        self.assertNotIn('LIVE_INPUT_UNAVAILABLE',r['blockers'])
        self.assertEqual(r['facts']['energy_sun'],'100')
        self.assertEqual(r['facts']['collaterals'][0]['minimum_debt_usdd'],'600')
        self.assertEqual(r['execution_authority'],'NONE')
        self.assertEqual(before,s)

    def test_matching_mainnet_is_not_mistaken_for_nile_mismatch_or_execution_authority(self):
        s=state();s['mandates']['m']['revisions'][0]['mandate']['terms']['borrowing']['consent']=True
        e=evidence('tron-mainnet',False);e['supply_rate']=result(42000000000)
        r=UsddReviews.assess('tron-mainnet',e,{},s,AT)
        self.assertNotIn('VAULT_DESTINATION_TOKEN_MISMATCH',r['blockers'])
        self.assertNotIn('RECURSIVE_SPREAD_NOT_POSITIVE',r['blockers'])
        self.assertNotIn('BORROWING_NOT_CONSENTED',r['blockers'])
        self.assertIn('EXECUTION_REVIEW_REQUIRED',r['blockers'])
        self.assertEqual(r['status'],'BLOCKED')

    def test_draft_debt_consent_never_overrides_confirmed_policy(self):
        s=state();s['workspace']['mandate']={'status':'DRAFT','terms':{'borrowing':{'consent':True}}}
        r=UsddReviews.assess('tron-nile',evidence(),{},s,AT)
        self.assertIn('BORROWING_NOT_CONSENTED',r['blockers'])
        self.assertEqual(r['policy_hash'],'a'*64)

    def test_missing_stale_or_malformed_live_reads_fail_closed(self):
        for change in ('missing','stale','malformed','wrong_token_binding'):
            with self.subTest(change=change):
                e=evidence()
                if change=='missing':e.pop('borrow_rate')
                elif change=='stale':e['start_block']['block_header']['raw_data']['timestamp']-=300000
                elif change=='malformed':e['underlying']=result(1,1,0)
                else:e['join_token']=result(123)
                r=UsddReviews.assess('tron-nile',e,{},state(),AT)
                self.assertIn('LIVE_INPUT_UNAVAILABLE',r['blockers'])
                self.assertEqual(r['status'],'BLOCKED')

    def test_pause_shutdown_and_expired_policy_are_reported(self):
        e=evidence();e.update(mint_paused=result(1),borrow_paused=result(1),vat_live=result(0))
        s=state();s['mandates']['m']['revisions'][0]['mandate']['terms']['expires_at']='2026-09-29T11:59:00+00:00'
        r=UsddReviews.assess('tron-nile',e,{},s,AT)
        for blocker in ('DESTINATION_SUPPLY_PAUSED','DESTINATION_BORROW_PAUSED','VAULT_SHUTDOWN','POLICY_INACTIVE'):
            self.assertIn(blocker,r['blockers'])

    def test_transport_path_has_no_write_rpc_and_scope_survives_persistence(self):
        fixture=evidence(); calls=[]; c=CONFIG['tron-nile']
        def request(url,body):
            calls.append((url,body))
            if url.endswith('getblock'):
                self.assertEqual(body,{'detail':False})
                value=fixture['start_block']
            elif url.endswith('getchainparameters'): value=fixture['energy']
            else:
                self.assertTrue(url.endswith('/walletsolidity/triggerconstantcontract'))
                self.assertNotIn('call_value',body)
                selector=body['function_selector']
                target=body['contract_address']
                if selector=='ilks(bytes32)':
                    name=next(k for k in ('vat','jug','spot') if address_hex(c[k])==target)
                    ilk=bytes.fromhex(body['parameter']).rstrip(b'\0').decode()
                    value=fixture[name+':'+ilk]
                else:
                    mapping={'usdd()':'join_token','underlying()':'underlying','comptroller()':'market_controller','markets(address)':'market','mintGuardianPaused(address)':'mint_paused','borrowGuardianPaused(address)':'borrow_paused','supplyRatePerBlock()':'supply_rate','borrowRatePerBlock()':'borrow_rate','getCash()':'cash','base()':'jug_base','live()':'vat_live'}
                    value=fixture[mapping[selector]]
            return json.dumps(value).encode()
        ctx=AuthenticatedContext(tenant_id='test',owner_id='owner',wallet='41'+'12'*20,network='tron-nile',session_id='test',trace_id='test',issued_at=AT,expires_at='2026-09-30T00:00:00+00:00')
        with tempfile.TemporaryDirectory() as tmp:
            repo=OperationalRepository(Path(tmp)/'store.sqlite')
            b=MachineBridge(repo,lambda:AT,None,SimpleNamespace(request=request))
            s=state();s['workspace']['intent']={'patch':{'borrowing_consent':True}}
            protected=digest({k:s['workspace'][k] for k in ('mandate','graph','approval','execution','intent')})
            b.commit(ctx,0,s)
            b.mutate(ctx,'POST','/v1/usdd-reviews',{'network':'nile'},'review')
            n=len(calls)
            b.mutate(ctx,'POST','/v1/usdd-reviews',{'network':'nile'},'review')
            self.assertEqual(n,len(calls))
            w=MachineBridge(repo,lambda:AT,None).workspace(ctx)
            self.assertIsNotNone(w['usdd_review'])
            self.assertEqual(protected,digest({k:w[k] for k in ('mandate','graph','approval','execution','intent')}))
            self.assertIsNone(b.workspace(replace(ctx,owner_id='other'))['usdd_review'])
            self.assertIsNone(b.workspace(replace(ctx,network='tron-mainnet'))['usdd_review'])

    def test_nonzero_padding_is_not_accepted_as_scalar(self):
        with self.assertRaises(MachineError):words(result(1,1,0),1,True)
        self.assertEqual(words(result(1,0,0),1,True),[1])

if __name__=='__main__':unittest.main()
