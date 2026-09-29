"""Bounded USDD transactions and receipt-driven non-atomic workflow state.

No caller-supplied target, selector, unlimited allowance, private key or swap.
Vault output can fund JustLend only when both contracts use the same token.
A later transaction is materialized from confirmed state, never a simulated
future balance. Each transaction needs a new exact approval and signature.
"""
import re
from copy import deepcopy
from decimal import Decimal, localcontext, InvalidOperation
from .values import MachineError, digest, decstr
from .tron_sources import address_hex
from .tron_crypto import function_selector, keccak256

CONFIG = {
 'tron-mainnet': {
  'registry':'THuVWkvAikvSqmoZXHMUQJAcocsgFr4wuk','actions':'TEk9usYZsunkc5oYijyMte6sGurspik2Js',
  'manager':'TDDWjmQaquEtUn1Pa8wCd8dfWFPdQLGPYL','jug':'TWttvCqVmiLip7PL8Aut2Hi37swqv7EmYd',
  'join':'TUajR7CbXU6hX8n3XtNkitFAD25JvP99K6','token':'TXDk8mbtRbXeYuMNS83CfKPaYYT8XWv9Hz',
  'market':'TKFRELGGoRgiayhwJTNNLqCNjFoLBh3Mnf','controller':'TGjYzgCyPobsNS9n6WcbdLVR9dH7mWqFx7',
  'joins':{'TRX-A':'TJ1VWPvFVq7sVsN7J7dWJVZz4SLT14qRUr','TRX-B':'TGQKnHDQNyc3QeHJ7YxH8wggdg89UVXyvX','TRX-C':'TPUPPLTYLdbW4jxwD5g2T7ystxsR9HL2mt'}},
 'tron-nile': {
  'registry':'TFYXC7Sx2UfMmvNeuSpGv2Ss4e8skzkgCS','actions':'TDAeZp2u3Z3aSXL9GTaqg3GQTG5oksNRJ4',
  'manager':'TLsfKsZ15SVMMfyyfvd2WXB1ZiexKavez2','jug':'TAJFAHMuxwVG18G55oXNJkPFuhg2zpPWZ6',
  'join':'TMhXGZgx1bQwWD2r9ZqS9rjp8QRU5v7R7Y','token':'TYQF9cAeJ3Faq8QXpHxTcFco72DRCQbgFt',
  'market':'TBqtwZhjP49heKsoTHeX5MhKBJMmyuP88b','controller':'TJUCStq3WqfKqZLuZje5v7z6Ua6iBry1P6',
  'joins':{'TRX-A':'TAnN3k475UhYY7jTEeM11WCw6pqsfMQwxW','TRX-B':'TND1RyHS7KHp3uo8sfcL66heceEMUBPJp9','TRX-C':'TE7WfUHoQ46fFzC9Ar3BHrDj4ceJVkzaRS'}}}
VERSION='machine-usdd-workflow-1'
TERMINAL={'CONFIRMED','FAILED','DISPUTED'}
FAILURE=keccak256(b'Failure(uint256,uint256,uint256)').hex()


def units(value, decimals=18, *, zero=False):
    if not isinstance(value,str) or len(value)>90: raise MachineError('Decimal amount required.')
    with localcontext() as ctx:
        ctx.prec=100
        try:n=Decimal(value)*10**decimals
        except InvalidOperation:raise MachineError('Decimal amount required.')
        if not n.is_finite() or n<0 or n!=int(n) or n>=2**256 or (not zero and n==0):
            raise MachineError('Amount is outside exact token precision or range.')
        return int(n)


def word(n):
    if type(n) is not int or not 0<=n<2**256: raise MachineError('uint256 required.')
    return n.to_bytes(32,'big').hex()


def addr(a): return address_hex(a)[2:].rjust(64,'0')


