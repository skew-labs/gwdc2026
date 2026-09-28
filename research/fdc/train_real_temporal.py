"""Train only FDC's temporal branch on real TRON protocol observations.

Task: predict the next reported USDD/TRON daily collateral, debt and supply.
This is self-supervised market representation research, not allocation training.
"""

import argparse
import copy
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import torch
from torch import nn

from finagent.store import Store

from .model import Config, FinancialDecisionCore
from .validate_real_tron import validate_release


INPUT_METRICS = ("collateralValue", "debt", "mintedUSDD", "usddTotalSupply",
                 "totalSupplyValue", "earnTvl", "earnApy")
TARGET_METRICS = ("collateralValue", "debt", "usddTotalSupply")
WINDOW = 14
MATURITY_HOURS = 48


def load_daily(release_dir: Path, *, maturity_hours: int = MATURITY_HOURS) -> list[dict]:
    if not 24 <= maturity_hours <= 168:
        raise ValueError("bounded observation maturity required")
    rows = [json.loads(line) for line in
            (release_dir / "observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    daily = sorted((row for row in rows if row["source_kind"] == "usdd_tron_daily"),
                   key=lambda row: row["event_time"])
    daily = [row for row in daily if
             datetime.fromisoformat(row["source_available_at"])
             - datetime.fromisoformat(row["event_time"]) >= timedelta(hours=maturity_hours)]
    if len(daily) < 100 or len({row["event_time"] for row in daily}) != len(daily):
        raise ValueError("insufficient or duplicate daily TRON observations")
    return daily


def _values(row: dict) -> list[float]:
    return [math.log1p(float(row["measurements"][name]["value"]))
            for name in INPUT_METRICS]


def make_samples(rows: list[dict], window: int = WINDOW) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    values = [_values(row) for row in rows]
    timestamps = [datetime.fromisoformat(row["event_time"]) for row in rows]
    features, time_features, targets = [], [], []
    target_indices = [INPUT_METRICS.index(name) for name in TARGET_METRICS]
    for end in range(window, len(rows)):
        history = values[end-window:end]
        times = []
        for j in range(end-window, end):
            previous = j-1 if j > end-window else j
            gap = (timestamps[j] - timestamps[previous]).total_seconds() / 86400
            age = (timestamps[end-1] - timestamps[j]).total_seconds() / 86400
            if gap < 0 or gap > 30 or age < 0:
                raise ValueError("invalid historical time order")
            times.append([age / 365, gap / 30, 1.0])
        features.append(history)
        time_features.append(times)
        targets.append([values[end][j] for j in target_indices])
    return (torch.tensor(features, dtype=torch.float32),
            torch.tensor(time_features, dtype=torch.float32),
            torch.tensor(targets, dtype=torch.float32))


class TemporalForecaster(nn.Module):
    def __init__(self):
        super().__init__()
        self.core = FinancialDecisionCore(Config(width=64, heads=4, layers=1,
                                                 numeric_features=16, time_features=3,
                                                 text_width=32))
        self.head = nn.Linear(64, len(TARGET_METRICS))

    def forward(self, history: torch.Tensor, times: torch.Tensor) -> torch.Tensor:
        batch = history.shape[0]
        padded = torch.zeros(batch, 1, history.shape[1], 16,
                             device=history.device, dtype=history.dtype)
        padded[:, 0, :, :len(INPUT_METRICS)] = history
        embedded = self.core.temporal_encode(
            padded, times[:, None, :, :],
            torch.ones(batch, 1, history.shape[1], device=history.device,
                       dtype=torch.bool))
        return self.head(embedded[:, 0])


def _relative_error(prediction: torch.Tensor, truth: torch.Tensor) -> dict:
    restored = torch.expm1(prediction).clamp_min(0)
    actual = torch.expm1(truth).clamp_min(0)
    error = ((restored - actual).abs() / actual.clamp_min(1)).mean(0)
    return {name: round(float(error[index]), 6)
            for index, name in enumerate(TARGET_METRICS)}


def score_unseen_day(checkpoint_path: Path, prior_rows: list[dict],
                     target_row: dict) -> dict:
    """Score a frozen prior checkpoint before the new daily row is trained on."""
    if len(prior_rows) < WINDOW:
        raise ValueError("insufficient prior daily history")
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    if (checkpoint.get("scope") != "real_tron_self_supervised_temporal_only"
            or checkpoint.get("maturity_hours") != MATURITY_HOURS
            or tuple(checkpoint["input_metrics"]) != INPUT_METRICS
            or tuple(checkpoint["target_metrics"]) != TARGET_METRICS
            or target_row["event_time"] <= checkpoint["dataset_through_event_time"]):
        raise ValueError("checkpoint is incompatible or has seen target date")
    history = sorted((row for row in prior_rows if row["event_time"] < target_row["event_time"]),
                     key=lambda row: row["event_time"])[-WINDOW:]
    if len(history) != WINDOW:
        raise ValueError("missing prior daily context")
    features, times, actual = make_samples(history + [target_row])
    model = TemporalForecaster()
    supplied = dict(checkpoint["temporal_state"])
    supplied.update({"head." + key: value for key, value in checkpoint["head_state"].items()})
    missing, unexpected = model.load_state_dict(supplied, strict=False)
    if unexpected or any(key.startswith("core.history_projection.")
                         or key.startswith("core.temporal_")
                         or key.startswith("head.") for key in missing):
        raise ValueError("incomplete temporal checkpoint")
    model.eval()
    with torch.no_grad():
        prediction = model((features - checkpoint["input_mean"])
                           / checkpoint["input_scale"], times)
        prediction = prediction * checkpoint["target_scale"] + checkpoint["target_mean"]
    persistence = torch.stack([features[:, -1, INPUT_METRICS.index(name)]
                               for name in TARGET_METRICS], dim=1)
    return {"event_time": target_row["event_time"],
            "source_available_at": target_row["source_available_at"],
            "checkpoint_release": checkpoint["release_id"],
            "model_relative_absolute_error": _relative_error(prediction, actual),
            "persistence_relative_absolute_error": _relative_error(persistence, actual)}


def train(store: Store, release_dir: Path, run_dir: Path, *, epochs: int = 20,
          seed: int = 17) -> dict:
    if not 1 <= epochs <= 100:
        raise ValueError("bounded epochs required")
    audit = validate_release(store, release_dir)
    daily = load_daily(release_dir)
    features, times, targets = make_samples(daily)
    n = len(targets)
    train_end = int(n * 0.7)
    dev_end = int(n * 0.85)
    if min(train_end, dev_end-train_end, n-dev_end) < 20:
        raise ValueError("insufficient chronological samples")
    mean = features[:train_end].reshape(-1, len(INPUT_METRICS)).mean(0)
    scale = features[:train_end].reshape(-1, len(INPUT_METRICS)).std(0).clamp_min(1e-5)
    target_mean = targets[:train_end].mean(0)
    target_scale = targets[:train_end].std(0).clamp_min(1e-5)
    normalized = (features - mean) / scale
    y = (targets - target_mean) / target_scale
    torch.manual_seed(seed)
    torch.set_num_threads(min(4, torch.get_num_threads()))
    model = TemporalForecaster()
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.001, weight_decay=0.01)
    best = None
    best_loss = float("inf")
    stale = 0
    for epoch in range(epochs):
        model.train()
        generator = torch.Generator().manual_seed(seed + epoch)
        for indices in torch.randperm(train_end, generator=generator).split(32):
            prediction = model(normalized[indices], times[indices])
            loss = nn.functional.mse_loss(prediction, y[indices])
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        model.eval()
        with torch.no_grad():
            dev_loss = float(nn.functional.mse_loss(
                model(normalized[train_end:dev_end], times[train_end:dev_end]),
                y[train_end:dev_end]))
        if dev_loss < best_loss:
            best_loss = dev_loss
            best = (epoch + 1, copy.deepcopy(model.state_dict()))
            stale = 0
        else:
            stale += 1
            if stale >= 5:
                break
    assert best is not None
    model.load_state_dict(best[1])
    model.eval()
    with torch.no_grad():
        predicted = model(normalized[dev_end:], times[dev_end:]) * target_scale + target_mean
    persistence = torch.stack([features[dev_end:, -1, INPUT_METRICS.index(name)]
                               for name in TARGET_METRICS], dim=1)
    model_error = _relative_error(predicted, targets[dev_end:])
    baseline_error = _relative_error(persistence, targets[dev_end:])
    run_dir.mkdir(parents=True, exist_ok=False)
    temporal_keys = ("history_projection.", "temporal_attention.",
                     "temporal_norm.", "temporal_null")
    temporal_state = {key: value for key, value in best[1].items()
                      if key.startswith(tuple("core." + item for item in temporal_keys))}
    checkpoint = {"temporal_state": temporal_state,
                  "head_state": model.head.state_dict(),
                  "input_metrics": INPUT_METRICS, "target_metrics": TARGET_METRICS,
                  "window": WINDOW, "input_mean": mean, "input_scale": scale,
                  "target_mean": target_mean, "target_scale": target_scale,
                  "release_id": audit["release_id"], "seed": seed,
                  "dataset_through_event_time": daily[-1]["event_time"],
                  "maturity_hours": MATURITY_HOURS,
                  "scope": "real_tron_self_supervised_temporal_only"}
    torch.save(checkpoint, run_dir / "temporal.pt")
    report = {"source_release": audit["release_id"], "daily_points": len(daily),
              "maturity_hours": MATURITY_HOURS,
              "dataset_through_event_time": daily[-1]["event_time"],
              "train_samples": train_end, "development_samples": dev_end-train_end,
              "holdout_samples": n-dev_end,
              "holdout_start": daily[WINDOW+dev_end]["event_time"],
              "holdout_end": daily[-1]["event_time"],
              "selected_epoch": best[0], "development_mse": round(best_loss, 6),
              "holdout_relative_mae": model_error,
              "persistence_relative_mae": baseline_error,
              "decision_labels": 0,
              "limitations": ["Provider history backfilled at one fetch time; revisions and original point-in-time availability are unknown",
                              "48-hour quarantine is a local policy, not provider finality",
                              "Single TRON USDD protocol series; not a JustLend allocation target",
                              "Only temporal branch is trained; plan, evidence and abstention heads remain synthetic-only"]}
    (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False,
                                               indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=20)
    args = parser.parse_args()
    print(json.dumps(train(Store(args.data_dir), args.release_dir,
                           args.run_dir, epochs=args.epochs),
                     ensure_ascii=False, indent=2))
