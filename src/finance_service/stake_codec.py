"""Strict protobuf validation for the five reviewed TRON staking operations."""

import hashlib, re
from economic_machine.signed_tx_validation import _book, _fields
from economic_machine.tron_sources import address_hex
from economic_machine.values import MachineError

# One owner signature, protobuf framing and the protocol result allowance.
# The gate below rejects any transaction exceeding this budget before signing.
BANDWIDTH_BYTES = 400

TYPES = {
    "FreezeBalanceV2Contract": 54,
    "VoteWitnessContract": 4,
    "UnfreezeBalanceV2Contract": 55,
    "WithdrawExpireUnfreezeContract": 56,
    "WithdrawBalanceContract": 13,
}


def decode(raw):
    f = _book(raw, {1: 2, 4: 2, 8: 0, 11: 2, 14: 0, 18: 0}, "staking transaction")
    if (
        not {1, 4, 8, 11, 14}.issubset(f)
        or len(f[1]) != 2
        or len(f[4]) != 8
        or f.get(18, 0) != 0
    ):
        raise MachineError("Invalid native staking TAPOS/time/fee fields.")
    c = _book(f[11], {1: 0, 2: 2, 5: 0}, "staking contract")
    if c.get(5, 0) != 0:
        raise MachineError("Only owner permission is supported for staking.")
    a = _book(c.get(2, b""), {1: 2, 2: 2}, "staking Any")
    kind = next((k for k, v in TYPES.items() if v == c.get(1)), None)
    if (
        not kind
        or a.get(1) != ("type.googleapis.com/protocol." + kind).encode()
        or 2 not in a
    ):
        raise MachineError("Unsupported staking contract type.")
    body = a[2]
    if kind == "VoteWitnessContract":
        owner = None
        votes = []
        for n, w, v in _fields(body):
            if n == 1 and w == 2 and owner is None:
                owner = address_hex(v.hex())
            elif n == 2 and w == 2:
                vote = _book(v, {1: 2, 2: 0}, "representative vote")
                if set(vote) != {1, 2} or vote[2] <= 0:
                    raise MachineError("Invalid voting amount.")
                votes.append(
                    {"vote_address": address_hex(vote[1].hex()), "vote_count": vote[2]}
                )
            else:
                raise MachineError("Unexpected voting field.")
        if (
            not owner
            or not 1 <= len(votes) <= 30
            or len({v["vote_address"] for v in votes}) != len(votes)
        ):
            raise MachineError("Invalid vote allocation.")
        value = {
            "owner_address": owner,
            "votes": sorted(votes, key=lambda x: x["vote_address"]),
        }
    else:
        b = _book(
            body,
            (
                {1: 2, 2: 0, 3: 0}
                if kind in ("FreezeBalanceV2Contract", "UnfreezeBalanceV2Contract")
                else {1: 2}
            ),
            "staking body",
        )
        if 1 not in b:
            raise MachineError("Native owner missing.")
        value = {"owner_address": address_hex(b[1].hex())}
        if kind in ("FreezeBalanceV2Contract", "UnfreezeBalanceV2Contract"):
            if b.get(2, 0) <= 0 or b.get(3, 0) not in (0, 1):
                raise MachineError("Native amount/resource invalid.")
            value.update(
                **{
                    (
                        "frozen_balance"
                        if kind == "FreezeBalanceV2Contract"
                        else "unfreeze_balance"
                    ): b[2]
                },
                resource="ENERGY" if b.get(3, 0) == 1 else "BANDWIDTH",
            )
    return {
        "kind": kind,
        "value": value,
        "timestamp": f[14],
        "expiration": f[8],
        "ref_block_bytes": f[1].hex(),
        "ref_block_hash": f[4].hex(),
    }


def validate_unsigned(transaction, expected, now_ms):
    raw_hex = transaction.get("raw_data_hex", "")
    txid = transaction.get("txID", "")
    if (
        not isinstance(raw_hex, str)
        or len(raw_hex) > 16384
        or not re.fullmatch("[0-9a-f]+", raw_hex)
        or len(raw_hex) % 2
    ):
        raise MachineError("Invalid canonical native transaction bytes.")
    raw = bytes.fromhex(raw_hex)
    if hashlib.sha256(raw).hexdigest() != txid:
        raise MachineError("Native transaction ID does not match bytes.")
    d = decode(raw)
    json_raw = transaction.get("raw_data", {})
    contracts = json_raw.get("contract")
    if (
        transaction.get("visible", False) is not False
        or not isinstance(contracts, list)
        or len(contracts) != 1
    ):
        raise MachineError("One canonical native contract required.")
    c = contracts[0]
    value = c.get("parameter", {}).get("value", {})
    if d["kind"] == "VoteWitnessContract" and isinstance(value.get("votes"), list):
        value = {
            **value,
            "votes": sorted(value["votes"], key=lambda x: x["vote_address"]),
        }
    if (
        c.get("type") != d["kind"]
        or value != d["value"]
        or c.get("Permission_id", 0) != 0
        or c.get("parameter", {}).get("type_url")
        != "type.googleapis.com/protocol." + d["kind"]
    ):
        raise MachineError("Native wallet projection differs from protobuf bytes.")
    allowed = {
        "ref_block_bytes",
        "ref_block_hash",
        "expiration",
        "timestamp",
        "contract",
        "fee_limit",
    }
    if (
        set(json_raw) - allowed
        or json_raw.get("fee_limit", 0) != 0
        or any(
            json_raw.get(k) != d[k]
            for k in ("ref_block_bytes", "ref_block_hash", "timestamp", "expiration")
        )
    ):
        raise MachineError("Native transaction JSON metadata changed.")
    if {"kind": d["kind"], "value": d["value"]} != expected:
        raise MachineError("Native transaction differs from reviewed action.")
    if (
        not now_ms - 60000 <= d["timestamp"] <= now_ms + 5000
        or not now_ms < d["expiration"] <= now_ms + 300000
    ):
        raise MachineError("Native transaction validity window is unsupported.")
    if len(raw) + 128 > BANDWIDTH_BYTES:
        raise MachineError("Native transaction exceeds reviewed bandwidth bound.")
    return d
