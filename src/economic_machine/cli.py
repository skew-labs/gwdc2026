"""Economic Machine CLI with bounded read-only node observations; no wallet path."""

import argparse
import json
from pathlib import Path

from .authenticated_basket import commit_authenticated_basket, verify_authenticated_basket
from .basket import commit_basket, verify_basket
from .chain_binding import (prepare_chain_binding, verify_chain_binding,
                            prepare_authenticated_chain_binding,
                            prepare_attestation_message,
                            verify_authenticated_chain_binding)
from .compiler import compile_program
from .grid_search import search_grid, verify_grid
from .kernel import EconomicKernel
from .portfolio import select_portfolio
from .product_adapter import assemble_portfolio_inputs, verify_portfolio_inputs
from .signed_evidence import assemble_signed_portfolio_inputs, verify_signed_portfolio_inputs
from .runtime import MachineRuntime
from .spec import SPEC, SPEC_HASH, conformance_vectors
from .tron_consumption_read import assess_basket_consumption, read_tron_transaction
from .tron_registry_read import assess_registry_observation, read_registry_observation
from .tron_yield import plan_tron_yield, verify_tron_yield_plan
from .vault_batch import prepare_vault_batch, verify_vault_batch
from .vault_registration import (assess_batch_registry, observe_batch_registry,
                                 prepare_batch_attestations)


def _read(path: Path) -> dict:
    result = json.loads(path.read_text(encoding="utf-8"), parse_float=lambda _: (_ for _ in ()).throw(
        ValueError("JSON floats are forbidden")))
    if not isinstance(result, dict):
        raise ValueError("JSON object required")
    return result


