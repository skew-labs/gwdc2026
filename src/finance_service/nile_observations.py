"""Nile-specific JustLend observations, replayed from captured RPC bytes.

The pinned official address manifest is verified against live getters. This
reader never substitutes mainnet API prices, contracts, or rates for Nile data.
"""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal, localcontext
from economic_machine.capabilities import normalize_capability, capability_hash, product_id
from economic_machine.tron_sources import address_hex, parse_raw
from economic_machine.values import MachineError, canonical, digest, decstr, utc

ROOT='https://nile.trongrid.io'
MANIFEST={
    'network':'tron-nile', 'market':'TT6Qk1qrBM4MgyskYZx5pjeJjvv3fdL2ih',
    'token':'TPYwAC9Y4uUcT2QH3WPPjqxzJSJWymMoMS', 'comptroller':'TJUCStq3WqfKqZLuZje5v7z6Ua6iBry1P6',
    'asset':'USDT', 'decimals':6, 'blocks_per_year':10512000,
    'source':'https://github.com/justlend/mcp-server-justlend/blob/7d53a6ca755fbe70724ae6aaed9276462716cee2/src/core/chains.ts',
    'rate_convention':'constant supplyRatePerBlock compounded over 10512000 blocks/year; 3-second-block convention',
}
REGISTRY_SOURCE='justlend_nile_rpc_registry'
NATIVE_MANIFEST={**MANIFEST, 'market':'TKM7w4qFmkXQLEF2MgrQroBYpd5TY7i1pq', 'token':None, 'asset':'TRX'}


def uint(response):
    if response.get('result',{}).get('result') is not True:
        raise MachineError('Nile constant call did not succeed.')
    result=response.get('constant_result')
    if not isinstance(result,list) or len(result)!=1 or not isinstance(result[0],str):
        raise MachineError('Nile ABI output is missing.')
    # Deployed delegator returns two trailing zero words for some uint/address getters.
    raw=bytes.fromhex(result[0])
    if len(raw) not in {32,96} or any(raw[32:]):
        raise MachineError('Nile ABI return width or padding differs from the supported contract.')
    return int.from_bytes(raw[:32], 'big')


