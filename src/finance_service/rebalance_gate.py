"""Shared PR09 economic/cooldown decision, without scheduling side effects."""
from datetime import datetime, timedelta
from economic_machine.values import MachineError, utc


def rebalance_reasons(benefit, cost, last_completed_at, cooldown_seconds, at):
    def amount(value, label):
        if not isinstance(value, str) or not value.isascii() or not value.isdigit() or (len(value)>1 and value.startswith('0')):
            raise MachineError('invalid '+label)
        return int(value)
    benefit=amount(benefit,'rebalance benefit'); cost=amount(cost,'rebalance cost')
    if type(cooldown_seconds) is not int or not 0 <= cooldown_seconds <= 604800:
        raise MachineError('invalid rebalance cooldown')
    reasons=[]
    if benefit <= cost: reasons.append('EXPECTED_BENEFIT_NOT_ABOVE_COST')
    if last_completed_at is not None and datetime.fromisoformat(utc(at)) < datetime.fromisoformat(utc(last_completed_at))+timedelta(seconds=cooldown_seconds):
        reasons.append('REBALANCE_COOLDOWN_ACTIVE')
    return reasons
