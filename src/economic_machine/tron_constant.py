"""TRON protobuf result decoding and narrowly scoped JustLend view ABI handling.

Protocol Result.ret defaults to SUCESS=0. Wallet.callConstantContract explicitly
sets FAILED on runtime errors/reverts; JsonFormat omits default scalar fields.
JustLend's legacy delegateToViewAndReturn advances the return pointer by 64
bytes without reducing the length. Only the known entries/getters below may
return the resulting two zero words. Other ABIs retain exact-length decoding.
References and content hashes are recorded in the RPC fix implementation report.
"""

import re

from .values import MachineError


SELECTORS = {
    "getCash()": "3b1d21a2", "exchangeRateStored()": "182df0f5",
    "balanceOf(address)": "70a08231", "borrowBalanceStored(address)": "95dd9193",
    "exchangeRate()": "3ba0b9a9", "totalUnderlying()": "c70920bc",
    "vat()": "36569e77", "proxies(address)": "c4552791", "owner()": "8da5cb5b",
    "owns(uint256)": "8161b120", "urns(uint256)": "2726b073", "ilks(uint256)": "2c2cb9fd",
    "urns(bytes32,address)": "2424be5c", "ilks(bytes32)": "d9638d36",
    "aggregate3((address,bool,bytes)[])": "82ad56cb",
    "getBlockNumber()": "42cbb15c", "getCurrentBlockTimestamp()": "0f28c97d",
}
MULTICALL = "41e777b7157d44d95ef48a8483c743a5d3f8a72180"
LEGACY_DELEGATORS = frozenset({
    "41ea09611b57e89d67fbb33a516eb90508ca95a3e5",  # jUSDT
    "4165c9fede72ba73cd1b0dca2a974c070153dc6fcb",  # jUSDD
    "41e7f8a90ede3d84c7c0166bd84a4635e4675accfc",  # jUSDDOLD
})
LEGACY_GETTERS = frozenset({"getCash()", "exchangeRateStored()", "balanceOf(address)", "borrowBalanceStored(address)"})


def _enum(value, names, numbers):
    return (type(value) is str and value in names) or (type(value) is int and value in numbers)


def constant_result(request, contract, owner, selector, parameter=""):
    if selector not in SELECTORS or (selector.startswith("aggregate3") and contract != MULTICALL):
        raise MachineError("unsupported read-only constant selector/contract")
    payload = {"owner_address": owner, "contract_address": contract,
               "function_selector": selector, "parameter": parameter, "visible": False}
    response = request("/walletsolidity/triggerconstantcontract", payload)
    if not isinstance(response, dict) or "Error" in response or "error" in response:
        raise MachineError("TRON constant RPC error")
    result = response.get("result")
    if (not isinstance(result, dict) or result.get("result") is not True
        or not _enum(result.get("code", 0), {"SUCCESS"}, {0}) or result.get("message") not in (None, "")):
        raise MachineError("TRON constant API/runtime failure")
    transaction = response.get("transaction")
    ret = transaction.get("ret") if isinstance(transaction, dict) else None
    if not isinstance(ret, list) or len(ret) != 1 or not isinstance(ret[0], dict):
        raise MachineError("TRON constant transaction result missing")
    # Absence of the ret scalar in an existing Result message is proto3 SUCESS.
    # An absent Result message is NOT success. Explicit failure always wins.
    status = ret[0]
    if not _enum(status.get("ret", 0), {"SUCESS", "SUCCESS"}, {0}):
        raise MachineError("TRON constant VM failed")
    if "contractRet" in status and not _enum(status["contractRet"], {"SUCCESS"}, {1}):
        raise MachineError("TRON explicit contract result is not success")
    raw_data = transaction.get("raw_data")
    contracts = raw_data.get("contract") if isinstance(raw_data, dict) else None
    if not isinstance(contracts, list) or len(contracts) != 1:
        raise MachineError("constant response request binding missing")
    item = contracts[0]
    if not isinstance(item, dict) or item.get("type") != "TriggerSmartContract":
        raise MachineError("constant response contract type mismatch")
    param = item.get("parameter")
    value = param.get("value") if isinstance(param, dict) else None
    if (not isinstance(value, dict) or value.get("owner_address") != owner or value.get("contract_address") != contract
        or value.get("data") != SELECTORS[selector] + parameter
        or any(type(value.get(key, 0)) is not int or value.get(key, 0) != 0
               for key in ("call_value", "call_token_value", "token_id"))):
        raise MachineError("constant response address/calldata/value mismatch")
    results = response.get("constant_result")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], str):
        raise MachineError("TRON constant result missing")
    raw = results[0]
    if not re.fullmatch(r"[0-9a-fA-F]+", raw):
        raise MachineError("TRON constant result is not hex")
    return raw


def decode_words(contract, selector, raw, words=1):
    if type(words) is not int or not 1 <= words <= 5:
        raise MachineError("unsupported ABI result width")
    expected = words * 64
    if not isinstance(raw, str) or re.fullmatch(r"[0-9a-fA-F]+", raw) is None:
        raise MachineError("TRON result is not hex")
    if len(raw) != expected:
        if not (contract in LEGACY_DELEGATORS and selector in LEGACY_GETTERS and words == 1
                and len(raw) == expected + 128 and raw[expected:] == "0" * 128):
            raise MachineError("TRON ABI width/padding mismatch")
        raw = raw[:expected]
    return [int(raw[index:index+64], 16) for index in range(0, expected, 64)]


def read_constant(request, contract, owner, selector, parameter="", words=1):
    return decode_words(contract, selector, constant_result(request, contract, owner, selector, parameter), words)
