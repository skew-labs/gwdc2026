"""Read/simulate-only RPC port for the fixed USDD workflow.

All write broadcasts live in the durable service after exact wallet validation.
"""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from economic_machine.usdd_workflow import CONFIG, addr, word, FAILURE
from economic_machine.tron_sources import address_hex, parse_raw
from economic_machine.tron_registry_read import _block
from economic_machine.values import MachineError, digest
from .usdd_review import words, returned_address, CONFIG as READ_CONFIG
from .resource_costs import quote_resources,signed_bandwidth_bound

ZERO='41'+'0'*40
ANCHORS={'tron-mainnet':'00000000000000010ff5414c5cfbe9eae982e8cef7eb2399a39118e1206c8247',
 'tron-nile':'0000000000000001f1b9db4e1f7a39e5bcb4636382f3b24ee38a718e2bd4ddb0'}
# This deployed registry has cleared its stored ABI and creation bytecode.
# Fixed build() ABI: official USDD reference ccf1fb5 src/core/abis.ts.
# Runtime SHA-256 observed through getcontractinfo on 2026-09-29 UTC.
REGISTRY_RUNTIME='9c0566af9ee7031788fce44f6f0b8292e145a9e13869a7388f6b0cc914be70f6'
TOKEN_RUNTIME='8fedf680814d2192ecd786ecfe741124160f38b61b0846ceef0fe8bb94e7c657'
CLAIM_RUNTIME='9087829bdc47c0518b42a5f3edb01c56d014824fd8a1f418aceec0efddde9a1d'