def _read_leg_specs(path: Path) -> list[dict]:
    document = _read(path)
    if (set(document) != {"schema_version", "legs"}
            or document["schema_version"] != "economic-vault-leg-specs-1"
            or not isinstance(document["legs"], list)):
        raise ValueError("vault leg specs document is invalid")
    return document["legs"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline deterministic Economic Machine")
    parser.add_argument("command", choices=("compile", "run", "init-state", "register",
                                            "evaluate", "ingest", "tick", "verify", "status",
                                            "begin-execution",
                                            "pause", "resume", "intent-status",
                                            "assess-inference", "verify-inference", "escalations",
                                            "spec", "spec-vectors", "portfolio-select",
                                            "tron-yield-plan", "tron-yield-verify",
                                            "portfolio-assemble", "portfolio-assemble-verify",
                                            "portfolio-assemble-signed", "portfolio-assemble-signed-verify",
                                            "basket-commit-signed", "basket-verify-signed",
                                            "vault-batch-plan-signed", "vault-batch-verify-signed",
                                            "vault-batch-attest-signed", "vault-batch-observe-signed",
                                            "vault-batch-assess-signed",
                                            "chain-bind-signed", "chain-bind-verify-signed",
                                            "chain-attest-message-signed",
                                            "tron-observe-signed", "tron-assess-signed",
                                            "tron-basket-observe-signed", "tron-basket-assess-signed",
                                            "basket-commit", "basket-verify",
                                            "grid-search", "grid-verify", "chain-bind",
                                            "chain-bind-verify", "tron-observe",
                                            "tron-assess", "tron-tx-observe",
                                            "tron-basket-observe", "tron-basket-assess"))
    parser.add_argument("--db", type=Path, default=Path("data/economic-machine-demo.sqlite3"))
    parser.add_argument("--program", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--delta", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--trust-roots", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--commitment", type=Path)
    parser.add_argument("--grid-verdict", type=Path)
    parser.add_argument("--chain-context", type=Path)
    parser.add_argument("--chain-binding", type=Path)
    parser.add_argument("--vault-context", type=Path)
    parser.add_argument("--leg-specs", type=Path)
    parser.add_argument("--vault-plan", type=Path)
    parser.add_argument("--batch-deadline", type=int)
    parser.add_argument("--reader-config", type=Path)
    parser.add_argument("--observation", type=Path)
    parser.add_argument("--txid")
    parser.add_argument("--valid-until")
    parser.add_argument("--attestor-epoch", type=int)
    parser.add_argument("--scope", type=Path)
    parser.add_argument("--program-id")
    parser.add_argument("--event-id")
    parser.add_argument("--receipt-hash")
    parser.add_argument("--assessment-hash")
    parser.add_argument("--reason")
    parser.add_argument("--at")
    args = parser.parse_args()
    if args.command in {"tron-yield-plan", "tron-yield-verify"}:
        if not args.request:
            parser.error("--request required")
        request = _read(args.request)
        if args.command == "tron-yield-plan":
            result = plan_tron_yield(request)
        else:
            if not args.observation:
                parser.error("--observation required")
            result = {"verified_replay": verify_tron_yield_plan(
                _read(args.observation), request)}
    elif args.command == "portfolio-select":
        if not args.request:
            parser.error("--request required")
        result = select_portfolio(_read(args.request))
    elif args.command in {"portfolio-assemble", "portfolio-assemble-verify"}:
        if not args.request or not args.bundle:
            parser.error("--request and --bundle required")
        template, bundle = _read(args.request), _read(args.bundle)
        if args.command == "portfolio-assemble":
            result = assemble_portfolio_inputs(template, bundle)
        else:
            if not args.observation:
                parser.error("--observation required")
            result = {"verified_replay": verify_portfolio_inputs(
                _read(args.observation), template, bundle)}
    elif args.command in {"portfolio-assemble-signed", "portfolio-assemble-signed-verify"}:
        if not args.request or not args.bundle or not args.trust_roots:
            parser.error("--request, --bundle and --trust-roots required")
        template, signed = _read(args.request), _read(args.bundle)
        roots = _read(args.trust_roots)
        if args.command == "portfolio-assemble-signed":
            result = assemble_signed_portfolio_inputs(template, signed, roots)
        else:
            if not args.observation:
                parser.error("--observation required")
            result = {"verified_replay": verify_signed_portfolio_inputs(
                _read(args.observation), template, signed, roots)}
    elif args.command in {"basket-commit-signed", "basket-verify-signed",
                          "vault-batch-plan-signed", "vault-batch-verify-signed",
                          "vault-batch-attest-signed", "vault-batch-observe-signed",
                          "vault-batch-assess-signed",
                          "chain-bind-signed", "chain-bind-verify-signed",
                          "chain-attest-message-signed",
                          "tron-observe-signed", "tron-assess-signed",
                          "tron-basket-observe-signed", "tron-basket-assess-signed"}:
        if not all((args.request, args.bundle, args.trust_roots, args.state, args.policy)):
            parser.error("--request, --bundle, --trust-roots, --state and --policy required")
        template, signed, roots = _read(args.request), _read(args.bundle), _read(args.trust_roots)
        state, policy = _read(args.state), _read(args.policy)
        if args.command == "basket-commit-signed":
            if not args.valid_until:
                parser.error("--valid-until required")
            result = commit_authenticated_basket(template, signed, roots, state, policy,
                                                 valid_until=args.valid_until)
        else:
            if not args.commitment:
                parser.error("--commitment required")
            commitment = _read(args.commitment)
            replay = verify_authenticated_basket(commitment, template, signed, roots,
                                                 state, policy)
            if args.command == "basket-verify-signed":
                result = {"verified_replay": replay}
            elif args.command in {"vault-batch-plan-signed", "vault-batch-verify-signed",
                                  "vault-batch-attest-signed", "vault-batch-observe-signed",
                                  "vault-batch-assess-signed"}:
                if not all((args.vault_context, args.leg_specs, args.at,
                            args.batch_deadline is not None)):
                    parser.error("--vault-context, --leg-specs, --at and --batch-deadline required")
                context = _read(args.vault_context)
                specs = _read_leg_specs(args.leg_specs)
                if args.command == "vault-batch-plan-signed":
                    result = prepare_vault_batch(
                        commitment, template, signed, roots, state, policy,
                        context, specs, prepared_at=args.at,
                        batch_deadline_epoch_seconds=args.batch_deadline)
                else:
                    if not args.vault_plan:
                        parser.error("--vault-plan required")
                    plan = _read(args.vault_plan)
                    if args.command == "vault-batch-verify-signed":
                        result = {"verified_replay": verify_vault_batch(
                            plan, commitment, template, signed, roots, state, policy,
                            context, specs, prepared_at=args.at,
                            batch_deadline_epoch_seconds=args.batch_deadline)}
                    elif args.command == "vault-batch-attest-signed":
                        if args.attestor_epoch is None:
                            parser.error("--attestor-epoch required")
                        result = prepare_batch_attestations(
                            plan, commitment, template, signed, roots, state,
                            policy, context, specs, prepared_at=args.at,
                            batch_deadline_epoch_seconds=args.batch_deadline,
                            attestor_epoch=args.attestor_epoch)
                    elif args.command == "vault-batch-observe-signed":
                        if not args.reader_config:
                            parser.error("--reader-config required")
                        observations = observe_batch_registry(
                            plan, commitment, template, signed, roots, state,
                            policy, context, specs, _read(args.reader_config),
                            prepared_at=args.at,
                            batch_deadline_epoch_seconds=args.batch_deadline)
                        assessment = assess_batch_registry(
                            observations, plan, commitment, template, signed,
                            roots, state, policy, context, specs, prepared_at=args.at,
                            batch_deadline_epoch_seconds=args.batch_deadline)
                        result = {"observations": observations, "assessment": assessment}
                    else:
                        if not args.observation:
                            parser.error("--observation required")
                        observed = _read(args.observation)
                        result = assess_batch_registry(
                            observed.get("observations", observed), plan,
                            commitment, template, signed, roots, state, policy,
                            context, specs, prepared_at=args.at,
                            batch_deadline_epoch_seconds=args.batch_deadline)
            else:
                if not replay:
                    parser.error("authenticated basket commitment does not replay")
                if not args.chain_context:
                    parser.error("--chain-context required")
                context = _read(args.chain_context)
                if args.command == "chain-bind-signed":
                    result = prepare_authenticated_chain_binding(
                        commitment, template, signed, roots, state, policy, context)
                else:
                    if not args.chain_binding:
                        parser.error("--chain-binding required")
                    binding = _read(args.chain_binding)
                    binding_replay = verify_authenticated_chain_binding(
                        binding, commitment, template, signed, roots, state, policy, context)
                    if args.command == "chain-bind-verify-signed":
                        result = {"verified_replay": binding_replay}
                    else:
                        if not binding_replay:
                            parser.error("authenticated chain binding does not replay")
                        if args.command == "chain-attest-message-signed":
                            if args.attestor_epoch is None:
                                parser.error("--attestor-epoch required")
                            result = prepare_attestation_message(
                                binding, commitment, template, signed, roots, state, policy,
                                context, attestor_epoch=args.attestor_epoch)
                        elif args.command == "tron-observe-signed":
                            if not args.reader_config:
                                parser.error("--reader-config required")
                            observation = read_registry_observation(binding, _read(args.reader_config))
                            assessment = assess_registry_observation(binding, observation)
                            result = {"verified_replay": True, "observation": observation,
                                      "assessment": assessment}
                        elif args.command == "tron-basket-observe-signed":
                            if not args.reader_config or not args.txid:
                                parser.error("--reader-config and --txid required")
                            observation = read_tron_transaction(args.txid, _read(args.reader_config))
                            assessment = assess_basket_consumption(binding, observation)
                            result = {"verified_replay": True, "observation": observation,
                                      "assessment": assessment}
                        else:
                            if not args.observation:
                                parser.error("--observation required")
                            observed = _read(args.observation)
                            assessment = (assess_registry_observation
                                          if args.command == "tron-assess-signed"
                                          else assess_basket_consumption)(
                                              binding, observed.get("observation", observed))
                            result = {"verified_replay": True, "assessment": assessment}
    elif args.command == "tron-tx-observe":
        if not args.txid or not args.reader_config:
            parser.error("--txid and --reader-config required")
        result = read_tron_transaction(args.txid, _read(args.reader_config))
    elif args.command in {"grid-search", "grid-verify"}:
        if not args.request:
            parser.error("--request required")
        if args.command == "grid-search":
            result = search_grid(_read(args.request))
        else:
            if not args.grid_verdict:
                parser.error("--grid-verdict required")
            result = {"verified_replay": verify_grid(_read(args.grid_verdict),
                                                      _read(args.request))}
    elif args.command in {"chain-bind", "chain-bind-verify", "tron-observe",
                          "tron-assess", "tron-basket-observe", "tron-basket-assess"}:
        if not all((args.request, args.state, args.policy, args.commitment,
                    args.chain_context)):
            parser.error("--request, --state, --policy, --commitment and --chain-context required")
        request, state, policy = _read(args.request), _read(args.state), _read(args.policy)
        commitment, context = _read(args.commitment), _read(args.chain_context)
        if args.command == "chain-bind":
            result = prepare_chain_binding(commitment, request, state, policy, context)
        else:
            if not args.chain_binding:
                parser.error("--chain-binding required")
            binding = _read(args.chain_binding)
            replay = verify_chain_binding(binding, commitment, request, state, policy, context)
            if args.command == "chain-bind-verify":
                result = {"verified_replay": replay}
            else:
                if not replay:
                    parser.error("basket binding does not replay")
                if args.command == "tron-observe":
                    if not args.reader_config:
                        parser.error("--reader-config required")
                    observation = read_registry_observation(binding, _read(args.reader_config))
                    result = {"verified_replay": True, "observation": observation,
                              "assessment": assess_registry_observation(binding, observation)}
                elif args.command == "tron-basket-observe":
                    if not args.reader_config or not args.txid:
                        parser.error("--reader-config and --txid required")
                    observation = read_tron_transaction(args.txid, _read(args.reader_config))
                    result = {"verified_replay": True, "observation": observation,
                              "assessment": assess_basket_consumption(binding, observation)}
                else:
                    if not args.observation:
                        parser.error("--observation required")
                    observed = _read(args.observation)
                    result = {"verified_replay": True, "assessment":
                              (assess_registry_observation if args.command == "tron-assess"
                               else assess_basket_consumption)(
                                   binding, observed.get("observation", observed))}
    elif args.command in {"basket-commit", "basket-verify"}:
        if not args.request or not args.state or not args.policy:
            parser.error("--request, --state and --policy required")
        request, state, policy = _read(args.request), _read(args.state), _read(args.policy)
        if args.command == "basket-commit":
            if not args.valid_until:
                parser.error("--valid-until required")
            result = commit_basket(request, select_portfolio(request), state, policy,
                                   valid_until=args.valid_until)
        else:
            if not args.commitment:
                parser.error("--commitment required")
            result = {"verified_replay": verify_basket(_read(args.commitment),
                                                        request, state, policy)}
    elif args.command == "spec":
        result = {"spec_hash": SPEC_HASH, "spec": SPEC}
    elif args.command == "spec-vectors":
        result = {"schema_version": "economic-isa-conformance-1",
                  "spec_hash": SPEC_HASH, "vectors": conformance_vectors()}
    elif args.command == "compile":
        if not args.program:
            parser.error("--program required")
        result = compile_program(_read(args.program))
    elif args.command == "run":
        if not args.program or not args.state or not args.at:
            parser.error("--program, --state, --at required")
        result = EconomicKernel().evaluate(compile_program(_read(args.program)),
                                           _read(args.state), at=args.at)
    else:
        runtime = MachineRuntime(args.db)
        if args.command == "init-state":
            if not args.state:
                parser.error("--state required")
            result = runtime.install_state(_read(args.state))
        elif args.command == "register":
            if not args.program:
                parser.error("--program required")
            result = runtime.register_program(_read(args.program))
        elif args.command == "evaluate":
            if not args.program_id or not args.at:
                parser.error("--program-id and --at required")
            result = runtime.evaluate(args.program_id, at=args.at)
        elif args.command == "ingest":
            if not args.delta or not args.event_id:
                parser.error("--delta and --event-id required")
            result = runtime.ingest(_read(args.delta), event_id=args.event_id)
        elif args.command == "tick":
            if not args.at:
                parser.error("--at required")
            result = runtime.tick(at=args.at)
        elif args.command in {"pause", "resume"}:
            if not args.program_id or not args.reason:
                parser.error("--program-id and --reason required")
            operation = runtime.pause_program if args.command == "pause" else runtime.resume_program
            result = operation(args.program_id, reason=args.reason)
        elif args.command == "intent-status":
            if not args.receipt_hash or not args.at:
                parser.error("--receipt-hash and --at required")
            result = runtime.intent_status(args.receipt_hash, at=args.at)
        elif args.command == "begin-execution":
            if not args.receipt_hash or not args.at:
                parser.error("--receipt-hash and --at required")
            result = runtime.begin_execution(args.receipt_hash, at=args.at)
        elif args.command == "assess-inference":
            if not args.candidate or not args.scope or not args.at:
                parser.error("--candidate, --scope and --at required")
            result = runtime.record_inference(_read(args.candidate), _read(args.scope), at=args.at)
        elif args.command == "verify-inference":
            if not args.assessment_hash:
                parser.error("--assessment-hash required")
            result = {"verified_replay": runtime.verify_inference(args.assessment_hash)}
        elif args.command == "escalations":
            result = {"pending_escalations": runtime.pending_escalations(),
                      "llm_calls": 0}
        elif args.command == "verify":
            if not args.receipt_hash:
                parser.error("--receipt-hash required")
            result = {"verified_replay": runtime.verify_receipt(args.receipt_hash)}
        else:
            result = runtime.status()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True,
                     indent=2 if args.command in {"spec", "spec-vectors"} else None))


if __name__ == "__main__":
    main()
