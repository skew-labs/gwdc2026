"""Bounded research training on validated, synthetic-only episodes.

No checkpoint from this script is eligible for live plan generation.
"""

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

from .features import FEATURE_VERSION, INPUT_KEYS, collate, dimensions, load_episodes, vectorize
from .baseline import FlatMLP
from .encoder import MODEL_ID, REVISION, encode_episodes
from .model import Config, FinancialDecisionCore
from .projection import inspect_and_project
from .validate import validate


def objective(outputs: dict, batch: dict) -> tuple[torch.Tensor, dict]:
    mode_loss = nn.functional.cross_entropy(outputs["mode_logits"], batch["target_mode"])
    target_plan = batch["target_plan_mask"].float()
    plan_loss = nn.functional.binary_cross_entropy_with_logits(outputs["plan_scores"], target_plan)
    log_alloc = nn.functional.log_softmax(outputs["allocation_logits"], dim=-1)
    log_alloc = log_alloc.masked_fill(torch.isneginf(log_alloc), 0)
    per_plan_alloc = -(batch["target_allocations"] * log_alloc).sum(-1)
    allocation_loss = (per_plan_alloc * target_plan).sum() / target_plan.sum().clamp_min(1)
    # Null evidence is a sentinel, not a positive label.
    evidence_target = batch["target_evidence"]
    safe_evidence_logits = outputs["evidence_logits"].masked_fill(
        torch.isneginf(outputs["evidence_logits"]), 0)
    evidence_loss = nn.functional.binary_cross_entropy_with_logits(
        safe_evidence_logits, evidence_target, reduction="none")
    evidence_valid = torch.cat((batch["evidence_valid"],
                                torch.ones_like(batch["evidence_valid"][:, :1])), dim=1)
    evidence_mask = (batch["target_plan_mask"][..., None]
                     & evidence_valid[:, None, :]).expand_as(evidence_loss)
    evidence_loss = (evidence_loss * evidence_mask).sum() / evidence_mask.sum().clamp_min(1)
    shares = outputs["allocation_logits"].softmax(-1)
    hold = shares[..., -1]
    supply = shares[..., :-1]
    minimum_hold = batch["goal_features"][:, None, 1]
    risk_cap = (batch["goal_features"][:, 3] * 0.50
                + batch["goal_features"][:, 4] * 0.75
                + batch["goal_features"][:, 5])[:, None]
    reserve_error = torch.relu(minimum_hold - hold)
    risk_error = torch.relu(supply.sum(-1) - risk_cap)
    cash_ratio = batch["asset_features"][:, None, :, 1]
    cash_error = torch.relu(supply - cash_ratio).sum(-1)
    eligible = (batch["asset_features"][:, None, :, 3]
                * batch["asset_features"][:, None, :, 4])
    eligibility_error = (supply * (1 - eligible)).sum(-1)
    per_plan_constraint = (reserve_error + risk_error
                           + cash_error + eligibility_error)
    constraint_loss = (per_plan_constraint * target_plan).sum() / target_plan.sum().clamp_min(1)
    total = (mode_loss + 0.3 * plan_loss + allocation_loss
             + 0.2 * evidence_loss + 2.0 * constraint_loss)
    return total, {"mode_ce": mode_loss.detach().item(),
                   "plan_bce": plan_loss.detach().item(),
                   "allocation_ce": allocation_loss.detach().item(),
                   "evidence_bce": evidence_loss.detach().item(),
                   "constraint_violation": constraint_loss.detach().item()}


def run_epoch(model, loader, optimizer, device: str) -> dict:
    training = optimizer is not None
    model.train(training)
    totals = {"loss": 0.0, "mode_correct": 0, "allocation_mae": 0.0,
              "plan_count": 0, "episodes": 0}
    started = time.monotonic()
    for cpu_batch in loader:
        batch = {key: value.to(device) for key, value in cpu_batch.items()}
        inputs = {key: batch[key] for key in INPUT_KEYS}
        with torch.set_grad_enabled(training):
            outputs = model(**inputs)
            loss, _ = objective(outputs, batch)
            if training:
                optimizer.zero_grad(set_to_none=True)
                loss.backward()
                nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
        count = batch["target_mode"].shape[0]
        totals["loss"] += float(loss.detach()) * count
        totals["mode_correct"] += int((outputs["mode_logits"].argmax(-1)
                                       == batch["target_mode"]).sum())
        predicted = outputs["allocation_logits"].softmax(-1)
        mask = batch["target_plan_mask"]
        totals["allocation_mae"] += float((predicted - batch["target_allocations"])
                                          .abs().mean(-1)[mask].sum().detach())
        totals["plan_count"] += int(mask.sum())
        totals["episodes"] += count
    return {"loss": totals["loss"] / totals["episodes"],
            "mode_accuracy": totals["mode_correct"] / totals["episodes"],
            "allocation_mae": totals["allocation_mae"] / max(1, totals["plan_count"]),
            "episodes": totals["episodes"], "plans": totals["plan_count"],
            "elapsed_seconds": round(time.monotonic() - started, 3)}


