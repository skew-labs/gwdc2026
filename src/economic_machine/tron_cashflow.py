"""Snapshot-bound monetary comparisons, before PR04 portfolio optimization.

The assembler replays raw responses; user conditions come from a confirmed
record. Prices, exits, resource demand and future-rate conventions are explicit
scenario inputs. A computed forecast is never an eligible execution or oracle.
"""

from datetime import datetime
from decimal import Decimal

from .application import confirmed_mandate, single_asset_budget
from .liquidity import check_schedule, lending_exit, native_exit, net_recovery, recovery_curve
from .mandate import integer, reserve_amount
from .resource_cost import rental_income, transaction_costs
from .values import MachineError, decstr, digest, require_keys, utc
from .vault_accounting import vault_cashflow
from .yield_math import YEAR, exact, net_cashflow, period_yield, quantity, reward_income, rounded


VERSION = 'economic-tron-cashflow-1'
QUOTE_KEYS = {'product_id', 'principal', 'prices_base', 'costs_base', 'redemption_seconds',
              'reward_claim_seconds', 'reward_haircut_bps', 'exit_tranches', 'native', 'vault', 'resources'}
NATIVE_KEYS = {'voting_rate', 'lock_remaining_seconds', 'resource_recovery_seconds',
               'window', 'rental'}
VAULT_KEYS = {'deployed_usdd', 'stored_debt_usdd', 'fee_age_seconds', 'fee_convention',
              'destination_id', 'destination_redemption_seconds', 'collateral_release_seconds', 'shocks'}


class Unavailable(MachineError):
    pass


def rate(value, convention='EFFECTIVE_APY'):
    return {'annual_fraction': value, 'convention': convention, 'day_count': 'ACT_365F'}


class BoundFacts:
    def __init__(self, snapshot, at, max_age):
        self.snapshot, self.used, self.at, self.max_age = snapshot, {}, at, max_age

    def get(self, path, unit):
        fact = self.snapshot['facts'].get(path)
        if (not fact or fact['unit'] != unit or fact['quality'] not in {'VALID', 'VALID_ZERO'}
                or fact['availability'] != 'AVAILABLE'):
            raise Unavailable('INPUT_UNAVAILABLE:' + path)
        age = (datetime.fromisoformat(self.at) - datetime.fromisoformat(fact['received_at'])).total_seconds()
        if not 0 <= age <= self.max_age:
            raise Unavailable('INPUT_RECEIPT_EXPIRED:' + path)
        if any(c['path'] == path and c['status'] == 'DISAGREE' for c in self.snapshot['comparisons']):
            raise Unavailable('INPUT_CONFLICT:' + path)
        if {'SOURCE_DISAGREEMENT', 'STALE_OBSERVATION', 'REGISTRY_UNAVAILABLE',
            'SOURCE_RECEIPT_SKEW'} & set(fact['withheld_reasons']):
            raise Unavailable('INPUT_CONFLICT:' + path)
        self.used[path] = {key: fact[key] for key in ('value', 'unit', 'capture_hash',
                            'source_id', 'observed_at', 'block', 'state_eligible', 'withheld_reasons')}
        return quantity(fact['value'], signed=True)


def _price(quote, asset):
    prices = quote['prices_base']
    if not isinstance(prices, dict) or not set(prices) <= {'USDT', 'USDD', 'TRX'}:
        raise MachineError('only explicit USDT/USDD/TRX base prices supported')
    for value in prices.values():
        if quantity(value) <= 0:
            raise MachineError('positive price assumption required')
    if prices.get('USDT') != '1':
        raise MachineError('USDT base price must be exactly one')
    if asset not in prices:
        raise Unavailable('PRICE_ASSUMPTION_MISSING:' + asset)
    return quantity(prices[asset])


