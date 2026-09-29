"""Wallet-signed Nile TRC10 -> native TRX funding through protocol exchanges.

Only the exact prepared protobuf may be submitted, once. An uncertain broadcast
is durable before the network call and is resolved by solidified-chain reads.
"""
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

from economic_machine.signed_tx_validation import _book
from economic_machine.tron_crypto import recover_tron_address
from economic_machine.tron_sources import address_hex, address_base58, parse_raw
from economic_machine.values import MachineError, canonical, digest, decstr

ROOT = 'https://nile.trongrid.io'
TOKEN = '1005416'
TYPE = 'type.googleapis.com/protocol.ExchangeTransactionContract'


def decode_exchange(tx):
    raw = bytes.fromhex(tx['raw_data_hex'])
    if hashlib.sha256(raw).hexdigest() != tx['txID']:
        raise MachineError('Transaction ID does not match its protobuf.')
    f = _book(raw, {1:2,4:2,8:0,11:2,14:0}, 'exchange raw')
    if set(f) != {1,4,8,11,14} or len(f[1]) != 2 or len(f[4]) != 8:
        raise MachineError('Invalid exchange transaction fields.')
    c = _book(f[11], {1:0,2:2}, 'exchange contract')
    if set(c) != {1,2} or c[1] != 44:
        raise MachineError('Only a single owner-permission exchange trade is allowed.')
    a = _book(c[2], {1:2,2:2}, 'exchange Any')
    if set(a) != {1,2} or a[1] != TYPE.encode():
        raise MachineError('Exchange contract type differs.')
    p = _book(a[2], {1:2,2:0,3:2,4:0,5:0}, 'exchange fields')
    if set(p) != {1,2,3,4,5}:
        raise MachineError('Incomplete exchange trade.')
    v = dict(owner_address=p[1].hex(), exchange_id=p[2], token_id=p[3].decode(), quant=p[4], expected=p[5])
    projected = dict(ref_block_bytes=f[1].hex(), ref_block_hash=f[4].hex(), expiration=f[8], timestamp=f[14],
        contract=[dict(type='ExchangeTransactionContract', parameter=dict(type_url=TYPE, value={**v, 'token_id':p[3].hex()}))])
    if tx.get('visible') is not False or tx.get('raw_data') != projected:
        raise MachineError('Displayed transaction does not match the signed bytes.')
    return v


