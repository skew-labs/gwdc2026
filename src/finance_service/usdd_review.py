"""Live USDD route assessment. Queries only; never grants execution authority.

USDD ticker equality is insufficient, especially across Nile deployments. This
reader binds the Vault's join token to the destination's actual underlying and
keeps variable-rate forecasts separate from transaction quotes and approvals.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal, localcontext

from economic_machine.tron_sources import address_hex, address_base58, parse_raw
from economic_machine.tron_registry_read import _block
from economic_machine.values import MachineError, digest, decstr
from economic_machine.yield_math import rounded, exact

SOURCE = 'https://github.com/decentralized-usd/mcp-server-usdd/blob/ccf1fb52f796a2c4a69fa0aa67625dd5c705e2fe/src/core/chains.ts'
DESTINATION_SOURCE = 'https://github.com/justlend/mcp-server-justlend/blob/7d53a6ca755fbe70724ae6aaed9276462716cee2/src/core/chains.ts'
CONFIG = {
    'tron-nile': dict(root='https://nile.trongrid.io',
        usdd='TYQF9cAeJ3Faq8QXpHxTcFco72DRCQbgFt', join='TMhXGZgx1bQwWD2r9ZqS9rjp8QRU5v7R7Y',
        vat='TDAv6rniTrqjYqA64VVpnJxFfgmveN2LUA', jug='TAJFAHMuxwVG18G55oXNJkPFuhg2zpPWZ6',
        spot='TBDV5R9ivEndudp16rAW8Yb129Um8wFKsA',
        market='TBqtwZhjP49heKsoTHeX5MhKBJMmyuP88b', comptroller='TJUCStq3WqfKqZLuZje5v7z6Ua6iBry1P6'),
    'tron-mainnet': dict(root='https://api.trongrid.io',
        usdd='TXDk8mbtRbXeYuMNS83CfKPaYYT8XWv9Hz', join='TUajR7CbXU6hX8n3XtNkitFAD25JvP99K6',
        vat='TH5dhX7o39afSbfDT2e3c9k4itWjNKD4D9', jug='TWttvCqVmiLip7PL8Aut2Hi37swqv7EmYd',
        spot='TU8Z8CeUd7pnXSMHTNqRgK6Qxxxyzsba1n',
        market='TKFRELGGoRgiayhwJTNNLqCNjFoLBh3Mnf', comptroller='TGjYzgCyPobsNS9n6WcbdLVR9dH7mWqFx7'),
}
RAY = 10**27
RAD = 10**45
YEAR = 31536000


def words(response, size, padded=False):
    value = response.get('constant_result')
    if response.get('result', {}).get('result') is not True or not isinstance(value, list) or len(value) != 1:
        raise MachineError('USDD live contract read failed.')
    try:
        raw = bytes.fromhex(value[0])
    except (ValueError, TypeError):
        raise MachineError('USDD ABI return encoding differs.') from None
    # Some JustLend delegator uint/address getters append two zero words.
    if len(raw) != size*32 and not (padded and len(raw) == 96 and size <= 2 and not any(raw[size*32:])):
        raise MachineError('USDD ABI return width differs.')
    return [int.from_bytes(raw[i*32:(i+1)*32], 'big') for i in range(size)]


def returned_address(response):
    value = words(response, 1, True)[0]
    if not 0 < value < 2**160:
        raise MachineError('A nonzero USDD contract address is required.')
    return '41' + format(value, '040x')


def annual(rate, periods):
    with localcontext() as ctx:
        ctx.prec = 80
        value = Decimal(rate)
        if not 1 <= value <= Decimal('1.001'):
            raise MachineError('USDD rate is outside supported calculation bounds.')
        result = value**periods - 1
        if result > 10:
            raise MachineError('USDD annualized rate exceeds supported forecast bounds.')
        return decstr(rounded(result, 18))


class UsddReviews:
    def __init__(self, bridge):
        self.bridge = bridge

    def refresh(self, context, state):
        if self.bridge.observations is None:
            raise MachineError('Live USDD observations are unavailable.')
        scope = context.authorize(self.bridge.clock())
        config = CONFIG[scope['network']]
        request = self.bridge.observations.request
        owner = scope['wallet']
        evidence, errors = {}, {}

        def rpc(path, body=None):
            if path.endswith('/getnowblock'):
                path=path.removesuffix('getnowblock')+'getblock';body={'detail':False}
            data = parse_raw(request(config['root'] + '/' + path, body or {}))
            if not isinstance(data, dict) or data.get('Error') or data.get('error'):
                raise MachineError('USDD RPC data unavailable.')
            return data

        def call(contract, selector, parameter=''):
            return rpc('walletsolidity/triggerconstantcontract', dict(owner_address=owner,
                contract_address=address_hex(config[contract]), function_selector=selector,
                parameter=parameter, visible=False))

        market_arg = address_hex(config['market'])[2:].rjust(64, '0')
        queries = {
            'start_block': lambda: rpc('walletsolidity/getnowblock'),
            'energy': lambda: rpc('wallet/getchainparameters'),
            'join_token': lambda: call('join', 'usdd()'),
            'underlying': lambda: call('market', 'underlying()'),
            'market_controller': lambda: call('market', 'comptroller()'),
            'market': lambda: call('comptroller', 'markets(address)', market_arg),
            'mint_paused': lambda: call('comptroller', 'mintGuardianPaused(address)', market_arg),
            'borrow_paused': lambda: call('comptroller', 'borrowGuardianPaused(address)', market_arg),
            'supply_rate': lambda: call('market', 'supplyRatePerBlock()'),
            'borrow_rate': lambda: call('market', 'borrowRatePerBlock()'),
            'cash': lambda: call('market', 'getCash()'),
            'jug_base': lambda: call('jug', 'base()'),
            'vat_live': lambda: call('vat', 'live()'),
        }
        for ilk in ('TRX-A', 'TRX-B', 'TRX-C'):
            parameter = ilk.encode().hex().ljust(64, '0')
            for contract in ('vat', 'jug', 'spot'):
                queries[contract + ':' + ilk] = lambda c=contract, p=parameter: call(c, 'ilks(bytes32)', p)

        def read(item):
            name, fn = item
            try:
                return name, fn(), None
            except Exception:
                return name, None, 'SOURCE_UNAVAILABLE'
        with ThreadPoolExecutor(max_workers=3) as pool:
            for name, value, error in pool.map(read, queries.items()):
                if error: errors[name] = error
                else: evidence[name] = value
        try:
            evidence['end_block'] = rpc('walletsolidity/getnowblock')
        except Exception:
            errors['end_block'] = 'SOURCE_UNAVAILABLE'

        at = self.bridge.clock()
        report = self.assess(scope['network'], evidence, errors, state, at)
        state['usdd_review_evidence'] = {'responses': evidence, 'errors': errors, 'report_hash': report['hash']}
        state['workspace']['usdd_review'] = report
        return report

    @staticmethod
    @exact
    def assess(network, evidence, errors, state, at):
        from .portfolio_review import confirmed_policy
        config = CONFIG[network]
        blockers = ['EXECUTION_REVIEW_REQUIRED']
        details = []
        facts = {'energy_sun': None, 'bandwidth_sun': None, 'vault_token': None,
            'destination_token': None, 'token_match': None, 'supply_apy': None,
            'borrow_apy': None, 'loop_spread': None, 'market_cash_usdd': None,
            'collateral_factor': None, 'collaterals': []}
        record = confirmed_policy(state)
        terms = record['mandate']['terms'] if record else None
        policy_hash = record['policy_hash'] if record else None
        if not terms or terms['borrowing']['consent'] is not True:
            blockers.append('BORROWING_NOT_CONSENTED')
        if record:
            start_at = datetime.fromisoformat(terms['effective_at'].replace('Z', '+00:00'))
            end_at = min(datetime.fromisoformat(terms['expires_at'].replace('Z', '+00:00')), start_at + timedelta(seconds=terms['horizon_seconds']))
            if not start_at <= datetime.fromisoformat(at.replace('Z', '+00:00')) < end_at:
                blockers.append('POLICY_INACTIVE')
        if errors:
            blockers.append('LIVE_INPUT_UNAVAILABLE')
        try:
            start, end = _block(evidence['start_block']), _block(evidence['end_block'])
            now = datetime.fromisoformat(at.replace('Z', '+00:00')).timestamp()*1000
            if not 0 <= now-start['timestamp_ms'] <= 300000 or not start['number'] <= end['number'] or end['timestamp_ms']-start['timestamp_ms'] > 120000:
                raise MachineError('USDD observation is stale or inconsistent.')
            block = {'from': start['number'], 'to': end['number']}
        except (KeyError, MachineError, TypeError, ValueError):
            blockers.append('LIVE_INPUT_UNAVAILABLE'); block = None
        try:
            params = {p['key']: p.get('value', 0) for p in evidence['energy']['chainParameter']}
            for output, key in (('energy_sun', 'getEnergyFee'), ('bandwidth_sun', 'getTransactionFee')):
                if type(params[key]) is not int or params[key] <= 0: raise ValueError()
                facts[output] = str(params[key])
        except (KeyError, TypeError, ValueError):
            blockers.append('LIVE_INPUT_UNAVAILABLE')
        try:
            token = returned_address(evidence['join_token'])
            underlying = returned_address(evidence['underlying'])
            controller = returned_address(evidence['market_controller'])
            if token != address_hex(config['usdd']) or controller != address_hex(config['comptroller']):
                raise MachineError('USDD configured contract binding changed.')
            facts.update(vault_token=address_base58(token), destination_token=address_base58(underlying), token_match=token == underlying)
            if token != underlying: blockers.append('VAULT_DESTINATION_TOKEN_MISMATCH')
            listed, factor = words(evidence['market'], 2, True)
            if listed != 1 or not 0 < factor < 10**18: blockers.append('DESTINATION_COLLATERAL_UNAVAILABLE')
            facts['collateral_factor'] = decstr(Decimal(factor)/10**18)
            if words(evidence['mint_paused'], 1, True)[0] != 0: blockers.append('DESTINATION_SUPPLY_PAUSED')
            if words(evidence['borrow_paused'], 1, True)[0] != 0: blockers.append('DESTINATION_BORROW_PAUSED')
            if words(evidence['vat_live'], 1)[0] != 1: blockers.append('VAULT_SHUTDOWN')
            # Constant-rate annualization is a forecast, not an executable return.
            supply = words(evidence['supply_rate'], 1, True)[0]
            borrow = words(evidence['borrow_rate'], 1, True)[0]
            facts['supply_apy'] = annual(Decimal(1)+Decimal(supply)/10**18, 10512000)
            facts['borrow_apy'] = annual(Decimal(1)+Decimal(borrow)/10**18, 10512000)
            facts['loop_spread'] = decstr(Decimal(facts['supply_apy'])-Decimal(facts['borrow_apy']))
            facts['market_cash_usdd'] = decstr(Decimal(words(evidence['cash'], 1, True)[0])/10**18)
            if supply <= borrow: blockers.append('RECURSIVE_SPREAD_NOT_POSITIVE')
        except (KeyError, TypeError, ValueError, MachineError):
            blockers.append('LIVE_INPUT_UNAVAILABLE')
        for ilk in ('TRX-A', 'TRX-B', 'TRX-C'):
            try:
                art, rate, spot, line, dust = words(evidence['vat:' + ilk], 5)
                _, mat = words(evidence['spot:' + ilk], 2)
                duty, _ = words(evidence['jug:' + ilk], 2)
                base = words(evidence['jug_base'], 1)[0]
                if min(rate, spot, mat, dust) <= 0: raise ValueError()
                # Vat spot is already divided by liquidation ratio and par. It
                # gives debt capacity per normalized TRX; no $1 peg is assumed.
                debt_per_trx = Decimal(spot)/RAY
                minimum = Decimal(dust)/RAD
                global_debt = Decimal(art)*rate/RAD
                row = {'ilk': ilk, 'minimum_debt_usdd': decstr(minimum),
                    'liquidation_ratio': decstr(Decimal(mat)/RAY),
                    'debt_capacity_per_trx': decstr(debt_per_trx),
                    'minimum_trx_at_liquidation': decstr(rounded(minimum/debt_per_trx, 6, liability=True)),
                    'stability_apy': annual(Decimal(duty+base)/RAY, YEAR),
                    'debt_ceiling_remaining_usdd': decstr(max(Decimal(0), Decimal(line)/RAD-global_debt)),
                    'collateral_capacity_upper_bound_usdd': None, 'policy_meets_minimum': None}
                if terms and terms['base_asset'] == 'TRX' and len(terms['capital']) == 1 and terms['capital'][0]['asset'] == 'TRX':
                    from economic_machine.mandate import reserve_amount
                    capital = Decimal(terms['capital'][0]['amount'])
                    cash = reserve_amount(terms['immediate_cash'], decstr(capital))
                    policy_ratio = max(Decimal(mat)/RAY, Decimal(terms['borrowing']['min_collateral_ratio_bps'])/10000) + Decimal(terms['borrowing']['liquidation_buffer_bps'])/10000
                    capacity = max(Decimal(0), capital-cash) * debt_per_trx * (Decimal(mat)/RAY) / policy_ratio
                    row['collateral_capacity_upper_bound_usdd'] = decstr(rounded(capacity, 18))
                    row['policy_meets_minimum'] = capacity >= minimum
                facts['collaterals'].append(row)
            except (KeyError, TypeError, ValueError, MachineError):
                blockers.append('LIVE_INPUT_UNAVAILABLE')
        if len(facts['collaterals']) == 3 and all(r['policy_meets_minimum'] is False for r in facts['collaterals']):
            blockers.append('VAULT_MINIMUM_DEBT')
        if facts['token_match'] is False:
            details.append('The Vault issues a different token contract from the configured JustLend market. A verified conversion or matching deployment is required.')
        if 'VAULT_MINIMUM_DEBT' in blockers:
            details.append('Your confirmed TRX capital is below the minimum Vault debt requirement even before transaction fees.')
        if 'RECURSIVE_SPREAD_NOT_POSITIVE' in blockers:
            details.append('The current base supply rate does not cover same-token borrowing interest. Additional borrow-and-resupply cycles reduce modeled returns before fees. Incentives are excluded.')
        if 'BORROWING_NOT_CONSENTED' in blockers:
            details.append('Your confirmed conditions do not permit borrowing. Draft chat edits do not grant that permission.')
        if 'LIVE_INPUT_UNAVAILABLE' in blockers:
            details.append('Some required live reads are unavailable or inconsistent; no executable quote can be issued.')
        details.append('A separately selected workflow, complete fee reserve, live step simulation and exact wallet approval are required before any USDD transaction.')
        expiry = datetime.fromisoformat(at.replace('Z', '+00:00')) + timedelta(minutes=5)
        if block is not None:
            from datetime import timezone
            expiry = min(expiry, datetime.fromtimestamp(start['timestamp_ms']/1000, timezone.utc) + timedelta(minutes=5))
        result = {'id': 'usdd-review-' + digest({'network': network, 'at': at, 'evidence': evidence})[:24],
            'network': network.removeprefix('tron-'), 'observed_at': at,
            'expires_at': expiry.isoformat(),
            'status': 'BLOCKED', 'policy_hash': policy_hash,
            'blockers': sorted(set(blockers)), 'reason': ' '.join(details), 'facts': facts,
            'source_urls': [SOURCE, DESTINATION_SOURCE, config['root']],
            'block_range': block,
            'basis': 'Independent solidified RPC calls within a bounded observation window. Variable rates annualized at 3 seconds per block and 365 days per year; excludes incentives. No atomic state proof, execution quote, guaranteed APY or authority.',
            'execution_authority': 'NONE', 'evidence_hash': digest({'responses': evidence, 'errors': errors})}
        result['hash'] = digest(result)
        return result