class UsddChain:
    def __init__(self,network,owner,request,clock):
        self.network=network;self.owner=owner;self.request=request;self.clock=clock
        self.c=CONFIG[network];self.root=READ_CONFIG[network]['root']

    def rpc(self,path,body=None):
        raw=self.request(self.root+'/'+path,body or {})
        result=parse_raw(raw,max_bytes=8388608 if path.rsplit("/",1)[-1] in {"getblock","getblockbynum","getblockbyid","getnowblock"} else 1048576)
        if result.get('Error') or result.get('error'):raise MachineError('USDD RPC unavailable: '+path)
        return result

    def head(self,solid=True):
        result=_block(self.rpc(('walletsolidity' if solid else 'wallet')+'/getblock',{'detail':False}))
        if not 0<=datetime.fromisoformat(self.clock()).timestamp()*1000-result['timestamp_ms']<=300000:
            raise MachineError('USDD chain head is stale.')
        return result

    def call(self,contract,signature,parameter='',count=1):
        r=self.rpc('walletsolidity/triggerconstantcontract',dict(owner_address=self.owner,
            contract_address=address_hex(contract),function_selector=signature,parameter=parameter,visible=False))
        return words(r,count,padded=count<=2)

    def address(self,contract,signature,parameter=''):
        n=self.call(contract,signature,parameter)[0]
        if n>=2**160:raise MachineError('Invalid returned address.')
        return '41'+format(n,'040x')

    def entered_markets(self):
        r=self.rpc('walletsolidity/triggerconstantcontract',dict(owner_address=self.owner,
            contract_address=address_hex(self.c['controller']),function_selector='getAssetsIn(address)',parameter=addr(self.owner),visible=False))
        data=r.get('constant_result')
        if r.get('result',{}).get('result') is not True or not isinstance(data,list) or len(data)!=1:
            raise MachineError('Complete collateral market membership is required.')
        raw=data[0]
        try:
            if len(raw)<128 or len(raw)%64:raise ValueError()
            values=[int(raw[i:i+64],16) for i in range(0,len(raw),64)]
            if values[0]!=32 or not 0<=values[1]<=32 or len(values)!=values[1]+2 or any(n>=2**160 for n in values[2:]):raise ValueError()
        except (ValueError,TypeError):raise MachineError('Invalid collateral market membership encoding.')
        result=sorted('41'+format(n,'040x') for n in values[2:])
        if len(result)!=len(set(result)):raise MachineError('Duplicate collateral market membership.')
        return result

    def snapshot(self,workflow=None):
        c=self.c
        # Verify chain identity, not merely a UI network label.
        anchor=self.rpc('walletsolidity/getblock',{'id_or_num':'1','detail':False})
        if anchor.get('blockID')!=ANCHORS[self.network]:raise MachineError('USDD RPC network identity differs.')
        for _ in range(3):
            before=self.head();arg=addr(self.owner)
            queries={
              'wallet':lambda:self.rpc('walletsolidity/getaccount',{'address':self.owner,'visible':False}),
              'token':lambda:self.address(c['market'],'underlying()'),
              'join_token':lambda:self.address(c['join'],'usdd()'),
              'controller':lambda:self.address(c['market'],'comptroller()'),
              'market':lambda:self.call(c['market'],'getAccountSnapshot(address)',arg,4),
              'membership':lambda:self.call(c['controller'],'checkMembership(address,address)',arg+addr(c['market']))[0],
              'entered_markets':self.entered_markets,
              'cash':lambda:self.call(c['market'],'getCash()')[0],
              'proxy':lambda:self.address(c['registry'],'proxies(address)',arg),
            }
            with ThreadPoolExecutor(max_workers=3) as pool:
                fs={k:pool.submit(fn) for k,fn in queries.items()};x={k:f.result() for k,f in fs.items()}
            if x['controller']!=address_hex(c['controller']):raise MachineError('USDD controller binding differs.')
            if any(m!=address_hex(c['market']) for m in x['entered_markets']):
                raise MachineError('Other collateral markets require a consolidated debt review before this isolated USDD route can run.')
            if workflow and workflow['mode']=='VAULT' and x['token']!=x['join_token']:
                raise MachineError('Vault and lending market use different tokens on this network.')
            if self.network=='tron-mainnet' and x['token']!=address_hex(c['token']):raise MachineError('Unexpected mainnet USDD token.')
            token=x['token'];proxy=x['proxy'] if x['proxy']!=ZERO else None
            err,shares,debt,rate=x['market']
            if err or not rate:raise MachineError('USDD market position read failed.')
            queries={
              'token_balance':lambda:self.call(token,'balanceOf(address)',arg)[0],
              'market_allowance':lambda:self.call(token,'allowance(address,address)',arg+addr(c['market']))[0],
              'decimals':lambda:self.call(token,'decimals()')[0],
              'accrued_market_debt':lambda:self.call(c['market'],'borrowBalanceCurrent(address)',arg)[0],
              'current_exchange_rate':lambda:self.call(c['market'],'exchangeRateCurrent()')[0],
            }
            if proxy:
                queries.update(proxy_owner=lambda:self.address(proxy,'owner()'),
                    cdp_count=lambda:self.call(c['manager'],'count(address)',addr(proxy))[0],
                    last_cdp=lambda:self.call(c['manager'],'last(address)',addr(proxy))[0],
                    proxy_allowance=lambda:self.call(token,'allowance(address,address)',arg+addr(proxy))[0])
            with ThreadPoolExecutor(max_workers=3) as pool:
                fs={k:pool.submit(fn) for k,fn in queries.items()};extra={k:f.result() for k,f in fs.items()}
            if extra['decimals']!=18:raise MachineError('USDD token decimals differ.')
            if proxy and extra['proxy_owner']!=self.owner:raise MachineError('DSProxy owner differs from wallet.')
            cdp_id=(workflow or {}).get('cdp_id')
            if workflow and workflow['cursor']<len(workflow['steps']) and workflow['steps'][workflow['cursor']]['operation']=='VAULT_OPEN':cdp_id=extra.get('last_cdp')
            vault={'cdp_id':str(cdp_id) if cdp_id else None,'cdp_owner':None,'ilk':None,'vault_debt':'0','accrued_vault_debt':'0','collateral_wad':'0'}
            if cdp_id:
                owner=self.address(c['manager'],'owns(uint256)',word(int(cdp_id)))
                urn=self.address(c['manager'],'urns(uint256)',word(int(cdp_id)))
                ilk_raw=self.call(c['manager'],'ilks(uint256)',word(int(cdp_id)))[0]
                ilk=ilk_raw.to_bytes(32,'big').rstrip(b'\0').decode('ascii')
                if owner!=proxy or ilk not in c['joins']:raise MachineError('Vault ownership or collateral binding differs.')
                vat=READ_CONFIG[self.network]['vat'];ilk_word=word(ilk_raw)
                ink,art=self.call(vat,'urns(bytes32,address)',ilk_word+addr(urn),2)
                _,rate_v,_,_,_=self.call(vat,'ilks(bytes32)',ilk_word,5)
                # triggerconstantcontract simulates accrual without submitting
                # a transaction. Stored rate alone can omit unpaid interest.
                accrued_rate=self.call(c['jug'],'drip(bytes32)',ilk_word)[0] if art else rate_v
                vault.update(cdp_owner=owner,ilk=ilk,vault_debt=str((art*rate_v+10**27-1)//10**27),
                    accrued_vault_debt=str((art*max(rate_v,accrued_rate)+10**27-1)//10**27),
                    collateral_wad=str(ink),vault_art=str(art),vault_rate=str(rate_v))
            after=self.head()
            # Calls are evidence only; bound the set and compare exact balances
            # again immediately before broadcast. Never claim atomic RPC proof.
            if after['number']>=before['number'] and after['timestamp_ms']-before['timestamp_ms']<=30000:break
        else:raise MachineError('USDD state read window is too wide.')
        wallet=x['wallet']
        if wallet.get('address')!=self.owner or type(wallet.get('balance',0)) is not int:
            raise MachineError('An active funded wallet is required on the selected network.')
        return {'network':self.network,'owner':self.owner,'token':token,'join_token':x['join_token'],
            'trx_balance':str(wallet.get('balance',0)),'shares':str(shares),'market_debt':str(debt),
            'accrued_market_debt':str(max(debt,extra['accrued_market_debt'])),
            'current_exchange_rate':str(extra['current_exchange_rate']),
            'exchange_rate':str(rate),'market_cash':str(x['cash']),'membership':bool(x['membership']),
            'entered_markets':x['entered_markets'],
            'proxy':proxy,'proxy_owner':extra.get('proxy_owner'),'cdp_count':str(extra.get('cdp_count',0)),
            'token_balance':str(extra['token_balance']),'market_allowance':str(extra['market_allowance']),
            'proxy_allowance':str(extra.get('proxy_allowance',0)),**vault,
            'block_from':before,'block_to':after,'observed_at':self.clock()}

    def simulate(self,call,fee_cap):
        addresses={call['target']}
        if call.get('inner_target'):addresses.add(call['inner_target'])
        codes={}
        for target in addresses:
            info=self.rpc('wallet/getcontractinfo',{'value':target,'visible':False})
            code=info.get('smart_contract',{});runtime=info.get('runtimecode')
            if address_hex(code.get('contract_address'))!=target or not runtime:
                raise MachineError('Deployed USDD contract code is unavailable.')
            runtime_hash=hashlib.sha256(bytes.fromhex(runtime)).hexdigest()
            signature=call['signature'] if target==call['target'] else call['inner_signature']
            expected_name=signature.split('(')[0]
            expected_types=signature.split('(',1)[1][:-1].split(',') if not signature.endswith('()') else []
            entries=code.get('abi',{}).get('entrys',[])
            if not entries and self.network=='tron-mainnet' and target==address_hex(self.c['registry']) and runtime_hash==REGISTRY_RUNTIME:
                entries=[{'type':'Function','name':'build','inputs':[]}]
            if not entries and self.network=='tron-mainnet' and target==address_hex(self.c['token']) and runtime_hash==TOKEN_RUNTIME:
                entries=[{'type':'Function','name':'approve','inputs':[{'type':'address'},{'type':'uint256'}]}]
            implementation=None
            if target==address_hex(self.c['controller']):
                impl=self.address(target,'comptrollerImplementation()')
                implementation=self.rpc('wallet/getcontractinfo',{'value':impl,'visible':False})
                meta=implementation.get('smart_contract',{})
                if not implementation.get('runtimecode') or address_hex(meta.get('contract_address'))!=impl:
                    raise MachineError('Collateral controller implementation unavailable.')
                entries=meta.get('abi',{}).get('entrys',[])
            if not entries and call.get('inner_target') and target==call['target'] and signature=='execute(address,bytes)':
                # Registry provenance and ownership are independently read in
                # snapshot(). Factory-created proxies do not store an ABI.
                registry=self.rpc('wallet/getcontractinfo',{'value':address_hex(self.c['registry']),'visible':False})
                registry_hash=hashlib.sha256(bytes.fromhex(registry.get('runtimecode',''))).hexdigest()
                if self.network!='tron-mainnet' or registry_hash!=REGISTRY_RUNTIME:raise MachineError('Verified proxy registry runtime required.')
                codes['proxy_registry']={'runtime_bytecode_hash':registry_hash}
                entries=[{'type':'Function','name':'execute','inputs':[{'type':'address'},{'type':'bytes'}]}]
            if call['operation']=='CLAIM_USDD':expected_types=['tuple[]']
            matches=[e for e in entries if e.get('type')=='Function' and e.get('name')==expected_name and [i['type'] for i in e.get('inputs',[])]==expected_types]
            if len(matches)!=1:raise MachineError('Exact deployed USDD ABI is unavailable.')
            if call['operation']=='CLAIM_USDD' and runtime_hash==CLAIM_RUNTIME and not matches[0]['inputs'][0].get('components'):
                # TRON's stored ABI omits nested tuple components. The exact
                # single-token schema is in pinned JustLend rewards.ts.
                matches[0]['inputs'][0]['components']=[{'type':t} for t in ('uint256','uint256','uint256','bytes32[]')]
            if call['operation']=='CLAIM_USDD' and [x['type'] for x in matches[0]['inputs'][0].get('components',[])]!=['uint256','uint256','uint256','bytes32[]']:
                raise MachineError('USDD reward tuple ABI differs from the fixed claim.')
            codes[target]={'runtime_bytecode_hash':runtime_hash,'abi_hash':digest(entries)}
            if implementation:
                codes[target].update(implementation=impl,implementation_hash=hashlib.sha256(bytes.fromhex(implementation['runtimecode'])).hexdigest())
            # Bind upgradeable market implementation as well as delegator code.
            if target==address_hex(self.c['market']):
                impl=self.address(target,'implementation()')
                deployed=self.rpc('wallet/getcontractinfo',{'value':impl,'visible':False})
                if not deployed.get('runtimecode') or address_hex(deployed.get('smart_contract',{}).get('contract_address'))!=impl:raise MachineError('Lending implementation bytecode unavailable.')
                codes[target]['implementation']=impl
                codes[target]['implementation_hash']=hashlib.sha256(bytes.fromhex(deployed['runtimecode'])).hexdigest()
        simulation=self.rpc('wallet/triggerconstantcontract',dict(owner_address=self.owner,contract_address=call['target'],
            function_selector=call['signature'],parameter=call['data'][8:],call_value=int(call['value_sun']),fee_limit=fee_cap,visible=False))
        if simulation.get('result',{}).get('result') is not True or type(simulation.get('energy_used')) is not int or simulation['energy_used']<=0:
            raise MachineError('USDD transaction simulation did not succeed.')
        if any(l.get('topics',[None])[0]==FAILURE for l in simulation.get('logs',[])):
            raise MachineError('JustLend simulation emitted a protocol Failure.')
        result=simulation.get('constant_result');op=call['operation']
        if op.startswith('APPROVE'):
            if words(simulation,1)[0]!=1:raise MachineError('Token approval simulation returned false.')
        elif op in ('SUPPLY','BORROW','REPAY','REPAY_ALL','REDEEM','REDEEM_SHARES'):
            if words(simulation,1,True)[0]!=0:raise MachineError('Lending simulation returned a protocol error.')
        elif op=='ENTER_MARKET':
            if result!=[word(32)+word(1)+word(0)]:raise MachineError('Entering collateral market failed.')
        else:
            if not isinstance(result,list) or len(result)!=1:raise MachineError('Proxy simulation result is missing.')
        parameters=self.rpc('wallet/getchainparameters')
        resources=self.rpc('wallet/getaccountresource',{'address':self.owner,'visible':False})
        size=signed_bandwidth_bound(self.owner,call['target'],call['data'],int(call['value_sun']),timestamp_ms=int(datetime.fromisoformat(self.clock()).timestamp()*1000),fee_limit_sun=fee_cap)
        costs=quote_resources(simulation['energy_used'],size,parameters,resources)
        if costs['full_total_burn_bound_sun']>fee_cap:raise MachineError('Live USDD cost exceeds this transaction fee cap.')
        return {'simulation':simulation,'codes':codes,'costs':costs,'observed_at':self.clock()}