@exact
def _quote(snapshot, terms, quote, facts, capital):
    require_keys(quote, QUOTE_KEYS, 'TRON cashflow quote')
    name = quote['product_id']
    product = snapshot['products'].get(name)
    if product is None:
        raise Unavailable('UNSUPPORTED_PRODUCT')
    cap = product['capability']
    is_vault = name.startswith('usdd.vault.')
    is_native = name == 'tron.native.stake'
    is_strx = name == 'justlend.strx'
    is_supply = name in {'justlend.v1.jUSDT', 'justlend.v1.jUSDD', 'justlend.v1.jUSDDOLD'}
    if not (is_vault or is_native or is_strx or is_supply):
        raise Unavailable('UNSUPPORTED_PRODUCT')
    if is_supply and not product['new_supply_allowed']:
        raise Unavailable('LEGACY_MARKET_NEW_SUPPLY_EXCLUDED')
    if is_vault != (quote['vault'] is not None) or is_native != (quote['native'] is not None):
        raise MachineError('product-specific accounting terms mismatch')
    if not is_strx and quote['exit_tranches'] is not None:
        raise MachineError('custom exit tranches only supported for sTRX queue assumptions')
    amount = quantity(quote['principal'])
    if amount <= 0:
        raise MachineError('positive underlying principal required')
    horizon = terms['horizon_seconds']
    integer(quote['redemption_seconds'], 'redemption seconds', 0, YEAR)
    integer(quote['reward_haircut_bps'], 'reward haircut', 0, 10000)
    if quote['reward_claim_seconds'] is not None:
        integer(quote['reward_claim_seconds'], 'reward claim seconds', 0, 2 * YEAR)
    asset = 'TRX' if is_native or is_strx else name.split('.')[-1].split('-')[0] if is_vault else cap['token']['asset']
    decimals = 6 if asset in {'TRX', 'USDT'} else cap['token']['decimals']
    if amount != rounded(amount, decimals):
        raise MachineError('principal contains a fractional underlying token atom')
    price = _price(quote, asset)
    principal = rounded(amount * price, liability=True)
    costs = dict(require_keys(quote['costs_base'], {'entry', 'exit', 'conversion', 'network'}, 'costs'))
    resource_detail = None
    if quote['resources'] is not None:
        resources = require_keys(quote['resources'], {'window', 'transactions'}, 'resource assumptions')
        if quantity(costs['network']) != 0:
            raise MachineError('network fee cannot count both a supplied total and resource burn')
        resource_detail = transaction_costs(resources['window'], resources['transactions'], {
            'sun_per_energy': _whole(facts.get('tron.chain.getEnergyFee', 'sun_per_energy')),
            'sun_per_byte': _whole(facts.get('tron.chain.getTransactionFee', 'sun_per_byte')),
            'trx_price_base': decstr(_price(quote, 'TRX'))})
        costs['network'] = resource_detail['cost_base']
    reasons, exposures = [], {asset: principal}
    if cap['action'] not in terms['allowed_actions']:
        reasons.append('ACTION_NOT_ALLOWED')
    detail, income, repayment_reserve, principal_loss = {}, Decimal(0), Decimal(0), Decimal(0)
    if is_vault:
        if terms['borrowing']['consent'] is not True:
            raise Unavailable('BORROWING_NOT_CONSENTED')
        v = require_keys(quote['vault'], VAULT_KEYS, 'vault quote')
        dest = v['destination_id']
        if dest != 'justlend.v1.jUSDD' or not snapshot['products'][dest]['new_supply_allowed']:
            raise Unavailable('VAULT_DESTINATION_UNSUPPORTED')
        destination_rate = rate(decstr(facts.get(dest + '.supply_apy', 'annual_fraction')))
        # No free destination_apy input exists: the rate, identity and snapshot
        # commitment all come from the same replayed destination observation.
        minimum = max(facts.get(name + '.liquidation_ratio', 'multiple'),
                      Decimal(terms['borrowing']['min_collateral_ratio_bps']) / 10000)
        fee_rate = rate(decstr(facts.get(name + '.stability_fee', 'annual_fraction')), v['fee_convention'])
        min_debt = facts.get('usdd.config.' + product['ilk'] + '.minimum_debt', 'USDD')
        deployed, debt = quantity(v['deployed_usdd']), quantity(v['stored_debt_usdd'])
        if debt < min_debt:
            reasons.append('VAULT_MINIMUM_DEBT')
        type_debt = facts.get(name + '.type_debt', 'USDD')
        ceiling = facts.get(name + '.type_debt_ceiling', 'USDD')
        if type_debt + debt > ceiling:
            reasons.append('VAULT_TYPE_DEBT_CEILING')
        usdd_px = _price(quote, 'USDD')
        detail = vault_cashflow(collateral_base=decstr(principal), deployed_usdd=v['deployed_usdd'],
            stored_debt_usdd=v['stored_debt_usdd'], fee_rate=fee_rate, fee_age_seconds=v['fee_age_seconds'],
            horizon_seconds=horizon, destination_rate=destination_rate, usdd_price_base=decstr(usdd_px),
            min_ratio=decstr(minimum), buffer_bps=terms['borrowing']['liquidation_buffer_bps'], shocks=v['shocks'])
        detail['destination_binding'] = {'product_id': dest, 'identity_hash': snapshot['products'][dest]['identity_hash'],
                                       'snapshot_hash': snapshot['snapshot_hash'], 'rate': destination_rate}
        reasons.extend(detail['reason_codes'])
        future_debt = quantity(detail['modeled_future_debt_usdd'])
        if future_debt * usdd_px > quantity(terms['borrowing']['max_debt']['amount']):
            reasons.append('VAULT_DEBT_LIMIT')
        if 'SUPPLY' not in terms['allowed_actions']:
            reasons.append('DESTINATION_ACTION_NOT_ALLOWED')
        dest_value = deployed * usdd_px
        if dest_value * 10000 > capital * terms['protocol_caps_bps'].get('justlend', 0):
            reasons.append('DESTINATION_PROTOCOL_LIMIT')
        if any(quantity(s['loss_base']) > quantity(terms['limits']['stress_loss']['amount']) for s in detail['scenarios']):
            reasons.append('VAULT_STRESS_LOSS_LIMIT')
        exposures['USDD'] = exposures.get('USDD', Decimal(0)) + (deployed + future_debt) * usdd_px
        income = quantity(detail['net_income_base'], signed=True)
        # Reserve the debt gap without assuming forecast profit is claimable.
        repayment_reserve = rounded(max(Decimal(0), future_debt - deployed) * usdd_px, liability=True)
        capacity = facts.get(dest + '.available_cash', 'USDD')
        integer(v['destination_redemption_seconds'], 'destination exit seconds', 0, YEAR)
        integer(v['collateral_release_seconds'], 'collateral release seconds', 0, YEAR)
        delay = v['destination_redemption_seconds'] + v['collateral_release_seconds']
        curve = recovery_curve(decstr(amount), [{'amount': decstr(amount),
            'after_seconds': delay if capacity >= deployed else None}])
    elif is_native:
        n = require_keys(quote['native'], NATIVE_KEYS, 'native stake quote')
        detail['voting'] = period_yield(decstr(amount), n['voting_rate'], horizon)
        income = quantity(detail['voting']['income'], signed=True) * price
        principal_loss = max(Decimal(0), -income)
        delay = facts.get('tron.chain.getUnfreezeDelayDays', 'days') * 86400
        curve = native_exit(decstr(amount), lock_remaining_seconds=n['lock_remaining_seconds'],
            resource_recovery_seconds=n['resource_recovery_seconds'], unstake_seconds=_whole(delay))
        if n['rental'] is not None:
            from .resource_cost import resource_budget
            rental = require_keys(n['rental'], {'rented_energy', 'quoted_base_per_energy', 'occupied_windows',
                                               'commission_bps', 'window_seconds'}, 'rental quote')
            integer(rental['window_seconds'], 'rental window seconds', 1, YEAR)
            if rental['occupied_windows'] * rental['window_seconds'] > horizon:
                raise MachineError('rental occupancy exceeds holding period')
            detail['rental'] = rental_income(n['window'], **{k: v for k, v in rental.items() if k != 'window_seconds'})
            resource_budget(n['window'])
            income += quantity(detail['rental']['net_base'])
            if quote['resources'] is not None and quote['resources']['window'] != n['window']:
                raise MachineError('rental and transaction costs must share one resource window')
            if 'DELEGATE_ENERGY' not in terms['allowed_actions']:
                reasons.append('ENERGY_DELEGATION_NOT_ALLOWED')
        elif n['window'] is not None:
            raise MachineError('resource window without rental terms')
    else:
        path = name + ('.aggregate_apy' if is_strx else '.supply_apy')
        detail['base'] = period_yield(decstr(amount), rate(decstr(facts.get(path, 'annual_fraction'))), horizon,
                                      decimals=6 if asset == 'TRX' else cap['token']['decimals'])
        income = quantity(detail['base']['income'], signed=True) * price
        principal_loss = max(Decimal(0), -income)
        if is_strx:
            if quote['exit_tranches'] is None:
                raise Unavailable('STRX_EXIT_QUEUE_UNKNOWN')
            curve = recovery_curve(decstr(amount), quote['exit_tranches'])
        else:
            detail['reward'] = reward_income(decstr(amount), rate(decstr(facts.get(name + '.reward_apy', 'annual_fraction'))),
                horizon, claim_after_seconds=quote['reward_claim_seconds'], haircut_bps=quote['reward_haircut_bps'],
                decimals=cap['token']['decimals'])
            # Mining APY is a notional return on supplied value; no USDD=USDT
            # peg is invented. Haircut/claim time describe conversion proceeds.
            income += quantity(detail['reward']['claimable_income']) * price
            curve = lending_exit(decstr(amount), decstr(facts.get(name + '.available_cash', asset)),
                                 settlement_seconds=quote['redemption_seconds'])
    accounting = net_cashflow(decstr(principal), decstr(rounded(income)), costs)
    total_cost = quantity(accounting['total_cost_base'])
    cash_left = capital - principal - total_cost - repayment_reserve
    reserve = reserve_amount(terms['immediate_cash'], decstr(capital))
    if cash_left < reserve:
        reasons.append('CAPITAL_WITH_FEES_AND_RESERVES_EXCEEDED')
    if total_cost > quantity(terms['limits']['fee_amount']['amount']):
        reasons.append('FEE_LIMIT')
    if principal > quantity(terms['limits']['single_amount']['amount']):
        reasons.append('SINGLE_AMOUNT_LIMIT')
    for symbol, exposure in exposures.items():
        if symbol != 'USDT' and exposure * 10000 > capital * terms['price_exposure_caps_bps'].get(symbol, 0):
            reasons.append('PRICE_EXPOSURE_LIMIT:' + symbol)
    if principal * 10000 > capital * terms['protocol_caps_bps'].get(cap['protocol'], 0):
        reasons.append('PROTOCOL_LIMIT')
    # Positive forecast income is never cash for a withdrawal constraint.
    # Principal erosion, however, must reduce it even if unclaimed rewards
    # might later offset the economic loss. Apply the whole horizon loss as a
    # conservative deduction at every earlier checkpoint.
    base_curve = net_recovery(curve, decstr(price), debt_due=decstr(rounded(principal_loss, liability=True)))
    requirements = [{'after_seconds': 0, 'minimum': decstr(reserve)}] + [
        {'after_seconds': item['after_seconds'], 'minimum': decstr(reserve_amount(item['minimum'], decstr(capital)))}
        for item in terms['withdrawals']]
    checks = check_schedule(base_curve, decstr(max(Decimal(0), cash_left)), requirements)
    if not all(c['satisfied'] for c in checks):
        reasons.append('LIQUIDITY_REQUIREMENT')
    return {'status': 'EXCLUDED' if reasons else 'CALCULATED_ASSUMPTIONS', 'reason_codes': sorted(set(reasons)),
            'principal_asset': asset, 'principal_amount': decstr(amount), 'accounting': accounting,
            'repayment_reserve_base': decstr(repayment_reserve), 'unallocated_cash_base': decstr(cash_left),
            'principal_loss_reserve_base': decstr(rounded(principal_loss, liability=True)),
            'liquidity': base_curve, 'liquidity_checks': checks, 'detail': detail, 'resources': resource_detail,
            'price_exposure_base': {k: decstr(v) for k, v in sorted(exposures.items())}}


