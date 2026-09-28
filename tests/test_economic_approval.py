import copy
import unittest

from economic_machine.approval import (ANCHOR_VERSION, SESSION_VERSION,
    build_wallet_signature_request, compile_guard_envelope, confirm_approval,
    prepare_approval_card, verify_approval_session)
from economic_machine.preflight import compile_preflight
from economic_machine.values import MachineError, digest
import test_economic_tx_graph as fixtures


AT = fixtures.AT
HASH = "a" * 64


def session(scope):
    return {"schema_version": SESSION_VERSION, "session_id": "session-approval-1",
        "scope": copy.deepcopy(scope), "auth_context_hash": HASH,
        "issued_at": "2026-09-28T12:00:00Z", "expires_at": "2026-09-28T12:04:00Z",
        "status": "AUTHENTICATED"}


def anchor(network="tron-mainnet"):
    number = 12345678
    block_id = number.to_bytes(8, "big").hex() + "12" * 24
    return {"schema_version": ANCHOR_VERSION, "network": network,
        "block_number": str(number), "block_id": block_id,
        "ref_block_bytes": number.to_bytes(8, "big")[6:8].hex(),
        "ref_block_hash": bytes.fromhex(block_id)[8:16].hex(),
        "observed_at": "2026-09-28T12:00:59Z",
        "valid_until": "2026-09-28T12:03:00Z"}


def ready_context():
    prod, snap, intent, account, quote, bindings, graph = fixtures.supply_graph(
        allowance=1_000_000_000)
    step = next(row for row in graph["steps"] if row["operation"] == "JUSTLEND_SUPPLY")
    sim = fixtures.simulation(step, decoded="0", outputs=[fixtures.asset(
        "jUSDT", fixtures.SHARE, 8, 99_000_000_000)])
    manifest = compile_preflight(graph, fixtures.fresh(account), [sim], at=AT)
    return prod, graph, manifest, step


def confirmation(card, scope):
    return {"approval_hash": card["approval_hash"], "session_id": card["session_id"],
        "auth_context_hash": card["auth_context_hash"], "wallet": scope["wallet"],
        "network": scope["network"], "decision": "APPROVE", "confirmed_at": AT}


def guard_binding(graph):
    binding = {"schema_version": "economic-execution-guard-binding-1",
        "network": "tron-mainnet", "owner_address": graph["scope"]["wallet"],
        "guard_address": "41" + "66" * 20,
        "adapter_address": "41" + "77" * 20,
        "underlying_token": fixtures.TOKEN, "market_address": fixtures.MARKET,
        "share_token": fixtures.MARKET, "guard_code_hash": "1" * 64,
        "adapter_code_hash": "2" * 64, "status": "VERIFIED",
        "evidence_hash": "3" * 64}
    entry = {key: binding[key] for key in ("network", "owner_address", "guard_address",
        "adapter_address", "underlying_token", "market_address", "share_token",
        "guard_code_hash", "adapter_code_hash", "evidence_hash")}
    binding["registry_entry_hash"] = digest({"domain": binding["schema_version"],
        "registry_entry": entry})
    return binding


def guard_policy(guard):
    payload = {"schema_version": "economic-execution-guard-policy-1",
        "network": guard["network"], "entry_hashes": [guard["registry_entry_hash"]],
        "issued_at": "2026-09-28T12:00:00+00:00",
        "expires_at": "2026-09-28T12:04:00+00:00"}
    payload["policy_hash"] = digest({"domain": payload["schema_version"],
        "policy": payload})
    return payload


