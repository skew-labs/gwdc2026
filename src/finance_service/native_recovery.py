"""Public recovery pointers from durable records; never expose signing payloads."""
from datetime import datetime, timezone
from economic_machine.tron_sources import address_base58


def pending_requests(state):
    return [(txid, row) for txid, row in state.get('native_requests', {}).items()
            if not row.get('broadcast_attempted') and not row.get('resolution')]


def recovery_projection(state, context):
    pointers = []
    for txid, row in pending_requests(state):
        expires = row['transaction']['raw_data']['expiration'] / 1000
        pointers.append({'txid': txid, 'graph_id': row['graph_id'], 'step_id': row['step_id'],
            'approval_id': row['approval_id'], 'account': address_base58(context.wallet),
            'network': context.network.removeprefix('tron-'), 'phase': 'PREPARED',
            'expires_at': datetime.fromtimestamp(expires, timezone.utc).isoformat(),
            'recover_after': datetime.fromtimestamp(expires + 60, timezone.utc).isoformat()})
    return pointers
