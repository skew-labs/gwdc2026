"""Read-only verification of public Nile integration receipts.

Never accepts keys, signs, or broadcasts. Local hashes establish capture
consistency, not independent chain consensus or full protocol reconciliation.
"""
import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RPC = 'https://nile.trongrid.io'
TRANSACTIONS = {
    'supply': '877d3dcb132ff55b37f0cb24286c9ce3fce4e0966c21bdc4b0924f6d7652a7c2',
    'redeem': 'b83d55426b98f73ff458e61d003f062a58d2588bbcad21b3d02ebf72f1f0d628',
}


def fetch(method, txid):
    request = urllib.request.Request(
        RPC + '/walletsolidity/' + method,
        data=json.dumps({'value': txid}).encode(),
        headers={'Content-Type': 'application/json', 'User-Agent': 'faat-read-only-verifier'},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError('RPC response exceeds capture bound')
    return json.loads(raw)


def validate(txid, transaction, receipt):
    if transaction.get('txID') != txid or receipt.get('id') != txid:
        raise ValueError('transaction/receipt identity mismatch')
    if hashlib.sha256(bytes.fromhex(transaction['raw_data_hex'])).hexdigest() != txid:
        raise ValueError('raw transaction hash mismatch')
    if transaction.get('ret', [{}])[0].get('contractRet') != 'SUCCESS':
        raise ValueError('transaction did not report contract success')
    if receipt.get('receipt', {}).get('result') != 'SUCCESS':
        raise ValueError('solid receipt did not report success')
    if type(receipt.get('blockNumber')) is not int or receipt['blockNumber'] <= 0:
        raise ValueError('missing receipt block')
    if type(receipt.get('fee')) is not int or receipt['fee'] < 0:
        raise ValueError('invalid receipt fee')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true', help='Refresh public solidified RPC captures')
    args = parser.parse_args()
    destination = ROOT / 'evidence/nile'
    destination.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, txid in TRANSACTIONS.items():
        path = destination / (name + '.json')
        if args.refresh:
            transaction = fetch('gettransactionbyid', txid)
            receipt = fetch('gettransactioninfobyid', txid)
            validate(txid, transaction, receipt)
            document = {'network': 'nile', 'rpc': RPC,
                        'captured_at': datetime.now(UTC).isoformat(),
                        'transaction': transaction, 'receipt': receipt}
            path.write_text(json.dumps(document, indent=2, sort_keys=True) + '\n')
        document = json.loads(path.read_text())
        if document.get('network') != 'nile' or document.get('rpc') != RPC:
            raise ValueError('capture network/source mismatch')
        validate(txid, document['transaction'], document['receipt'])
        entries.append({'path': str(path.relative_to(ROOT)), 'txid': txid,
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                        'block': document['receipt']['blockNumber'],
                        'fee_sun': str(document['receipt']['fee']),
                        'energy_used': document['receipt']['receipt'].get('energy_usage_total')})
    report = {'schema': 'faat-nile-public-receipts-v1', 'status': 'PASS',
              'mode': 'PUBLIC_RPC_REFRESH' if args.refresh else 'OFFLINE_CAPTURE_CHECK',
              'checked_at': datetime.now(UTC).isoformat(), 'transactions': entries}
    if args.refresh:
        (destination / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
