"""FDC V3 synthetic-only training with supervised counterfactual deltas."""

import argparse
import hashlib
import json
import random
import time
from dataclasses import asdict
from pathlib import Path

import torch
from torch import nn
from torch.utils.data import DataLoader

from .baseline import FlatMLP
from .encoder import MODEL_ID, REVISION, encode_episodes
from .features_v3 import (DEPENDENCY_INDEX, FEATURE_VERSION, INPUT_KEYS,
                          collate_pairs, dimensions, load_episodes, vectorize)
from .model import Config, FinancialDecisionCore
from .validate_v3 import validate


def paired(rows: list[dict], cfg: Config, dims: tuple[int, int, int],
           text_lookup: dict | None) -> list[tuple[dict, dict]]:
    by_family = {}
    for row in rows:
        by_family.setdefault(row["scenario_family"], []).append(row)
    result = []
    for family in sorted(by_family):
        items = by_family[family]
        if len(items) != 2:
            raise ValueError("V3 trainer requires base and counterfactual per family")
        base = next(item for item in items if item["parent_episode_id"] is None)
        other = next(item for item in items if item["parent_episode_id"] is not None)
        result.append((vectorize(base, cfg, dims, text_lookup),
                       vectorize(other, cfg, dims, text_lookup)))
    return result


def _masked_bce(logits: torch.Tensor, target: torch.Tensor,
                mask: torch.Tensor) -> torch.Tensor:
    safe = logits.masked_fill(torch.isneginf(logits), 0)
    loss = nn.functional.binary_cross_entropy_with_logits(safe, target, reduction="none")
    selected = mask.expand_as(loss)
    return (loss * selected).sum() / selected.sum().clamp_min(1)


def _eligible_mask(batch: dict) -> torch.Tensor:
    asset = batch["asset_features"]
    goal = batch["goal_features"]
    return (asset[..., 3] * asset[..., 4] * (1 - asset[..., 7])
            * (1 - asset[..., 8]) * (1 - asset[..., 9])
            * (asset[..., 5] <= goal[:, None, 3]).float()
            * (asset[..., 6] <= goal[:, None, 5]).float()
            * (asset[..., 12] == 0).float() * (asset[..., 13] >= 0).float())


def _pair_delta_loss(predicted: torch.Tensor, truth: torch.Tensor,
                     valid: torch.Tensor | None = None) -> torch.Tensor:
    difference = ((predicted[1::2] - predicted[0::2])
                  - (truth[1::2] - truth[0::2])).abs()
    if valid is None:
        return difference.mean()
    selected = valid.expand_as(difference)
    return (difference * selected).sum() / selected.sum().clamp_min(1)


