"""One fixed public getter batch, with its in-call block number and timestamp.

No general user calldata or arbitrary multicall targets are exposed. No writes
are sent: aggregate3 is invoked only through triggerconstantcontract simulation.
"""

from .tron_constant import MULTICALL, SELECTORS, constant_result, decode_words
from .tron_registry_read import _block, _word
from .values import MachineError


def public_calls(products):
    calls = []
    for name in sorted(products):
        if name.startswith("justlend.v1."):
            calls.extend((products[name]["capability"]["contract"], selector)
                         for selector in ("getCash()", "exchangeRateStored()"))
    strx = products["justlend.strx"]["capability"]["contract"]
    calls.extend([(strx, "exchangeRate()"), (strx, "totalUnderlying()"),
                  (MULTICALL, "getBlockNumber()"), (MULTICALL, "getCurrentBlockTimestamp()")])
    if len(calls) != 10 or len(set(calls)) != 10:
        raise MachineError("public batch requires exactly eight product getters and two block getters")
    return calls


def encode_calls(calls):
    if not 1 <= len(calls) <= 10:
        raise MachineError("public batch call budget exceeded")
    tuples = []
    for contract, selector in calls:
        # Every supported public getter takes no arguments. allowFailure=false.
        if selector not in {"getCash()", "exchangeRateStored()", "exchangeRate()", "totalUnderlying()",
                            "getBlockNumber()", "getCurrentBlockTimestamp()"}:
            raise MachineError("non-public getter in public batch")
        tuples.append(_word(int(contract[2:], 16)) + _word(0) + _word(96)
                      + _word(4) + SELECTORS[selector].ljust(64, "0"))
    offset = len(calls) * 32
    offsets = []
    for item in tuples:
        offsets.append(_word(offset))
        offset += len(item) // 2
    return _word(32) + _word(len(calls)) + "".join(offsets) + "".join(tuples)


def decode_results(raw, calls):
    if not isinstance(raw, str) or len(raw) % 64 or len(raw) > 20000:
        raise MachineError("invalid multicall result size")
    try:
        data = bytes.fromhex(raw)
    except ValueError as exc:
        raise MachineError("invalid multicall result hex") from exc

    def word(offset):
        if offset < 0 or offset + 32 > len(data):
            raise MachineError("truncated multicall ABI")
        return int.from_bytes(data[offset:offset+32], "big")

    if word(0) != 32 or word(32) != len(calls):
        raise MachineError("multicall count/array offset mismatch")
    cursor = 64 + 32 * len(calls)
    results = {}
    for index, (contract, selector) in enumerate(calls):
        if word(64 + 32*index) != cursor - 64 or word(cursor) != 1 or word(cursor+32) != 64:
            raise MachineError("multicall failed result or noncanonical offset")
        length = word(cursor+64)
        if length not in {32, 96}:
            raise MachineError("unexpected public getter byte length")
        start, end = cursor+96, cursor+96+length
        if end > len(data):
            raise MachineError("truncated multicall getter")
        results[(contract, selector)] = decode_words(contract, selector, data[start:end].hex())[0]
        cursor = end
    if cursor != len(data):
        raise MachineError("trailing/overlapping multicall data")
    return results


def read_public_batch(request, products, caller):
    calls = public_calls(products)
    raw = constant_result(request, MULTICALL, caller, "aggregate3((address,bool,bytes)[])", encode_calls(calls))
    results = decode_results(raw, calls)
    height = results.pop((MULTICALL, "getBlockNumber()"))
    timestamp = results.pop((MULTICALL, "getCurrentBlockTimestamp()"))
    if height >= 1 << 63 or timestamp >= 1 << 63:
        raise MachineError("invalid in-call block metadata")
    block = _block(request("/walletsolidity/getblockbynum", {"num": height}))
    if block["number"] != height or block["timestamp_ms"] // 1000 != timestamp:
        raise MachineError("in-call block/header number or timestamp mismatch")
    return results, block