def _whole(value):
    if value < 0 or value != value.to_integral_value():
        raise MachineError('integer chain parameter required')
    return int(value)


@exact
def calculate_tron_cashflows(record, snapshot, assumptions, *, assembler, at):
    at = utc(at)
    mandate = confirmed_mandate(record, at)
    snapshot = assembler.verify(snapshot)
    if snapshot['network'] != mandate['scope']['network'] or (
            snapshot['scope'] is not None and snapshot['scope'] != mandate['scope']):
        raise MachineError('financial calculation scope differs from mandate')
    age = (datetime.fromisoformat(at) - datetime.fromisoformat(snapshot['as_of'])).total_seconds()
    if not 0 <= age <= assembler.config['max_age_seconds']:
        raise MachineError('snapshot is stale or from the future')
    directory = next(c for c in snapshot['captures'] if c['source_id'] == 'justlend_contracts')
    registry_age = (datetime.fromisoformat(at) - datetime.fromisoformat(directory['received_at'])).total_seconds()
    if directory['error'] or not 0 <= registry_age <= assembler.config['max_registry_age_seconds']:
        raise MachineError('product registry is stale or unavailable')
    require_keys(assumptions, {'schema_version', 'snapshot_hash', 'quotes'}, 'cashflow assumptions')
    if assumptions['schema_version'] != VERSION or assumptions['snapshot_hash'] != snapshot['snapshot_hash']:
        raise MachineError('cashflow assumptions version/snapshot binding mismatch')
    quotes = assumptions['quotes']
    if not isinstance(quotes, list) or not 1 <= len(quotes) <= 16:
        raise MachineError('bounded independent quote list required')
    asset, capital, _ = single_asset_budget(mandate)
    if asset != 'USDT':
        raise MachineError('cashflow base must be USDT')
    results, seen = [], set()
    for quote in quotes:
        require_keys(quote, QUOTE_KEYS, 'TRON cashflow quote')
        name = quote['product_id']
        if not isinstance(name, str) or name in seen:
            raise MachineError('duplicate/invalid cashflow product id')
        seen.add(name)
        facts = BoundFacts(snapshot, at, assembler.config['max_age_seconds'])
        try:
            row = _quote(snapshot, mandate['terms'], quote, facts, quantity(capital))
        except Unavailable as exc:
            row = {'status': 'WITHHELD', 'reason_codes': [str(exc)]}
        row.update(product_id=name, inputs=facts.used, assumption_hash=digest(quote))
        results.append(row)
    result = {'schema_version': VERSION, 'at': at, 'scope': mandate['scope'], 'snapshot_hash': snapshot['snapshot_hash'],
              'mandate_policy_hash': record['policy_hash'], 'mandate_draft_hash': record['draft_hash'],
              'assumptions_hash': digest(assumptions), 'mode': snapshot['mode'], 'base_asset': asset,
              'comparison_scope': 'INDEPENDENT_SINGLE_POSITION_QUOTES_NOT_A_PORTFOLIO', 'quotes': results,
              'forecast_basis': 'CONSTANT_RATES_PRICES_AND_EXPLICIT_EXIT_RESOURCE_ASSUMPTIONS',
              'remaining_gates': ['PORTFOLIO_OPTIMIZATION_AND_FULL_POLICY_REPLAY', 'LIVE_PRICES_AND_EXIT_QUOTES',
                                  'EXACT_VAULT_REPAYMENT', 'TRANSACTION_PREFLIGHT', 'USER_SIGNATURE'],
              'execution_authority': 'NONE'}
    result['calculation_hash'] = digest(result)
    return result


def verify_tron_cashflows(result, record, snapshot, assumptions, *, assembler, at):
    try:
        return result == calculate_tron_cashflows(record, snapshot, assumptions, assembler=assembler, at=at)
    except (MachineError, KeyError, TypeError, ValueError):
        return False
