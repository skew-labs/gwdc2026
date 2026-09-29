"""Funding trust boundaries and interrupted-submit recovery; never broadcasts live."""
import copy, hashlib, json, tempfile, unittest
from pathlib import Path
from dataclasses import replace
from finance_service.nile_funding import NileFunding, decode_exchange, TYPE
from finance_service.operational_repository import OperationalRepository
from finance_service.context import AuthenticatedContext
from economic_machine.values import MachineError, canonical
from economic_machine.signed_tx_validation import _encode_varint
from test_economic_tron_execution import sign_hash, wallet_for

AT='2026-09-29T09:00:00+00:00'
KEY=9  # Public test fixture only; never used on-chain.

def field(n,v):
    if isinstance(v,int):return _encode_varint(n*8)+_encode_varint(v)
    return _encode_varint(n*8+2)+_encode_varint(len(v))+v

def transaction(owner, exchange_id=50, quantity=10000000, minimum=17820000):
    value=b''.join([field(1,bytes.fromhex(owner)),field(2,exchange_id),field(3,b'1005416'),field(4,quantity),field(5,minimum)])
    contract=field(1,44)+field(2,field(1,TYPE.encode())+field(2,value))
    raw=b''.join([field(1,b'aa'),field(4,b'bbbbbbbb'),field(8,1790672460000),field(11,contract),field(14,1790672400000)])
    data=dict(ref_block_bytes='6161',ref_block_hash='6262626262626262',expiration=1790672460000,timestamp=1790672400000,
        contract=[dict(type='ExchangeTransactionContract',parameter=dict(type_url=TYPE,value=dict(owner_address=owner,exchange_id=exchange_id,token_id='31303035343136',quant=quantity,expected=minimum)))])
    return dict(txID=hashlib.sha256(raw).hexdigest(),raw_data_hex=raw.hex(),raw_data=data,visible=False)

class FundingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=OperationalRepository(Path(self.tmp.name)/'test.sqlite')
        self.ctx=AuthenticatedContext(tenant_id='t',owner_id='o',wallet=wallet_for(KEY),network='tron-nile',session_id='s',trace_id='t',issued_at=AT,expires_at='2026-09-29T10:00:00Z')
        self.broadcasts=0;self.timeout=False;self.free=600;self.enabled=1
        self.at=AT;self.rejected=False;self.solid_at=1790672400000;self.builds=0
        self.service=NileFunding(self.repo,lambda:self.at,self.rpc)
    def rpc(self,url,body):
        method=url.rsplit('/',1)[-1]
        if method=='getaccount':result={'address':self.ctx.wallet,'assetV2':[{'key':'1005416','value':100000000}]}
        elif method=='getaccountresource':result={'freeNetLimit':self.free}
        elif method=='listexchanges':result={'exchanges':[{'exchange_id':50,'first_token_id':'5f','first_token_balance':180000000,'second_token_id':'31303035343136','second_token_balance':90000000}]}
        elif method=='getchainparameters':result={'chainParameter':[{'key':'getTransactionFee','value':1000},{'key':'getAllowHardenExchangeCalculation','value':self.enabled}]}
        elif method=='exchangetransaction':
            self.builds+=1
            result=transaction(self.ctx.wallet,body['exchange_id'],body['quant'],body['expected'])
        elif method=='broadcasttransaction':
            self.broadcasts+=1
            self.assertEqual(self.service.load(self.ctx)[1]['status'],'SUBMISSION_UNKNOWN')
            if self.timeout:raise TimeoutError()
            result={'result':False,'code':'CONTRACT_VALIDATE_ERROR'} if self.rejected else {'result':True}
        elif method=='getnowblock':result={'block_header':{'raw_data':{'timestamp':self.solid_at}}}
        else:result={}
        return canonical(result)
    def signed(self,row,key=KEY):
        tx=copy.deepcopy(row['transaction']);tx['signature']=[sign_hash(bytes.fromhex(tx['txID']),key).hex()];return tx
    def test_zero_trx_with_free_bandwidth_can_prepare_and_submit_once(self):
        row=self.service.prepare(self.ctx,'10')
        self.assertEqual((row['estimated_trx'],row['minimum_trx'],row['max_fee_trx']),('18','17.82','0'))
        signed=self.signed(row)
        self.assertEqual(self.service.submit(self.ctx,signed)['status'],'SUBMITTED')
        self.service.submit(self.ctx,signed)
        self.assertEqual(self.broadcasts,1)
    def test_timeout_remains_durable_and_cannot_double_submit_or_replace(self):
        row=self.service.prepare(self.ctx,'10');signed=self.signed(row);self.timeout=True
        self.service.submit(self.ctx,signed)
        restarted=NileFunding(self.repo,lambda:AT,self.rpc)
        self.assertEqual(restarted.submit(self.ctx,signed)['status'],'SUBMISSION_UNKNOWN')
        with self.assertRaises(MachineError):restarted.prepare(self.ctx,'10')
        self.assertEqual(self.broadcasts,1)
    def test_wrong_signer_changed_display_or_bytes_never_broadcast(self):
        row=self.service.prepare(self.ctx,'10')
        with self.assertRaises(MachineError):self.service.submit(self.ctx,self.signed(row,10))
        changed=self.signed(row);changed['raw_data']['contract'][0]['parameter']['value']['quant']=1
        with self.assertRaises(MachineError):self.service.submit(self.ctx,changed)
        forged=copy.deepcopy(row['transaction']);forged['raw_data']['contract'][0]['parameter']['value']['expected']=1
        with self.assertRaises(MachineError):decode_exchange(forged)
        self.assertEqual(self.broadcasts,0)
    def test_network_scope_balance_and_resource_gates(self):
        with self.assertRaises(MachineError):self.service.prepare(replace(self.ctx,network='tron-mainnet'),'10')
        with self.assertRaises(MachineError):self.service.prepare(self.ctx,'101')
        self.free=0
        with self.assertRaises(MachineError):self.service.prepare(self.ctx,'10')
        self.assertEqual(self.broadcasts,0)

    def test_disabled_chain_never_constructs_transaction_or_requests_signature(self):
        self.enabled=0
        self.assertFalse(self.service.state(self.ctx)['support']['available'])
        with self.assertRaisesRegex(MachineError,'disables native exchange'):
            self.service.prepare(self.ctx,'10')
        self.assertEqual((self.builds,self.broadcasts),(0,0))
        self.assertIsNone(self.service.load(self.ctx)[1])

    def test_validation_rejection_only_finalizes_past_solidified_expiry(self):
        row=self.service.prepare(self.ctx,'10');self.rejected=True
        self.assertEqual(self.service.submit(self.ctx,self.signed(row))['status'],'SUBMISSION_UNKNOWN')
        self.assertEqual(self.service.reconcile(self.ctx)['status'],'SUBMISSION_UNKNOWN')
        self.at='2026-09-29T09:03:00+00:00';self.solid_at=1790672580000
        restarted=NileFunding(self.repo,lambda:self.at,self.rpc)
        failed=restarted.reconcile(self.ctx)
        self.assertEqual(failed['status'],'FAILED')
        self.assertEqual(failed['rejection_evidence']['receipt'],{})
        restarted.submit(self.ctx,self.signed(row))
        self.assertEqual(self.broadcasts,1)

    def test_timeout_is_not_declared_failed_just_because_time_passed(self):
        row=self.service.prepare(self.ctx,'10');self.timeout=True
        self.service.submit(self.ctx,self.signed(row))
        self.at='2026-09-29T09:03:00+00:00';self.solid_at=1790672580000
        self.assertEqual(self.service.reconcile(self.ctx)['status'],'SUBMISSION_UNKNOWN')