class NileAssembler:
    config={'max_age_seconds':300,'max_registry_age_seconds':300,'max_source_skew_seconds':120}

    def __init__(self, asset="USDT"):
        if asset not in {"USDT", "TRX"}: raise MachineError("Unsupported Nile capital asset.")
        self.manifest = NATIVE_MANIFEST if asset == "TRX" else MANIFEST

    def requests(self, scope):
        owner=scope['wallet']; market=address_hex(self.manifest['market']); token=address_hex(self.manifest['token']) if self.manifest['token'] else None
        def call(contract, function, parameter=''):
            return ('walletsolidity/triggerconstantcontract', dict(owner_address=owner,contract_address=contract,function_selector=function,parameter=parameter,visible=False))
        queries = {
            'market_code':('wallet/getcontract', {'value':market,'visible':False}),
            'underlying':call(market,'underlying()'),
            'comptroller':call(market,'comptroller()'),
            'token_decimals':call(token,'decimals()'),
            'supply_rate':call(market,'supplyRatePerBlock()'),
            'cash':call(market,'getCash()'),
            'wallet_balance':call(token,'balanceOf(address)',owner[2:].rjust(64,'0')),
        }
        if token is None:
            queries.pop('underlying')
            queries['token_decimals']=call(market,'decimals()')
            queries['wallet_balance']=('walletsolidity/getaccount',{'address':owner,'visible':False})
        return queries

    def read(self, scope, clock, request):
        if scope['network']!='tron-nile':raise MachineError('Nile reader scope mismatch.')
        def collect(item):
            name,(path,body)=item
            try:
                raw=request(ROOT+'/'+path,body)
                parse_raw(raw)
                return name,dict(path=path,body=body,raw_text=raw.decode(),raw_sha256=hashlib.sha256(raw).hexdigest(),received_at=clock(),error=None)
            except Exception:
                return name,dict(path=path,body=body,raw_text=None,raw_sha256=None,received_at=clock(),error='READ_UNAVAILABLE')
        with ThreadPoolExecutor(max_workers=3) as pool:
            values=dict(pool.map(collect,self.requests(scope).items()))
        at=clock()
        payload={'manifest':self.manifest,'responses':values}
        capture=dict(source_id=REGISTRY_SOURCE, network='tron-nile',scope=scope,received_at=at,
            source_url=self.manifest['source'],rpc_url=ROOT,raw_text=canonical(payload).decode(),
            error=None, mode='LIVE_READ', source_time_status='UNKNOWN',block=None)
        capture['capture_hash']=digest(capture)
        return self.assemble(capture,at,scope)

    def assemble(self,capture,as_of,scope):
        if scope['network']!='tron-nile' or capture['network']!='tron-nile' or capture['scope']!=scope:
            raise MachineError('Nile capture scope mismatch.')
        if (capture['source_id'] != REGISTRY_SOURCE or capture['source_url'] != self.manifest['source']
                or capture['rpc_url'] != ROOT or capture['mode'] != 'LIVE_READ' or capture['error'] is not None):
            raise MachineError('Unsupported Nile capture origin or mode.')
        payload=dict(capture);claimed=payload.pop('capture_hash')
        if digest(payload)!=claimed:raise MachineError('Nile RPC capture commitment differs.')
        content=parse_raw(capture['raw_text'].encode())
        if content['manifest']!=self.manifest:raise MachineError('Nile pinned manifest changed.')
        expected=self.requests(scope);responses=content['responses']
        if set(responses)!=set(expected):raise MachineError('Incomplete Nile RPC set.')
        decoded={}
        for name,row in responses.items():
            if (row['path'],row['body']) != expected[name]:raise MachineError('Nile RPC request binding changed.')
            age=(datetime.fromisoformat(utc(as_of))-datetime.fromisoformat(utc(row['received_at']))).total_seconds()
            if not 0<=age<=self.config['max_source_skew_seconds']:raise MachineError('Nile RPC receipt skew exceeds bound.')
            if row['error']:
                if row['error'] != 'READ_UNAVAILABLE' or row['raw_text'] is not None or row['raw_sha256'] is not None:
                    raise MachineError('Invalid failed Nile capture.')
                continue
            raw=row['raw_text'].encode()
            if hashlib.sha256(raw).hexdigest()!=row['raw_sha256']:raise MachineError('Nile RPC bytes changed.')
            decoded[name]=parse_raw(raw)
        identity=False
        if all(k in decoded for k in ['market_code','comptroller','token_decimals'] + (['underlying'] if self.manifest['token'] else [])):
            code=decoded['market_code']
            identity=(address_hex(code.get('contract_address'))==address_hex(self.manifest['market'])
                and bool(code.get('bytecode'))
                and (self.manifest['token'] is None or uint(decoded['underlying'])==int(address_hex(self.manifest['token'])[2:],16))
                and uint(decoded['comptroller'])==int(address_hex(self.manifest['comptroller'])[2:],16)
                and uint(decoded['token_decimals'])==(self.manifest['decimals'] if self.manifest['token'] else 8))
            if self.manifest['token'] is None:
                from .native_execution import verify_native_abi
                verify_native_abi(code)
            if not identity:raise MachineError('Live Nile market identity differs from official pinned addresses.')
        cap=normalize_capability(dict(schema_version='economic-product-capability-1',chain='TRON',network='tron-nile',
            contract=address_hex(self.manifest['market']),protocol='justlend',protocol_version='v1',action='SUPPLY',
            token={'asset':self.manifest['asset'],'address':address_hex(self.manifest['token']) if self.manifest['token'] else None,'decimals':6},
            stages={stage:{'status':'SUPPORTED' if identity and (stage=='read' or self.manifest['asset']=='TRX') else 'UNSUPPORTED',
                'evidence_hash':claimed if identity and (stage=='read' or self.manifest['asset']=='TRX') else None} for stage in ['read','quote','simulate','execute','reconcile']}))
        name='justlend.v1.j'+self.manifest['asset']
        product={'product_id':name,'identity_hash':product_id(cap),'capability_hash':capability_hash(cap),'capability':cap,
            'market_status':'pinned-testnet','new_supply_allowed':identity,'share_decimals':8}
        facts={}
        def add(path,value,unit,source):
            row=responses[source]; valid=value is not None and identity
            facts[path]={'value':value if valid else None,'unit':unit,'quality':('VALID_ZERO' if value=='0' else 'VALID') if valid else 'MISSING',
                'availability':'AVAILABLE' if valid else 'UNAVAILABLE','capture_hash':claimed,'source_id':REGISTRY_SOURCE,
                'received_at':row['received_at'],'observed_at':None,'block':None,'state_eligible':False,
                'withheld_reasons':['SOURCE_BLOCK_UNKNOWN','MULTI_CALL_NON_ATOMIC_READ']}
        annual=None
        if 'supply_rate' in decoded:
            per_block=uint(decoded['supply_rate'])
            if per_block>10**12:raise MachineError('Nile supply rate exceeds supported calculation bound.')
            with localcontext() as ctx:
                ctx.prec=48
                annual=decstr(((1+Decimal(per_block)/10**18)**self.manifest['blocks_per_year']-1).quantize(Decimal('1e-18')))
        add(name+'.supply_apy',annual,'annual_fraction','supply_rate')
        add(name+'.available_cash',decstr(Decimal(uint(decoded['cash']))/10**6) if 'cash' in decoded else None,self.manifest['asset'],'cash')
        balance=None
        if identity and 'wallet_balance' in decoded:
            row=decoded['wallet_balance']
            if self.manifest['asset']=='TRX':
                if row.get('address') != scope['wallet'] or type(row.get('balance',0)) is not int: raise MachineError('Nile wallet balance identity differs.')
                balance=decstr(Decimal(row.get('balance',0))/10**6)
            else: balance=decstr(Decimal(uint(row))/10**6)
        result=dict(schema_version='economic-nile-rpc-snapshot-1',as_of=utc(as_of),network='tron-nile',scope=scope,
            mode='LIVE_READ',registry_source_id=REGISTRY_SOURCE,registry_hash=digest(self.manifest),products={name:product},facts=facts,
            comparisons=[],provider_groups=['trongrid'],oracle_quorum=None,execution_enabled=False,
            captures=[capture],rpc_capture=None,wallet_token_balance=balance,
            unavailable=['USDD_VALUATION_UNVERIFIED','INCENTIVE_RATE_UNOBSERVED','EXECUTION_QUOTE_REQUIRED'],
            source_status={REGISTRY_SOURCE:'AVAILABLE' if identity else 'UNAVAILABLE'})
        result['snapshot_hash']=digest(result)
        return result

    def verify(self,snapshot):
        replay=self.assemble(snapshot['captures'][0],snapshot['as_of'],snapshot['scope'])
        if replay!=snapshot:raise MachineError('Nile snapshot differs from captured RPC replay.')
        return replay
