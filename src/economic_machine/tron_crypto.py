"""Small dependency-free Keccak and secp256k1 recovery primitives for TRON.

These functions verify already-signed transaction payloads.  They never create
private keys or signatures and deliberately accept only canonical low-s,
single-key recoverable signatures.
"""

from .values import MachineError


_ROT = (
    0, 1, 62, 28, 27, 36, 44, 6, 55, 20, 3, 10, 43,
    25, 39, 41, 45, 15, 21, 8, 18, 2, 61, 56, 14,
)
_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
    0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)
_MASK = (1 << 64) - 1
_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_HALF_N = _N // 2
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _rol(value, amount):
    if amount == 0:
        return value
    return ((value << amount) | (value >> (64 - amount))) & _MASK


def _permutation(state):
    for constant in _RC:
        column = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15]
                  ^ state[x + 20] for x in range(5)]
        delta = [column[(x - 1) % 5] ^ _rol(column[(x + 1) % 5], 1)
                 for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] ^= delta[x]
        moved = [0] * 25
        for x in range(5):
            for y in range(5):
                moved[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(
                    state[x + 5 * y], _ROT[x + 5 * y])
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] = (moved[x + 5 * y]
                    ^ ((~moved[(x + 1) % 5 + 5 * y]) & moved[(x + 2) % 5 + 5 * y])) & _MASK
        state[0] ^= constant


def keccak256(data: bytes) -> bytes:
    if not isinstance(data, bytes):
        raise MachineError("Keccak input must be bytes")
    rate = 136
    padded = bytearray(data)
    padded.append(0x01)
    padded.extend(b"\x00" * ((rate - len(padded) % rate) % rate))
    padded[-1] |= 0x80
    state = [0] * 25
    for offset in range(0, len(padded), rate):
        block = padded[offset:offset + rate]
        for index in range(rate // 8):
            state[index] ^= int.from_bytes(block[index * 8:index * 8 + 8], "little")
        _permutation(state)
    return b"".join(value.to_bytes(8, "little") for value in state)[:32]


def function_selector(signature: str) -> str:
    if not isinstance(signature, str) or not signature or any(ord(c) > 127 for c in signature):
        raise MachineError("ASCII function signature required")
    return keccak256(signature.encode("ascii"))[:4].hex()


def _inverse(value, modulus):
    if value % modulus == 0:
        raise MachineError("elliptic curve inverse does not exist")
    return pow(value, modulus - 2, modulus)


def _add(first, second):
    if first is None:
        return second
    if second is None:
        return first
    x1, y1 = first
    x2, y2 = second
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    slope = ((3 * x1 * x1) * _inverse(2 * y1, _P) if first == second
             else (y2 - y1) * _inverse(x2 - x1, _P)) % _P
    x3 = (slope * slope - x1 - x2) % _P
    return x3, (slope * (x1 - x3) - y1) % _P


def _multiply(scalar, point):
    result = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    return result


def recover_tron_address(message_hash: bytes, signature: bytes) -> str:
    if not isinstance(message_hash, bytes) or len(message_hash) != 32:
        raise MachineError("32-byte signed hash required")
    if not isinstance(signature, bytes) or len(signature) != 65:
        raise MachineError("65-byte recoverable signature required")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:64], "big")
    recovery = signature[64]
    if recovery in {27, 28}:
        recovery -= 27
    if recovery not in {0, 1} or not 1 <= r < _N or not 1 <= s <= _HALF_N:
        raise MachineError("noncanonical secp256k1 signature")
    x = r
    if x >= _P:
        raise MachineError("invalid recovery point")
    alpha = (pow(x, 3, _P) + 7) % _P
    y = pow(alpha, (_P + 1) // 4, _P)
    if y & 1 != recovery:
        y = _P - y
    point = (x, y)
    if _multiply(_N, point) is not None:
        raise MachineError("signature point is outside secp256k1 subgroup")
    z = int.from_bytes(message_hash, "big")
    public = _multiply(_inverse(r, _N), _add(_multiply(s, point),
                                             _multiply((-z) % _N, _G)))
    if public is None:
        raise MachineError("signature recovered point at infinity")
    encoded = public[0].to_bytes(32, "big") + public[1].to_bytes(32, "big")
    return "41" + keccak256(encoded)[-20:].hex()