def feasibility_report(model, loader, rows: list[dict], device: str) -> dict:
    model.eval()
    checked = 0
    violations = 0
    correction = 0.0
    cursor = 0
    with torch.no_grad():
        for cpu_batch in loader:
            batch = {key: value.to(device) for key, value in cpu_batch.items()}
            outputs = model(**{key: batch[key] for key in INPUT_KEYS})
            shares = outputs["allocation_logits"].softmax(-1)[:, 0, :].cpu().tolist()
            for probabilities in shares:
                row = rows[cursor]
                cursor += 1
                if row["target"]["mode"] != "propose":
                    continue
                # Padding is excluded. Index -1 is the wallet-hold candidate.
                actual = probabilities[:len(row["candidates"])] + [probabilities[-1]]
                result = inspect_and_project(row, actual)
                checked += 1
                violations += result["raw_violation"]
                correction += result["correction_l1_fraction"]
    return {"propose_episodes": checked,
            "raw_violation_rate": violations / max(1, checked),
            "mean_correction_l1_fraction": correction / max(1, checked),
            "post_projection_violation_rate": 0,
            "scope": "synthetic_known_constraints_only"}


def main() -> None:
    parser = argparse.ArgumentParser(description="FDC synthetic research trainer")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--epochs", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--learning-rate", type=float, default=0.0003)
    parser.add_argument("--seed", type=int, default=20260924)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    parser.add_argument("--architecture", choices=["fdc", "flat_mlp"], default="fdc")
    parser.add_argument("--text-encoder", choices=["hash", "e5-small"], default="hash")
    args = parser.parse_args()
    if args.schema is None:
        parser.error("--schema is required for training")
    if not 1 <= args.epochs <= 100 or not 1 <= args.batch_size <= 256:
        parser.error("bounded positive epochs and batch size required")
    if not 0 < args.learning_rate <= 0.01:
        parser.error("learning rate outside allowed range")
    if args.device == "cuda" and not torch.cuda.is_available():
        parser.error("CUDA unavailable; do not infer GPU authorization")
    if args.output_dir.exists():
        parser.error("output directory already exists")
    audit = validate(args.dataset, args.schema)
    if set(audit["source_modes"]) != {"synthetic_fixture"}:
        parser.error("this research trainer accepts synthetic fixtures only")
    train_rows = load_episodes(args.dataset, "train")
    dev_rows = load_episodes(args.dataset, "development")
    holdout_rows = load_episodes(args.dataset, "synthetic_holdout")
    if not train_rows or not dev_rows or not holdout_rows:
        parser.error("train, development, and synthetic_holdout splits required")
    cfg = Config()
    dims = dimensions(train_rows + dev_rows + holdout_rows)
    if dims[0] > 32 or dims[1] > 64 or dims[2] > 64:
        parser.error("dataset exceeds FDC research slot limits")
    torch.manual_seed(args.seed)
    random.seed(args.seed)
    text_lookup = (encode_episodes(train_rows + dev_rows + holdout_rows)
                   if args.text_encoder == "e5-small" else None)
    datasets = {name: [vectorize(row, cfg, dims, text_lookup) for row in rows]
                for name, rows in (("train", train_rows), ("development", dev_rows),
                                   ("synthetic_holdout", holdout_rows))}
    generator = torch.Generator().manual_seed(args.seed)
    loaders = {name: DataLoader(rows, batch_size=args.batch_size,
                                shuffle=name == "train", collate_fn=collate,
                                generator=generator)
               for name, rows in datasets.items()}
    model = (FinancialDecisionCore(cfg) if args.architecture == "fdc"
             else FlatMLP(cfg, dims)).to(args.device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=0.01)
    history = []
    best_dev = float("inf")
    best_state = None
    for epoch in range(1, args.epochs + 1):
        train_metrics = run_epoch(model, loaders["train"], optimizer, args.device)
        dev_metrics = run_epoch(model, loaders["development"], None, args.device)
        history.append({"epoch": epoch, "train": train_metrics, "development": dev_metrics})
        print(json.dumps(history[-1]), flush=True)
        if dev_metrics["loss"] < best_dev:
            best_dev = dev_metrics["loss"]
            best_state = {key: value.detach().cpu().clone()
                          for key, value in model.state_dict().items()}
    model.load_state_dict(best_state)
    holdout = run_epoch(model, loaders["synthetic_holdout"], None, args.device)
    feasibility = feasibility_report(
        model, loaders["synthetic_holdout"], holdout_rows, args.device)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    digest = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
    report = {"status": "RESEARCH_SYNTHETIC_ONLY", "model": args.architecture,
              "objective_version": "constraints-v1",
              "text_encoder": {"kind": args.text_encoder,
                               "model_id": MODEL_ID if text_lookup is not None else None,
                               "revision": REVISION if text_lookup is not None else None},
              "feature_version": FEATURE_VERSION, "dataset_sha256": digest,
              "dataset_audit": audit, "config": asdict(cfg), "dimensions": dims,
              "seed": args.seed, "device": args.device, "epochs": history,
              "synthetic_holdout": holdout,
              "synthetic_feasibility": feasibility,
              "limitations": ["No real independent gold set", "Lexical hash encoder is not Qwen",
                              "No calibrated abstention or executable transaction path"]}
    torch.save({"state_dict": best_state, "config": asdict(cfg),
                "feature_version": FEATURE_VERSION,
                "architecture": args.architecture, "dimensions": dims,
                "text_encoder": args.text_encoder,
                "objective_version": "constraints-v1",
                "status": "RESEARCH_SYNTHETIC_ONLY"}, args.output_dir / "checkpoint.pt")
    (args.output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(args.output_dir / "report.json"),
                      "synthetic_holdout": holdout,
                      "synthetic_feasibility": feasibility}, ensure_ascii=False))


if __name__ == "__main__":
    main()