def encode(signature, values):
    """Only this fixed selector set is accepted; no arbitrary DSProxy execution."""
    kinds={
      'approve(address,uint256)':('address','uint'), 'build()':(),
      'mint(uint256)':('uint',),'borrow(uint256)':('uint',),'repayBorrow(uint256)':('uint',),
      'redeem(uint256)':('uint',),'redeemUnderlying(uint256)':('uint',),
      'open(address,bytes32,address)':('address','bytes32','address'),
      'lockTRXAndDraw(address,address,address,address,uint256,uint256)':('address','address','address','address','uint','uint'),
      'safeWipe(address,address,uint256,uint256,address)':('address','address','uint','uint','address'),
      'safeWipeAll(address,address,uint256,address)':('address','address','uint','address'),
      'freeTRX(address,address,uint256,uint256)':('address','address','uint','uint'),
    }
    if signature=='multiClaim((uint256,uint256,uint256,bytes32[])[])':
        if len(values)!=4:raise MachineError('One complete USDD reward claim required.')
        period,index,amount,proof=values
        if not isinstance(proof,list) or not 1<=len(proof)<=24 or any(not isinstance(x,str) or re.fullmatch(r'[0-9a-f]{64}',x) is None for x in proof):
            raise MachineError('Bounded canonical Merkle proof required.')
        return function_selector(signature)+word(32)+word(1)+word(32)+word(period)+word(index)+word(amount)+word(128)+word(len(proof))+''.join(proof)
    if signature=='enterMarkets(address[])':
        if len(values)!=1:raise MachineError('Exactly one collateral market required.')
        return function_selector(signature)+word(32)+word(1)+addr(values[0])
    types=kinds.get(signature)
    if types is None or len(types)!=len(values):raise MachineError('Unsupported fixed USDD call.')
    out=[]
    for kind,value in zip(types,values):
        if kind=='address':out.append(addr(value))
        elif kind=='uint':out.append(word(value))
        else:
            if value not in ('TRX-A','TRX-B','TRX-C'):raise MachineError('Unsupported collateral ilk.')
            out.append(value.encode().hex().ljust(64,'0'))
    return function_selector(signature)+''.join(out)