def objective(outputs: dict, batch: dict,
              pair_loss_weight: float = 0.5,
              pair_objective: str = "full") -> tuple[torch.Tensor, dict]:
    if pair_objective not in {"full", "allocation_mode"}:
        raise ValueError("unsupported pair objective")
    target_mode = batch["target_mode"]
    mask = batch["target_plan_mask"]
    mode = nn.functional.cross_entropy(outputs["mode_logits"], target_mode)
    count = nn.functional.binary_cross_entropy_with_logits(
        outputs["plan_scores"], mask.float())
    log_alloc = outputs["allocation_logits"].log_softmax(-1)
    log_alloc = log_alloc.masked_fill(torch.isneginf(log_alloc), 0)
    allocation = -(batch["target_allocations"] * log_alloc).sum(-1)
    allocation = (allocation * mask).sum() / mask.sum().clamp_min(1)
    evidence_valid = torch.cat((batch["evidence_valid"],
                                torch.ones_like(batch["evidence_valid"][:, :1])), dim=1)
    evidence_mask = mask[..., None] & evidence_valid[:, None, :]
    evidence = _masked_bce(outputs["evidence_logits"], batch["target_evidence"],
                           evidence_mask)
    dependency_mask = mask[..., None] & torch.ones_like(batch["target_dependency"],
                                                        dtype=torch.bool)
    dependency = _masked_bce(outputs["dependency_logits"],
                             batch["target_dependency"], dependency_mask)
    question_weights = torch.ones(batch["target_question"].shape[-1],
                                  device=target_mode.device)
    question_weights[:6] = 5
    question = nn.functional.binary_cross_entropy_with_logits(
        outputs["question_field_logits"], batch["target_question"],
        pos_weight=question_weights)

    shares = outputs["allocation_logits"].softmax(-1)
    supply = shares[..., :-1]
    hold = shares[..., -1]
    goal = batch["goal_features"]
    reserve_error = torch.relu(goal[:, None, 1] - hold)
    total_error = torch.relu(supply.sum(-1) - goal[:, None, 7])
    cash_error = torch.relu(supply - batch["asset_features"][:, None, :, 1]).sum(-1)
    group_relations = batch["relations"][..., 2]
    group_exposure = torch.einsum("bij,bkj->bki", group_relations, supply)
    group_error = torch.relu(group_exposure - goal[:, None, None, 8]).amax(-1)
    eligible = _eligible_mask(batch)
    ineligible_error = (supply * (1 - eligible[:, None, :])).sum(-1)
    constraint = ((reserve_error + total_error + cash_error + group_error
                   + ineligible_error) * mask).sum() / mask.sum().clamp_min(1)

    pred_delta = shares[1::2] - shares[0::2]
    target_delta = batch["target_allocations"][1::2] - batch["target_allocations"][0::2]
    comparable = mask[0::2] & mask[1::2]
    pair_allocation = (((pred_delta - target_delta).abs().mean(-1) * comparable).sum()
                       / comparable.sum().clamp_min(1))
    pred_mode_delta = (outputs["mode_logits"].softmax(-1)[1::2]
                       - outputs["mode_logits"].softmax(-1)[0::2])
    true_mode_delta = (nn.functional.one_hot(target_mode[1::2], 3)
                       - nn.functional.one_hot(target_mode[0::2], 3)).float()
    pair_mode = (pred_mode_delta - true_mode_delta).abs().mean()
    pair_plan = _pair_delta_loss(outputs["plan_scores"].sigmoid(), mask.float())
    pair_evidence = _pair_delta_loss(
        outputs["evidence_logits"][..., :-1].sigmoid(),
        batch["target_evidence"][..., :-1],
        comparable[..., None] & batch["evidence_valid"][0::2, None, :]
        & batch["evidence_valid"][1::2, None, :])
    pair_dependency = _pair_delta_loss(
        outputs["dependency_logits"][..., :-1].sigmoid(),
        batch["target_dependency"][..., :-1],
        comparable[..., None] & batch["constraint_valid"][0::2, None, :]
        & batch["constraint_valid"][1::2, None, :])
    pair_question = _pair_delta_loss(
        outputs["question_field_logits"].sigmoid(), batch["target_question"])
    pair_terms = pair_allocation + 0.4 * pair_mode
    if pair_objective == "full":
        pair_terms = (pair_terms + 0.1 * pair_plan + 0.2 * pair_evidence
                      + 0.2 * pair_dependency + 0.2 * pair_question)
    total = (mode + 0.3 * count + allocation + 0.2 * evidence
             + 0.2 * dependency + 0.3 * question + 2 * constraint
             + pair_loss_weight * pair_terms)
    return total, {"mode_ce": float(mode.detach()),
                   "allocation_ce": float(allocation.detach()),
                   "question_bce": float(question.detach()),
                   "evidence_bce": float(evidence.detach()),
                   "dependency_bce": float(dependency.detach()),
                   "constraint": float(constraint.detach()),
                   "pair_allocation_l1": float(pair_allocation.detach()),
                   "pair_mode_l1": float(pair_mode.detach()),
                   "pair_plan_l1": float(pair_plan.detach()),
                   "pair_evidence_l1": float(pair_evidence.detach()),
                   "pair_dependency_l1": float(pair_dependency.detach()),
                   "pair_question_l1": float(pair_question.detach())}