def guard_simulation(graph, step, guard):
    envelope = compile_guard_envelope(graph, step["step_id"], guard,
        nonce="7", expires_at="2026-09-28T12:03:00Z")
    minimum = step["expected_outputs"][0]["amount_base_units"]
    return {"schema_version": "economic-guard-simulation-1",
        "network": graph["scope"]["network"],
        "guard_address": guard["guard_address"], "envelope_hash": digest(envelope),
        "status": "SUCCESS", "recorded_at": AT, "evidence_hash": "4" * 64,
        "projected_output_base_units": minimum,
        "owner_output_delta_base_units": minimum, "simulated_fee_sun": "500000",
        "guard_underlying_residual": "0", "guard_share_residual": "0",
        "adapter_underlying_residual": "0", "adapter_share_residual": "0",
        "allowances_reset": True}


class ApprovalTests(unittest.TestCase):
    def test_direct_approval_requires_ready_simulation_and_separate_confirmation(self):
        _, graph, manifest, step = ready_context()
        active = session(graph["scope"])
        card = prepare_approval_card(graph, manifest, active, step["step_id"],
            execution_path="DIRECT_WALLET", nonce="0", prepared_at=AT,
            expires_at="2026-09-28T12:03:00Z")
        self.assertEqual(card["status"], "AWAITING_EXPLICIT_APPROVAL")
        self.assertEqual(card["signature_status"], "NOT_REQUESTED")
        self.assertEqual(card["execution_authority"], "NONE")
        self.assertEqual(card["fresh_account_hash"], manifest["fresh_account_hash"])
        self.assertEqual(card["snapshot_hash"], graph["snapshot_hash"])
        self.assertEqual(card["fee_limit_sun"], step["action"]["fee_limit_sun"])
        approved = confirm_approval(card, active, confirmation(card, graph["scope"]))
        self.assertEqual(approved["status"], "APPROVED_UNSIGNED")
        self.assertEqual(approved["approval_effect"], "WALLET_SIGNATURE_REQUEST_ALLOWED")
        request = build_wallet_signature_request(approved, active, anchor(), issued_at=AT)
        self.assertEqual(request["status"], "AWAITING_WALLET_SIGNATURE")
        self.assertEqual(request["data_hex"][:8], "a0712d68")
        self.assertEqual(request["execution_authority"], "NONE")

    def test_session_wallet_network_or_auth_change_invalidates_approval(self):
        _, graph, manifest, step = ready_context()
        active = session(graph["scope"])
        card = prepare_approval_card(graph, manifest, active, step["step_id"],
            execution_path="DIRECT_WALLET", nonce="0", prepared_at=AT,
            expires_at="2026-09-28T12:03:00Z")
        for field, value in (("session_id", "different-session"),
                             ("auth_context_hash", "b" * 64)):
            changed = copy.deepcopy(active)
            changed[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(MachineError, "context"):
                confirm_approval(card, changed, confirmation(card, graph["scope"]))
        approved = confirm_approval(card, active, confirmation(card, graph["scope"]))
        changed = copy.deepcopy(active)
        changed["scope"]["wallet"] = "41" + "99" * 20
        self.assertFalse(verify_approval_session(approved, changed, at=AT))
        with self.assertRaisesRegex(MachineError, "context"):
            build_wallet_signature_request(approved, changed, anchor(), issued_at=AT)

    def test_unready_or_tampered_preflight_cannot_create_card(self):
        _, graph, manifest, step = ready_context()
        active = session(graph["scope"])
        bad = copy.deepcopy(manifest)
        bad["step_reports"][1]["status"] = "BLOCKED"
        with self.assertRaisesRegex(MachineError, "commitment"):
            prepare_approval_card(graph, bad, active, step["step_id"],
                execution_path="DIRECT_WALLET", nonce="0", prepared_at=AT,
                expires_at="2026-09-28T12:03:00Z")

    def test_guard_path_binds_fixed_code_market_tokens_limits_and_nonce(self):
        _, graph, manifest, step = ready_context()
        active = session(graph["scope"])
        guard = guard_binding(graph)
        simulation = guard_simulation(graph, step, guard)
        card = prepare_approval_card(graph, manifest, active, step["step_id"],
            execution_path="EXECUTION_GUARD", nonce="7", prepared_at=AT,
            expires_at="2026-09-28T12:03:00Z", guard_binding=guard,
            guard_simulation=simulation, guard_policy=guard_policy(guard))
        envelope = card["envelope"]
        self.assertEqual(envelope["target_address"], guard["guard_address"])
        self.assertEqual(envelope["function_selector"],
            "executeSupply(bytes32,bytes32,bytes32,uint256,uint256,uint256,uint64,uint64)")
        self.assertEqual(len(envelope["parameter_hex"]), 8 * 64)
        self.assertEqual(card["guard_simulation_hash"], digest(simulation))
        forged = copy.deepcopy(guard)
        forged["market_address"] = "41" + "88" * 20
        with self.assertRaisesRegex(MachineError, "registry entry"):
            prepare_approval_card(graph, manifest, active, step["step_id"],
                execution_path="EXECUTION_GUARD", nonce="7", prepared_at=AT,
                expires_at="2026-09-28T12:03:00Z", guard_binding=forged,
                guard_simulation=simulation, guard_policy=guard_policy(guard))

    def test_guard_path_rejects_missing_or_false_postcondition_simulation(self):
        _, graph, manifest, step = ready_context()
        active = session(graph["scope"])
        guard = guard_binding(graph)
        with self.assertRaisesRegex(MachineError, "binding, trust policy and simulation"):
            prepare_approval_card(graph, manifest, active, step["step_id"],
                execution_path="EXECUTION_GUARD", nonce="7", prepared_at=AT,
                expires_at="2026-09-28T12:03:00Z", guard_binding=guard)
        base = guard_simulation(graph, step, guard)
        for key, value in (("allowances_reset", False),
                           ("guard_underlying_residual", "1"),
                           ("envelope_hash", "f" * 64),
                           ("owner_output_delta_base_units", "0"),
                           ("simulated_fee_sun", "999999999")):
            bad = copy.deepcopy(base)
            bad[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(
                    MachineError, "postconditions"):
                prepare_approval_card(graph, manifest, active, step["step_id"],
                    execution_path="EXECUTION_GUARD", nonce="7", prepared_at=AT,
                    expires_at="2026-09-28T12:03:00Z", guard_binding=guard,
                    guard_simulation=bad, guard_policy=guard_policy(guard))

    def test_guard_binding_must_be_in_active_server_trust_policy(self):
        _, graph, manifest, step = ready_context()
        active = session(graph["scope"])
        guard = guard_binding(graph)
        simulation = guard_simulation(graph, step, guard)
        policy = guard_policy(guard)
        policy["entry_hashes"] = ["f" * 64]
        unsigned = {key: value for key, value in policy.items() if key != "policy_hash"}
        policy["policy_hash"] = digest({"domain": policy["schema_version"],
            "policy": unsigned})
        with self.assertRaisesRegex(MachineError, "absent from"):
            prepare_approval_card(graph, manifest, active, step["step_id"],
                execution_path="EXECUTION_GUARD", nonce="7", prepared_at=AT,
                expires_at="2026-09-28T12:03:00Z", guard_binding=guard,
                guard_simulation=simulation, guard_policy=policy)

    def test_tapos_anchor_is_derived_from_exact_block(self):
        _, graph, manifest, step = ready_context()
        active = session(graph["scope"])
        card = prepare_approval_card(graph, manifest, active, step["step_id"],
            execution_path="DIRECT_WALLET", nonce="0", prepared_at=AT,
            expires_at="2026-09-28T12:03:00Z")
        approved = confirm_approval(card, active, confirmation(card, graph["scope"]))
        bad = anchor()
        bad["ref_block_hash"] = "00" * 8
        with self.assertRaisesRegex(MachineError, "TAPOS"):
            build_wallet_signature_request(approved, active, bad, issued_at=AT)


if __name__ == "__main__":
    unittest.main()