def proxy_call(network, proxy, signature, values):
    c=CONFIG[network]
    if not signature.startswith(('open(', 'lockTRXAndDraw(', 'safeWipe(', 'safeWipeAll(', 'freeTRX(')):
        raise MachineError('Unsupported proxy composition.')
    inner=bytes.fromhex(encode(signature,values))
    # DSProxy execute(address,bytes): fixed actions contract, length and padding.
    parameter=addr(c['actions'])+word(64)+word(len(inner))+inner.hex().ljust(((len(inner)+31)//32)*64,'0')
    return {'target':address_hex(proxy),'signature':'execute(address,bytes)',
        'data':function_selector('execute(address,bytes)')+parameter,
        'inner_target':address_hex(c['actions']),'inner_signature':signature}


def compile_call(network, owner, step, observed):
    c=CONFIG[network];op=step['operation'];amount=int(step.get('amount','0'))
    proxy=observed.get('proxy');cdp=observed.get('cdp_id');ilk=step.get('ilk','TRX-C')
    target=None;signature=None;args=[];value=0
    if op in ('VAULT_OPEN','VAULT_DRAW','VAULT_REPAY','VAULT_REPAY_ALL','VAULT_FREE'):
        if not proxy or address_hex(observed.get('proxy_owner'))!=address_hex(owner):raise MachineError('Wallet-owned DSProxy required.')
        if op!='VAULT_OPEN' and (not cdp or observed.get('cdp_owner')!=address_hex(proxy) or observed.get('ilk')!=ilk):
            raise MachineError('Vault owner, id or collateral differs from this workflow.')
        if op=='VAULT_OPEN':signature='open(address,bytes32,address)';args=[c['manager'],ilk,proxy]
        elif op=='VAULT_DRAW':
            signature='lockTRXAndDraw(address,address,address,address,uint256,uint256)'
            args=[c['manager'],c['jug'],c['joins'][ilk],c['join'],int(cdp),amount];value=int(step['collateral_sun'])
        elif op=='VAULT_REPAY':signature='safeWipe(address,address,uint256,uint256,address)';args=[c['manager'],c['join'],int(cdp),amount,proxy]
        elif op=='VAULT_REPAY_ALL':signature='safeWipeAll(address,address,uint256,address)';args=[c['manager'],c['join'],int(cdp),proxy]
        else:
            if int(observed.get('vault_debt','0')):raise MachineError('Collateral release requires zero independently observed debt.')
            if not amount or amount%10**12:raise MachineError('TRX collateral cannot be released below SUN precision.')
            signature='freeTRX(address,address,uint256,uint256)';args=[c['manager'],c['joins'][ilk],int(cdp),amount//10**12]
        call=proxy_call(network,proxy,signature,args)
    else:
        mapping={'PROXY_CREATE':('registry','build()'), 'SUPPLY':('market','mint(uint256)'),
            'BORROW':('market','borrow(uint256)'), 'REPAY':('market','repayBorrow(uint256)'), 'REPAY_ALL':('market','repayBorrow(uint256)'),
            'REDEEM':('market','redeemUnderlying(uint256)'), 'REDEEM_SHARES':('market','redeem(uint256)'),
            'ENTER_MARKET':('controller','enterMarkets(address[])')}
        if op=='CLAIM_USDD':
            if network!='tron-mainnet' or observed['token']!=address_hex(c['token']):raise MachineError('Verified mainnet USDD reward token required.')
            target='TYxJzmeDyxuxFbaGywjivfkft75qLeS485';signature='multiClaim((uint256,uint256,uint256,bytes32[])[])'
            args=[int(step['merkle_index']),int(step['claim_index']),amount,step['proof']]
        elif op in ('APPROVE_MARKET','APPROVE_PROXY'):
            target=observed['token'];signature='approve(address,uint256)'
            spender=c['market'] if op=='APPROVE_MARKET' else proxy
            if not spender:raise MachineError('Confirmed spender required.')
            args=[spender,amount]
        elif op in mapping:
            key,signature=mapping[op];target=c[key]
            args=[] if op=='PROXY_CREATE' else [c['market']] if op=='ENTER_MARKET' else [2**256-1] if op=='REPAY_ALL' else [amount]
        else:raise MachineError('Unsupported USDD workflow operation.')
        call={'target':address_hex(target),'signature':signature,'data':encode(signature,args),'inner_target':None,'inner_signature':None}
    return {**call,'owner':address_hex(owner),'value_sun':str(value),'operation':op,
        'amount':str(amount),'network':network}


def build_steps(*, amount, collateral_sun=0, ilk='TRX-C', loops=0, borrow_bps=0, has_proxy=False):
    if type(amount) is not int or amount<=0 or type(loops) is not int or not 0<=loops<=4:
        raise MachineError('Positive USDD amount and at most four explicit loops required.')
    if type(borrow_bps) is not int or not 0<=borrow_bps<=8500 or (loops and not borrow_bps):
        raise MachineError('Bounded borrow fraction required.')
    steps=[]
    def add(op, amt=0, **more):
        steps.append({'id':'usdd-step-'+str(len(steps)+1),'operation':op,'amount':str(amt),
            'status':'WAITING','txid':None,'receipt':None,**more})
    if collateral_sun:
        if not has_proxy:add('PROXY_CREATE')
        add('VAULT_OPEN',ilk=ilk)
        add('VAULT_DRAW',amount,collateral_sun=str(collateral_sun),ilk=ilk)
    add('APPROVE_MARKET',amount);add('SUPPLY',amount)
    if loops:add('ENTER_MARKET')
    previous=amount
    for _ in range(loops):
        borrowed=previous*borrow_bps//10000
        if not borrowed:raise MachineError('Loop amount rounds to zero.')
        add('BORROW',borrowed);add('APPROVE_MARKET',borrowed);add('SUPPLY',borrowed)
        previous=borrowed
    return steps


def loop_cashflow(principal, supply_apy, reward_apr, borrow_apy, stability_apy, *, days, loops, borrow_bps, costs, equity):
    """Cash rewards are linear, not auto-compounded. All external costs in USDD.

    Equity excludes borrowed funds. Positive spread is necessary but not a
    guarantee; rate/price/reward changes and liquidation require fresh review.
    """
    with localcontext() as ctx:
        ctx.prec=80
        p=Decimal(principal);eq=Decimal(equity);t=Decimal(days)/365
        if p<=0 or eq<=0 or not 0<t<=1 or type(loops) is not int or not 0<=loops<=4:
            raise MachineError('Invalid USDD forecast capital or duration.')
        rates=[Decimal(x) for x in (supply_apy,reward_apr,borrow_apy,stability_apy)]
        if any(not x.is_finite() or not 0<=x<=10 for x in rates):raise MachineError('Invalid forecast rates.')
        if not isinstance(costs,list) or not costs:raise MachineError('Explicit complete round-trip costs required.')
        total_cost=sum((Decimal(x) for x in costs),Decimal(0))
        if any(not Decimal(x).is_finite() or Decimal(x)<0 for x in costs):raise MachineError('Invalid round-trip cost.')
        if type(borrow_bps) is not int or not 0<=borrow_bps<=8500:raise MachineError('Invalid borrow fraction.')
        supplied=p;next_amount=p;debt=Decimal(0)
        for _ in range(loops):
            next_amount=Decimal(int(next_amount*10**18*borrow_bps/10000))/10**18
            supplied+=next_amount;debt+=next_amount
        supply_growth=(1+rates[0])**t-1+rates[1]*t
        borrow_growth=(1+rates[2])**t-1
        loan=p*((1+rates[3])**t-1)
        income=supplied*supply_growth
        interest=debt*borrow_growth+loan
        net=income-interest-total_cost
        return {k:decstr(v) for k,v in {'supplied_usdd':supplied,'recursive_debt_usdd':debt,
            'gross_income_usdd':income,'debt_interest_usdd':interest,'total_costs_usdd':total_cost,
            'net_income_usdd':net,'equity_usdd':eq,'holding_return':net/eq,
            'annualized_net_return':(1+net/eq)**(1/t)-1 if net>-eq else Decimal(-1),
            'marginal_loop_return':supply_growth-borrow_growth}.items()}


def advance(workflow, txid, receipt, after):
    """Idempotent terminal transition; success requires operation post-state."""
    w=deepcopy(workflow)
    if txid in w.get('confirmed_txids',[]):
        prior=next(x for x in w['steps'] if x['txid']==txid)
        if prior['receipt']!=receipt:raise MachineError('Conflicting receipt for an already confirmed transaction.')
        return w
    if w['cursor']>=len(w['steps']):raise MachineError('Workflow is already complete.')
    index=w['cursor'];step=w['steps'][index]
    if step['status']=='CONFIRMED':
        if step['txid']==txid:return w
        raise MachineError('A different transaction already completed this step.')
    if step.get('txid')!=txid or step['status'] not in ('SUBMISSION_UNKNOWN','SUBMITTED'):
        raise MachineError('No matching durable submitted step.')
    if receipt.get('solidified') is not True or receipt.get('txid')!=txid:
        raise MachineError('A matching solidified receipt is required.')
    if receipt.get('success') is not True:
        step.update(status='FAILED',receipt=receipt);w['status']='NEEDS_RECOVERY';return w
    before=step['before'];op=step['operation'];amount=int(step['amount'])
    if any(x.get('topics',[None])[0]==FAILURE and x.get('address')==address_hex(CONFIG[w['network']]['market'])[2:] for x in receipt.get('logs',[])):
        step.update(status='FAILED',receipt=receipt);w['status']='NEEDS_RECOVERY';return w
    checks={'PROXY_CREATE':lambda: bool(after.get('proxy')) and not before.get('proxy') and after.get('proxy_owner')==w['owner'],
      'VAULT_OPEN':lambda: after.get('cdp_id')!=before.get('cdp_id') and bool(after.get('cdp_id')) and after.get('cdp_owner')==after.get('proxy') and after.get('ilk')==step['ilk'],
      'VAULT_DRAW':lambda: int(after['token_balance'])-int(before['token_balance'])==amount and int(after['vault_debt'])>=int(before['vault_debt'])+amount and int(after['collateral_wad'])-int(before['collateral_wad'])==int(step['collateral_sun'])*10**12,
      'APPROVE_MARKET':lambda:int(after['market_allowance'])==amount,
      'APPROVE_PROXY':lambda:int(after['proxy_allowance'])==amount,
      'SUPPLY':lambda:int(before['token_balance'])-int(after['token_balance'])==amount and int(after['shares'])-int(before['shares'])>=int(step['minimum_shares']),
      'ENTER_MARKET':lambda:after.get('membership') is True,
      'BORROW':lambda:int(after['token_balance'])-int(before['token_balance'])==amount and int(after['market_debt'])>=int(before['market_debt'])+amount,
      'REPAY_ALL':lambda:int(after['market_debt'])==0 and 0<int(before['token_balance'])-int(after['token_balance'])<=amount,
      'CLAIM_USDD':lambda:int(after['token_balance'])-int(before['token_balance'])==amount,
      'REPAY':lambda:int(before['token_balance'])-int(after['token_balance'])==amount and int(after['market_debt'])<int(before['market_debt']),
      'REDEEM':lambda:int(after['token_balance'])-int(before['token_balance'])==amount and int(after['shares'])<int(before['shares']),
      'REDEEM_SHARES':lambda:int(before['shares'])-int(after['shares'])==amount and int(after['token_balance'])>int(before['token_balance']),
      'VAULT_REPAY':lambda:int(before['token_balance'])-int(after['token_balance'])==amount and int(after['vault_debt'])<int(before['vault_debt']),
      'VAULT_REPAY_ALL':lambda:int(after['vault_debt'])==0 and 0<int(before['token_balance'])-int(after['token_balance'])<=int(step['repay_cap']),
      'VAULT_FREE':lambda:int(after['vault_debt'])==0 and int(before['collateral_wad'])-int(after['collateral_wad'])==amount and int(after['trx_balance'])+int(receipt['fee_sun'])-int(before['trx_balance'])==amount//10**12,
    }
    if op not in checks or not checks[op]():
        step.update(status='DISPUTED',receipt=receipt,after=after);w['status']='DISPUTED';return w
    step.update(status='CONFIRMED',receipt=receipt,after=after)
    w['confirmed_txids']=list(dict.fromkeys([*w.get('confirmed_txids',[]),txid]))
    w['cursor']+=1;w['status']='COMPLETE' if w['cursor']==len(w['steps']) else 'AWAITING_NEXT_REVIEW'
    w['observed']=after
    return w
