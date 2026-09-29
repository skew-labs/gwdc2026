"""Current official JustLend market API, separated from legacy capture shapes.

API observations are a single provider, not an independent price oracle. Reward
budgets are a present annualized forecast; the API supplies no guaranteed end
date. Never substitute this feed for exact on-chain balance/loan checks.
"""
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from economic_machine.values import MachineError, digest, decstr
from economic_machine.tron_sources import parse_raw, address_hex

HOST='https://labc.ablesdxd.link'
MARKET='TKFRELGGoRgiayhwJTNNLqCNjFoLBh3Mnf'
TOKEN='TXDk8mbtRbXeYuMNS83CfKPaYYT8XWv9Hz'
SOURCE='https://github.com/justlend/mcp-server-justlend/blob/7d53a6ca755fbe70724ae6aaed9276462716cee2/src/core/services/markets.ts'


def normalize_market(markets, details, at):
    if markets.get('code')!=0 or details.get('code')!=0:raise MachineError('Current JustLend API unavailable.')
    matches=[x for x in markets['data']['jtokenList'] if x.get('jtokenAddress')==MARKET]
    if len(matches)!=1:raise MachineError('Unique current USDD market required.')
    m=matches[0];d=details['data']
    if any(address_hex(x['collateralAddress'])!=address_hex(TOKEN) or address_hex(x['jtokenAddress'])!=address_hex(MARKET) for x in (m,d)):
        raise MachineError('USDD market API token binding changed.')
    if m.get('collateralDecimal')!=18 or m.get('isValid') not in (1,'1'):raise MachineError('USDD market is not active.')
    ms=m['lastBlocktimestamp']
    if type(ms) is not int or not 0<=datetime.fromisoformat(at).timestamp()*1000-ms<=3600000:
        raise MachineError('USDD API market state is older than one hour or from the future.')
    with localcontext() as ctx:
        ctx.prec=80
        tvl=Decimal(d['depositedUSD']);daily=Decimal(str(d['farmRewardUSD24h']))
        usdd=Decimal(str(d['farmRewardUsddAmount24h']));trx=Decimal(str(d['farmRewardTrxAmount24h']))
        price=Decimal(markets['data']['trxPrice'])/10**18
        usdd_price=Decimal(d['priceUSD'])
        if any(not n.is_finite() for n in [tvl,daily,usdd,trx,price,usdd_price]) or min(tvl,price,usdd_price)<=0 or min(daily,usdd,trx)<0:
            raise MachineError('Invalid current reward budget or asset valuation.')
        if abs(daily-usdd*usdd_price-trx*price)>max(Decimal('.01'),daily*Decimal('.01')):
            raise MachineError('Reported reward currencies do not reconcile to daily USD budget.')
        reward=daily*365/tvl
        if reward>10:raise MachineError('Reward forecast exceeds supported bounds.')
        result={'schema_version':'machine-justlend-market-api-3','network':'tron-mainnet',
            'market':MARKET,'token':TOKEN,'received_at':at,
            'market_observed_at':datetime.fromtimestamp(ms/1000,datetime.fromisoformat(at).tzinfo).isoformat(),
            'expires_at':(datetime.fromisoformat(at)+timedelta(minutes=5)).isoformat(),
            'reward_apr':decstr(reward),'reward_daily_usd':decstr(daily),'tvl_usd':decstr(tvl),
            'trx_usd':decstr(price),'usdd_usd':decstr(usdd_price),
            'reward_end_at':None,'reward_guaranteed':False,'reward_auto_compounded':False,
            'source':SOURCE,'api_url':HOST+'/justlend/markets/jtokenDetails?jtokenAddr='+MARKET,
            'basis':'Provider reward budget / supplied value × 365. Future reward duration and wallet claim eligibility are not guaranteed.',
            'evidence_hash':digest({'markets':markets,'details':details})}
        result['hash']=digest(result);return result


def read_market(request,clock):
    markets=parse_raw(request(HOST+'/justlend/markets'))
    details=parse_raw(request(HOST+'/justlend/markets/jtokenDetails?jtokenAddr='+MARKET))
    return normalize_market(markets,details,clock()), {'markets':markets,'details':details}