def run_epoch(model, loader, optimizer, pair_loss_weight: float = 0.5,
              pair_objective: str = "full") -> dict:
    training = optimizer is not None
    model.train(training)
    total_loss = 0.0
    correct = 0
    question_hits = 0
    question_total = 0
    plan_count_correct = 0
    allocation_error = 0.0
    allocation_count = 0
    evidence_counts = [0, 0, 0]
    dependency_counts = [0, 0, 0]
    raw_constraint_violations = 0
    raw_constraint_plans = 0
    episodes = 0
    elapsed = time.monotonic()
    for batch in loader:
        with torch.set_grad_enabled(training):
            outputs = model(**{name: batch[name] for name in INPUT_KEYS})
            loss, _ = objective(outputs, batch, pair_loss_weight, pair_objective)
            if training:
                optimizer.zero_grad(set_to_none=True)
                loss.backward()
                nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
        size = batch["target_mode"].shape[0]
        total_loss += float(loss.detach()) * size
        correct += int((outputs["mode_logits"].argmax(-1) == batch["target_mode"]).sum())
        predicted_plan_mask = outputs["plan_scores"].sigmoid() >= 0.5
        plan_count_correct += int((predicted_plan_mask.sum(-1)
                                   == batch["target_plan_mask"].sum(-1)).sum())
        shares = outputs["allocation_logits"].softmax(-1)
        selected = batch["target_plan_mask"]
        allocation_error += float((shares - batch["target_allocations"]).abs().mean(-1)[selected].sum().detach())
        allocation_count += int(selected.sum())
        for logits, target, valid, counters in (
            (outputs["evidence_logits"][..., :-1], batch["target_evidence"][..., :-1],
             batch["evidence_valid"], evidence_counts),
            (outputs["dependency_logits"][..., :-1], batch["target_dependency"][..., :-1],
             batch["constraint_valid"], dependency_counts),
        ):
            active = selected[..., None] & valid[:, None, :]
            predicted = (logits.sigmoid() >= 0.5) & active
            truth = (target > 0) & active
            counters[0] += int((predicted & truth).sum())
            counters[1] += int((predicted & ~truth).sum())
            counters[2] += int((~predicted & truth).sum())
        hold = shares[..., -1]
        supply = shares[..., :-1]
        goal = batch["goal_features"]
        grouped = torch.einsum("bij,bkj->bki", batch["relations"][..., 2], supply)
        violated = ((hold + 1e-5 < goal[:, None, 1])
                    | (supply.sum(-1) > goal[:, None, 7] + 1e-5)
                    | (supply > batch["asset_features"][:, None, :, 1] + 1e-5).any(-1)
                    | (grouped > goal[:, None, None, 8] + 1e-5).any(-1)
                    | ((supply * (1 - _eligible_mask(batch)[:, None, :])).sum(-1) > 1e-5))
        raw_constraint_violations += int((violated & selected).sum())
        raw_constraint_plans += int(selected.sum())
        asks = batch["target_mode"] == 1
        if asks.any():
            prediction = outputs["question_field_logits"][asks, :6].sigmoid() >= 0.5
            truth = batch["target_question"][asks, :6] > 0
            question_hits += int((prediction == truth).all(-1).sum())
            question_total += int(asks.sum())
        episodes += size
    def f1(counts):
        true_positive, false_positive, false_negative = counts
        denominator = 2 * true_positive + false_positive + false_negative
        return 2 * true_positive / denominator if denominator else None

    return {"loss": total_loss / episodes, "mode_accuracy": correct / episodes,
            "plan_count_exact": plan_count_correct / episodes,
            "allocation_mae": allocation_error / allocation_count if allocation_count else None,
            "evidence_f1": f1(evidence_counts),
            "dependency_f1": f1(dependency_counts),
            "raw_constraint_violation_rate": (raw_constraint_violations / raw_constraint_plans
                                              if raw_constraint_plans else None),
            "ask_question_exact": question_hits / question_total if question_total else None,
            "episodes": episodes, "elapsed_seconds": round(time.monotonic() - elapsed, 3)}