class NileFunding:
    def __init__(self, repository, clock, request):
        self.repository, self.clock, self.request = repository, clock, request

    def rpc(self, method, body=None):
        value = parse_raw(self.request(ROOT + '/' + method, body or {}))
        if value.get('Error') or value.get('error'):
            raise MachineError('Nile node could not complete ' + method + '.')
        return value

    def load(self, context):
        context.authorize(self.clock())
        if context.network != 'tron-nile':
            raise MachineError('Test token funding is available only on Nile.')
        try:
            row = self.repository.get_record(context.scope, 'NILE_FUNDING', 'current')
            return row['version'], row['body']
        except MachineError as exc:
            if str(exc) != 'service record not found in authenticated scope': raise
            return 0, None

    def save(self, context, version, value):
        self.repository.put_record(context.scope, 'NILE_FUNDING', 'current', value,
            expected_version=version, at=self.clock())
        return value

    def state(self, context):
        _, row = self.load(context)
        try:
            parameters = self.rpc('wallet/getchainparameters')
            enabled = next((x.get('value',0) for x in parameters.get('chainParameter',[]) if x['key']=='getAllowHardenExchangeCalculation'), 0) == 1
            support = {'available':enabled, 'reason':None if enabled else 'Native TRC10 exchanges are disabled by the current Nile chain configuration.', 'checked_at':self.clock()}
        except Exception:
            support = {'available':False, 'reason':'Nile exchange availability could not be verified.', 'checked_at':self.clock()}
        return {'funding':row, 'support':support}

    def prepare(self, context, amount):
        version, prior = self.load(context)
        if prior and prior['status'] in {'SUBMITTED', 'SUBMISSION_UNKNOWN'}:
            raise MachineError('Resolve the previous funding transaction before preparing another.')
        if not isinstance(amount, str) or not re.fullmatch(r'(0|[1-9][0-9]{0,8})(\.[0-9]{1,6})?', amount):
            raise MachineError('Enter a positive TRN amount with at most six decimal places.')
        quantity = int(Decimal(amount) * 10**6)
        if not 0 < quantity <= 100 * 10**6:
            raise MachineError('Each test funding swap is limited to 100 TRN.')
        owner = address_hex(context.wallet)
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = [pool.submit(self.rpc, method, body) for method, body in [
                ('wallet/getaccount', {'address':owner}), ('wallet/getaccountresource', {'address':owner}),
                ('wallet/listexchanges', {}), ('wallet/getchainparameters', {})]]
            account, resources, exchanges, parameters = [f.result() for f in jobs]
        hardened = next((x.get('value',0) for x in parameters.get('chainParameter',[]) if x['key']=='getAllowHardenExchangeCalculation'), 0)
        if hardened != 1:
            raise MachineError('Nile currently disables native exchange trades (getAllowHardenExchangeCalculation=0). No new wallet signature will be requested. Existing TRN remains in your wallet.')
        available = next((x['value'] for x in account.get('assetV2', []) if x['key'] == TOKEN), 0)
        if available < quantity:
            raise MachineError('The connected Nile wallet has insufficient faucet TRN (TRC10 #1005416).')
        candidates = []
        for row in exchanges.get('exchanges', []):
            first, second = row.get('first_token_id'), row.get('second_token_id')
            if {first, second} != {TOKEN.encode().hex(), '5f'}: continue
            x, y = (row.get('first_token_balance', 0), row.get('second_token_balance', 0))
            if first == '5f': x, y = y, x
            if x > 0 and y > 0:
                candidates.append((quantity * y // (x + quantity), row))
        if not candidates: raise MachineError('No liquid direct TRN/TRX exchange is available on Nile.')
        estimated, exchange = max(candidates, key=lambda x: x[0])
        minimum = estimated * 9900 // 10000
        if minimum <= 0: raise MachineError('The amount is too small to obtain native TRX.')
        payload = dict(owner_address=owner, exchange_id=exchange['exchange_id'],
            token_id=TOKEN.encode().hex(), quant=quantity, expected=minimum, visible=False)
        tx = self.rpc('wallet/exchangetransaction', payload)
        if tx.get('Error') or 'txID' not in tx:
            raise MachineError('The Nile node rejected the proposed swap.')
        tx['visible'] = False
        decoded = decode_exchange(tx)
        if decoded != dict(owner_address=owner, exchange_id=exchange['exchange_id'], token_id=TOKEN, quant=quantity, expected=minimum):
            raise MachineError('Node transaction differs from the reviewed swap.')
        # Conservative bound: raw protobuf + one recoverable signature + envelope/result reserve.
        bandwidth_bound = len(bytes.fromhex(tx['raw_data_hex'])) + 67 + 128
        free = max(0, resources.get('freeNetLimit',0) - resources.get('freeNetUsed',0))
        staked = max(0, resources.get('NetLimit',0) - resources.get('NetUsed',0))
        fee_per_byte = next((x.get('value',0) for x in parameters.get('chainParameter',[]) if x['key']=='getTransactionFee'), None)
        if fee_per_byte is None: raise MachineError('Current bandwidth price could not be verified.')
        # A transaction must fit a resource bucket in full; do not combine free and staked buckets.
        max_fee = 0 if max(free, staked) >= bandwidth_bound else bandwidth_bound * fee_per_byte
        if account.get('balance',0) < max_fee:
            raise MachineError('Available bandwidth cannot cover this swap and native TRX is insufficient for its fee.')
        result = dict(network='nile', account=address_base58(owner), status='PREPARED',
            txid=tx['txID'], transaction=tx, amount_trn=decstr(Decimal(quantity)/10**6),
            estimated_trx=decstr(Decimal(estimated)/10**6), minimum_trx=decstr(Decimal(minimum)/10**6),
            exchange_id=exchange['exchange_id'], slippage_bps=100, max_fee_trx=decstr(Decimal(max_fee)/10**6),
            free_bandwidth=free, bandwidth_bound=bandwidth_bound,
            expires_at=datetime.fromtimestamp(tx['raw_data']['expiration']/1000,timezone.utc).isoformat(),
            quoted_at=self.clock(), updated_at=self.clock(), receipt=None,
            message='Review this Nile test token exchange, then sign it in TronLink.',
            evidence=dict(exchange=exchange, account=account, resources=resources, parameters=parameters,
                source=ROOT, quote_method='constant product estimate; 1% minimum output enforced by native contract',
                node_validation='unsigned transaction constructed successfully; not yet executed'))
        return self.save(context, version, result)

    def submit(self, context, signed):
        version, row = self.load(context)
        if not row or row['txid'] != signed.get('txID'):
            raise MachineError('No matching prepared funding transaction.')
        if row['status'] != 'PREPARED': return row
        original = row['transaction']
        if signed.get('raw_data_hex') != original['raw_data_hex'] or signed.get('raw_data') != original['raw_data']:
            raise MachineError('Wallet changed the reviewed funding transaction.')
        decode_exchange({**signed, 'visible':False})
        if datetime.fromisoformat(self.clock()) >= datetime.fromisoformat(row['expires_at']):
            raise MachineError('Funding transaction expired. Obtain a fresh quote.')
        signatures = signed.get('signature')
        if not isinstance(signatures,list) or len(signatures)!=1 or not re.fullmatch(r'[a-fA-F0-9]{130}',signatures[0]):
            raise MachineError('Exactly one wallet signature is required.')
        signer = recover_tron_address(bytes.fromhex(row['txid']), bytes.fromhex(signatures[0]))
        if signer != context.wallet: raise MachineError('Funding signer differs from the connected wallet.')
        # Commit before broadcast. Duplicate calls return this record and never broadcast again.
        row = deepcopy(row)
        row.update(status='SUBMISSION_UNKNOWN', updated_at=self.clock(), message='Signed transaction recorded. Checking network submission.')
        self.save(context, version, row)
        outgoing = {**original, 'signature':signatures}
        try:
            response = self.rpc('wallet/broadcasttransaction', outgoing)
        except Exception:
            return row
        row['broadcast_response'] = response
        if response.get('result') is True:
            row.update(status='SUBMITTED',message='Broadcast accepted. Waiting for solidified confirmation.')
        else:
            # A rejection does not prove the same tx was not already accepted elsewhere.
            row['message'] = 'Node did not acknowledge acceptance. Reconcile this transaction before retrying.'
        row['updated_at'] = self.clock()
        return self.save(context, version+1, row)

    def reconcile(self, context):
        version, row = self.load(context)
        if not row or row['status'] not in {'SUBMITTED','SUBMISSION_UNKNOWN'}: return row
        receipt = self.rpc('walletsolidity/gettransactioninfobyid', {'value':row['txid']})
        transaction = self.rpc('walletsolidity/gettransactionbyid', {'value':row['txid']})
        if receipt.get('id') != row['txid'] or transaction.get('txID') != row['txid']:
            head = self.rpc('walletsolidity/getnowblock')
            solid_at = head.get('block_header', {}).get('raw_data', {}).get('timestamp', 0)
            expired_at = row['transaction']['raw_data']['expiration']
            if not receipt and not transaction and solid_at > expired_at + 60000 and row.get('broadcast_response',{}).get('code') == 'CONTRACT_VALIDATE_ERROR':
                row = deepcopy(row)
                row.update(status='FAILED', updated_at=self.clock(),
                    message='Nile rejected this exchange before inclusion. The solidified chain is past its expiry with no transaction receipt; no exchange was executed. Native exchanges are disabled on the current Nile configuration.')
                row['rejection_evidence'] = {'solid_head':head['block_header'], 'transaction':transaction, 'receipt':receipt}
                return self.save(context, version, row)
            return row
        got = {**transaction, 'visible':False}
        decode_exchange(got)
        if got['raw_data_hex'] != row['transaction']['raw_data_hex']:
            raise MachineError('Solidified transaction differs from the prepared transaction.')
        success = len(transaction.get('ret',[])) == 1 and transaction['ret'][0].get('contractRet') == 'SUCCESS'
        row = deepcopy(row)
        row.update(status='CONFIRMED' if success else 'FAILED',receipt=receipt,updated_at=self.clock(),
            message='Nile exchange confirmed in the solidified chain.' if success else 'The exchange failed on-chain.')
        row['after_account'] = self.rpc('walletsolidity/getaccount', {'address':context.wallet})
        return self.save(context, version, row)