def pair_report(model, loader, rows: list[dict]) -> dict:
    model.eval()
    effects = {"changed": {"count": 0, "delta_l1_sum": 0.0,
                            "all_pairs": 0, "mode_delta_l1_sum": 0.0},
               "unchanged": {"count": 0, "delta_l1_sum": 0.0,
                              "all_pairs": 0, "mode_delta_l1_sum": 0.0}}
    index = 0
    with torch.no_grad():
        for batch in loader:
            outputs = model(**{name: batch[name] for name in INPUT_KEYS})
            shares = outputs["allocation_logits"].softmax(-1)
            delta = shares[1::2] - shares[0::2]
            truth = batch["target_allocations"][1::2] - batch["target_allocations"][0::2]
            comparable = batch["target_plan_mask"][0::2] & batch["target_plan_mask"][1::2]
            mode_probs = outputs["mode_logits"].softmax(-1)
            mode_delta = mode_probs[1::2] - mode_probs[0::2]
            true_mode_delta = (nn.functional.one_hot(batch["target_mode"][1::2], 3)
                               - nn.functional.one_hot(batch["target_mode"][0::2], 3)).float()
            for pair_index in range(delta.shape[0]):
                base, other = rows[index]
                index += 1
                effect = other["expected_pair_effect"]
                effects[effect]["all_pairs"] += 1
                effects[effect]["mode_delta_l1_sum"] += float(
                    (mode_delta[pair_index] - true_mode_delta[pair_index]).abs().mean())
                valid = comparable[pair_index]
                if not valid.any():
                    continue
                error = float((delta[pair_index][valid] - truth[pair_index][valid]).abs().mean())
                effects[effect]["count"] += 1
                effects[effect]["delta_l1_sum"] += error
    return {key: {"all_pairs": value["all_pairs"],
                  "mode_delta_mae": value["mode_delta_l1_sum"] / value["all_pairs"]
                  if value["all_pairs"] else None,
                  "comparable_pairs": value["count"],
                  "allocation_delta_mae": (value["delta_l1_sum"] / value["count"]
                                           if value["count"] else None)}
            for key, value in effects.items()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=5)
    parser.add_argument("--batch-families", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260924)
    parser.add_argument("--architecture", choices=("fdc", "flat_mlp"), default="fdc")
    parser.add_argument("--text-encoder", choices=("hash", "e5-small"), default="hash")
    parser.add_argument("--pair-loss-weight", type=float, default=0.5)
    parser.add_argument("--pair-objective", choices=("allocation_mode", "full"),
                        default="full")
    args = parser.parse_args()
    if not 1 <= args.epochs <= 100 or not 1 <= args.batch_families <= 128:
        parser.error("bounded positive epochs/batch size required")
    if not 0 <= args.pair_loss_weight <= 2:
        parser.error("pair loss weight must be 0..2")
    if args.output_dir.exists():
        parser.error("output directory already exists")
    audit = validate(args.dataset, args.schema)
    if audit["episodes"] != audit["families"] * 2:
        parser.error("only fully paired synthetic datasets are trainable")
    rows = {name: load_episodes(args.dataset, name)
            for name in ("train", "development", "synthetic_holdout")}
    if any(not values for values in rows.values()):
        parser.error("all three family-disjoint splits required")
    cfg = Config()
    dims = dimensions([row for values in rows.values() for row in values])
    if dims[0] > 32 or dims[1] > 64 or dims[2] > 64:
        parser.error("slot limit exceeded")
    torch.manual_seed(args.seed)
    random.seed(args.seed)
    lookup = (encode_episodes([row for values in rows.values() for row in values])
              if args.text_encoder == "e5-small" else None)
    pairs = {name: paired(values, cfg, dims, lookup) for name, values in rows.items()}
    loaders = {name: DataLoader(pairs[name], batch_size=args.batch_families,
                                shuffle=name == "train", collate_fn=collate_pairs,
                                generator=torch.Generator().manual_seed(args.seed))
               for name in pairs}
    model = (FinancialDecisionCore(cfg) if args.architecture == "fdc"
             else FlatMLP(cfg, dims, constraints=len(DEPENDENCY_INDEX)))
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.0003, weight_decay=0.01)
    best = float("inf")
    best_state = None
    history = []
    for epoch in range(1, args.epochs + 1):
        train = run_epoch(model, loaders["train"], optimizer,
                          args.pair_loss_weight, args.pair_objective)
        dev = run_epoch(model, loaders["development"], None,
                        args.pair_loss_weight, args.pair_objective)
        history.append({"epoch": epoch, "train": train, "development": dev})
        print(json.dumps(history[-1]), flush=True)
        if dev["loss"] < best:
            best = dev["loss"]
            best_state = {key: value.detach().cpu().clone()
                          for key, value in model.state_dict().items()}
    model.load_state_dict(best_state)
    holdout = run_epoch(model, loaders["synthetic_holdout"], None,
                        args.pair_loss_weight, args.pair_objective)
    counterfactual = pair_report(model, loaders["synthetic_holdout"],
                                 [tuple(items) for items in _ordered_rows(rows["synthetic_holdout"])])
    args.output_dir.mkdir(parents=True)
    objective_version = ("typed-pair-all-v2" if args.pair_objective == "full"
                         else "typed-pair-allocation-mode-v1")
    report = {"status": "SYNTHETIC_RESEARCH_ONLY",
              "objective_version": objective_version,
              "dataset_sha256": hashlib.sha256(
        args.dataset.read_bytes()).hexdigest(), "dataset_audit": audit,
        "architecture": args.architecture, "parameters": sum(p.numel() for p in model.parameters()),
        "feature_version": FEATURE_VERSION, "config": asdict(cfg), "dimensions": dims,
        "text_encoder": {"kind": args.text_encoder,
                         "model_id": MODEL_ID if lookup is not None else None,
                         "revision": REVISION if lookup is not None else None},
        "seed": args.seed, "pair_loss_weight": args.pair_loss_weight,
        "pair_objective": args.pair_objective,
        "history": history, "synthetic_holdout": holdout,
        "synthetic_counterfactual": counterfactual,
        "limitations": ["Same-generator synthetic split, not independent real gold",
                        "No Qwen baseline, live transaction, or NPU execution"]}
    torch.save({"state_dict": best_state, "config": asdict(cfg),
                "feature_version": FEATURE_VERSION, "architecture": args.architecture,
                "dimensions": dims, "objective_version": objective_version,
                "status": "SYNTHETIC_RESEARCH_ONLY"},
               args.output_dir / "checkpoint.pt")
    (args.output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(args.output_dir / "report.json"),
                      "synthetic_holdout": holdout,
                      "synthetic_counterfactual": counterfactual}, ensure_ascii=False))


def _ordered_rows(rows: list[dict]) -> list[tuple[dict, dict]]:
    by_family = {}
    for row in rows:
        by_family.setdefault(row["scenario_family"], []).append(row)
    return [(next(item for item in by_family[key] if item["parent_episode_id"] is None),
             next(item for item in by_family[key] if item["parent_episode_id"] is not None))
            for key in sorted(by_family)]


if __name__ == "__main__":
    main()
